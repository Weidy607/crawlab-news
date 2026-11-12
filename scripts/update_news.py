"""Preview or upload the same news project to mapped Crawlab task IDs."""

import argparse
import ast
import base64
import fnmatch
import hashlib
import json
import re
import shlex
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Task:
    name: str
    task_id: str
    command: str


def load_tasks(path):
    tasks = []
    names, ids = set(), set()
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-z0-9_]+)=([a-fA-F0-9]{24})\|CMD=(.+)", line)
        if not match:
            raise ValueError(f"Invalid task map line {number}: {path}")
        name, task_id, command = match.groups()
        task_id = task_id.lower()
        if name in names or task_id in ids:
            raise ValueError(f"Duplicate name or ID at task map line {number}")
        names.add(name)
        ids.add(task_id)
        tasks.append(Task(name, task_id, command))
    if not tasks:
        raise ValueError("No mapped tasks found")
    return tasks


def check_project(project, tasks):
    for relative in ("scrapy.cfg", "requirements.txt", "azure_ai.py", "send_email_report.py",
                     "news/settings.py", "news/pipelines.py", "news/spiders"):
        if not (project / relative).exists():
            raise ValueError(f"Missing project file: {project / relative}")
    names = set()
    for path in (project / "news/spiders").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "name"
                    for target in statement.targets
                ) and isinstance(statement.value, ast.Constant):
                    names.add(statement.value.value)
    for task in tasks:
        command = shlex.split(task.command)
        if len(command) < 3 or command[:2] != ["scrapy", "crawl"]:
            raise ValueError(f"Expected a Scrapy command for {task.name}")
        if command[2] == "base" or command[2] not in names:
            raise ValueError(f"Mapped spider not found: {command[2]}")


def ignore_upload_files(directory, names):
    patterns = (
        "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".git", ".agents",
        ".venv*", "venv", ".env", ".env.*", ".crawlab_tasks.map",
        "*.swp", "*.log", "test_*.py", "tests", "*preview*.html", "*preview*.png",
    )
    return {name for name in names if any(fnmatch.fnmatch(name, pattern) for pattern in patterns)}


def normalize_api(url):
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid Crawlab server URL")
    path = parsed.path.rstrip("/")
    if path not in ("", "/api"):
        raise ValueError("Expected the Crawlab root URL or /api URL")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.lower(), "/api", "", ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the authentication token to a redirected host.
        return None


class CrawlabAPI:
    def __init__(self, server_file, auth_file):
        server = json.loads(server_file.read_text(encoding="utf-8-sig"))
        auth = json.loads(auth_file.read_text(encoding="utf-8-sig"))
        self.address = normalize_api(server["api_address"])
        self.project_id = server["project_id"]
        if not re.fullmatch(r"[a-f0-9]{24}", self.project_id):
            raise ValueError("Invalid expected Crawlab project ID")
        if normalize_api(auth.get("api_address", "")) != self.address:
            raise ValueError("Login config points to another server; no request was sent")
        self.token = auth.get("token")
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("No login token found in the local Crawlab CLI config")
        self.token = self.token.strip()
        if any(ord(char) < 32 or ord(char) > 126 for char in self.token):
            raise ValueError("Invalid token format in the local login config")
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, endpoint, data=None, content_type=None):
        headers = {"Accept": "application/json", "Authorization": self.token}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.address + endpoint, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Crawlab HTTP {exc.code} for {endpoint.split('?')[0]}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ValueError("Crawlab connection failed; check server and network") from None
        if not isinstance(payload, dict) or payload.get("status") != "ok" or payload.get("error"):
            # Error payloads can contain server configuration; don't print them.
            raise ValueError(f"Crawlab API rejected {endpoint.split('?')[0]}")
        return payload.get("data")

    def preflight(self, tasks):
        metadata = {}
        for task in tasks:
            data = self.request(f"/spiders/{task.task_id}")
            if not isinstance(data, dict) or data.get("_id") != task.task_id:
                raise ValueError(f"Remote task ID mismatch: {task.name}")
            if data.get("project_id") != self.project_id or data.get("cmd") != task.command:
                raise ValueError(f"Remote project or command mismatch: {task.name}; no more uploads attempted")
            metadata[task.task_id] = {key: data.get(key) for key in ("name", "cmd", "project_id")}
        return metadata

    def save(self, task, relative, content):
        remote_path = "/" + relative.as_posix()
        boundary = "crawlab-" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"path\"\r\n\r\n{remote_path}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{relative.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        self.request(f"/spiders/{task.task_id}/files/save", body, f"multipart/form-data; boundary={boundary}")
        info = self.request(f"/spiders/{task.task_id}/files/info?" + urllib.parse.urlencode({"path": remote_path}))
        expected = hashlib.md5(content).digest()
        if not isinstance(info, dict) or info.get("md5") not in (
            expected.hex(), base64.b64encode(expected).decode("ascii")
        ):
            raise ValueError(f"Uploaded file checksum mismatch: {task.name}:{remote_path}")


