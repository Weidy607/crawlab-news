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

# Current public sections; the former /podcasts endpoint now returns 404.
LISTING_PAGES = [
    "https://www.thomabravo.com/press-releases",
    "https://www.thomabravo.com/news-awards",
    "https://www.thomabravo.com/behind-the-deal",
]

ALLOWED_ARTICLE_PATHS = {"/press-releases/", "/news-awards/", "/insights/", "/behind-the-deal/"}

_TITLE_PREFIX_RE = re.compile(
    r'^(?:PORTFOLIO|TRANSACTION|AWARDS?|NEWS|FIRM|COMMUNITY|BUYOUTS)'
    r'(?:\s+|\s*[:\-–—]\s*)',
    flags=re.IGNORECASE,
)

# Matches date anywhere in a string, e.g. "FIRM • APRIL 15, 2026 Title..."
_INLINE_DATE_RE = re.compile(
    r'(January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+\d{1,2},\s+\d{4}',
    re.IGNORECASE,
)


def _strip_title_prefix(raw: str) -> str:
    if not raw:
        return raw
    t = raw.strip()
    prev = None
    while prev != t:
        prev = t
        t = _TITLE_PREFIX_RE.sub("", t).lstrip("-–—: ").strip()
    return t


def _looks_like_article(url: str, is_podcast_page: bool = False, anchor_text: str = "") -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = (parsed.path or "").rstrip("/")

    if host not in {"thomabravo.com", "www.thomabravo.com"}:
        return False
    if any(path.lower().endswith(ext) for ext in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".mp3", ".mp4", ".zip")):
        return False
    if "/tag/" in path.lower():
        return False

    # Root listing pages themselves — skip
    bare = path.lower().rstrip("/")
    if bare in ("/press-releases", "/news-awards", "/podcasts", "/insights", "/behind-the-deal"):
        return False

    if is_podcast_page:
        return "read the transcript" in anchor_text.lower()

    for prefix in ALLOWED_ARTICLE_PATHS:
        if path.lower().startswith(prefix):
            return True
    return False


