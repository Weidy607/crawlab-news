import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "news"))
import runtime_config


class RuntimeConfigTests(unittest.TestCase):
    def test_environment_overrides_local_values(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "runtime_config.py"
            module.with_name(".env.local.json").write_text(
                json.dumps({"EXAMPLE_KEY": "local", "EXAMPLE_SECOND": "local-second"}),
                encoding="utf-8",
            )
            with patch.object(runtime_config, "__file__", str(module)), patch.dict(
                os.environ, {"EXAMPLE_KEY": "environment"}, clear=True
            ):
                runtime_config.load_local_environment()
                self.assertEqual("environment", os.environ["EXAMPLE_KEY"])
                self.assertEqual("local-second", os.environ["EXAMPLE_SECOND"])

    def test_missing_local_file_is_optional(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            runtime_config, "__file__", str(Path(directory) / "runtime_config.py")
        ), patch.dict(os.environ, {}, clear=True):
            runtime_config.load_local_environment()
            self.assertEqual({}, dict(os.environ))

    def test_invalid_config_does_not_partially_load(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "runtime_config.py"
            module.with_name(".env.local.json").write_text(
                '{"EXAMPLE_KEY": "value", "INVALID": 123}', encoding="utf-8"
            )
            with patch.object(runtime_config, "__file__", str(module)), patch.dict(
                os.environ, {}, clear=True
            ):
                with self.assertRaises(ValueError):
                    runtime_config.load_local_environment()
                self.assertEqual({}, dict(os.environ))
