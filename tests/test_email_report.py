import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "news"))
import send_email_report as report
import news_tagging as tagging


class EmailReportSelectionTests(unittest.TestCase):
    def setUp(self):
        self.args = report.build_runtime_args(hours=24, limit=1000, dry_run=True)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        for mocked in (
            patch.object(report, "resolve_mysql_config", return_value={"table": "ai_news"}),
            patch.object(report, "connect_ai_news_mysql", return_value=self.connection),
            patch.object(report, "has_metadata_column", return_value=True),
            patch.object(report, "pymysql", MagicMock()),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        capture = contextlib.redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def stored_row(self, title, included=True, **decision_overrides):
        row = {
            "title_zh": title, "website": "TechCrunch", "url": "https://example.com/" + title,
            "publish_date": "2026-11-14", "content_full": "The company develops enterprise software.",
            "ai_summary_zh": "- 企业软件行业动态。", "ai_summary_en": "- Enterprise software news.",
            "tags": "Industry Insight, " + (tagging.INCLUDE_TAG if included else tagging.EXCLUDE_TAG),
        }
        decision = {
            "included": included, "industry": "SOFTWARE_SAAS" if included else "NON_IT",
            "topic": "INDUSTRY_INSIGHT" if included else "UNRELATED", "confidence": "high",
            "evidence": row["content_full"], **decision_overrides,
        }
        row[tagging.METADATA_COLUMN] = json.dumps({
            "version": tagging.VERSION, "status": "ok", "decision": decision,
            "source_hash": tagging.source_hash(tagging.report_row(row)),
        })
        return row

    def test_industry_news_without_capital_keywords_matches_global_daily_selection(self):
        self.cursor.fetchall.return_value = [self.stored_row("软件行业动态"), self.stored_row("非IT新闻", False)]
        with patch("azure_ai._call_azure_openai", side_effect=AssertionError("筛选不得调用AI")):
            selected = report.fetch_invest_mna_news(self.args)
        candidates = [
            {**tagging.report_row(row), tagging.METADATA_COLUMN: row[tagging.METADATA_COLUMN]}
            for row in self.cursor.fetchall.return_value
        ]
        self.assertEqual(tagging.filter_tagged_news(candidates)[0], selected)
        self.assertEqual(["软件行业动态"], [row["标题(中文)"] for row in selected])
        query, params = self.cursor.execute.call_args.args
        self.assertIn("TRIM(website) IN", query)
        self.assertEqual([1, *report.DAILY_WEBSITE_ALLOWLIST], params)
        self.assertNotIn("LIKE", query.upper())
        self.assertNotIn("LIMIT", query.upper())
        self.assertIn("publish_date < DATE_ADD(CURDATE(), INTERVAL 1 DAY)", query)
        self.connection.close.assert_called_once()

    def test_limit_applies_after_classification(self):
        self.args.limit = 1
        self.cursor.fetchall.return_value = [
            self.stored_row("非IT新闻", False), self.stored_row("应收录"), self.stored_row("超过上限"),
        ]
        self.assertEqual(["应收录"], [row["标题(中文)"] for row in report.fetch_invest_mna_news(self.args)])

    def test_pending_classification_blocks_sending_instead_of_empty_notice(self):
        pending = self.stored_row("待审核")
        pending[tagging.METADATA_COLUMN] = None
        self.cursor.fetchall.return_value = [pending]
        with patch.object(report, "send_digest_email") as send:
            self.assertEqual(1, report.run_email_report(self.args))
        send.assert_not_called()

    def test_missing_metadata_column_stops_query_and_closes_connection(self):
        with patch.object(report, "has_metadata_column", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "backfill_news_tags"):
                report.fetch_invest_mna_news(self.args)
        self.cursor.execute.assert_not_called()
        self.connection.close.assert_called_once()

    def test_mixed_roundup_reuses_the_shared_extracted_summaries(self):
        entries = [{"name": "Acme", "industry": "SOFTWARE_SAAS",
                    "evidence": "The company develops enterprise software.",
                    "summary_zh": "Acme提供企业软件。", "summary_en": "Acme develops enterprise software."}]
        row = self.stored_row("行业汇总", industry="MIXED", relevant_items=entries)
        self.cursor.fetchall.return_value = [row]
        selected = report.fetch_invest_mna_news(self.args)
        self.assertEqual("Acme提供企业软件。", selected[0]["AI摘要(中文)"])
        self.assertEqual(entries, selected[0]["汇总提取条目"])


class EmptyEmailReportTests(unittest.TestCase):
    def setUp(self):
        self.args = report.build_runtime_args(dry_run=False)
        capture = contextlib.redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_empty_results_send_a_notice(self):
        with patch.object(report, "fetch_invest_mna_news", return_value=[]), patch.object(
            report, "email_enabled", return_value=True
        ), patch.object(report, "send_digest_email", return_value=True) as send:
            self.assertEqual(0, report.run_email_report(self.args))
        send.assert_called_once_with([], "日报", self.args)

    def test_empty_dry_run_does_not_load_email_config_or_send(self):
        self.args.dry_run = True
        with patch.object(report, "fetch_invest_mna_news", return_value=[]), patch.object(
            report, "email_enabled"
        ) as configured, patch.object(report, "send_digest_email") as send:
            self.assertEqual(0, report.run_email_report(self.args))
        configured.assert_not_called()
        send.assert_not_called()

    def test_empty_notice_send_failure_is_reported(self):
        with patch.object(report, "fetch_invest_mna_news", return_value=[]), patch.object(
            report, "email_enabled", return_value=True
        ), patch.object(report, "send_digest_email", return_value=False):
            self.assertEqual(1, report.run_email_report(self.args))

    def test_database_failure_does_not_send_a_no_news_notice(self):
        with patch.object(report, "fetch_invest_mna_news", side_effect=RuntimeError("unavailable")), patch.object(
            report, "send_digest_email"
        ) as send:
            self.assertEqual(1, report.run_email_report(self.args))
        send.assert_not_called()

    def test_empty_notice_is_bilingual_and_has_no_generated_insights(self):
        with patch.object(report, "generate_core_insight") as insights:
            chinese = report.build_mna_digest_list_markdown([], "日报")
            english = report.build_mna_digest_list_markdown([], "Daily", "en")
        insights.assert_not_called()
        self.assertIn("AI领域并购资讯监控日报", chinese)
        self.assertIn("共0条", chinese)
        self.assertIn("本期暂无符合条件", chinese)
        self.assertIn("AI M&A Intelligence Monitor Daily", english)
        self.assertIn("0 AI/Investment/M&A", english)
        self.assertIn("No matching", english)


if __name__ == "__main__":
    unittest.main()
