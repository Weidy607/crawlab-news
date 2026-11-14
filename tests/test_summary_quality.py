import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "news"))
import azure_ai as ai
from news.pipelines import TechCrunchMySQLPipeline
from news.spiders.direct_news_sources import TonghuashunSpider
from bs4 import BeautifulSoup


def response(source, zh="公司完成500万元融资，资金用于研发。", en="The company raised RMB 5 million for research.", quality="complete"):
    return json.dumps({"source_quality": quality, "article_kind": "single", "reason": "正文不足" if quality == "insufficient" else "",
                       "points": [] if quality == "insufficient" else [{"subject": "公司", "zh": zh, "en": en, "evidence": source}]}, ensure_ascii=False)


class SummaryQualityTests(unittest.TestCase):
    def setUp(self):
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)
        enabled = patch.object(ai, "azure_summary_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_split_covers_all_source_including_tail_without_changing_numbers(self):
        text = ("2026年，公司融资500万元，增长1.5%。\n" * 900) + "尾部项目完成新一轮融资。"
        chunks = ai.split_summary_source(text)
        self.assertGreater(len(chunks), 2)
        self.assertEqual(text, "".join(chunks))
        self.assertTrue(all(len(chunk) <= 8000 for chunk in chunks))
        self.assertIn("尾部项目", chunks[-1])

    def test_bilingual_summary_reads_every_chunk_and_keeps_late_roundup_projects(self):
        text = ("首部企业完成融资，资金用于产品研发。\n" * 450) + "尾部企业B完成600万元融资。"
        bodies = []

        def generate(messages, **kwargs):
            body = json.loads(messages[1]["content"])["body"]
            bodies.append(body)
            if "尾部企业B" in body:
                value = json.loads(response("尾部企业B完成600万元融资。", "尾部企业B完成600万元融资。", "Company B raised RMB 6 million."))
                subject = "B"
            else:
                value = json.loads(response("首部企业完成融资", "首部企业完成融资，资金用于产品研发。", "The first company raised funding for development."))
                subject = "首部企业"
            value["article_kind"] = "roundup"
            value["points"][0].update(subject=subject, event="funding")
            return json.dumps(value, ensure_ascii=False)

        with patch.object(ai, "_call_azure_openai", side_effect=generate):
            result = ai.summarize_news_bilingual(text, max_length=20)
        self.assertEqual(text, "".join(bodies))
        self.assertEqual("ok", result["status"])
        self.assertIn("600万元", result["zh"])
        self.assertIn("6 million", result["en"])
        self.assertEqual(len(bodies), len(result["points"]))

    def test_later_chunk_failure_never_returns_only_the_first_projects(self):
        text = "公司已完成新融资。\n" * 900
        with patch.object(ai, "_call_azure_openai", side_effect=[response("公司已完成新融资。"), "broken", "broken"]):
            result = ai.summarize_news_bilingual(text)
        self.assertEqual("failed", result["status"])
        self.assertEqual("", result["zh"])
        self.assertEqual([], result["points"])

    def test_unverifiable_evidence_retries_then_rejects_the_summary(self):
        with patch.object(ai, "_call_azure_openai", return_value=response("正文中并不存在的证据")) as call:
            result = ai.summarize_news_bilingual("公司宣布完成500万元融资。")
        self.assertEqual(4, call.call_count)
        self.assertEqual("failed", result["status"])
        self.assertEqual("", result["en"])

    def test_noncontiguous_quote_can_be_repaired_only_after_fact_check(self):
        source = "公司宣布完成500万元融资，资金用于产品研发。"
        repair = json.dumps({"items": [{"id": 0, "supported": True, "evidence": "公司宣布完成500万元融资"}]}, ensure_ascii=False)
        with patch.object(ai, "_call_azure_openai", side_effect=[response("错误的原文拼接引用"), repair]) as call:
            result = ai.summarize_news_bilingual(source)
        self.assertEqual("ok", result["status"])
        self.assertEqual(2, call.call_count)
        self.assertEqual("公司宣布完成500万元融资", result["points"][0]["evidence"])

    def test_evidence_repair_cannot_approve_unsupported_claims(self):
        bad = response("错误的原文拼接引用")
        refusal = json.dumps({"items": [{"id": 0, "supported": False, "evidence": ""}]})
        with patch.object(ai, "_call_azure_openai", side_effect=[bad, refusal, bad, refusal]):
            result = ai.summarize_news_bilingual("公司只宣布研发计划，没有披露融资。")
        self.assertEqual("failed", result["status"])
        self.assertEqual([], result["points"])

    def test_caption_only_input_is_not_saved_as_a_successful_ai_summary(self):
        with patch.object(ai, "_call_azure_openai", return_value=response("", quality="insufficient")):
            self.assertEqual("", ai.summarize_text_zh("图1：公司工厂。图片来源：公司官网。"))
    def test_structured_summary_requires_nonempty_bilingual_fields(self):
        with patch.object(ai, "_call_azure_openai", return_value=response("公司发布新产品", zh="")):
            result = ai.summarize_news_bilingual("公司发布新产品，其性能提升20%。")
        self.assertEqual("failed", result["status"])

    def test_parser_does_not_override_the_models_point_count(self):
        value = json.loads(response("公司已完成新融资。"))
        value["points"] *= 4
        result = ai._parse_summary_result(json.dumps(value, ensure_ascii=False), "公司已完成新融资。")
        self.assertEqual(value["points"], result["points"])

    def test_code_never_rejects_retries_or_slices_ai_output_due_to_length(self):
        source = "公司完成500万元融资，资金用于研发。"
        text = "公司完成融资。" * 15
        verbose = response(source, zh=text)
        with patch.object(ai, "_call_azure_openai", return_value=verbose) as call:
            result = ai.summarize_news_bilingual(source)
        self.assertEqual(1, call.call_count)
        self.assertEqual("ok", result["status"])
        self.assertEqual("- " + text, result["zh"])

    def test_concise_roundup_keeps_all_projects_without_a_global_length_cap(self):
        source = "公司完成500万元融资，资金用于研发。"
        value = json.loads(response(source))
        value["article_kind"] = "roundup"
        value["points"] = [{"subject": str(i), "event": "funding", "evidence": source,
                            "zh": f"项目{i}完成500万元融资，资金用于企业软件产品研发和市场拓展。",
                            "en": f"Project {i} raised RMB 5 million for enterprise software development and expansion."}
                           for i in range(10)]
        with patch.object(ai, "_call_azure_openai", return_value=json.dumps(value, ensure_ascii=False)):
            result = ai.summarize_news_bilingual(source)
        self.assertEqual("ok", result["status"])
        self.assertEqual(10, len(result["points"]))
        self.assertIn("项目9", result["zh"])

    def test_roundup_does_not_reject_or_cut_model_descriptions_by_length(self):
        source = "A公司融资500万元；B公司开发企业软件。"
        first = json.loads(response(source))
        first["article_kind"] = "roundup"
        first["points"] = [
            {"subject": "A", "event": "funding", "evidence": source,
             "zh": "A公司融资500万元，用于研发、产品升级、市场拓展、团队招聘及业务扩张，服务各类行业客户并提升用户体验和交付效率。",
             "en": "A raised RMB 5 million for development and expansion."},
            {"subject": "B", "event": "product", "evidence": source,
             "zh": "B：开发企业软件。", "en": "B develops enterprise software."},
        ]
        with patch.object(ai, "_call_azure_openai", return_value=json.dumps(first, ensure_ascii=False)) as call:
            result = ai.summarize_news_bilingual(source)
        self.assertEqual(1, call.call_count)
        self.assertEqual("ok", result["status"])
        self.assertEqual(["A", "B"], [point["subject"] for point in result["points"]])
        self.assertEqual(first["points"], result["points"])

    def test_cross_chunk_article_is_consolidated_even_with_only_three_verbose_points(self):
        source = "公司宣布完成新融资，业务增长但面临竞争。"
        point = {"subject": "公司", "event": "funding", "evidence": source,
                 "zh": "公司融资用于研发。" * 10, "en": "The company raised funding for research."}
        short = {**point, "zh": "公司完成融资，用于研发；业务增长但面临竞争。"}
        payload = json.dumps({"items": [{"id": 0, "points": [short]}]}, ensure_ascii=False)
        with patch.object(ai, "_call_azure_openai", return_value=payload) as call:
            result = ai._consolidate_summary_points([point, dict(point), dict(point)], "analysis", source, consolidate_article=True)
        call.assert_called_once()
        self.assertEqual([short], result)

    def test_roundup_consolidation_keeps_each_distinct_company(self):
        point_a = {"subject": "A", "event": "funding", "zh": "A融资500万元。", "en": "A raised RMB 5 million.", "evidence": "A融资500万元。"}
        point_b = {"subject": "B", "event": "funding", "zh": "B融资600万元。", "en": "B raised RMB 6 million.", "evidence": "B融资600万元。"}
        source = "A融资500万元。B融资600万元。"
        result = json.dumps({"items": [{"id": 0, "points": [point_a]}]}, ensure_ascii=False)
        with patch.object(ai, "_call_azure_openai", return_value=result):
            points = ai._consolidate_summary_points([point_a, dict(point_a), point_b], "roundup", source)
        self.assertEqual([point_a, point_b], points)

    def test_roundup_consolidation_rejects_a_missing_project(self):
        point = {"subject": "A", "event": "funding", "zh": "A融资500万元。", "en": "A raised RMB 5 million.", "evidence": "A融资500万元。"}
        with patch.object(ai, "_call_azure_openai", return_value='{"items": []}'):
            with self.assertRaises(ValueError):
                ai._consolidate_summary_points([point, dict(point)], "roundup", point["evidence"])

    def test_same_company_independent_events_are_never_merged(self):
        points = [{"subject": "A", "event": event, "zh": "独立事件。", "en": "Separate event.", "evidence": "独立事件的原文。"}
                  for event in ("funding:Series A", "acquisition:B", "product:C")]
        with patch.object(ai, "_call_azure_openai") as call:
            self.assertEqual(points, ai._consolidate_summary_points(points, "roundup", "独立事件的原文。"))
        call.assert_not_called()

    def pipeline(self):
        pipeline = TechCrunchMySQLPipeline({})
        spider = MagicMock()
        spider.name = "tonghuashun"
        return pipeline, spider

    def row(self):
        return {"title_zh": "公司融资", "title_en": "Funding", "content_full": "公司宣布完成500万元融资，资金用于研发。",
                "content_summary_zh": "网站简介", "content_summary_en": "Source description",
                "ai_summary_zh": "未提供可供总结的正文内容。", "ai_summary_en": "Source description"}

    def test_pipeline_replaces_placeholders_and_copied_descriptions_with_aligned_bullets(self):
        pipeline, spider = self.pipeline()
        row = self.row()
        pipeline._summarize_news_bilingual = MagicMock(return_value={"status": "ok", "zh": "- 公司融资500万元。", "en": "- The company raised RMB 5 million."})
        pipeline._translate_text_azure = MagicMock()
        pipeline._enrich_summaries(row, spider)
        self.assertEqual("- 公司融资500万元。", row["ai_summary_zh"])
        self.assertEqual("- The company raised RMB 5 million.", row["ai_summary_en"])
        self.assertEqual("网站简介", row["content_summary_zh"])
        pipeline._translate_text_azure.assert_not_called()

    def test_pipeline_does_not_relabel_local_excerpts_after_ai_failure(self):
        pipeline, spider = self.pipeline()
        row = self.row()
        pipeline._summarize_news_bilingual = MagicMock(side_effect=RuntimeError("unavailable"))
        pipeline._enrich_summaries(row, spider)
        self.assertEqual("", row["ai_summary_zh"])
        self.assertEqual("", row["ai_summary_en"])
        self.assertEqual("网站简介", row["content_summary_zh"])

    def test_pipeline_semantic_source_quality_check_clears_unsupported_existing_summaries(self):
        pipeline, spider = self.pipeline()
        row = self.row()
        row["ai_summary_zh"] = "旧摘要声称公司已获得融资。"
        pipeline._summarize_news_bilingual = MagicMock(return_value={"status": "insufficient", "reason": "正文只有图片说明"})
        pipeline._summarize_text_zh = MagicMock()
        pipeline._enrich_summaries(row, spider)
        self.assertEqual("", row["ai_summary_zh"])
        pipeline._summarize_text_zh.assert_not_called()
        spider.logger.warning.assert_called()

    def test_bare_text_and_br_body_survives_when_only_caption_has_p_tag(self):
        body = "<p style='text-align:center'>图 1：公司厂区照片，图片来源：公司公告及公开资料。</p>"
        body += "<span>华虹宏力</span>宣布回购3亿至5亿元，回购股份将全部注销。<br><br>"
        body += "公司预计2027年毛利率约25%，但需满足产能满载和售价提升的条件。"
        html = "<div class='article-content'>" + body + "</div><main>" + ("无关推荐内容。" * 600) + "</main>"
        spider = TonghuashunSpider()
        content = spider._extract_content(BeautifulSoup(html, "html.parser"))
        self.assertIn("3亿至5亿元", content)
        self.assertIn("产能满载", content)
        self.assertNotIn("图 1", content)
        self.assertNotIn("无关推荐", content)

    def test_full_body_is_not_cut_at_old_source_limits(self):
        from news.spiders.techcrunch_api import TechCrunchApiSpider
        from news.spiders.techcrunch_venture import TechCrunchVentureSpider
        from news.spiders.techstartups import TechStartupsSpider
        from news.spiders.bvp import BvpSpider
        from news.spiders.thomabravo import ThomabravoSpider
        from news.spiders.hongshan import HongshanSpider
        from news.spiders.a16z import A16zSpider
        paragraphs = [f"<p>Project {i} raised $10 million to develop its enterprise software platform and expand its engineering team.</p>" for i in range(100)]
        html = "<article><div class='entry-content post-content prose'>" + "".join(paragraphs) + "</div></article>"
        for cls in (TechCrunchApiSpider, TechCrunchVentureSpider, TechStartupsSpider, BvpSpider, ThomabravoSpider, HongshanSpider, A16zSpider):
            with self.subTest(source=cls.__name__):
                spider = cls()
                if cls in (BvpSpider, ThomabravoSpider, HongshanSpider, A16zSpider):
                    content = spider._extract_content(BeautifulSoup(html, "html.parser"), html)
                else:
                    content = spider._extract_content(html)
                self.assertIn("Project 99", content)
                self.assertGreater(len(content), 8000)
        content = TonghuashunSpider()._extract_content(BeautifulSoup(html, "html.parser"))
        self.assertIn("Project 99", content)


if __name__ == "__main__":
    unittest.main()
