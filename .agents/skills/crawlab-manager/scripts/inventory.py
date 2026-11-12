"""Read-only Crawlab inventory using only Python's standard library."""

import argparse
import ast
import json
import re
import shlex
from collections import defaultdict
from pathlib import Path


def find_root(value=None):
    if value:
        candidates = [Path(value).expanduser().resolve()]
    else:
        current = Path.cwd().resolve()
        candidates = []
        for parent in (current, *current.parents):
            candidates.extend((parent, parent / "crawlab-news"))
    for candidate in candidates:
        if (candidate / "docker-compose.yml").is_file() and (
            candidate / "news/scrapy.cfg"
        ).is_file():
            return candidate
    raise ValueError("Cannot locate crawlab-news root; pass --root explicitly.")


def inventory(root):
    report = {"root": str(root), "spiders": [], "tasks": [], "warnings": [], "errors": []}
    names = defaultdict(list)
    for project in ("news",):
        project_root = root / project
        for path in sorted(project_root.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
            except (OSError, UnicodeError, SyntaxError) as exc:
                report["errors"].append(f"{relative}: {exc}")
                continue
            if "spiders" not in path.relative_to(project_root).parts:
                continue
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                for statement in node.body:
                    value = None
                    if isinstance(statement, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "name"
                        for target in statement.targets
                    ):
                        value = statement.value
                    elif isinstance(statement, ast.AnnAssign) and isinstance(
                        statement.target, ast.Name
                    ) and statement.target.id == "name":
                        value = statement.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        name = value.value
                        if name:
                            row = {"project": project, "name": name, "class": node.name,
                                   "file": relative, "base": name == "base"}
                            report["spiders"].append(row)
                            names[(project, name)].append(relative)
    for (project, name), files in names.items():
        if len(files) > 1:
            report["errors"].append(f"Duplicate {project} spider {name}: {files}")
    mapping = root / "news/.crawlab_tasks.map"
    mapped_names = set()
    mapped_ids = set()
    if mapping.is_file():
        for number, raw in enumerate(mapping.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([^=\s]+)=([a-fA-F0-9]{24})\|CMD=(.+)", line)
            if not match:
                report["errors"].append(f"Invalid task map line {number}")
                continue
            name, task_id, command = match.groups()
            if name in mapped_names or task_id.lower() in mapped_ids:
                report["errors"].append(f"Duplicate task mapping at line {number}: {name}")
            mapped_names.add(name)
            mapped_ids.add(task_id.lower())
            task = {"name": name, "id": task_id, "command": command}
            report["tasks"].append(task)
            try:
                argv = shlex.split(command)
            except ValueError as exc:
                report["errors"].append(f"Invalid command for {name}: {exc}")
                continue
            if len(argv) >= 3 and argv[:2] == ["scrapy", "crawl"]:
                task["spider"] = argv[2]
                if ("news", argv[2]) not in names or argv[2] == "base":
                    report["errors"].append(f"Mapped spider not found/usable: {argv[2]}")
            else:
                report["warnings"].append(f"Non-crawl command for {name}; verify manually")
    registered = {task.get("spider") for task in report["tasks"]}
    report["unmapped_news_spiders"] = sorted({
        row["name"] for row in report["spiders"]
        if row["project"] == "news" and not row["base"] and row["name"] not in registered
    })
    if report["unmapped_news_spiders"]:
        report["warnings"].append("Unmapped sources may already exist remotely; discover IDs before creating.")
    report["warnings"].append("Static inventory only; use scrapy list to confirm runtime discovery.")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="Crawlab repository root")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()
    try:
        report = inventory(find_root(args.root))
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(2, f"inventory: {exc}\n")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Root: {report['root']}")
        for row in report["spiders"]:
            suffix = " [shared base]" if row["base"] else ""
            print(f"  {row['project']}/{row['name']}{suffix}: {row['file']}")
        print("Mapped tasks:")
        for task in report["tasks"]:
            print(f"  {task['name']}: {task['id']} | {task['command']}")
        print("Unmapped news names: " + ", ".join(report["unmapped_news_spiders"]))
        for kind in ("warnings", "errors"):
            for message in report[kind]:
                print(f"{kind.upper()}: {message}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
