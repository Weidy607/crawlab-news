from .baseSpider import BaseSpider
from send_email_report import build_runtime_args, run_email_report


def _to_bool(value):
    if value is None:
        return None
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


class EmailReportSpider(BaseSpider):
    name = "email_report"
    custom_settings = {
        "ITEM_PIPELINES": {},
    }

    def _run_email_report(self):
        overrides = {
            "hours": int(self.hours) if getattr(self, "hours", None) else None,
            "limit": int(self.limit) if getattr(self, "limit", None) else None,
            "subject_suffix": getattr(self, "subject_suffix", None),
            "dry_run": _to_bool(getattr(self, "dry_run", None)),
            "db_host": getattr(self, "db_host", None),
            "db_port": int(self.db_port) if getattr(self, "db_port", None) else None,
            "db_user": getattr(self, "db_user", None),
            "db_password": getattr(self, "db_password", None),
            "db_name": getattr(self, "db_name", None),
            "db_table": getattr(self, "db_table", None),
            "smtp_server": getattr(self, "smtp_server", None),
            "smtp_port": int(self.smtp_port) if getattr(self, "smtp_port", None) else None,
            "smtp_user": getattr(self, "smtp_user", None),
            "smtp_pass": getattr(self, "smtp_pass", None),
            "email_to": getattr(self, "email_to", None),
        }
        exit_code = run_email_report(build_runtime_args(**overrides))
        if exit_code != 0:
            raise RuntimeError(f"email_report failed with exit code {exit_code}")

    def start_requests(self):
        """Compatibility entry point for Scrapy versions older than 2.13."""
        self._run_email_report()
        return []
