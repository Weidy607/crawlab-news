import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "news"))
import news_tagging as tagging
import send_all_sources_email_report as report
import backfill_news_tags as backfill
from news.pipelines import TechCrunchMySQLPipeline


class NewsTaggingTests(unittest.TestCase):
    def row(self):
        return {"网站": "TechCrunch", "标题(英文)": "Acme funding", "链接": "https://example.com/acme",
                "完整内容": "Acme sells enterprise software and raised $5 million.",
                "AI摘要(中文)": "- Acme提供企业软件，融资500万美元。", "标签": "Other"}

    def decision(self, **overrides):
        return {"id": 0, "industry": "SOFTWARE_SAAS", "topic": "CAPITAL", "confidence": "high",
                "evidence": self.row()["完整内容"], "reason": "企业软件融资",
                "tags": ["Enterprise Software", "Funding"], **overrides}

    def classify(self, row=None, **overrides):
        row = dict(row or self.row())
        call = MagicMock(return_value=json.dumps({"items": [self.decision(**overrides)]}, ensure_ascii=False))
        tags, metadata = tagging.classify_news_for_storage(row, call)
        row.update(标签=tags, **{tagging.METADATA_COLUMN: json.dumps(metadata, ensure_ascii=False)})
        return row, metadata, call

    def test_ingestion_saves_authoritative_tags_without_three_tag_cap(self):
        tags = ["AI", "Funding", "M&A", "Cloud Computing", "Enterprise Software"]
        row, metadata, call = self.classify(tags=tags)
        self.assertEqual("ok", metadata["status"])
        self.assertEqual(tags + [tagging.INCLUDE_TAG], tagging.tag_tokens(row["标签"]))
        kept, reviews = tagging.filter_tagged_news([row])
        self.assertEqual([row], kept)
        self.assertEqual("database_tags", reviews[0]["selection"])
        call.assert_called_once()

    def test_ingestion_sees_entire_article_including_tail(self):
        row = self.row()
        row["完整内容"] = "文章背景。" * 4000 + row["完整内容"]
        _, metadata, call = self.classify(row)
        material = call.call_args.args[0][1]["content"]
        self.assertIn("Acme sells enterprise software and raised $5 million.", material)
        self.assertGreater(len(material), 20000)
        self.assertEqual("ok", metadata["status"])

    def test_generated_summary_cannot_be_used_as_source_evidence_when_body_exists(self):
        row = {**self.row(), "完整内容": "The retailer sells cosmetics through livestream marketing.",
               "AI摘要(英文)": "Acme sells enterprise software and raised $5 million."}
        self.assertNotIn("raised $5 million", tagging.industry_context(row))
        self.assertIn("retailer sells cosmetics", tagging.industry_context(row))

    def test_old_ai_and_funding_tags_cannot_include_retail_marketing(self):
        row = {**self.row(), "标签": "AI,Funding", "完整内容": "消费品牌在直播中使用AI数字人做营销。"}
        row, metadata, _ = self.classify(row, industry="NON_IT", topic="UNRELATED", evidence="消费品牌在直播中使用AI数字人做营销", tags=["E-commerce"])
        self.assertEqual(["E-commerce", tagging.EXCLUDE_TAG], tagging.tag_tokens(row["标签"]))
        self.assertFalse(metadata["decision"]["included"])
        self.assertEqual([], tagging.filter_tagged_news([row])[0])

    def test_mixed_roundup_uses_saved_project_summaries_and_keeps_source_unchanged(self):
        row = self.row()
        row["AI摘要(中文)"] = "- 药企融资8000万美元。\n- 电池公司完成融资。"
        projects = [{"name": "Acme", "industry": "SOFTWARE_SAAS", "evidence": row["完整内容"],
                     "summary_zh": "Acme提供企业软件，融资500万美元。", "summary_en": row["完整内容"]}]
        stored, metadata, _ = self.classify(row, industry="MIXED", relevant_items=projects)
        with patch("azure_ai._call_azure_openai", side_effect=AssertionError("email must be offline")) as ai:
            kept, _ = tagging.filter_tagged_news([stored])
            html = report.build_report_html(report.prepare_report_summaries(kept), report.parse_args([]))
        ai.assert_not_called()
        self.assertIn("500万美元", html)
        self.assertNotIn("8000万美元", html)
        self.assertEqual(row["AI摘要(中文)"], stored["AI摘要(中文)"])
        self.assertEqual(projects, metadata["decision"]["relevant_items"])

    def test_failed_or_uncertain_classification_is_pending_and_blocks_complete_report(self):
        cases = [MagicMock(return_value=""), MagicMock(side_effect=RuntimeError("content_filter")),
                 MagicMock(return_value=json.dumps({"items": [self.decision(confidence="low")]})),
                 MagicMock(return_value=json.dumps({"items": [self.decision(tags=[])]}))]
        for classify in cases:
            with self.subTest(classify=classify):
                row = self.row()
                tags, metadata = tagging.classify_news_for_storage(row, classify)
                self.assertIn(tagging.PENDING_TAG, tagging.tag_tokens(tags))
                self.assertEqual("pending", metadata["status"])
                row.update(标签=tags, **{tagging.METADATA_COLUMN: metadata})
                with self.assertRaisesRegex(RuntimeError, "停止发送以免漏报"):
                    tagging.filter_tagged_news([row])

    def test_old_labels_stale_source_and_contradictory_markers_cannot_silently_drop_news(self):
        stored, _, _ = self.classify()
        cases = [self.row(), {**stored, "完整内容": "公司更新融资金额。"},
                 {**stored, "标签": tagging.INCLUDE_TAG + "," + tagging.EXCLUDE_TAG},
                 {**stored, "标签": "AI, Funding, 非IT投研日报"}]
        for row in cases:
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                tagging.filter_tagged_news([row])

    def test_pipeline_reclassifies_source_tags_and_preserves_all_industry_summaries(self):
        pipeline = TechCrunchMySQLPipeline({})
        spider = MagicMock(name="spider")
        spider.name = "eastmoney"
        data = {label: self.row().get(label, "") for label in tagging.FIELD_MAP.values()}
        data["完整内容(英文)"] = self.row()["完整内容"]
        data["标签"] = "AI,IPO"
        with patch.object(pipeline, "_enrich_summaries"), patch("news.pipelines.classify_news_for_storage", return_value=(tagging.EXCLUDE_TAG, {"status": "ok"})) as ai:
            row = pipeline._prepare_row(data, spider)
        self.assertEqual(tagging.EXCLUDE_TAG, row["tags"])
        self.assertEqual(data["AI摘要(中文)"], row["ai_summary_zh"])
        self.assertEqual("AI,IPO", ai.call_args.args[0]["标签"])
        self.assertEqual({"status": "ok"}, json.loads(row[tagging.METADATA_COLUMN]))

    def test_metadata_and_tags_are_written_in_the_same_insert_or_update(self):
        for exists in (None, {"id": 1}):
            for full_body in (False, True):
                pipeline = TechCrunchMySQLPipeline({})
                pipeline.conn = MagicMock()
                pipeline.cursor = MagicMock()
                pipeline.cursor.fetchone.return_value = exists
                pipeline._supports_content_full = full_body
                row = {key: "value" for key in tagging.FIELD_MAP}
                row.update(match_type="Funding", **{tagging.METADATA_COLUMN: '{"status":"ok"}'})
                pipeline._save_to_mysql(row, MagicMock())
                sql, params = pipeline.cursor.execute.call_args.args
                self.assertIn(tagging.METADATA_COLUMN, sql)
                self.assertEqual(sql.count("%s"), len(params))
                self.assertIn(row[tagging.METADATA_COLUMN], params)

    def test_backfill_update_guards_every_source_field_and_previous_metadata(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 0
        row = {field: "old" for field in tagging.FIELD_MAP}
        row.update(id=99, **{tagging.METADATA_COLUMN: "old metadata"})
        self.assertFalse(backfill.save_labels(connection, "ai_news", row, "new tags", {"status": "ok"}))
        sql, params = cursor.execute.call_args.args
        self.assertEqual(len(params), sql.count("%s"))
        for field in tagging.FIELD_MAP:
            self.assertIn(f"COALESCE(`{field}`, '')", sql)
        self.assertIn("old metadata", params)
        self.assertNotIn("SET ai_summary", sql)

    def test_entire_send_path_uses_only_stored_tags_and_summaries(self):
        row, _, _ = self.classify()
        output_dir = Path(__file__).resolve().parents[1] / "output"
        output_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_dir) as directory:
            args = report.parse_args(["--send", "--email-to", "reader@example.com", "--output", str(Path(directory) / "report.html")])
            args.dry_run = False
            config = {"smtp_user": "sender@example.com", "smtp_pass": "test", "smtp_server": "smtp.example.com", "smtp_port": 465}
            with patch.object(report, "fetch_news", return_value=[row]), patch.object(report.original, "email_enabled", return_value=True), patch.object(
                report.original, "resolve_email_config", return_value=config
            ), patch.object(report.smtplib, "SMTP_SSL") as smtp, patch("azure_ai._call_azure_openai", side_effect=AssertionError("email must not call AI")) as ai:
                smtp.return_value.__enter__.return_value.send_message.return_value = {}
                self.assertEqual(0, report.run_email_report(args))
            ai.assert_not_called()
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual("reader@example.com", message["To"])
            self.assertTrue(Path(args.output).is_file())

    def test_backfill_defaults_to_inventory_without_ai_or_schema_mutation(self):
        output_dir = Path(__file__).resolve().parents[1] / "output"
        output_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_dir) as directory:
            args = backfill.parse_args(["--output", str(Path(directory) / "inventory.json")])
            with patch.object(backfill.original, "resolve_mysql_config", return_value={"table": "ai_news"}), patch.object(
                backfill.original, "connect_ai_news_mysql"
            ), patch.object(backfill, "fetch_candidates", return_value=[{"id": 1, "tags": "AI"}]), patch.object(
                backfill, "classify_news_for_storage"
            ) as ai, patch.object(backfill, "ensure_metadata_column") as schema, patch.object(backfill, "save_labels") as save:
                self.assertEqual(0, backfill.run(args))
            ai.assert_not_called()
            schema.assert_not_called()
            save.assert_not_called()

    def test_schema_initialization_is_idempotent_and_handles_two_workers(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        with patch.object(tagging, "has_metadata_column", return_value=True):
            tagging.ensure_metadata_column(connection, "ai_news")
        cursor.execute.assert_not_called()
        with patch.object(tagging, "has_metadata_column", return_value=False):
            cursor.execute.side_effect = Exception(1060, "Duplicate column")
            tagging.ensure_metadata_column(connection, "ai_news")
            cursor.execute.side_effect = Exception(1142, "ALTER denied")
            with self.assertRaises(Exception):
                tagging.ensure_metadata_column(connection, "ai_news")


if __name__ == "__main__":
    unittest.main()
