import contextlib
import io
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import update_news


class UpdateNewsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crawlab-upload-test-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "news"
        (self.project / "news/spiders").mkdir(parents=True)
        for relative in ("scrapy.cfg", "requirements.txt", "azure_ai.py", "news_tagging.py", "send_email_report.py",
                         "news/settings.py", "news/pipelines.py"):
            (self.project / relative).write_text("", encoding="utf-8")
        (self.project / "news/spiders/sources.py").write_text(
            'class First:\n    name = "first"\nclass Second:\n    name = "second"\n',
            encoding="utf-8",
        )
        self.mapping = self.project / ".crawlab_tasks.map"
        self.mapping.write_text(
            'first=' + 'a' * 24 + '|CMD=scrapy crawl first\n'
            'second=' + 'b' * 24 + '|CMD=scrapy crawl second\n', encoding="utf-8",
        )
        self.output = io.StringIO()
        self.output_context = contextlib.redirect_stdout(self.output)
        self.output_context.__enter__()
        self.addCleanup(self.output_context.__exit__, None, None, None)

    def run_main(self, *arguments):
        return update_news.main(["--project-dir", str(self.project), *arguments])

    def api(self):
        api = MagicMock()
        api.address = "http://example.test/api"
        api.preflight.side_effect = lambda tasks: {
            task.task_id: {"name": task.name, "cmd": task.command, "project_id": "c" * 24}
            for task in tasks
        }
        return api

    def test_default_preview_does_not_connect(self):
        with patch.object(update_news, "CrawlabAPI") as factory:
            self.assertEqual(0, self.run_main())
        factory.assert_not_called()
        self.assertIn("Previewed 2 tasks", self.output.getvalue())

    def test_same_clean_snapshot_and_metadata_preserved(self):
        (self.project / "__pycache__").mkdir()
        for relative in (".env", "test_local.py", "__pycache__/cached.pyc", "report_preview.html"):
            (self.project / relative).write_text("local only", encoding="utf-8")
        api = self.api()
        calls = []

        def save(task, relative, content):
            calls.append((task.name, relative.as_posix(), content))
            if len(calls) == 1:
                # Edits during deployment must not create different versions per task.
                (self.project / "azure_ai.py").write_text("changed", encoding="utf-8")

        api.save.side_effect = save
        with patch.object(update_news, "CrawlabAPI", return_value=api):
            self.assertEqual(0, self.run_main("--execute"))
        first = [(path, content) for name, path, content in calls if name == "first"]
        second = [(path, content) for name, path, content in calls if name == "second"]
        self.assertEqual(first, second)
        paths = {path for path, _ in first}
        self.assertIn("news/spiders/sources.py", paths)
        self.assertNotIn(".env", paths)
        self.assertNotIn(".crawlab_tasks.map", paths)
        self.assertNotIn("test_local.py", paths)
        self.assertNotIn("report_preview.html", paths)
        self.assertFalse(any("__pycache__" in path for path in paths))
        self.assertTrue((self.project / ".env").is_file())
        self.assertEqual(3, api.preflight.call_count)

    def test_failure_stops_remaining_uploads(self):
        api = self.api()
        api.save.side_effect = ValueError("upload failed")
        with patch.object(update_news, "CrawlabAPI", return_value=api):
            self.assertEqual(2, self.run_main("--execute"))
        self.assertEqual(1, api.save.call_count)
        self.assertIn("partially updated", self.output.getvalue())

    def test_remote_mismatch_aborts_before_any_write(self):
        api = self.api()
        api.preflight.side_effect = ValueError("command mismatch")
        with patch.object(update_news, "CrawlabAPI", return_value=api):
            self.assertEqual(2, self.run_main("--execute"))
        api.save.assert_not_called()

    def test_selection_uploads_only_requested_id(self):
        api = self.api()
        with patch.object(update_news, "CrawlabAPI", return_value=api):
            self.assertEqual(0, self.run_main("--names", "second", "--execute"))
        self.assertEqual({'b' * 24}, {call.args[0].task_id for call in api.save.call_args_list})

    def test_remote_check_is_read_only(self):
        api = self.api()
        with patch.object(update_news, "CrawlabAPI", return_value=api):
            self.assertEqual(0, self.run_main("--check-remote"))
        api.preflight.assert_called_once()
        api.save.assert_not_called()

    def test_unknown_name_does_not_connect(self):
        with patch.object(update_news, "CrawlabAPI") as factory:
            self.assertEqual(2, self.run_main("--names", "missing", "--execute"))
        factory.assert_not_called()

    def test_duplicate_ids_are_rejected_before_connecting(self):
        self.mapping.write_text(self.mapping.read_text().replace('b' * 24, 'a' * 24))
        with patch.object(update_news, "CrawlabAPI") as factory:
            self.assertEqual(2, self.run_main("--execute"))
        factory.assert_not_called()

    def test_stale_spider_mapping_is_rejected(self):
        self.mapping.write_text(self.mapping.read_text().replace("crawl second", "crawl deleted"))
        with patch.object(update_news, "CrawlabAPI") as factory:
            self.assertEqual(2, self.run_main("--execute"))
        factory.assert_not_called()


class APITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = Path(self.temp.name) / "server.json"
        self.auth = Path(self.temp.name) / "auth.json"
        self.server.write_text(json.dumps({"api_address": "http://example.test/#/spiders", "project_id": "c" * 24}))
        self.auth.write_text(json.dumps({"api_address": "http://example.test/api", "token": "secret-test-token"}))
        self.api = update_news.CrawlabAPI(self.server, self.auth)
        self.task = update_news.Task("first", "a" * 24, "scrapy crawl first")

    def test_server_mismatch_never_sends_credentials(self):
        self.auth.write_text(json.dumps({"api_address": "http://other.test/api", "token": "secret-test-token"}))
        with patch.object(update_news.urllib.request, "build_opener") as factory:
            with self.assertRaisesRegex(ValueError, "another server"):
                update_news.CrawlabAPI(self.server, self.auth)
        factory.assert_not_called()

    def test_http_200_application_error_is_failure(self):
        self.api.opener = MagicMock()
        self.api.opener.open.return_value.__enter__.return_value = io.BytesIO(
            b'{"status":"error","error":"sensitive server details"}'
        )
        with self.assertRaises(ValueError) as error:
            self.api.request("/spiders")
        self.assertNotIn("sensitive", str(error.exception))

    def test_preflight_checks_project_command_and_id(self):
        correct = {"_id": self.task.task_id, "project_id": "c" * 24, "cmd": self.task.command, "name": "Display name"}
        for key in ("_id", "project_id", "cmd"):
            with self.subTest(key=key), patch.object(self.api, "request", return_value={**correct, key: "different"}):
                with self.assertRaises(ValueError):
                    self.api.preflight([self.task])
        with patch.object(self.api, "request", return_value=correct):
            self.assertEqual("Display name", self.api.preflight([self.task])[self.task.task_id]["name"])

    def test_multipart_has_posix_path_and_checks_remote_hash(self):
        content = b"code\r\n"
        digest = base64.b64encode(hashlib.md5(content).digest()).decode("ascii")
        with patch.object(self.api, "request", side_effect=[None, {"md5": digest}]) as request:
            self.api.save(self.task, Path("news/spiders/source.py"), content)
        self.assertIn(b"/news/spiders/source.py\r\n", request.call_args_list[0].args[1])
        self.assertIn(content, request.call_args_list[0].args[1])
        self.assertIn(b'filename="source.py"', request.call_args_list[0].args[1])
        self.assertIn("/files/info?path=", request.call_args_list[1].args[0])
        with patch.object(self.api, "request", side_effect=[None, {"md5": "wrong"}]):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.api.save(self.task, Path("source.py"), content)


if __name__ == "__main__":
    unittest.main()
