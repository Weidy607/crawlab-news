import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

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


class TechStartupsSpider(BaseSpider):
	name = "techstartups"
	allowed_domains = ["techstartups.com"]
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
		self._api_total_posts = 0
		self._time_filtered_posts = 0
		self._queued_article_requests = 0

	def start_requests(self):
		base_url = "https://techstartups.com/wp-json/wp/v2/posts"
		headers = {
			"User-Agent": USER_AGENT,
			"Accept": "application/json, text/plain, */*",
			"Accept-Language": "en-US,en;q=0.9",
			"Referer": "https://techstartups.com/",
		}

		for page in range(1, 6):
			params = {
				"page": page,
				"per_page": 100,
				"orderby": "date",
				"order": "desc",
				"_fields": "id,link,date,date_gmt,title",
			}
			yield scrapy.Request(
				url=f"{base_url}?{urlencode(params)}",
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
			self.logger.warning("TechStartups API JSON parse failed (page=%s): %s", page, e)
			return

		if not posts:
			return

		for post in posts:
			self._api_total_posts += 1
			date_iso = post.get("date_gmt") or post.get("date") or ""
			dt = self._parse_dt(date_iso)
			if not dt or dt < self.cutoff:
				self._time_filtered_posts += 1
				continue

			link = post.get("link") or ""
			if not link or link in self._seen_links:
				continue

			self._seen_links.add(link)
			title = post.get("title", "")
			if isinstance(title, dict):
				title = title.get("rendered", "")

			yield scrapy.Request(
				url=link,
				callback=self.parse_article,
				meta={
					"title": title,
					"date_iso": date_iso,
				},
				headers={"User-Agent": USER_AGENT},
				dont_filter=True,
			)
			self._queued_article_requests += 1

		self.logger.info(
			"TechStartups API stats: total=%s filtered_by_time=%s queued=%s hours=%s cutoff=%s",
			self._api_total_posts,
			self._time_filtered_posts,
			self._queued_article_requests,
			self.hours,
			self.cutoff.isoformat(),
		)

	def closed(self, reason):
		if self._seen_links:
			return
		if not feedparser:
			self.logger.warning("feedparser 未安装，无法 RSS 兜底")
			return

		rss_url = "https://techstartups.com/feed/"
		headers = {
			"User-Agent": USER_AGENT,
			"Accept": "application/rss+xml, application/xml, text/xml, */*",
			"Accept-Language": "en-US,en;q=0.9",
			"Referer": "https://techstartups.com/",
		}

		try:
			import requests

			r = requests.get(rss_url, headers=headers, timeout=(10, 30))
			r.raise_for_status()
			feed = feedparser.parse(r.content)
		except Exception as e:
			self.logger.error("RSS 拉取失败: %s", e)
			return

		if not getattr(feed, "entries", None):
			return

		for entry in feed.entries[:100]:
			link = getattr(entry, "link", "")
			if not link or link in self._seen_links:
				continue

			published = getattr(entry, "published", "")
			dt = self._parse_dt(published)
			if not dt and getattr(entry, "published_parsed", None):
				try:
					dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
				except Exception:
					dt = None

			if not dt or dt < self.cutoff:
				continue

			self._seen_links.add(link)
			title = getattr(entry, "title", "")
			yield scrapy.Request(
				url=link,
				callback=self.parse_article,
				meta={
					"title": title,
					"date_iso": dt.isoformat(),
					"author": getattr(entry, "author", ""),
				},
				headers={"User-Agent": USER_AGENT},
				dont_filter=True,
			)

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
		dt = self._parse_dt(date_iso)
		if dt and dt < self.cutoff:
			self.logger.debug("Skip TechStartups article outside cutoff: %s (%s < %s)", response.url, dt.isoformat(), self.cutoff.isoformat())
			return
		pub_date = dt.strftime("%Y-%m-%d") if dt else ""

		author = (response.meta.get("author") or "").strip()
		if not author:
			author = self._extract_author(soup)

		content = self._extract_content(response.text)

		row = {
			"标题(中文)": "",
			"标题(英文)": title,
			"链接": response.url,
			"网站": "TechStartups",
			"发布日期": pub_date,
			"作者": author,
			"标签": "",
			"内容摘要(中文)": "",
			"内容摘要(英文)": "",
			"AI摘要(中文)": "",
			"AI摘要(英文)": "",
			"完整内容(英文)": content,
		}

		print("=== TechStartups ===")
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
		container = soup.find("div", class_=["entry-content", "post-content", "article-content"])

		if not container:
			for cls in ["entry-content", "post-content", "article-content"]:
				container = soup.find("div", class_=lambda x: x and cls in x)
				if container:
					break

		if not container:
			container = soup.find("article") or soup.find("body") or soup

		for tag in container.find_all(["script", "style", "nav", "footer", "aside", "iframe", "noscript"]):
			tag.decompose()

		paragraphs = []
		seen = set()
		for p in container.find_all(["p", "h2", "h3", "h4"]):
			text = p.get_text(" ", strip=True)
			if not text or text in seen:
				continue
			seen.add(text)
			paragraphs.append(text)

		return "\n".join(paragraphs).strip()
