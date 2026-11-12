import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import scrapy

from .baseSpider import BaseSpider

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    import feedparser
except Exception:
    feedparser = None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

RSS_URL = "https://techcrunch.com/category/venture/feed/"


class TechCrunchVentureSpider(BaseSpider):
    name = "techcrunch_venture"
    allowed_domains = ["techcrunch.com"]
    custom_settings = {
        "ITEM_PIPELINES": {
            "news.pipelines.TechCrunchMySQLPipeline": 100,
            "crawlab.scrapy.pipelines.CrawlabPipeline": 200,
        },
        "DOWNLOAD_DELAY": 0.5,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_MAX_DELAY": 8.0,
    }

    def __init__(self, hours: int = 36, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.hours = int(hours)
        except Exception:
            self.hours = 36
        self.cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours)
        self._seen_links = set()

    def start_requests(self):
        yield scrapy.Request(
            url=RSS_URL,
            callback=self.parse_rss,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://techcrunch.com/",
            },
            dont_filter=True,
        )

    def parse_rss(self, response):
        if not feedparser:
            self.logger.error("feedparser 未安装，venture spider 无法运行")
            return

        feed = feedparser.parse(response.body)
        entries = getattr(feed, "entries", [])
        if not entries:
            self.logger.warning("TechCrunch Venture RSS 无条目")
            return

        self.logger.info("TechCrunch Venture RSS 条目数: %s", len(entries))

        for entry in entries:
            try:
                pub_date = None
                if getattr(entry, "published_parsed", None):
                    pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif getattr(entry, "published", None):
                    try:
                        from email.utils import parsedate_to_datetime
                        pub_date = parsedate_to_datetime(entry.published)
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                if not pub_date or pub_date < self.cutoff:
                    continue

                link = getattr(entry, "link", "")
                if not link or link in self._seen_links:
                    continue
                self._seen_links.add(link)

                title = getattr(entry, "title", "")
                author = getattr(entry, "author", "")
                summary = getattr(entry, "summary", "")
                summary_text = self._strip_html(summary)

                yield scrapy.Request(
                    url=link,
                    callback=self.parse_article,
                    headers={"User-Agent": USER_AGENT},
                    meta={
                        "title": title,
                        "date_iso": pub_date.isoformat(),
                        "author": author,
                        "content_summary_en": summary_text[:300] + "..." if len(summary_text) > 300 else summary_text,
                    },
                    dont_filter=True,
                )
            except Exception as e:
                self.logger.warning("RSS 条目处理失败: %s", e)

    def parse_article(self, response):
        soup = BeautifulSoup(response.text, "html.parser") if BeautifulSoup else None

        title = (response.meta.get("title") or "").strip()
        if not title and soup:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)

        date_iso = response.meta.get("date_iso", "")
        detail_date = self._extract_date_iso(soup)
        if detail_date:
            date_iso = detail_date

        author = (response.meta.get("author") or "").strip()
        if not author:
            author = self._extract_author(soup)

        content = self._extract_content(response.text)
        rss_summary = response.meta.get("content_summary_en", "")

        pub_date = ""
        dt = self._parse_dt(date_iso)
        if dt:
            pub_date = dt.strftime("%Y-%m-%d")

        row = {
            "标题(中文)": "",
            "标题(英文)": title,
            "链接": response.url,
            "网站": "TechCrunch Venture",
            "发布日期": pub_date,
            "作者": author,
            "标签": "",
            "内容摘要(中文)": "",
            "内容摘要(英文)": rss_summary,
            "AI摘要(中文)": "",
            "AI摘要(英文)": "",
            "完整内容(英文)": content,
        }

        print("=== TechCrunch Venture ===")
        print(json.dumps(row, ensure_ascii=False))
        yield row

    def _parse_dt(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        text = str(value).strip()
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
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
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
                    candidates.extend(data["@graph"])
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
            ".wp-block-post-content", ".entry-content", ".post-content",
            ".article-content", "[data-module='ArticleBody']", "main article",
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
                pattern in text_lower for pattern in [
                    "sign up for", "subscribe to", "newsletter", "read more",
                    "share on", "facebook", "twitter", "linkedin", "follow us",
                    "get updates", "privacy policy", "terms of use", "cookie policy",
                ]
            ):
                continue
            if any(
                marker in text_lower for marker in [
                    "views expressed", "should not be construed",
                    "investment advice", "disclaimer",
                ]
            ):
                break
            seen.add(text)
            paragraphs.append(text)
        content_text = " ".join(paragraphs).strip()
        if len(content_text) > 2000:
            content_text = content_text[:2000] + "..."
        return content_text

    def _strip_html(self, text: str) -> str:
        if not text:
            return ""
        if BeautifulSoup is None:
            return " ".join(scrapy.Selector(text=text).css("::text").getall()).strip()
        return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
