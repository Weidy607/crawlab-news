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

LISTING_URL = "https://www.hongshan.com/newsroom/"
ALLOWED_HOSTS = {"hongshan.com", "www.hongshan.com"}

_ARTICLE_DATE_RE = re.compile(r"发表\s*(20\d{2})[./年](\d{1,2})[./月](\d{1,2})")


def _looks_like_article(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = (parsed.path or "").rstrip("/")

    if host not in ALLOWED_HOSTS:
        return False
    if not path or path in ("/newsroom", "/newsroom/"):
        return False
    if "/article/" not in path.lower():
        return False
    if any(path.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".zip", ".mp4")):
        return False
    return True


class HongshanSpider(BaseSpider):
    name = "hongshan"
    allowed_domains = ["hongshan.com", "www.hongshan.com"]
    custom_settings = {
        "ITEM_PIPELINES": {
            "news.pipelines.TechCrunchMySQLPipeline": 100,
            "crawlab.scrapy.pipelines.CrawlabPipeline": 200,
        },
        "DOWNLOAD_DELAY": 0.8,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
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
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.hongshan.com/",
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

        if soup:
            for a in soup.select("a[href]"):
                href = (a.get("href") or "").strip()
                if not href or href.startswith("#"):
                    continue

                full = urljoin(response.url, href)
                if not _looks_like_article(full):
                    continue
                if full in self._seen_links:
                    continue

                self._seen_links.add(full)
                article_urls.append(full)

        self._listing_total = len(article_urls)
        self.logger.info("hongshan listing found %d URLs from %s", len(article_urls), response.url)

        for url in article_urls:
            self._queued_article_requests += 1
            yield scrapy.Request(
                url=url,
                callback=self.parse_article,
                headers={"User-Agent": USER_AGENT},
                dont_filter=True,
            )

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

        date_iso = self._extract_date_iso(soup)
        dt = self._parse_dt(date_iso)

        if dt and dt < self.cutoff:
            self._time_filtered_posts += 1
            self.logger.debug("Skipping old article: %s (%s)", response.url, date_iso)
            return

        pub_date = dt.strftime("%Y-%m-%d") if dt else ""
        author = self._extract_author(soup)
        content = self._extract_content(soup, response.text)

        self.logger.info(
            "hongshan stats: listing=%s filtered_by_time=%s queued=%s hours=%s",
            self._listing_total,
            self._time_filtered_posts,
            self._queued_article_requests,
            self.hours,
        )

        row = {
            "标题(中文)": "",
            "标题(英文)": title,
            "链接": response.url,
            "网站": "hongshan",
            "发布日期": pub_date,
            "作者": author,
            "标签": "",
            "内容摘要(中文)": "",
            "内容摘要(英文)": "",
            "AI摘要(中文)": "",
            "AI摘要(英文)": "",
            "完整内容(英文)": content,
        }

        print("=== hongshan ===")
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
            pass

        m = _ARTICLE_DATE_RE.search(text)
        if m:
            y, mo, da = m.groups()
            try:
                dt = datetime(int(y), int(mo), int(da), tzinfo=timezone.utc)
                return dt
            except Exception:
                return None

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
            ".date",
        ]:
            el = soup.select_one(sel)
            if not el:
                continue
            raw = el.get("datetime") or el.get("content") or el.get_text(" ", strip=True)
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

        meta_author = soup.find("meta", attrs={"name": "author"}) or soup.find("meta", attrs={"property": "article:author"})
        if meta_author and meta_author.get("content"):
            return meta_author["content"].strip()

        a = soup.find("a", rel="author")
        if a:
            return a.get_text(" ", strip=True)

        byline = soup.find(string=re.compile(r"^\s*By\s+", re.I))
        if byline:
            return re.sub(r"^\s*By\s+", "", str(byline).strip(), flags=re.I).strip()

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
            "免责声明",
            "copyright",
            "相关阅读",
            "推荐阅读",
            "相关文章",
            "订阅",
            "newsletter",
            "subscribe",
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
            if any(marker in low for marker in cut_markers):
                if parts:
                    break
                continue
            if len(text) < 2 and not el.find("a"):
                continue

            seen.add(text)
            parts.append(text)

        content_text = "\n".join(parts).strip()
        if len(content_text) > 4000:
            content_text = content_text[:4000] + "..."
        return content_text