def upload(tasks, project, api):
    # Check every target before the first write, including its command and news project.
    metadata = api.preflight(tasks)
    print(f"Verified {len(tasks)} targets on {api.address}", flush=True)
    with tempfile.TemporaryDirectory(prefix="crawlab-news-upload-") as temporary:
        staging = Path(temporary) / "news"
        shutil.copytree(project, staging, ignore=ignore_upload_files)
        files = sorted(path for path in staging.rglob("*") if path.is_file())
        for index, task in enumerate(tasks, 1):
            print(f"[{index}/{len(tasks)}] Uploading {task.name} ({task.task_id}), {len(files)} files", flush=True)
            try:
                for path in files:
                    api.save(task, path.relative_to(staging), path.read_bytes())
                if api.preflight([task]) != {task.task_id: metadata[task.task_id]}:
                    raise ValueError(f"Remote task metadata changed during upload: {task.name}")
            except (OSError, ValueError):
                print(f"Stopped at {task.name}; {index - 1} tasks completed. This task may be partially updated.", flush=True)
                raise
            print(f"  Verified {len(files)} file checksums; task metadata retained.", flush=True)
    print(f"Uploaded and verified {len(tasks)} tasks. No tasks were run; remote-only files were retained.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parents[1] / "news")
    parser.add_argument("--tasks-file", type=Path, help="Defaults to the project's .crawlab_tasks.map")
    parser.add_argument("--names", nargs="+", help="Update selected mapped names instead of all")
    parser.add_argument("--server-config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "crawlab.server.json")
    parser.add_argument("--auth-config", type=Path, default=Path.home() / ".crawlab/cli/config.json",
                        help="Local login config; credentials are never copied to the project")
    parser.add_argument("--check-remote", action="store_true", help="Validate all remote targets without uploading")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (the default)")
    mode.add_argument("--execute", action="store_true", help="Upload files to the mapped existing tasks")
    args = parser.parse_args(argv)
    try:
        project = args.project_dir.resolve()
        tasks = load_tasks(args.tasks_file or project / ".crawlab_tasks.map")
        if args.names:
            unknown = set(args.names) - {task.name for task in tasks}
            if unknown:
                raise ValueError("Unknown mapped names: " + ", ".join(sorted(unknown)))
            tasks = [task for task in tasks if task.name in args.names]
        check_project(project, tasks)
        print(f"Project: {project}")
        print("Files-only upload plan (remote configuration is retained):")
        for task in tasks:
            print(f"  {task.name}: {task.task_id} | expected command: {task.command}")
        api = None
        if args.execute or args.check_remote:
            api = CrawlabAPI(args.server_config, args.auth_config)
            if args.check_remote and not args.execute:
                api.preflight(tasks)
                print(f"Verified {len(tasks)} remote targets on {api.address}")
        if not args.execute:
            print(f"Previewed {len(tasks)} tasks. Add --execute to upload.")
            return 0
        return upload(tasks, project, api)
    except (OSError, UnicodeError, SyntaxError, ValueError, KeyError, TypeError) as exc:
        print(f"Error: {exc}")
        return 2
    except KeyboardInterrupt:
        print("Interrupted; check the last task on Crawlab before retrying.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
