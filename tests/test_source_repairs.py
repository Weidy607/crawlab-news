import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "news"))
try:
    from scrapy import Request
    from scrapy.http import HtmlResponse
    from news.spiders.bvp import BvpSpider
    from news.spiders.thomabravo import ThomabravoSpider, _looks_like_article
    from news.spiders.crunchbase import CrunchbaseSpider, FEEDS
    from news.pipelines import TechCrunchMySQLPipeline
    RUNTIME_AVAILABLE = True
except ImportError:
    RUNTIME_AVAILABLE = False


@unittest.skipUnless(RUNTIME_AVAILABLE, "Install news runtime dependencies to test source parsing")
class SourceRepairsTest(unittest.TestCase):
    def setUp(self):
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)
        self.cutoff = datetime(2026, 11, 10, tzinfo=timezone.utc)

    def response(self, url, body, meta=None):
        return HtmlResponse(url, body=body.encode(), encoding="utf-8", request=Request(url, meta=meta or {}))

    def test_bvp_news_in_next_payload_without_links(self):
        posts = [
            {"kind": "news", "path": "/news/new-round", "publishedAt": "2026-11-11T13:00:00Z", "contributors": None},
            {"kind": "news", "path": "/news/old-round", "publishedAt": "2026-09-01T13:00:00Z"},
            {"kind": "memo", "path": "/news/not-news", "publishedAt": "2026-11-11T13:00:00Z"},
        ]
        payload = '0:["$",null,' + json.dumps({"posts": posts}) + ']'
        html = '<script>self.__next_f.push(' + json.dumps([1, payload]) + ')</script>'
        spider = BvpSpider(); spider.cutoff = self.cutoff
        requests = list(spider.parse_listing(self.response("https://www.bvp.com/atlas?type=news", html)))
        self.assertEqual(["https://www.bvp.com/news/new-round"], [r.url for r in requests])
        self.assertEqual(1, spider._time_filtered_posts)
        rows = list(spider.parse_article(self.response(requests[0].url, "<article><h1>Round</h1><p>Real article body.</p></article>", requests[0].meta)))
        self.assertEqual("2026-11-11", rows[0]["发布日期"])
        self.assertEqual("Real article body.", rows[0]["完整内容(英文)"])

    def test_thoma_current_podcast_paths_and_host_boundary(self):
        self.assertTrue(_looks_like_article("https://www.thomabravo.com/behind-the-deal/new-episode"))
        self.assertFalse(_looks_like_article("https://www.thomabravo.com/behind-the-deal"))
        self.assertFalse(_looks_like_article("https://fake-thomabravo.com/press-releases/fake"))

    def test_thoma_keeps_date_filter_and_extracts_body_without_navigation(self):
        spider = ThomabravoSpider(); spider.cutoff = self.cutoff
        html = '<nav><p>Navigation copy</p></nav><main><h1>A deal</h1><p>October 15, 2026</p><div class="font-body"><p>Real deal body</p></div></main>'
        response = self.response("https://www.thomabravo.com/press-releases/a-deal", html)
        self.assertEqual([], list(spider.parse_article(response)))
        self.assertEqual(1, spider._time_filtered_posts)
        spider.cutoff = datetime(2026, 10, 1, tzinfo=timezone.utc)
        row = list(spider.parse_article(response))[0]
        self.assertEqual("2026-10-15", row["发布日期"])
        self.assertEqual("Real deal body", row["完整内容(英文)"])

    def feed(self, body=None):
        encoded = '&lt;p&gt;Full article body that is not a summary.&lt;/p&gt;' if body is None else body
        return '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item><title>Funding round</title><link>https://news.crunchbase.com/venture/funding/</link><pubDate>Wed, 11 Nov 2026 13:00:04 +0000</pubDate><dc:creator>Reporter</dc:creator><description>Short teaser</description><content:encoded>' + encoded + '</content:encoded></item></channel></rss>'

    def test_crunchbase_uses_full_feed_body_and_does_not_fake_ai_fields(self):
        spider = CrunchbaseSpider(); spider.cutoff = self.cutoff
        response = self.response(FEEDS[0], self.feed(), {"feed_index": 0})
        rows = list(spider.parse_feed(response))
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("2026-11-11", row["发布日期"])
        self.assertEqual("Reporter", row["作者"])
        self.assertEqual("Full article body that is not a summary.", row["完整内容(英文)"])
        self.assertEqual("", row["AI摘要(英文)"])
        self.assertEqual("", row["AI摘要(中文)"])
        self.assertEqual([], list(spider.parse_feed(response)))

    def test_crunchbase_rejects_stale_entries(self):
        spider = CrunchbaseSpider(); spider.cutoff = datetime(2026, 11, 12, tzinfo=timezone.utc)
        self.assertEqual([], list(spider.parse_feed(self.response(FEEDS[0], self.feed(), {"feed_index": 0}))))

    def test_crunchbase_missing_full_body_fetches_detail_instead_of_using_teaser(self):
        spider = CrunchbaseSpider(); spider.cutoff = self.cutoff
        output = list(spider.parse_feed(self.response(FEEDS[0], self.feed(""), {"feed_index": 0})))
        self.assertIsInstance(output[0], Request)
        self.assertEqual("https://news.crunchbase.com/venture/funding/", output[0].url)

    def test_crunchbase_invalid_feed_schedules_fallback_before_spider_closes(self):
        spider = CrunchbaseSpider()
        requests = list(spider.parse_feed(self.response(FEEDS[0], "<html>Blocked</html>", {"feed_index": 0})))
        self.assertEqual(FEEDS[1], requests[0].url)
        self.assertEqual(1, requests[0].meta["feed_index"])

    def test_crunchbase_mysql_uses_configured_host(self):
        config = {"host": "host.docker.internal", "port": 3306, "db": "news", "table": "ai_news"}
        pipeline = TechCrunchMySQLPipeline(config)
        spider = MagicMock(); spider.name = "crunchbase"
        with patch.object(pipeline, "_init_ai_enrichment"), patch("news.pipelines.pymysql.connect") as connect:
            pipeline.open_spider(spider)
        self.assertEqual("host.docker.internal", connect.call_args.kwargs["host"])
        self.assertEqual(config, pipeline.mysql_cfg)


if __name__ == "__main__":
    unittest.main()
