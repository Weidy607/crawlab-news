"""Load optional local credentials; runtime environment variables take priority."""

import json
import os
from pathlib import Path


def load_local_environment():
    path = Path(__file__).resolve().with_name(".env.local.json")
    if not path.exists():
        return
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise ValueError(".env.local.json must contain string keys and values")
    for key, value in values.items():
        os.environ.setdefault(key, value)
