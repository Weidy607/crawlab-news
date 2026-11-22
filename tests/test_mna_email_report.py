import os
import sys
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "news"))
import send_mna_email_report_v2 as report


class MnaDealFixtures:
    def deal(self, **kwargs):
        item = report.Deal(
            1, "交易新闻", "收购方", "标的", "未披露", None, "签约待交割",
            "SEMICONDUCTORS_INFRA", "交易摘要", "来源", "https://example.com/deal",
            date(2026, 11, 18),
        )
        return replace(item, **kwargs)


class MnaGeographyTests(MnaDealFixtures, unittest.TestCase):
    def geography(self, buyer, target, **kwargs):
        return report.normalize_geography({
            "acquirer_region": buyer, "acquirer_region_confidence": "high",
            "region": target, "region_confidence": "high", **kwargs,
        }, "原文")

    def test_missing_or_unverified_geography_is_pending(self):
        for row in (
            {},
            {"china_relation": "非中国交易", "china_confidence": "low"},
            {"china_relation": "中国境内交易", "china_evidence": "虚构证据"},
            {"region": "中国", "region_confidence": "low"},
        ):
            with self.subTest(row=row):
                self.assertEqual(("未披露", "待核验", "未披露"), report.normalize_geography(row, "原文"))

    def test_both_parties_determine_relation_despite_wrong_model_label(self):
        for buyer, target, expected in (
            ("中国", "中国", "中国境内交易"),
            ("中国", "亚太（不含中国）", "中国企业海外并购"),
            ("北美", "中国", "外资收购中国标的"),
            ("北美", "欧洲", "非中国交易"),
        ):
            with self.subTest(buyer=buyer, target=target):
                self.assertEqual((target, expected, buyer), self.geography(
                    buyer, target, china_relation="非中国交易", china_confidence="high",
                ))

    def test_overseas_target_does_not_prove_non_chinese_buyer(self):
        self.assertEqual(("北美", "待核验", "未披露"), self.geography(
            "未披露", "北美", china_relation="待核验", china_confidence="low",
        ))

    def test_non_chinese_parties_do_not_require_exact_regions(self):
        for buyer, target in (("Stripe", "OpenRouter"), ("英伟达", "Inception"), ("Meta", "Conductor"), ("黑石数字基础设施信托", "AWS两个合资企业")):
            with self.subTest(buyer=buyer, target=target):
                region, relation, acquirer_region = report.normalize_geography({
                    "acquirer_is_chinese": False, "target_is_chinese": False,
                    "china_confidence": "high", "china_relation": "待核验",
                }, "")
                self.assertEqual(("未披露", "非中国交易", "未披露"), (region, relation, acquirer_region))
                self.assertTrue(report.is_international_deal(self.deal(
                    acquirer=buyer, target=target, region=region, china_relation=relation, acquirer_region=acquirer_region,
                )))

    def test_verified_non_china_relation_survives_missing_region_details(self):
        self.assertEqual(("未披露", "非中国交易", "未披露"), report.normalize_geography({
            "china_relation": "非中国交易", "china_confidence": "high",
        }, ""))

    def test_binary_china_identity_overrides_missing_or_conflicting_geography(self):
        self.assertEqual(("未披露", "中国企业海外并购", "中国"), report.normalize_geography({
            "acquirer_is_chinese": True, "target_is_chinese": False, "china_confidence": "high",
        }, ""))
        self.assertEqual(("中国", "中国境内交易", "中国"), self.geography(
            "中国", "中国", acquirer_is_chinese=False, target_is_chinese=False, china_confidence="high",
        ))

    def test_string_and_unverified_binary_flags_are_not_accepted(self):
        for flags in (
            {"acquirer_is_chinese":"false", "target_is_chinese":"false", "china_confidence":"high"},
            {"acquirer_is_chinese":False, "target_is_chinese":False, "china_confidence":"low"},
        ):
            self.assertEqual(("未披露", "待核验", "未披露"), report.normalize_geography(flags, ""))

    def test_literal_source_evidence_can_verify_regions(self):
        source = "收购方总部在中国。标的总部在日本。"
        row = {
            "acquirer_region": "中国", "acquirer_region_evidence": "收购方总部在中国。",
            "region": "亚太（不含中国）", "region_evidence": "标的总部在日本。",
        }
        self.assertEqual(("亚太（不含中国）", "中国企业海外并购", "中国"), report.normalize_geography(row, source))

    def test_chinese_language_or_china_market_mentions_do_not_change_parties(self):
        item = self.deal(
            acquirer="特斯拉", target="SpaceX", region="北美", acquirer_region="北美",
            china_relation="非中国交易", ai_summary_zh="公司在中国有业务。",
        )
        self.assertTrue(report.is_international_deal(item))
        self.assertFalse(report.is_china_deal(item))

    def test_rendering_separates_domestic_overseas_and_unknown(self):
        domestic = self.deal(acquirer="艾为电子", target="钰泰股份", region="中国", acquirer_region="中国", china_relation="中国境内交易")
        china_buyer = self.deal(acquirer="鸿海", target="索尼车载相机业务", region="亚太（不含中国）", acquirer_region="中国", china_relation="中国企业海外并购")
        overseas = self.deal(acquirer="SAP", target="TechWolf", region="欧洲", acquirer_region="欧洲", china_relation="非中国交易")
        unknown = self.deal(acquirer="待确认主体", target="待确认标的")
        output = report.render_email_html([domestic, china_buyer, overseas, unknown], date(2026, 11, 13), date(2026, 11, 20), "2026-11-22")
        international = output.split("<tr id='section-03'", 1)[1].split("<tr id='section-04'", 1)[0]
        self.assertIn("SAP", international)
        for name in ("艾为电子", "鸿海", "待确认主体"):
            self.assertNotIn(name, international)
        self.assertIn("地区待核验交易｜共1笔", output)
        self.assertIn("全球并购概览｜共4笔", output)
        self.assertIn("中国相关交易｜共2笔", output)

    def test_duplicate_unknown_does_not_erase_verified_relation(self):
        saved = self.deal(region="北美", acquirer_region="北美", china_relation="非中国交易")
        report.merge_duplicate_deal(saved, self.deal())
        self.assertTrue(report.is_international_deal(saved))
        report.merge_duplicate_deal(saved, self.deal(acquirer_region="中国", china_relation="中国企业海外并购"))
        self.assertFalse(report.is_international_deal(saved))
        self.assertTrue(report.is_china_deal(saved))

    def test_cache_revision_cannot_be_bypassed_by_pinned_base_version(self):
        with patch.dict(os.environ, {"MNA_ANALYSIS_RULE_VERSION": "old-version"}):
            self.assertNotEqual("old-version", report.analysis_rule_version())
            self.assertIn("geo-v2-20261122", report.analysis_rule_version())


