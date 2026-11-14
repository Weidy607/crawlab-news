"""Direct-source spiders for sites previously reached through Google News.

Each spider has an independent Scrapy name/task but shares the same conservative
list discovery, time filtering, article extraction and ai_news output contract.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

import scrapy

from .baseSpider import BaseSpider

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CHINA_TZ = timezone(timedelta(hours=8))


def _response_encoding(response) -> str:
    """Prefer an explicit charset and strict UTF-8 over statistical guessing."""
    declared = (response.encoding or "").strip().lower()
    if declared and declared not in {"iso-8859-1", "latin-1", "latin1"}:
        return declared
    try:
        response.content.decode("utf-8", errors="strict")
        return "utf-8"
    except UnicodeDecodeError:
        return response.apparent_encoding or declared or "utf-8"


class DirectNewsRequestsMiddleware:
    """Use requests for sites where Scrapy/Twisted TLS is rejected."""

    def process_request(self, request, spider):
        if not getattr(spider, "use_requests_middleware", True):
            return None
        import requests as requests_lib
        from scrapy.http import TextResponse

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Referer": getattr(spider, "home_url", request.url),
        }
        headers.update(request.headers.to_unicode_dict())
        try:
            response = requests_lib.get(request.url, headers=headers, timeout=(10, 30), allow_redirects=True)
            response.raise_for_status()
            response_headers = {
                key: value for key, value in response.headers.items()
                if key.lower() not in ("content-encoding", "transfer-encoding")
            }
            return TextResponse(
                url=response.url,
                status=response.status_code,
                headers=response_headers,
                body=response.content,
                encoding=_response_encoding(response),
                request=request,
            )
        except Exception as exc:
            spider.logger.warning("direct requests middleware failed for %s: %s", request.url, exc)
            return None


class DirectNewsMixin:
    source_name = ""
    home_url = ""
    list_urls = ()
    article_pattern = r"$^"
    content_selectors = ()
    publication_selectors = ()
    use_requests_middleware = True
    default_timezone = CHINA_TZ
    max_articles_per_page = 120
    custom_settings = {
        "ITEM_PIPELINES": {
            "news.pipelines.TechCrunchMySQLPipeline": 100,
            "crawlab.scrapy.pipelines.CrawlabPipeline": 200,
        },
        "DOWNLOADER_MIDDLEWARES": {
            "news.spiders.direct_news_sources.DirectNewsRequestsMiddleware": 50,
            "news.middlewares.proxy.KuaidailiProxyMiddleware": None,
        },
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": 2,
        "DOWNLOAD_TIMEOUT": 30,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_MAX_DELAY": 8.0,
    }

    def __init__(self, hours: int = 36, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.hours = max(1, int(hours))
        except Exception:
            self.hours = 36
        self.cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours)
        self._seen_links = set()
        self._list_candidates = 0
        self._time_filtered = 0
        self._emitted = 0

    def start_requests(self):
        for url in self.list_urls or (self.home_url,):
            yield scrapy.Request(
                url=url,
                callback=self.parse_list,
                headers={"User-Agent": USER_AGENT, "Referer": self.home_url},
                dont_filter=True,
            )

    def parse_list(self, response):
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup 不可用，无法解析 %s", self.source_name)
            return
        soup = BeautifulSoup(response.text, "html.parser")
        queued = 0
        for anchor in soup.select("a[href]"):
            link = self._canonical_article_url(urljoin(response.url, anchor.get("href") or ""))
            if not re.search(self.article_pattern, link, re.I) or link in self._seen_links:
                continue
            self._seen_links.add(link)
            self._list_candidates += 1
            title = self._clean_text(anchor.get("title") or anchor.get_text(" ", strip=True))
            yield scrapy.Request(
                url=link,
                callback=self.parse_article,
                headers={"User-Agent": USER_AGENT, "Referer": response.url},
                meta={"list_title": title[:500]},
                dont_filter=True,
            )
            queued += 1
            if queued >= self.max_articles_per_page:
                break
        self.logger.info("%s list candidates=%s queued=%s url=%s", self.source_name, self._list_candidates, queued,
                         response.url)

    def parse_article(self, response):
        soup = BeautifulSoup(response.text, "html.parser") if BeautifulSoup else None
        title = self._extract_title(soup) or self._clean_text(response.meta.get("list_title"))
        published = self._extract_published_at(soup)
        if published is None:
            self.logger.warning("%s 未找到可靠发布时间，保留文章但发布日期留空 url=%s", self.source_name,
                                response.url)
        if published and published < self.cutoff:
            self._time_filtered += 1
            return
        content = self._strip_media_captions(self._extract_content(soup))
        if not title or len(content) < 80:
            self.logger.warning("%s 详情解析不足 title=%s content_len=%s url=%s", self.source_name, bool(title),
                                len(content), response.url)
            return
        summary = self._strip_media_captions(self._extract_summary(soup))
        if not summary:
            summary = content[:500].rsplit("。", 1)[0] or content[:500]
        author = self._extract_author(soup)
        # Match Crunchbase: leave tags empty so TechCrunchMySQLPipeline uses
        # Azure classification first and its shared keyword classifier as fallback.
        tags = ""
        pub_date = published.astimezone(self.default_timezone).strftime("%Y-%m-%d") if published else ""
        row = {
            "标题(中文)": title if self._contains_chinese(title) else "",
            "标题(英文)": "" if self._contains_chinese(title) else title,
            "链接": self._canonical_article_url(response.url),
            "网站": self.source_name,
            "发布日期": pub_date,
            "作者": author,
            "标签": tags,
            "内容摘要(中文)": summary[:1000] if self._contains_chinese(summary) else "",
            "内容摘要(英文)": "" if self._contains_chinese(summary) else summary[:1000],
            # AI summaries are generated by the database pipeline from the
            # cleaned full body. A source description is not an AI summary.
            "AI摘要(中文)": "",
            "AI摘要(英文)": "",
            "完整内容(英文)": content,
            "match_type": "",
        }
        self._emitted += 1
        print(f"=== {self.source_name} ===")
        print(json.dumps(row, ensure_ascii=False))
        yield row

    def closed(self, reason):
        self.logger.info(
            "%s finished: candidates=%s time_filtered=%s emitted=%s hours=%s",
            self.source_name,
            self._list_candidates,
            self._time_filtered,
            self._emitted,
            self.hours,
        )

    def _extract_title(self, soup) -> str:
        if not soup:
            return ""
        for attrs in (
            {"property": "og:title"}, {"name": "twitter:title"}, {"name": "title"},
        ):
            node = soup.find("meta", attrs=attrs)
            if node and node.get("content"):
                return self._clean_title(node.get("content"))
        node = soup.find("h1")
        if node:
            return self._clean_title(node.get_text(" ", strip=True))
        return self._clean_title(soup.title.get_text(" ", strip=True)) if soup.title else ""

    def _extract_summary(self, soup) -> str:
        if not soup:
            return ""
        for attrs in ({"name": "description"}, {"property": "og:description"}):
            node = soup.find("meta", attrs=attrs)
            if node and node.get("content"):
                value = self._clean_text(node.get("content"))
                if len(value) >= 30:
                    return value
        return ""

    def _extract_author(self, soup) -> str:
        if not soup:
            return self.source_name
        for attrs in ({"name": "author"}, {"property": "article:author"}):
            node = soup.find("meta", attrs=attrs)
            if node and node.get("content"):
                return self._clean_text(node.get("content"))[:200]
        node = soup.select_one("[rel='author'], .author, .article-author, [class*='author']")
        value = self._clean_text(node.get_text(" ", strip=True)) if node else ""
        return value[:200] or self.source_name

    def _extract_published_at(self, soup) -> Optional[datetime]:
        if not soup:
            return None
        for attrs in (
            {"property": "article:published_time"}, {"name": "publishdate"}, {"name": "pubdate"},
            {"itemprop": "datePublished"},
        ):
            node = soup.find("meta", attrs=attrs)
            if node and node.get("content"):
                parsed = self._parse_dt(node.get("content"))
                if parsed:
                    return parsed
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                graph = item.get("@graph") if isinstance(item.get("@graph"), list) else []
                for candidate in [item, *graph]:
                    if not isinstance(candidate, dict):
                        continue
                    kinds = candidate.get("@type", [])
                    kinds = kinds if isinstance(kinds, list) else [kinds]
                    if not any(isinstance(kind, str) and kind in {
                        "Article", "NewsArticle", "BlogPosting", "Report", "AnalysisNewsArticle",
                        "OpinionNewsArticle",
                    } for kind in kinds):
                        continue
                    parsed = self._parse_dt(candidate.get("datePublished"))
                    if parsed:
                        return parsed
        # Only known publication fields belong here. The first date/time on a
        # page may describe a certificate, a bond maturity or a related article.
        for selector in (*self.publication_selectors, "[itemprop='datePublished']", "time[pubdate]"):
            for node in soup.select(selector):
                parsed = self._parse_dt(node.get("content") or node.get("datetime")
                                        or node.get_text(" ", strip=True))
                if parsed:
                    return parsed
        return None

    def _extract_content(self, soup) -> str:
        if not soup:
            return ""
        candidates = []
        selectors = [*self.content_selectors, "[itemprop='articleBody']", "article", "main"]
        for selector in selectors:
            try:
                matches = soup.select(selector)
                substantive = [node for node in matches if len(node.get_text(" ", strip=True)) >= 80]
                if substantive:
                    candidates = substantive
                    break
            except Exception:
                continue
        if not candidates:
            candidates = [soup.body or soup]
        container = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)))
        for selector in (
            "figcaption", ".img_descr", ".img-description", ".image-caption",
            ".pic-caption", ".photo-caption", "[class*='caption']",
        ):
            for node in container.select(selector):
                node.decompose()
        for tag in container.find_all([
            "script", "style", "nav", "footer", "aside", "iframe", "noscript", "button", "svg",
        ]):
            tag.decompose()
        # Some sites (e.g. 同花顺) put all prose in bare text + <br>, with only
        # the picture caption in <p>. Reading <p> nodes alone loses the real body.
        for node in list(container.find_all("p")):
            value = self._clean_text(node.get_text(" ", strip=True))
            centered = "center" in str(node.get("style") or "").lower()
            if centered and re.match(r"^(?:图\s*\d+|Figure\s*\d+)[\s：:.]", value, re.I):
                node.decompose()
        separator = "\u2029"
        for node in container.find_all("br"):
            node.replace_with(separator)
        for node in container.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
            node.insert_after(separator)
        paragraphs, seen = [], set()
        for line in container.get_text().split(separator):
            text = self._clean_text(line)
            if len(text) < 2 or text in seen:
                continue
            lowered = text.lower()
            if len(text) < 100 and any(marker in lowered for marker in (
                "责任编辑", "免责声明", "相关阅读", "推荐阅读", "扫码", "下载app", "打开app", "举报",
                "privacy policy", "cookie policy", "sign up", "subscribe", "read more", "share this",
            )):
                continue
            seen.add(text)
            paragraphs.append(text)
        return "\n".join(paragraphs).strip()

    @staticmethod
    def _strip_media_captions(value: str) -> str:
        """Remove leading photo captions that are not article facts."""
        text = re.sub(r"[^\S\n]+", " ", str(value or "")).strip()
        caption_patterns = (
            r"^图左[：:].{0,100}?[；;，,]\s*图右[：:].{0,100}?[。.!！]\s*",
            r"^(?:图片|图像|照片)(?:来源|说明|注)[：:].{0,160}?[。.!！]\s*",
            r"^(?:资料图|配图|示意图)(?:[：:].{0,120}?)?[。.!！]?\s*",
        )
        for pattern in caption_patterns:
            text = re.sub(pattern, "", text, count=1, flags=re.I)
        return text.strip()

    def _parse_dt(self, value) -> Optional[datetime]:
        text = self._clean_text(value)
        if not text:
            return None
        if re.fullmatch(r"\d{14}", text):
            try:
                return datetime.strptime(text, "%Y%m%d%H%M%S").replace(
                    tzinfo=self.default_timezone).astimezone(timezone.utc)
            except ValueError:
                return None
        # Normalize only the date component, preserving ISO fractional seconds
        # and time-zone offsets. Require matching separators (not 2027年1.6T).
        match = re.fullmatch(
            r"(?P<year>\d{4})(?P<sep>[-/.])(?P<month>\d{1,2})(?P=sep)(?P<day>\d{1,2})"
            r"(?P<time>(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?)",
            text,
        ) or re.fullmatch(
            r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
            r"(?P<time>(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?)",
            text,
        )
        if not match:
            return None
        normalized = (f"{int(match['year']):04d}-{int(match['month']):02d}-{int(match['day']):02d}"
                      + match["time"])
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or self.default_timezone).astimezone(timezone.utc)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(normalized, fmt).replace(tzinfo=self.default_timezone).astimezone(timezone.utc)
            except ValueError:
                continue
        return None

    def _canonical_article_url(self, value: str) -> str:
        parsed = urlparse(value)
        scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
        return urlunparse((scheme, parsed.netloc.lower(), parsed.path, "", "", ""))

    def _clean_title(self, value) -> str:
        title = self._clean_text(value)
        for suffix in (f" - {self.source_name}", f" | {self.source_name}", f"_{self.source_name}",
                       " - 新浪网", "_新浪财经_新浪网", " _ 东方财富网"):
            if title.lower().endswith(suffix.lower()):
                title = title[:-len(suffix)].strip()
        return title[:500]

    @staticmethod
    def _clean_text(value) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _contains_chinese(value: str) -> bool:
        return bool(re.search(r"[\u3400-\u9fff]", value or ""))


class PANewsSpider(DirectNewsMixin, BaseSpider):
    name = "panews"
    source_name = "PANews"
    home_url = "https://www.panewslab.com/zh"
    list_urls = ("https://www.panewslab.com/zh", "https://www.panewslab.com/zh/in-depth")
    allowed_domains = ["panewslab.com"]
    article_pattern = r"panewslab\.com/zh/articles/[0-9a-z-]+$"
    content_selectors = ("[class*='article-content']", "[class*='rich-text']", ".prose")


class RFIDWorldSpider(DirectNewsMixin, BaseSpider):
    name = "rfidworld"
    source_name = "RFID世界网"
    home_url = "https://www.rfidworld.com.cn/news/"
    allowed_domains = ["rfidworld.com.cn"]
    article_pattern = r"rfidworld\.com\.cn/news/\d{4}_[0-9A-F]+\.html$"
    content_selectors = (".news-content", ".article-content", "#content", ".content")


class BihaiNewsSpider(DirectNewsMixin, BaseSpider):
    name = "bihainews"
    source_name = "碧海新闻"
    home_url = "https://www.bihainews.com/"
    allowed_domains = ["bihainews.com"]
    article_pattern = r"bihainews\.com/bhn-info/[^/]+/\d+\.html$"
    content_selectors = (".article-content", ".news-content", "#content", ".content")


class FutuNewsSpider(DirectNewsMixin, BaseSpider):
    name = "futunn"
    source_name = "富途资讯"
    home_url = "https://news.futunn.com/main"
    allowed_domains = ["news.futunn.com"]
    article_pattern = r"news\.futunn\.com/post/\d+/[^/?#]+$"
    content_selectors = ("#content", ".origin_content", "[class*='article-content']", "[class*='articleContent']",
                         ".detail-content")
    # The middleware falls back to Scrapy's downloader if this site intermittently resets requests connections.


class TonghuashunSpider(DirectNewsMixin, BaseSpider):
    name = "tonghuashun"
    source_name = "同花顺财经"
    home_url = "https://news.10jqka.com.cn/"
    allowed_domains = ["10jqka.com.cn"]
    article_pattern = r"news\.10jqka\.com\.cn/20\d{6}/c\d+\.shtml$"
    content_selectors = (".main-text", ".atc-content", ".article-content", "#content")


class PedailySpider(DirectNewsMixin, BaseSpider):
    name = "pedaily"
    source_name = "投资界"
    home_url = "https://news.pedaily.cn/"
    allowed_domains = ["pedaily.cn"]
    article_pattern = r"news\.pedaily\.cn/20\d{4}/\d+\.shtml$"
    content_selectors = (".news-content", ".article-content", "#news-content", ".content")


class SohuNewsSpider(DirectNewsMixin, BaseSpider):
    name = "sohu_news"
    source_name = "搜狐网"
    home_url = "https://www.sohu.com/"
    allowed_domains = ["sohu.com"]
    article_pattern = r"sohu\.com/a/\d+_\d+$"
    content_selectors = ("#mp-editor", ".article-content", ".text", "article")


class SinaNewsSpider(DirectNewsMixin, BaseSpider):
    name = "sina_news"
    source_name = "新浪网"
    home_url = "https://www.sina.com.cn/"
    allowed_domains = ["sina.com.cn"]
    article_pattern = r"sina\.com\.cn/.*/20\d{2}-\d{2}-\d{2}/doc-[^/]+\.shtml$"
    content_selectors = ("#artibody", ".article-content", ".article", "article")


class SinaFinanceSpider(DirectNewsMixin, BaseSpider):
    name = "sina_finance"
    source_name = "新浪财经"
    home_url = "https://finance.sina.com.cn/"
    allowed_domains = ["finance.sina.com.cn"]
    article_pattern = r"finance\.sina\.com\.cn/.*/20\d{2}-\d{2}-\d{2}/doc-[^/]+\.shtml$"
    content_selectors = ("#artibody", ".article-content", ".article", "article")


class TravelDailySpider(DirectNewsMixin, BaseSpider):
    name = "traveldaily"
    source_name = "环球旅讯"
    home_url = "https://www.traveldaily.cn/"
    allowed_domains = ["traveldaily.cn"]
    article_pattern = r"traveldaily\.cn/article/\d+$"
    content_selectors = ("[class*='articleBody']", "[class*='articleContent']", ".article-content",
                         ".detail-content", "article")


class GuandianSpider(DirectNewsMixin, BaseSpider):
    name = "guandian"
    source_name = "观点网"
    home_url = "https://www.guandian.cn/"
    allowed_domains = ["guandian.cn"]
    article_pattern = r"guandian\.cn/article/20\d{6}/\d+\.html$"
    content_selectors = (".article-content", ".detail-content", "#content", ".content")
    publication_selectors = (".con_l_info_l > p",)


class LaoyaobaSpider(DirectNewsMixin, BaseSpider):
    name = "laoyaoba"
    source_name = "集微网"
    home_url = "https://www.laoyaoba.com/"
    allowed_domains = ["laoyaoba.com"]
    article_pattern = r"laoyaoba\.com/n/\d+$"
    content_selectors = (".news-content", ".article-content", "#content", ".content")

    def _extract_published_at(self, soup) -> Optional[datetime]:
        if soup:
            for script in soup.find_all("script"):
                text = script.string or ""
                match = re.search(r"\bvar\s+article_data\s*=\s*", text)
                if not match:
                    continue
                try:
                    data, _ = json.JSONDecoder().raw_decode(text[match.end():])
                except (ValueError, TypeError):
                    continue
                if isinstance(data, dict):
                    parsed = self._parse_dt(data.get("published_time"))
                    if parsed:
                        return parsed
        return super()._extract_published_at(soup)
