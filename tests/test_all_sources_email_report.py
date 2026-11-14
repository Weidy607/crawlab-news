import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "news"))
import send_all_sources_email_report as report
import send_email_report as original
import news_tagging as tagging


class AllSourcesEmailReportTests(unittest.TestCase):
    def setUp(self):
        self.args = original.build_runtime_args(hours=24, limit=1000, dry_run=True)
        (Path(__file__).resolve().parents[1] / "output").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "output")
        self.addCleanup(temporary.cleanup)
        self.args.output = str(Path(temporary.name) / "preview.html")
        self.rows = [
            {"网站": "TechCrunch", "标题(中文)": "国外融资新闻", "标题(英文)": "Overseas funding", "标签": "融资", "链接": "https://techcrunch.com/funding", "AI摘要(中文)": "数据库中文摘要。", "AI摘要(英文)": "Database summary."},
            {"网站": "东方财富", "标题(中文)": "国内投资新闻", "链接": "https://finance.eastmoney.com/a/1"},
            {"网站": "Hongshan", "标题(中文)": "红杉投资新闻"},
            {"网站": "新来源", "标题(中文)": "待归类新闻", "链接": "https://unknown.example/news"},
        ]
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def test_new_query_reads_all_sources_without_topic_tag_or_keyword_prefilter(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{"website": "东方财富", "title_zh": "投资", "publish_date": "2026-11-13"}]
        with patch.object(original, "resolve_mysql_config", return_value={"table": "ai_news"}), patch.object(
            original, "connect_ai_news_mysql", return_value=connection
        ), patch.object(original, "pymysql", MagicMock()):
            rows = report.fetch_news(self.args)
        query, params = cursor.execute.call_args.args
        self.assertNotIn("TRIM(website)", query)
        self.assertNotIn(original.INVEST_MNA_TAG_WHERE, query)
        self.assertNotIn("LIKE", query.upper())
        self.assertIn("DATE_SUB(CURDATE(), INTERVAL %s DAY)", query)
        self.assertIn("publish_date < DATE_ADD(CURDATE(), INTERVAL 1 DAY)", query)
        self.assertEqual((1,), params)
        self.assertNotIn("LIMIT", query.upper())
        self.assertEqual("东方财富", rows[0]["网站"])
        connection.close.assert_called_once()

    def test_original_daily_report_still_has_its_source_allowlist(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        with patch.object(original, "resolve_mysql_config", return_value={"table": "ai_news"}), patch.object(
            original, "connect_ai_news_mysql", return_value=connection
        ), patch.object(original, "pymysql", MagicMock()):
            original.fetch_invest_mna_news(self.args)
        query, params = cursor.execute.call_args.args
        self.assertIn("TRIM(website) IN", query)
        self.assertEqual(list(original.DAILY_WEBSITE_ALLOWLIST), params[1:-1])

    def test_complete_report_has_no_sql_item_limit(self):
        self.args.limit = 0
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        with patch.object(original, "resolve_mysql_config", return_value={"table": "ai_news"}), patch.object(
            original, "connect_ai_news_mysql", return_value=connection
        ), patch.object(original, "pymysql", MagicMock()):
            report.fetch_news(self.args)
        query, params = cursor.execute.call_args.args
        self.assertNotIn("LIMIT", query.upper())
        self.assertEqual((1,), params)
        self.assertIn("不设条数上限", report.build_report_html(self.rows, self.args))

    def test_new_report_defaults_to_unlimited_independently_of_original_report(self):
        with patch.dict("os.environ", {"EMAIL_REPORT_LIMIT": "1000", "ALL_SOURCES_EMAIL_REPORT_LIMIT": "0"}):
            self.assertEqual(0, report.parse_args([]).limit)
            self.assertEqual(12, report.parse_args(["--limit", "12"]).limit)
            self.assertEqual(1000, original.build_runtime_args().limit)

    def test_source_names_domains_and_unknowns(self):
        cases = [
            ({"网站": " 36 Kr "}, "domestic"),
            ({"网站": "hongshan"}, "domestic"),
            ({"网站": "PANews"}, "domestic"),
            ({"网站": "富途资讯"}, "domestic"),
            ({"网站": "https://finance.sina.com.cn/news"}, "domestic"),
            ({"网站": "新来源", "链接": "https://news.example.cn/story"}, "domestic"),
            ({"网站": "bvp"}, "international"),
            ({"网站": "Thoma Bravo"}, "international"),
            ({"网站": "RFI", "标题(中文)": "中国新闻"}, "international"),
            ({"网站": "", "链接": "https://news.crunchbase.com/story"}, "international"),
            ({"网站": "fake-techcrunch.com"}, "unknown"),
            ({"网站": "techcrunch.com.evil.example"}, "unknown"),
            ({"网站": "新来源"}, "unknown"),
            ({}, "unknown"),
        ]
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(expected, report.source_region(row))

    def test_bilingual_render_groups_every_article_once_and_uses_database_summaries(self):
        for language in ("zh", "en"):
            with self.subTest(language=language):
                content = report.build_report_markdown(self.rows, self.args, language)
                labels = report.REGION_LABELS[language]
                self.assertLess(content.index("## " + labels["international"]), content.index("## " + labels["domestic"]))
                self.assertLess(content.index("## " + labels["domestic"]), content.index("## " + labels["unknown"]))
                self.assertEqual(4, content.count("### "))
                self.assertIn("Database summary." if language == "en" else "数据库中文摘要。", content)
                self.assertIn("(2)", content)
                self.assertIn("待归类新闻", content)

    def test_empty_group_remains_visible(self):
        content = report.build_report_markdown([self.rows[0]], self.args)
        self.assertIn("## 国内新闻 (0)", content)
        self.assertIn("本期暂无符合条件的新闻", content)
        self.assertNotIn("## 来源地区待确认", content)

    def test_summary_points_preserve_all_content_and_existing_bullets(self):
        summary = "2026年融资500万元，增长1.5%。他说：“继续研发！”\n- 产品已上线。客户正在试用。\n• AI & SaaS <平台>\n短句。\n第五条。第六条。第七条。"
        points = report.summary_points(summary)
        self.assertEqual([
            "2026年融资500万元，增长1.5%。他说：“继续研发！”",
            "产品已上线。客户正在试用。", "AI & SaaS <平台>",
            "短句。", "第五条。第六条。第七条。",
        ], points)
        row = {**self.rows[0], "AI摘要(中文)": summary}
        content = report.render_news_item(1, row, "zh")
        self.assertIn("list-style-type:disc", content)
        self.assertEqual(len(points), content.count("<li "))
        self.assertIn("AI &amp; SaaS &lt;平台&gt;", content)
        self.assertIn("第七条。", content)
        markdown = report.build_report_markdown([row], self.args)
        for point in points:
            self.assertIn(f"- {point}", markdown)

    def test_html_bullets_use_database_summary_with_language_fallback(self):
        row = {**self.rows[0], "AI摘要(中文)": "", "内容摘要(中文)": "数据库备用摘要。第二点。"}
        content = report.render_news_item(1, row, "zh")
        self.assertIn("数据库备用摘要。", content)
        self.assertEqual(1, content.count("<li "))
        content = report.render_news_item(1, row, "en")
        self.assertIn("Database summary.", content)
        self.assertEqual(1, content.count("<li "))

    def test_dry_run_does_not_load_email_config_or_send(self):
        with patch.object(report, "fetch_news", return_value=self.rows), patch.object(report, "prepare_report_summaries", return_value=self.rows), patch.object(report, "filter_tagged_news", return_value=(self.rows, [])), patch.object(
            original, "email_enabled"
        ) as configured, patch.object(report, "send_report") as send:
            self.assertEqual(0, report.run_email_report(self.args))
        configured.assert_not_called()
        send.assert_not_called()

    def test_report_uses_ingestion_bullets_without_ai_or_rewriting(self):
        text = "- GMI融资6.68亿美元。其平台每周处理4万亿tokens。\n- Groq融资3.5亿美元。公司正转型。"
        row = {**self.rows[0], "AI摘要(中文)": text}
        with patch("azure_ai._call_azure_openai", side_effect=AssertionError("日报不得调用AI")) as call:
            updated = report.prepare_report_summaries([row])
            rendered = report.render_news_item(1, updated[0], "zh")
        call.assert_not_called()
        self.assertEqual(2, rendered.count("<li "))
        self.assertIn("其平台每周处理4万亿tokens。", rendered)
        self.assertEqual(text, updated[0]["AI摘要(中文)"])

    def test_stored_summary_review_cannot_drop_or_reorder_facts(self):
        row = {"AI摘要(中文)": "融资500万元。增长1.5%。"}
        for points in (["融资500万元。"], ["增长1.5%。", "融资500万元。"]):
            review = {"source_field": "AI摘要(中文)", "source_text": row["AI摘要(中文)"], "status": "ok", "points": points}
            with self.assertRaises(ValueError):
                report.validate_summary_review(row, "zh", review)

    def test_bullet_split_at_comma_is_rejected_even_when_all_characters_are_preserved(self):
        text = "公司发布新产品，其性能提升20%。"
        row = {"AI摘要(中文)": text}
        review = {"status": "ok", "source_field": "AI摘要(中文)", "source_text": text,
                  "points": ["公司发布新产品，", "其性能提升20%。"]}
        with self.assertRaises(ValueError):
            report.validate_summary_review(row, "zh", review)

    def test_new_email_has_distinct_subject_and_region_sections(self):
        config = {"smtp_user": "sender@example.com", "smtp_pass": "test", "smtp_server": "smtp.example.com", "smtp_port": 465}
        smtp = MagicMock()
        smtp.return_value.__enter__.return_value.send_message.return_value = {}
        with patch.object(original, "resolve_email_config", return_value=config), patch.object(
            original, "get_recipients_by_language", return_value={"zh": [{"email": "reader@example.com"}], "en": []}
        ), patch.object(report.smtplib, "SMTP_SSL", smtp):
            self.assertTrue(report.send_report(self.rows, self.args))
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual("全球投研日报", message["Subject"])
        body = message.get_payload()[0].get_payload(decode=True).decode("utf-8")
        self.assertIn("国内新闻", body)
        self.assertIn("国外新闻", body)

    def test_demo_send_is_explicitly_labeled_and_only_uses_selected_recipient(self):
        args = report.parse_args(["--demo", "--send", "--email-to", "reader@example.com"])
        config = {"smtp_user": "sender@example.com", "smtp_pass": "test", "smtp_server": "smtp.example.com", "smtp_port": 465}
        smtp = MagicMock()
        smtp.return_value.__enter__.return_value.send_message.return_value = {}
        with patch.object(original, "resolve_email_config", return_value=config), patch.object(report.smtplib, "SMTP_SSL", smtp):
            self.assertTrue(report.send_report(report.demo_rows(), args))
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual("reader@example.com", message["To"])
        self.assertIn("【排版测试】", message["Subject"])
        body = message.get_payload()[0].get_payload(decode=True).decode("utf-8")
        self.assertIn("以下为示例内容，不代表真实新闻", body)
        attachment = message.get_payload()[1]
        self.assertEqual("attachment", attachment.get_content_disposition())
        self.assertTrue(attachment.get_filename().endswith(".html"))
        self.assertEqual(body, attachment.get_payload(decode=True).decode("utf-8"))

    def test_preview_is_saved_without_sending_by_default(self):
        args = report.parse_args(["--demo", "--output", self.args.output])
        with patch.object(report, "send_report") as send:
            self.assertEqual(0, report.run_email_report(args))
        send.assert_not_called()
        self.assertIn("#15355f", Path(args.output).read_text(encoding="utf-8"))

    def test_industry_filter_rejects_retail_ai_marketing_and_keeps_core_software(self):
        rows = [
            {"标题(中文)": "李佳琦卖Token，品牌拍短剧：今年双11画风突变", "AI摘要(中文)": "双11营销从折扣直播转向短剧与AI内容，品牌使用AI数字人和Token会员卡。", "标签": "AI,IPO,Blockchain"},
            {"标题(中文)": "企业软件公司完成融资", "AI摘要(中文)": "该公司主营面向企业的SaaS客户管理软件。"},
            {"标题(中文)": "医院引入AI系统", "AI摘要(中文)": "该医院使用AI技术优化门诊服务。"},
        ]
        response = {"items": [
            {"id": 0, "industry": "NON_IT", "topic": "UNRELATED", "confidence": "high", "evidence": "品牌使用AI数字人和Token会员卡", "reason": "核心为消费品牌营销"},
            {"id": 1, "industry": "SOFTWARE_SAAS", "topic": "CAPITAL", "confidence": "high", "evidence": "主营面向企业的SaaS客户管理软件", "reason": "主营企业SaaS"},
            {"id": 2, "industry": "NON_IT", "topic": "UNRELATED", "confidence": "high", "evidence": "该医院使用AI技术优化门诊服务", "reason": "主体为医疗服务"},
        ]}
        classify = MagicMock(return_value=json.dumps(response, ensure_ascii=False))
        kept, reviews = tagging.filter_industry_news(rows, classify)
        self.assertEqual([rows[1]], kept)
        self.assertEqual([False, True, False], [item["included"] for item in reviews])
        prompt = classify.call_args.args[0][0]["content"]
        self.assertIn("主营业务", prompt)
        self.assertIn("李佳琦", prompt)

    def test_industry_filter_requires_complete_decisions_and_grounded_evidence(self):
        row = {"标题(中文)": "品牌引入AI营销", "AI摘要(中文)": "消费品牌推广新产品。", "标签": "AI"}
        fabricated = {"id": 0, "industry": "AI", "topic": "CAPITAL", "confidence": "high", "evidence": "这是一家提供人工智能模型的软件公司"}
        with self.assertRaises(RuntimeError):
            tagging.filter_industry_news([row], lambda *a, **k: json.dumps({"items": [fabricated]}))
        uncertain = {"id": 0, "industry": "AI", "topic": "CAPITAL", "confidence": "low", "evidence": "消费品牌推广新产品"}
        kept, _ = tagging.filter_industry_news([row], lambda *a, **k: json.dumps({"items": [uncertain]}))
        self.assertEqual([], kept)
        for invalid in ('', '{"items":[]}', '{"items":[{"id":0},{"id":0}]}'):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                tagging.filter_industry_news([row], lambda *a, **k: invalid)

    def test_mixed_roundup_keeps_all_relevant_entries_without_unrelated_summaries(self):
        facts = [
            ("Fractile", "IT_INFRASTRUCTURE", "Fractile develops AI chips and raised $720 million."),
            ("Rox", "AI", "Rox develops enterprise AI and raised $100 million."),
            ("Edelbyte", "IT_INFRASTRUCTURE", "Edelbyte builds data infrastructure and raised $70 million."),
            ("XILECT", "IT_INFRASTRUCTURE", "XILECT develops robotics chips and raised RMB 500 million."),
        ]
        row = {"标题(中文)": "多行业融资汇总", "网站": "TechStartups", "链接": "https://techstartups.com/roundup",
               "AI摘要(中文)": "无关药企、电池企业融资合计20亿美元。",
               "内容摘要(英文)": "Unrelated battery and drug financings.",
               "完整内容": "背景。" * 3500 + "\n".join(fact for _, _, fact in facts)}
        entries = [{"name": name, "industry": industry, "evidence": fact,
                    "summary_zh": name + "：" + fact, "summary_en": fact}
                   for name, industry, fact in facts]
        response = {"items": [{"id": 0, "industry": "MIXED", "topic": "CAPITAL", "confidence": "high",
                               "relevant_items": entries}]}
        classify = MagicMock(return_value=json.dumps(response))
        kept, reviews = tagging.filter_industry_news([row], classify)
        self.assertEqual(1, len(kept))
        self.assertTrue(reviews[0]["included"])
        self.assertEqual(entries, reviews[0]["relevant_items"])
        for language in ("zh", "en"):
            rendered = report.build_report_html(kept, self.args, language)
            for name, _, _ in facts:
                self.assertIn(name, rendered)
            self.assertNotIn("20亿美元", rendered)
            self.assertNotIn("Unrelated battery", rendered)
        self.assertIn("无关药企", row["AI摘要(中文)"])
        self.assertIn(facts[-1][2], classify.call_args.args[0][1]["content"])

    def test_mixed_roundup_with_missing_or_fabricated_evidence_stops_generation(self):
        row = {"标题(中文)": "融资汇总", "完整内容": "Fractile develops AI chips and raised $720 million."}
        for entries in (None, [], [{"name": "Fake", "industry": "AI", "evidence": "Fake raised $900 million.",
                                    "summary_zh": "虚构融资", "summary_en": "Fabricated financing"}]):
            response = {"items": [{"id": 0, "industry": "MIXED", "topic": "CAPITAL", "confidence": "high", "relevant_items": entries}]}
            with self.subTest(entries=entries), self.assertRaises(RuntimeError):
                tagging.filter_industry_news([row], lambda *a, **k: json.dumps(response))

    def test_roundup_paraphrased_evidence_is_repaired_without_losing_the_entry(self):
        fact = "Fractile develops AI chips and raised $720 million."
        entry = {"name": "Fractile", "industry": "IT_INFRASTRUCTURE", "evidence": "Fractile raised $720 million for AI chips.",
                 "summary_zh": "Fractile融资7.2亿美元，主营AI芯片。", "summary_en": fact}
        classify = MagicMock(return_value=json.dumps({"items": [{"id": 0, "evidence": fact}]}))
        result = tagging.extract_roundup_row({"完整内容": fact}, [entry], classify)
        self.assertEqual(fact, result["汇总提取条目"][0]["evidence"])
        self.assertEqual(entry["summary_zh"], result["AI摘要(中文)"])
        classify.assert_called_once()

    def test_classification_json_accepts_redundant_closers_but_not_extra_data(self):
        self.assertEqual({"items": []}, tagging.parse_classification_json('{"items":[]}}'))
        for content in ('{"items":[]} {"items":[1]}', '{"items":[]} explanation', '{"items":['):
            with self.subTest(content=content), self.assertRaises(ValueError):
                tagging.parse_classification_json(content)

    def test_industry_evidence_failure_rechecks_article_instead_of_silently_dropping_it(self):
        fact = "Acme sells enterprise software and raised $5 million."
        row = {"标题(英文)": "Acme funding", "完整内容": fact}
        decision = {"id": 0, "industry": "SOFTWARE_SAAS", "topic": "CAPITAL", "confidence": "high"}
        classify = MagicMock(side_effect=[
            json.dumps({"items": [{**decision, "evidence": "Acme raised $5 million for software."}]}),
            json.dumps({"items": [{"id": 0, "evidence": ""}]}),
            json.dumps({"items": [{**decision, "evidence": fact}]}),
        ])
        kept, reviews = tagging.filter_industry_news([row], classify)
        self.assertEqual([row], kept)
        self.assertTrue(reviews[0]["included"])
        self.assertEqual(fact, reviews[0]["evidence"])

    def test_topic_is_decided_semantically_instead_of_using_existing_tags(self):
        rows = [
            {"标题(英文)": "Acme announcement", "标签": "Other", "完整内容": "Acme sells enterprise software and raised $5 million."},
            {"标题(英文)": "Programming exercise", "标签": "AI,Funding", "完整内容": "An introductory programming exercise for students."},
        ]
        decisions = {"items": [
            {"id": 0, "industry": "SOFTWARE_SAAS", "topic": "CAPITAL", "confidence": "high", "evidence": rows[0]["完整内容"]},
            {"id": 1, "industry": "SOFTWARE_SAAS", "topic": "UNRELATED", "confidence": "high", "evidence": rows[1]["完整内容"]},
        ]}
        kept, _ = tagging.filter_industry_news(rows, lambda *a, **k: json.dumps(decisions))
        self.assertEqual([rows[0]], kept)

    def test_industry_filter_failure_stops_preview_and_sending(self):
        self.args.send = True
        self.args.dry_run = False
        with patch.object(report, "fetch_news", return_value=self.rows), patch.object(
            report, "filter_tagged_news", side_effect=RuntimeError("classification failed")
        ), patch.object(report, "send_report") as send:
            self.assertEqual(1, report.run_email_report(self.args))
        send.assert_not_called()
        self.assertFalse(Path(self.args.output).exists())

    def test_verbatim_evidence_allows_quotation_and_line_wrapping(self):
        text = "该公司提供 SaaS 客户管理软件。\n主营企业数据平台。"
        self.assertTrue(tagging.has_grounded_evidence('“该公司提供SaaS客户管理软件”', text))
        self.assertTrue(tagging.has_grounded_evidence('“该公司提供 SaaS 客户管理软件”“主营企业数据平台”', text))
        self.assertFalse(tagging.has_grounded_evidence('该公司提供智能制造硬件平台', text))
        self.assertFalse(tagging.has_grounded_evidence('“主营企业数据平台” 还提供完全虚构的业务', text))


if __name__ == "__main__":
    unittest.main()