class MnaDeduplicationTests(MnaDealFixtures, unittest.TestCase):
    def test_novosense_three_target_names_merge_with_advanced_stage_and_all_links(self):
        items = [self.deal(
            acquirer="纳芯微", target=name, stage=stage, transaction_facts=facts,
            amount_display="4.87亿元人民币", amount_usd_millions=None, amount_currency="CNY",
            published_at=date(2026,11,18 if i==0 else 17), transaction_date=date(2026,11,16),
            sources=[("来源",f"https://example.com/{i}/{j}") for j in range(2)],
        ) for i,(name,stage,facts) in enumerate((
            ("宁波隔空智能","拟议/要约","拟收购宁波隔空智能78.1520%股权，交割尚未完成。"),
            ("宁波隔空智能科技有限公司","拟议/要约","拟以4.87亿元收购宁波隔空智能科技有限公司78.15%股权。"),
            ("隔空智能","签约待交割","已经签署股权转让协议，等待交割。"),
        ))]
        merged = report.deduplicate_deals(items)
        self.assertEqual(1,len(merged))
        self.assertEqual("宁波隔空智能科技有限公司",merged[0].target)
        self.assertEqual("签约待交割",merged[0].stage)
        self.assertEqual("已经签署股权转让协议，等待交割。",merged[0].transaction_facts)
        self.assertEqual(6,len(merged[0].sources))
        output = report.render_deal_item(1,merged[0])
        for i in range(3):
            for j in range(2):
                self.assertIn(f"https://example.com/{i}/{j}",output)
        # A longer, older narrative must not replace facts from a later stage.
        report.merge_duplicate_deal(merged[0],self.deal(
            transaction_facts="尚在筹划，未披露签约。"*30,stage="拟议/要约",
        ))
        self.assertEqual("已经签署股权转让协议，等待交割。",merged[0].transaction_facts)

    def test_progress_announcement_for_same_ety_entity_is_one_deal(self):
        left = self.deal(acquirer="艾为电子",target="钰泰半导体股份有限公司",transaction_date=date(2026,11,9))
        right = self.deal(acquirer="艾为电子",target="钰泰股份",transaction_date=date(2026,11,17),
                          transaction_facts="披露筹划收购钰泰股份控股权事项的停牌进展公告。")
        self.assertTrue(report.same_deal(left,right))
        self.assertFalse(report.same_deal(left,replace(right,transaction_facts="再次收购钰泰股份控股权，披露停牌进展公告。")))
        self.assertFalse(report.same_deal(left,replace(right,transaction_date=date(2027,1,17))))
        self.assertFalse(report.same_deal(left,replace(right,transaction_facts="签署新的股权收购协议。")))

    def test_company_aliases_are_exact_not_substring_guesses(self):
        self.assertFalse(report.same_entity("隔空智能","隔空智能设备有限公司"))
        self.assertFalse(report.same_entity("隔空智能","北京隔空智能科技有限公司"))
        self.assertFalse(report.same_entity("钰泰股份","钰泰新材料股份有限公司"))
        self.assertTrue(report.same_entity("宁波隔空智能科技有限公司","隔空智能"))
        self.assertTrue(report.same_entity("钰泰半导体股份有限公司","钰泰股份"))

    def test_same_source_url_is_retained_once_when_labels_differ(self):
        saved=self.deal(sources=[("新浪网","https://example.com/article")])
        duplicate=self.deal(sources=[("新浪财经","https://example.com/article")])
        report.merge_duplicate_deal(saved,duplicate)
        self.assertEqual(1,len(saved.sources))

    def asset_pair(self):
        left = self.deal(
            acquirer="黑石的数据中心信托（BXDC）",
            target="亚马逊云服务（AWS）旗下两个数据中心合资园区",
            published_at=date(2026,11,17), region="北美", china_relation="非中国交易",
            transaction_facts="BXDC收购AWS旗下两个数据中心合资园区75.1%控股权益，标的为13栋新建带电外壳机房。",
            sources=[("观点网","https://www.guandian.cn/article/20261117/629062.html")],
        )
        right = self.deal(
            acquirer="黑石数字基础设施信托", target="AWS两个合资企业",
            published_at=date(2026,11,16), china_relation="非中国交易",
            transaction_facts="两个合资企业将拥有位于两个AWS数据中心园区的13个powered shells。",
            sources=[("观点网","https://www.guandian.cn/article/20261116/628534.html")],
        )
        return left,right

    def test_bxdc_alias_and_described_assets_merge_and_keep_both_sources(self):
        left,right = self.asset_pair()
        self.assertTrue(report.same_entity(left.acquirer,right.acquirer))
        self.assertTrue(report.same_deal(left,right))
        self.assertTrue(report.same_deal(right,left))
        merged = report.deduplicate_deals([left,right])
        self.assertEqual(1,len(merged))
        self.assertEqual(2,len(merged[0].sources))
        self.assertEqual("北美",merged[0].region)
        self.assertIsNone(merged[0].amount_usd_millions)
        report.validate_report_reconciliation(merged)

    def test_asset_counts_owner_buyer_and_dates_prevent_false_merges(self):
        left,right = self.asset_pair()
        for other in (
            replace(right,transaction_facts=right.transaction_facts.replace("13个","14个")),
            replace(right,transaction_facts=right.transaction_facts.replace("两个AWS","三个AWS")),
            replace(right,target="微软Azure两个合资企业"),
            replace(right,acquirer="其他基础设施基金"),
            replace(right,published_at=date(2026,10,16)),
            replace(right,transaction_facts="收购AWS两个合资企业，估值50亿美元。"),
            replace(right,transaction_date=date(2026,9,1)),
        ):
            with self.subTest(other=other):
                baseline = replace(left,transaction_date=date(2026,11,17))
                self.assertFalse(report.same_deal(baseline,other))

    def test_aliases_do_not_collapse_parent_and_other_blackstone_funds(self):
        self.assertFalse(report.same_entity("黑石数字基础设施信托","黑石房地产基金"))
        self.assertFalse(report.same_entity("黑石数字基础设施信托","Blackstone"))

    def test_rendering_counts_assets_once_and_shows_both_source_links(self):
        left,right = self.asset_pair()
        output = report.render_email_html([left,right],date(2026,11,13),date(2026,11,20),"2026-11-22")
        self.assertIn("国际交易｜共1笔",output)
        self.assertIn("全球并购概览｜共1笔",output)
        self.assertEqual(1,output.count("<table class='key-deal-card'"))
        self.assertIn("20261117/629062.html",output)
        self.assertIn("20261116/628534.html",output)


if __name__ == "__main__":
    unittest.main()
