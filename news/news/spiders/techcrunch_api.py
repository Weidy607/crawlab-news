import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import scrapy

from .baseSpider import BaseSpider

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class TechCrunchApiSpider(BaseSpider):
    name = "techcrunch_api"
    allowed_domains = ["techcrunch.com"]
    custom_settings = {
        "ITEM_PIPELINES": {
            # Enrich items before Crawlab persists them so Data tab matches MySQL writes.
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
        self._api_total_posts = 0
        self._time_filtered_posts = 0
        self._queued_article_requests = 0
        try:
            import feedparser
        except Exception:
            feedparser = None
        self._feedparser = feedparser

    def start_requests(self):
        base_url = "https://techcrunch.com/wp-json/wp/v2/posts"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://techcrunch.com/",
        }

        for page in range(1, 11):
            params = {
                "_embed": "1",
                "orderby": "date",
                "order": "desc",
                "context": "embed",
                "_fields": "id,link,date_gmt,date,title,_embedded.author",
                "per_page": 100,
                "page": page,
            }
            query = urlencode(params)
            yield scrapy.Request(
                url=f"{base_url}?{query}",
                method="GET",
                headers=headers,
                cb_kwargs={"page": page},
                dont_filter=True,
            )

    def parse(self, response, page: int):
        if response.status == 400 and b"rest_post_invalid_page_number" in response.body:
            return

        try:
            posts = json.loads(response.text)
        except Exception as e:
            self.logger.warning("API JSON parse failed (page=%s): %s", page, e)
            return

        if not posts:
            return

        for post in posts:
            self._api_total_posts += 1
            date_iso = post.get("date_gmt") or post.get("date")
            dt = self._parse_dt(date_iso)
            if not dt or dt < self.cutoff:
                self._time_filtered_posts += 1
                continue

            link = post.get("link") or ""
            if not link:
                continue
            if link in self._seen_links:
                continue
            self._seen_links.add(link)

            title = post.get("title", {})
            if isinstance(title, dict):
                title = title.get("rendered", "")

            embedded = post.get("_embedded", {})
            authors = embedded.get("author", []) if isinstance(embedded, dict) else []
            author = authors[0].get("name", "") if authors else ""

            yield scrapy.Request(
                url=link,
                callback=self.parse_article,
                meta={
                    "title": title,
                    "date_iso": date_iso or "",
                    "author": author,
                },
                headers={"User-Agent": USER_AGENT},
                dont_filter=True,
            )
            self._queued_article_requests += 1

        self.logger.info(
            "TechCrunch API stats: total=%s filtered_by_time=%s queued=%s hours=%s cutoff=%s",
            self._api_total_posts,
            self._time_filtered_posts,
            self._queued_article_requests,
            self.hours,
            self.cutoff.isoformat(),
        )

    def closed(self, reason):
        if self._seen_links:
            return
        if not self._feedparser:
            self.logger.warning("feedparser 未安装，无法 RSS 兜底")
            return
        self.logger.info("API 无结果，开始 RSS 兜底抓取")
        self._fetch_from_rss()

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
        # RSS fallback may pre-populate a summary via meta
        rss_summary = response.meta.get("content_summary_en", "")

        pub_date = ""
        dt = self._parse_dt(date_iso)
        if dt:
            pub_date = dt.strftime("%Y-%m-%d")

        row = {
            "标题(中文)": "",
            "标题(英文)": title,
            "链接": response.url,
            "网站": "TechCrunch",
            "发布日期": pub_date,
            "作者": author,
            "标签": "",
            "内容摘要(中文)": "",
            "内容摘要(英文)": rss_summary,
            "AI摘要(中文)": "",
            "AI摘要(英文)": "",
            "完整内容(英文)": content,
        }

        print("=== TechCrunch ===")
        print(json.dumps(row, ensure_ascii=False))
        yield row

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

        return "\n".join(paragraphs).strip()

    def _fetch_from_rss(self):
        rss_url = "https://techcrunch.com/feed/"
        try:
            import requests
        except Exception as e:
            self.logger.error("requests 不可用，RSS 兜底失败: %s", e)
            return

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://techcrunch.com/",
        }

        try:
            r = requests.get(rss_url, headers=headers, timeout=(10, 30))
            r.raise_for_status()
        except Exception as e:
            self.logger.error("RSS 拉取失败: %s", e)
            return

        feed = self._feedparser.parse(r.content)
        if not getattr(feed, "entries", None):
            self.logger.info("RSS 无内容")
            return

        for entry in feed.entries[:50]:
            try:
                pub_date = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "published"):
                    try:
                        from email.utils import parsedate_to_datetime
                        pub_date = parsedate_to_datetime(entry.published)
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                    except Exception:
                        pub_date = None

                if not pub_date or pub_date < self.cutoff:
                    continue

                link = getattr(entry, "link", "")
                if not link or link in self._seen_links:
                    continue
                self._seen_links.add(link)

                title = getattr(entry, "title", "")
                summary = getattr(entry, "summary", "")
                summary_text = self._strip_html(summary)
                content_summary = summary_text[:300] + "..." if len(summary_text) > 300 else summary_text

                row = {
                    "标题(中文)": "",
                    "标题(英文)": title,
                    "链接": link,
                    "网站": "TechCrunch",
                    "发布日期": pub_date.strftime("%Y-%m-%d"),
                    "作者": "RSS Feed",
                    "标签": "",
                    "内容摘要(中文)": "",
                    "内容摘要(英文)": content_summary,
                    "AI摘要(中文)": "",
                    "AI摘要(英文)": "",
                    "完整内容(英文)": "",
                }

                print("=== TechCrunch (RSS) ===")
                print(json.dumps(row, ensure_ascii=False))
                # Feed the item back into the Scrapy engine so it goes through
                # the pipeline (CrawlabPipeline + TechCrunchMySQLPipeline).
                self.crawler.engine.crawl(
                    scrapy.Request(
                        url=link,
                        callback=self.parse_article,
                        meta={
                            "title": row["标题(英文)"],
                            "date_iso": row["发布日期"],
                            "author": row["作者"],
                            "content_summary_en": row["内容摘要(英文)"],
                        },
                        dont_filter=True,
                    ),
                )
            except Exception as e:
                self.logger.warning("RSS 处理失败: %s", e)

            time.sleep(0.1)

    def _strip_html(self, text: str) -> str:
        if not text:
            return ""
        if BeautifulSoup is None:
            return " ".join(scrapy.Selector(text=text).css("::text").getall()).strip()
        return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
