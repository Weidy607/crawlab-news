import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urljoin

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
CHINA_TZ = timezone(timedelta(hours=8))


class EastmoneyRequestsMiddleware:
    """Use requests' TLS stack and preserve the final response URL/encoding."""

    def process_request(self, request, spider):
        import requests as requests_lib
        from scrapy.http import TextResponse

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Referer": "https://finance.eastmoney.com/",
        }
        headers.update(request.headers.to_unicode_dict())
        try:
            response = requests_lib.get(
                request.url,
                headers=headers,
                timeout=(10, 30),
                allow_redirects=True,
            )
            response.raise_for_status()
            response_headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() not in ("content-encoding", "transfer-encoding")
            }
            return TextResponse(
                url=response.url,
                status=response.status_code,
                headers=response_headers,
                body=response.content,
                encoding=response.apparent_encoding or "utf-8",
                request=request,
            )
        except Exception as exc:
            spider.logger.warning("EastmoneyRequestsMiddleware failed for %s: %s", request.url, exc)
            return None


class EastmoneySpider(BaseSpider):
    """东方财富直采：列表 API 优先，财经首页作为无结果兜底。"""

    name = "eastmoney"
    allowed_domains = ["eastmoney.com", "finance.eastmoney.com", "np-listapi.eastmoney.com"]
    custom_settings = {
        "ITEM_PIPELINES": {
            "news.pipelines.TechCrunchMySQLPipeline": 100,
            "crawlab.scrapy.pipelines.CrawlabPipeline": 200,
        },
        "DOWNLOADER_MIDDLEWARES": {
            "news.spiders.eastmoney.EastmoneyRequestsMiddleware": 50,
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

    def __init__(self, hours: int = 36, pages: int = 5, columns: str = "345", *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.hours = max(1, int(hours))
        except Exception:
            self.hours = 36
        try:
            self.pages = max(1, min(20, int(pages)))
        except Exception:
            self.pages = 5
        self.columns = [value.strip() for value in str(columns).split(",") if value.strip()] or ["345"]
        self.cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours)
        self._seen_links = set()
        self._api_total_posts = 0
        self._time_filtered_posts = 0
        self._queued_article_requests = 0

    def start_requests(self):
        endpoint = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://finance.eastmoney.com/",
        }
        for column in self.columns:
            for page in range(1, self.pages + 1):
                params = {
                    "client": "web",
                    "biz": "web_news_col",
                    "column": column,
                    "order": "1",
                    "needInteractData": "0",
                    "page_index": page,
                    "page_size": 100,
                    "req_trace": uuid.uuid4().hex,
                }
                yield scrapy.Request(
                    url=f"{endpoint}?{urlencode(params)}",
                    headers=headers,
                    callback=self.parse,
                    cb_kwargs={"page": page, "column": column},
                    dont_filter=True,
                )

    def parse(self, response, page: int, column: str):
        try:
            payload = json.loads(response.text.lstrip("\ufeff"))
        except Exception as exc:
            self.logger.warning("东方财富 API JSON 解析失败 column=%s page=%s: %s", column, page, exc)
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        posts = data.get("list", []) if isinstance(data, dict) else []
        if str(payload.get("code")) not in {"1", "200"} or not isinstance(posts, list):
            self.logger.warning(
                "东方财富 API 返回异常 column=%s page=%s code=%s message=%s",
                column,
                page,
                payload.get("code"),
                payload.get("message"),
            )
            return

        for post in posts:
            if not isinstance(post, dict):
                continue
            self._api_total_posts += 1
            published = self._parse_dt(post.get("showTime"))
            if not published or published < self.cutoff:
                self._time_filtered_posts += 1
                continue
            link = str(post.get("uniqueUrl") or post.get("url") or "").strip()
            if link.startswith("http://"):
                link = "https://" + link[len("http://"):]
            if not link or link in self._seen_links:
                continue
            self._seen_links.add(link)
            yield scrapy.Request(
                url=link,
                callback=self.parse_article,
                headers={"User-Agent": USER_AGENT, "Referer": "https://finance.eastmoney.com/"},
                meta={
                    "title": self._clean_text(post.get("title")),
                    "date_iso": str(post.get("showTime") or ""),
                    "author": self._clean_text(post.get("mediaName")),
                    "summary": self._clean_text(post.get("summary")),
                },
                dont_filter=True,
            )
            self._queued_article_requests += 1

        self.logger.info(
            "Eastmoney API stats: total=%s filtered_by_time=%s queued=%s hours=%s cutoff=%s",
            self._api_total_posts,
            self._time_filtered_posts,
            self._queued_article_requests,
            self.hours,
            self.cutoff.isoformat(),
        )

    def parse_article(self, response):
        soup = BeautifulSoup(response.text, "html.parser") if BeautifulSoup else None
        title = self._clean_text(response.meta.get("title")) or self._extract_title(soup)
        published = self._parse_dt(self._extract_date(soup) or response.meta.get("date_iso"))
        if published and published < self.cutoff:
            return
        author = self._clean_text(response.meta.get("author")) or self._extract_author(soup)
        summary = self._clean_text(response.meta.get("summary")) or self._extract_meta(soup, "description")
        content = self._extract_content(response.text)
        if not title or not content:
            self.logger.warning("东方财富详情页缺少标题或正文：%s", response.url)
            return

        pub_date = published.astimezone(CHINA_TZ).strftime("%Y-%m-%d") if published else ""
        tags = self._classify_tags(title, summary + " " + content[:2000])
        row = {
            "标题(中文)": title,
            "标题(英文)": "",
            "链接": response.url,
            "网站": "东方财富",
            "发布日期": pub_date,
            "作者": author,
            "标签": tags,
            "内容摘要(中文)": summary[:1000],
            "内容摘要(英文)": "",
            "AI摘要(中文)": summary[:1000],
            "AI摘要(英文)": "",
            # The shared pipeline stores this neutral full-text field in ai_news.content_full.
            "完整内容(英文)": content,
            "match_type": tags.split(",", 1)[0] if tags else "Other",
        }
        print("=== Eastmoney ===")
        print(json.dumps(row, ensure_ascii=False))
        yield row

    def closed(self, reason):
        if self._seen_links:
            return
        self.logger.info("东方财富 API 无可用结果，开始财经首页兜底抓取")
        self._fetch_from_homepage()

    def _fetch_from_homepage(self):
        if BeautifulSoup is None:
            self.logger.warning("BeautifulSoup 不可用，无法执行首页兜底")
            return
        try:
            import requests

            response = requests.get(
                "https://finance.eastmoney.com/",
                headers={"User-Agent": USER_AGENT, "Referer": "https://finance.eastmoney.com/"},
                timeout=(10, 30),
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as exc:
            self.logger.error("东方财富首页兜底失败: %s", exc)
            return

        queued = 0
        for anchor in soup.select("a[href]"):
            link = urljoin("https://finance.eastmoney.com/", anchor.get("href") or "")
            if not re.search(r"eastmoney\.com/a/\d+\.html(?:$|\?)", link) or link in self._seen_links:
                continue
            title = anchor.get_text(" ", strip=True)
            if len(title) < 8:
                continue
            self._seen_links.add(link)
            self.crawler.engine.crawl(scrapy.Request(
                url=link,
                callback=self.parse_article,
                meta={"title": title, "date_iso": "", "author": "东方财富", "summary": ""},
                headers={"User-Agent": USER_AGENT},
                dont_filter=True,
            ))
            queued += 1
            if queued >= 50:
                break
        self.logger.info("东方财富首页兜底排队详情页：%s", queued)
        time.sleep(0.1)

    def _parse_dt(self, value) -> Optional[datetime]:
        text = self._clean_text(value)
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y年%m月%d日 %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text[:19], fmt)
                return parsed.replace(tzinfo=CHINA_TZ).astimezone(timezone.utc)
            except Exception:
                continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or CHINA_TZ).astimezone(timezone.utc)
        except Exception:
            return None

    def _extract_title(self, soup) -> str:
        if not soup:
            return ""
        node = soup.select_one("#topbox .title") or soup.find("h1")
        if node:
            return self._clean_text(node.get_text(" ", strip=True))
        title = self._clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
        return re.sub(r"\s*[_\-]\s*东方财富网\s*$", "", title)

    def _extract_date(self, soup) -> str:
        if not soup:
            return ""
        infos = soup.select_one("#topbox .infos")
        text = infos.get_text(" ", strip=True) if infos else ""
        match = re.search(r"\d{4}年\d{2}月\d{2}日\s+\d{2}:\d{2}", text)
        return match.group(0) if match else ""

    def _extract_author(self, soup) -> str:
        if not soup:
            return ""
        infos = soup.select_one("#topbox .infos")
        text = infos.get_text(" ", strip=True) if infos else ""
        match = re.search(r"作者[:：]\s*(.+?)(?:\s+来源[:：]|$)", text)
        if match:
            return self._clean_text(match.group(1))[:200]
        match = re.search(r"来源[:：]\s*(.+?)(?:\s|$)", text)
        return self._clean_text(match.group(1))[:200] if match else "东方财富"

    def _extract_meta(self, soup, name: str) -> str:
        if not soup:
            return ""
        node = soup.find("meta", attrs={"name": name})
        return self._clean_text(node.get("content")) if node and node.get("content") else ""

    def _extract_content(self, html_text: str) -> str:
        if not html_text:
            return ""
        if BeautifulSoup is None:
            return " ".join(scrapy.Selector(text=html_text).css("#ContentBody p::text").getall()).strip()
        soup = BeautifulSoup(html_text, "html.parser")
        container = soup.select_one("#ContentBody") or soup.select_one(".txtinfos") or soup.find("article")
        if not container:
            return ""
        for tag in container.find_all(["script", "style", "nav", "footer", "aside", "iframe", "noscript", "form"]):
            tag.decompose()
        paragraphs = []
        seen = set()
        for node in container.find_all(["p", "h2", "h3", "h4", "li"]):
            text = self._clean_text(node.get_text(" ", strip=True))
            if not text or text in seen:
                continue
            if "文章来源：" in text or "责任编辑：" in text:
                break
            lowered = text.lower()
            if len(text) < 80 and any(marker in lowered for marker in (
                "免责声明", "风险提示", "扫码", "下载东方财富", "关注东方财富", "举报",
            )):
                continue
            seen.add(text)
            paragraphs.append(text)
        return " ".join(paragraphs).strip()[:12000]

    def _classify_tags(self, title: str, content: str) -> str:
        text = f"{title} {content}".lower()
        mapping = {
            "AI": ("人工智能", "ai", "大模型", "算力"),
            "Funding": ("融资", "募资", "投资", "估值"),
            "M&A": ("并购", "收购", "合并", "控股"),
            "IPO": ("ipo", "上市", "招股"),
            "Semiconductor": ("芯片", "半导体", "gpu"),
            "Cloud Computing": ("云计算", "数据中心", "saas"),
            "Cybersecurity": ("网络安全", "数据泄露", "勒索软件"),
            "Robotics": ("机器人", "具身智能"),
        }
        hits = [tag for tag, words in mapping.items() if any(word in text for word in words)]
        return ", ".join(hits[:3]) if hits else "Other"

    @staticmethod
    def _clean_text(value) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()
