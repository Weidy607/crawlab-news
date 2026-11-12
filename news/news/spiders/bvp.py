import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

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

LISTING_URL = "https://www.bvp.com/atlas?type=news"
ALLOWED_HOSTS = {"bvp.com", "www.bvp.com"}


def _looks_like_article(url: str) -> bool:
    p = urlparse(url)
    host = p.netloc.lower()
    path = (p.path or "").rstrip("/")
    if host not in ALLOWED_HOSTS or not path:
        return False
    if path in ("/news", "/newsroom"):
        return False
    if "/news/" not in path:
        return False
    if any(path.lower().endswith(ext) for ext in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".mp4", ".webp")):
        return False
    return True


class BvpSpider(BaseSpider):
    name = "bvp"
    allowed_domains = ["bvp.com", "www.bvp.com"]
    custom_settings = {
        "ITEM_PIPELINES": {
            "news.pipelines.TechCrunchMySQLPipeline": 100,
            "crawlab.scrapy.pipelines.CrawlabPipeline": 200,
        },
        "DOWNLOAD_DELAY": 1.0,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
        "REDIRECT_ENABLED": True,
        "HTTPERROR_ALLOWED_CODES": [],
    }

    def __init__(self, hours: int = 36, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.hours = int(hours)
        except Exception:
            self.hours = 36
        self.cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours)
        self._seen_links = set()
        self._listing_total = 0
        self._time_filtered_posts = 0
        self._queued_article_requests = 0

    def start_requests(self):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.bvp.com/",
        }
        yield scrapy.Request(
            url=LISTING_URL,
            headers=headers,
            callback=self.parse_listing,
            dont_filter=True,
        )

    def parse_listing(self, response):
        soup = BeautifulSoup(response.text, "html.parser") if BeautifulSoup else None
        article_urls = []
        hints = {}

        # Atlas renders cards on the client. Its public Next.js payload contains
        # the news paths, publication dates and contributors even without anchors.
        if soup:
            for script in soup.find_all("script"):
                text = (script.string or "").strip()
                if not text.startswith("self.__next_f.push("):
                    continue
                try:
                    packet = json.loads(text[text.index("(") + 1:text.rfind(")")])
                    payload = packet[1] if len(packet) > 1 and isinstance(packet[1], str) else ""
                    marker = '"posts":'
                    offset = payload.find(marker)
                    if offset < 0:
                        continue
                    posts, _ = json.JSONDecoder().raw_decode(payload[offset + len(marker):].lstrip())
                except (ValueError, TypeError, IndexError):
                    continue
                if not isinstance(posts, list):
                    continue
                for post in posts:
                    if not isinstance(post, dict) or post.get("kind") != "news":
                        continue
                    url = urljoin(response.url, post.get("path") or "")
                    if not _looks_like_article(url):
                        continue
                    hints[url] = {
                        "date_iso": post.get("publishedAt") or "",
                        "author": ", ".join(author["name"] for author in (post.get("contributors") or [])
                                            if isinstance(author, dict) and author.get("name")),
                    }
                    article_urls.append(url)

        if soup:
            for a in soup.select("a[href]"):
                href = (a.get("href") or "").strip()
                if not href or href.startswith("#"):
                    continue
                full = urljoin(response.url, href)
                if not _looks_like_article(full):
                    continue
                if full in article_urls:
                    continue
                article_urls.append(full)

        self._listing_total = len(article_urls)
        self.logger.info("bvp listing found %d URLs from %s", len(article_urls), response.url)

        for url in article_urls:
            if url in self._seen_links:
                continue
            self._seen_links.add(url)
            hint = hints.get(url, {})
            date = self._parse_dt(hint.get("date_iso"))
            if date and date < self.cutoff:
                self._time_filtered_posts += 1
                continue
            self._queued_article_requests += 1
            yield scrapy.Request(
                url=url,
                callback=self.parse_article,
                headers={"User-Agent": USER_AGENT},
                meta=hint,
                dont_filter=True,
            )
        self.logger.info("bvp listing stats: candidates=%s filtered_by_time=%s queued=%s hours=%s",
                         self._listing_total, self._time_filtered_posts, self._queued_article_requests, self.hours)

    def parse_article(self, response):
        soup = BeautifulSoup(response.text, "html.parser") if BeautifulSoup else None

        title = ""
        if soup:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)
            if not title:
                for sel in ["meta[property='og:title']", "meta[name='twitter:title']", "meta[name='parsely-title']"]:
                    m = soup.select_one(sel)
                    if m and m.get("content"):
                        title = m["content"].strip()
                        break

        date_iso = self._extract_date_iso(soup) or response.meta.get("date_iso", "")
        dt = self._parse_dt(date_iso)

        if dt and dt < self.cutoff:
            self._time_filtered_posts += 1
            self.logger.debug("Skipping old article: %s (%s)", response.url, date_iso)
            return

        pub_date = dt.strftime("%Y-%m-%d") if dt else ""
        author = self._extract_author(soup) or response.meta.get("author", "")
        content = self._extract_content(soup, response.text)

        self.logger.info(
            "bvp stats: listing=%s filtered_by_time=%s queued=%s hours=%s",
            self._listing_total,
            self._time_filtered_posts,
            self._queued_article_requests,
            self.hours,
        )

        row = {
            "标题(中文)": "",
            "标题(英文)": title,
            "链接": response.url,
            "网站": "bvp",
            "发布日期": pub_date,
            "作者": author,
            "标签": "",
            "内容摘要(中文)": "",
            "内容摘要(英文)": "",
            "AI摘要(中文)": "",
            "AI摘要(英文)": "",
            "完整内容(英文)": content,
        }

        print("=== bvp ===")
        print(json.dumps(row, ensure_ascii=False))
        yield row

    def _parse_dt(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        text = str(value).strip()
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
            pass
        try:
            import dateutil.parser as dp

            dt = dp.parse(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _extract_date_iso(self, soup) -> str:
        if not soup:
            return ""
        for sel in [
            "time[datetime]",
            "meta[property='article:published_time']",
            "meta[property='article:modified_time']",
            "meta[name='pubdate']",
            "meta[name='parsely-pub-date']",
            "meta[name='date']",
            "meta[name='publish_date']",
        ]:
            el = soup.select_one(sel)
            if el:
                raw = el.get("datetime") or el.get("content")
                if raw:
                    return raw.strip()

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
                val = item.get("datePublished") or item.get("dateCreated")
                if val:
                    return str(val)
        return ""

    def _extract_author(self, soup) -> str:
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
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                au = obj.get("author")
                names = []
                if isinstance(au, list):
                    for a in au:
                        if isinstance(a, dict) and a.get("name"):
                            names.append(a["name"].strip())
                        elif isinstance(a, str):
                            names.append(a.strip())
                elif isinstance(au, dict) and au.get("name"):
                    names.append(au["name"].strip())
                elif isinstance(au, str):
                    names.append(au.strip())
                if names:
                    return ", ".join(names[:5])

        a = soup.find("a", rel="author")
        if a:
            return a.get_text(" ", strip=True)

        m = soup.find("meta", attrs={"name": "author"}) or soup.find("meta", attrs={"property": "article:author"})
        if m and m.get("content"):
            return m["content"].strip()

        for node in soup.find_all(string=re.compile(r"^\s*By\s+", re.I)):
            txt = re.sub(r"^\s*By\s+", "", str(node).strip(), flags=re.I).strip()
            if txt and len(txt) <= 120:
                return txt
        return ""

    def _extract_content(self, soup, html_text: str) -> str:
        if not soup:
            if not html_text:
                return ""
            return " ".join(scrapy.Selector(text=html_text).css("p::text").getall()).strip()

        main = (
            soup.select_one("article")
            or soup.select_one("div.post-content, div.article-content, div.single-post, div.entry-content, div.rich-text, div.prose")
            or soup
        )

        for tag in main.find_all(["script", "style", "nav", "footer", "aside", "header", "iframe", "noscript", "figure", "picture", "svg", "button"]):
            tag.decompose()

        cut_markers = [
            "subscribe",
            "newsletter",
            "related",
            "read next",
            "view all",
            "disclaimer",
            "investment advice",
        ]

        parts = []
        seen = set()
        for el in main.find_all(["p", "h2", "h3", "h4", "li"]):
            if el.find(["img", "svg", "picture", "figure", "button"]):
                continue
            text = el.get_text(" ", strip=True)
            if not text or text in seen:
                continue
            low = text.lower()
            if len(text) < 20 and el.name == "li":
                continue
            if any(token in low for token in cut_markers):
                if parts:
                    break
                continue
            if text.count("|") >= 2:
                continue
            seen.add(text)
            parts.append(text)

        content_text = " ".join(parts).strip()
        if len(content_text) > 2000:
            content_text = content_text[:2000] + "..."
        return content_text
