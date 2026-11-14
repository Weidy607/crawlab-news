"""Keep generated reports in output/, independently of the working directory."""

from pathlib import Path
from typing import Optional


PROJECT_DIR = Path(__file__).resolve().parent
# Crawlab uploads news/ as a standalone project, without the repository wrapper.
REPOSITORY_DIR = PROJECT_DIR.parent
OUTPUT_DIR = (
    REPOSITORY_DIR if (REPOSITORY_DIR / "docker-compose.yml").is_file() else PROJECT_DIR
) / "output"


def report_output_path(filename: str, override: Optional[str] = None) -> Path:
    path = Path(override).resolve() if override else OUTPUT_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