class ThomabravoSpider(BaseSpider):
    name = "thomabravo"
    allowed_domains = ["thomabravo.com", "www.thomabravo.com"]
    custom_settings = {
        "ITEM_PIPELINES": {
            "news.pipelines.TechCrunchMySQLPipeline": 100,
            "crawlab.scrapy.pipelines.CrawlabPipeline": 200,
        },
        "DOWNLOAD_DELAY": 1.0,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_MAX_DELAY": 12.0,
        "REDIRECT_ENABLED": True,
        "HTTPERROR_ALLOWED_CODES": [],
    }

    def __init__(self, hours: int = 36, max_articles=50, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.hours = int(hours)
        except Exception:
            self.hours = 36
        self.cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours)
        self.max_articles = max(1, int(max_articles))
        self._seen_links = set()
        self._date_hints: dict = {}      # url -> date string from anchor text
        self._listing_total = 0
        self._time_filtered_posts = 0
        self._queued_article_requests = 0

    def start_requests(self):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.thomabravo.com/",
        }
        for page_url in LISTING_PAGES:
            yield scrapy.Request(
                url=page_url,
                headers=headers,
                callback=self.parse_listing,
                meta={"listing_url": page_url},
                dont_filter=True,
            )

    def closed(self, reason):
        self.logger.info("thomabravo totals: candidates=%s filtered_by_time=%s queued=%s hours=%s reason=%s",
                         self._listing_total, self._time_filtered_posts,
                         self._queued_article_requests, self.hours, reason)

    def parse_listing(self, response):
        soup = BeautifulSoup(response.text, "html.parser") if BeautifulSoup else None
        listing_url = response.meta.get("listing_url", response.url)
        is_podcast_page = "/podcasts" in listing_url
        article_urls = []

        if soup:
            for a in soup.select("a[href]"):
                href = (a.get("href") or "").strip()
                if not href or href.startswith("#"):
                    continue
                anchor_text = a.get_text(" ", strip=True)
                full = urljoin(response.url, href)
                if not _looks_like_article(full, is_podcast_page=is_podcast_page, anchor_text=anchor_text):
                    continue
                # Always extract date hint from ANY anchor pointing to this URL,
                # even if the URL is a duplicate — the second anchor (in the
                # category-search section) is typically the one that carries the date.
                if full not in self._date_hints:
                    m = _INLINE_DATE_RE.search(anchor_text)
                    if m:
                        self._date_hints[full] = m.group(0)

                if full in self._seen_links:
                    continue
                self._seen_links.add(full)

                article_urls.append(full)

        self._listing_total += len(article_urls)
        self.logger.info("thomabravo listing [%s] found %d URLs", listing_url, len(article_urls))

        for url in article_urls:
            if self._queued_article_requests >= self.max_articles:
                break
            # Pre-filter by anchor-text date hint to avoid unnecessary fetches
            if url in self._date_hints:
                dt_hint = self._parse_dt(self._date_hints[url])
                if dt_hint and dt_hint < self.cutoff:
                    self._time_filtered_posts += 1
                    continue

            self._queued_article_requests += 1
            yield scrapy.Request(
                url=url,
                callback=self.parse_article,
                headers={"User-Agent": USER_AGENT},
                dont_filter=True,
            )

    def parse_article(self, response):
        soup = BeautifulSoup(response.text, "html.parser") if BeautifulSoup else None

        # --- Title ---
        title = ""
        if soup:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)
            if not title:
                for sel in ["meta[property='og:title']", "meta[name='twitter:title']"]:
                    m = soup.select_one(sel)
                    if m and m.get("content"):
                        title = m["content"].strip()
                        break
        title = _strip_title_prefix(title)

        # --- Date ---
        date_iso = self._extract_date_iso(soup)
        if not date_iso and response.url in self._date_hints:
            date_iso = self._date_hints[response.url]
        dt = self._parse_dt(date_iso)

        if dt and dt < self.cutoff:
            self._time_filtered_posts += 1
            self.logger.debug("Skipping old article: %s (%s)", response.url, date_iso)
            return

        pub_date = dt.strftime("%Y-%m-%d") if dt else ""
        author = self._extract_author(soup) or "Thoma Bravo"
        content = self._extract_content(soup, response.text)

        self.logger.info(
            "thomabravo stats: listing=%s filtered_by_time=%s queued=%s hours=%s",
            self._listing_total,
            self._time_filtered_posts,
            self._queued_article_requests,
            self.hours,
        )

        row = {
            "标题(中文)": "",
            "标题(英文)": title,
            "链接": response.url,
            "网站": "thomabravo",
            "发布日期": pub_date,
            "作者": author,
            "标签": "",
            "内容摘要(中文)": "",
            "内容摘要(英文)": "",
            "AI摘要(中文)": "",
            "AI摘要(英文)": "",
            "完整内容(英文)": content,
        }

        print("=== thomabravo ===")
        print(json.dumps(row, ensure_ascii=False))
        yield row

    # ------------------------------------------------------------------ helpers

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
        # Fallback: search visible text for a date pattern like "APRIL 15, 2026".
        # First try short elements (< 80 chars) to avoid false-positives from body paragraphs.
        for el in soup.find_all(["p", "span", "div", "time", "li"]):
            t = el.get_text(" ", strip=True)
            m = _INLINE_DATE_RE.search(t)
            if m and len(t) < 80:
                return m.group(0)
        # Last resort: scan the first 2000 chars of visible page text
        body = soup.get_text(" ", strip=True)
        m = _INLINE_DATE_RE.search(body[:2000])
        if m:
            return m.group(0)
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
        return ""

    def _extract_content(self, soup, html_text: str) -> str:
        if not soup:
            if not html_text:
                return ""
            return " ".join(scrapy.Selector(text=html_text).css("p::text").getall()).strip()

        main = (
            soup.select_one("article")
            or soup.select_one("main .font-body")
            or soup.select_one("div.post-content, div.article-content, div.entry-content, div.rich-text, div.prose")
            or soup.select_one("main")
            or soup
        )

        for tag in main.find_all(["script", "style", "nav", "footer", "aside", "header", "iframe", "noscript", "figure", "picture", "svg", "button"]):
            tag.decompose()

        cut_markers = [
            "subscribe",
            "newsletter",
            "related",
            "read next",
            "more insights",
            "innovations, investments, strategies",  # site tagline
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
            if any(marker in low for marker in cut_markers):
                if parts:
                    break
                continue
            if len(text) < 5:
                continue
            seen.add(text)
            parts.append(text)

        content_text = " ".join(parts).strip()
        if len(content_text) > 4000:
            content_text = content_text[:4000] + "..."
        return content_text
