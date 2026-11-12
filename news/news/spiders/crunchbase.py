"""Crunchbase News from its public, full-text syndicated RSS feed."""

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import scrapy
from scrapy.exceptions import CloseSpider
from bs4 import BeautifulSoup

from .baseSpider import BaseSpider

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
FEEDS = ("https://feeds.feedburner.com/CrunchbaseNews", "https://news.crunchbase.com/feed/")
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


class CrunchbaseSpider(BaseSpider):
    name = "crunchbase"
    allowed_domains = ["news.crunchbase.com", "feeds.feedburner.com", "feeds2.feedburner.com"]
    custom_settings = {
        "ITEM_PIPELINES": {
            "news.pipelines.TechCrunchMySQLPipeline": 100,
            "crawlab.scrapy.pipelines.CrawlabPipeline": 200,
        },
        "DOWNLOADER_MIDDLEWARES": {"news.middlewares.proxy.KuaidailiProxyMiddleware": None},
        "REDIRECT_ENABLED": True,
        "HTTPERROR_ALLOWED_CODES": [],
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 1,
        "DOWNLOAD_TIMEOUT": 30,
    }

    def __init__(self, hours=36, max_articles=50, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hours = int(hours)
        self.max_articles = max(1, int(max_articles))
        self.cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours)
        self._seen_links = set()
        self._feed_total = self._time_filtered = self._emitted = 0

    def start_requests(self):
        yield self._feed_request(0)

    def _feed_request(self, index):
        return scrapy.Request(
            FEEDS[index], callback=self.parse_feed, errback=self.feed_failed,
            headers={"User-Agent": USER_AGENT}, meta={"feed_index": index},
            dont_filter=True,
        )

    def _next_feed(self, index):
        if index + 1 < len(FEEDS):
            return self._feed_request(index + 1)
        self.logger.error("All Crunchbase feeds failed; no articles were collected")
        raise CloseSpider("feed_unavailable")

    def feed_failed(self, failure):
        self.logger.warning("Crunchbase feed request failed: %s", failure.getErrorMessage())
        yield self._next_feed(failure.request.meta["feed_index"])

    def parse_feed(self, response):
        try:
            root = ET.fromstring(response.body)
            entries = root.findall("./channel/item")
        except ET.ParseError:
            entries = []
        if not entries:
            self.logger.warning("Crunchbase feed returned no RSS items: %s", response.url)
            yield self._next_feed(response.meta["feed_index"])
            return
        self._feed_total = len(entries)
        for entry in entries:
            date = self._parse_dt(entry.findtext("pubDate"))
            if not date or date < self.cutoff:
                self._time_filtered += 1
                continue
            url = (entry.findtext("link") or "").strip()
            if urlparse(url).hostname != "news.crunchbase.com" or url in self._seen_links:
                continue
            self._seen_links.add(url)
            if self._emitted >= self.max_articles:
                break
            self._emitted += 1
            metadata = {
                "title": entry.findtext("title") or "",
                "date_iso": date.isoformat(),
                "author": entry.findtext(CREATOR) or "",
                "description": entry.findtext("description") or "",
            }
            full_html = entry.findtext(CONTENT) or ""
            if full_html.strip():
                # The syndicated content is the article body, not a search snippet.
                yield self._row(url, metadata, full_html)
            else:
                yield scrapy.Request(url, callback=self.parse_article, meta=metadata,
                                     headers={"User-Agent": USER_AGENT})
        self.logger.info("Crunchbase feed stats: total=%s filtered_by_time=%s selected=%s hours=%s source=%s",
                         self._feed_total, self._time_filtered, self._emitted, self.hours, response.url)

    def parse_article(self, response):
        yield self._row(response.url, response.meta, response.text)

    def _row(self, url, metadata, full_html):
        soup = BeautifulSoup(full_html, "html.parser")
        title = metadata.get("title") or (soup.h1.get_text(" ", strip=True) if soup.h1 else "")
        date = self._parse_dt(metadata.get("date_iso"))
        content = self._extract_content(full_html)
        if not title.strip() or not content.strip():
            raise ValueError("Crunchbase article has no title or body: " + url)
        return {
            "标题(中文)": "", "标题(英文)": title.strip(), "链接": url,
            "网站": "Crunchbase", "发布日期": date.strftime("%Y-%m-%d") if date else "",
            "作者": metadata.get("author") or self._extract_author(soup),
            "标签": "", "内容摘要(中文)": "",
            "内容摘要(英文)": BeautifulSoup(metadata.get("description", ""), "html.parser").get_text(" ", strip=True),
            "AI摘要(中文)": "", "AI摘要(英文)": "", "完整内容(英文)": content,
        }

    def _parse_dt(self, date_iso: Optional[str]) -> Optional[datetime]:
        if not date_iso:
            return None

        text = str(date_iso).strip()
        if not text:
            return None

        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _extract_author(self, soup) -> str:
        if not soup:
            return ""

        meta_author = soup.find("meta", attrs={"name": "author"})
        if meta_author and meta_author.get("content"):
            return meta_author.get("content", "").strip()

        author_link = soup.find("a", rel="author")
        if author_link:
            return author_link.get_text(" ", strip=True)

        return ""

    def _extract_date_iso(self, soup) -> str:
        if not soup:
            return ""

        scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
        for script in scripts:
            text = script.string
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue

            candidates = []
            if isinstance(data, dict):
                if isinstance(data.get("@graph"), list):
                    candidates.extend(data.get("@graph") or [])
                candidates.append(data)
            elif isinstance(data, list):
                candidates.extend(data)

            for item in candidates:
                if not isinstance(item, dict):
                    continue
                value = item.get("datePublished") or item.get("dateCreated")
                if value:
                    return str(value)

        meta_date = soup.find("meta", attrs={"property": "article:published_time"})
        if meta_date and meta_date.get("content"):
            return str(meta_date.get("content"))

        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            return str(time_tag.get("datetime"))

        return ""

    def _extract_content(self, html_text: str) -> str:
        if not html_text:
            return ""

        if BeautifulSoup is None:
            return " ".join(scrapy.Selector(text=html_text).css("p::text").getall()).strip()

        soup = BeautifulSoup(html_text, "html.parser")
        selectors = [
            ".wp-block-post-content",
            ".entry-content",
            ".post-content",
            ".article-content",
            "[data-module='ArticleBody']",
            "main article",
        ]

        main_container = None
        for selector in selectors:
            elems = soup.select(selector)
            if elems:
                main_container = elems[0]
                break

        if not main_container:
            main_container = soup.find("article") or soup.find("body") or soup

        for tag in main_container.find_all(["script", "style", "nav", "footer", "aside", "iframe", "noscript"]):
            tag.decompose()

        paragraphs = []
        seen = set()
        for p in main_container.find_all(["p", "h2", "h3", "h4"]):
            text = p.get_text(" ", strip=True)
            if not text or text in seen:
                continue
            text_lower = text.lower()
            if len(text) < 100 and any(
                pattern in text_lower
                for pattern in [
                    "sign up for",
                    "subscribe to",
                    "newsletter",
                    "read more",
                    "share on",
                    "facebook",
                    "twitter",
                    "linkedin",
                    "follow us",
                    "get updates",
                    "privacy policy",
                    "terms of use",
                    "cookie policy",
                    "gdpr",
                ]
            ):
                continue

            if any(
                marker in text_lower
                for marker in [
                    "views expressed",
                    "should not be construed",
                    "investment advice",
                    "disclaimer",
                    "this post was published",
                ]
            ):
                break

            seen.add(text)
            paragraphs.append(text)

        content_text = " ".join(paragraphs).strip()
        return content_text
