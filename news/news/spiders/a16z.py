import json
import re
import xml.etree.ElementTree as ET
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

LISTING_URL = "https://a16z.com/news-content/"
ARCHIVE_URL = "https://www.a16z.news/archive"
ALLOWED_HOSTS = {"a16z.com", "www.a16z.com", "a16zcrypto.com", "www.a16zcrypto.com", "a16z.news", "www.a16z.news"}

_A16Z_DATE_RE = re.compile(r'^/20\d{2}/(0[1-9]|1[0-2])/[a-z0-9-]+/?$', re.IGNORECASE)
_A16Z_KEY_PATTERN = re.compile(r'^/(press|announcement|manifesto|benchmarks|reform|race)/[a-z0-9-]{6,}/?$', re.IGNORECASE)
_A16Z_SUBSTACK_RE = re.compile(r'^/p/[a-z0-9-]+/?$', re.IGNORECASE)


def _looks_like_article(url: str) -> bool:
	p = urlparse(url)
	host = p.netloc.lower()
	path = p.path.rstrip('/') or ''
	if not any(h in host for h in ALLOWED_HOSTS) or not path or path == '/':
		return False
	if any(path.lower().endswith(ext) for ext in ('.pdf', '.jpg', '.jpeg', '.png', '.gif', '.zip', '.mp4', '.webp')):
		return False
	if _A16Z_SUBSTACK_RE.match(path.lower()):
		return True
	if _A16Z_DATE_RE.match(path.lower()):
		return True
	if _A16Z_KEY_PATTERN.match(path.lower()):
		return True
	return False


class A16zSpider(BaseSpider):
	name = "a16z"
	allowed_domains = ["a16z.com", "a16zcrypto.com", "a16z.news", "www.a16z.news"]
	custom_settings = {
		"ITEM_PIPELINES": {
			"news.pipelines.TechCrunchMySQLPipeline": 100,
			"crawlab.scrapy.pipelines.CrawlabPipeline": 200,
		},
		"DOWNLOAD_DELAY": 1.0,
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
		self._listing_urls = []
		self._api_total_posts = 0
		self._time_filtered_posts = 0
		self._queued_article_requests = 0

	def start_requests(self):
		headers = {
			"User-Agent": USER_AGENT,
			"Accept": "text/html,application/xhtml+xml,*/*",
			"Accept-Language": "en-US,en;q=0.9",
			"Referer": "https://a16z.com/",
		}
		# Step 1: fetch listing page
		yield scrapy.Request(
			url=LISTING_URL,
			headers=headers,
			callback=self.parse_listing,
			dont_filter=True,
		)

	def parse_listing(self, response):
		"""Parse the a16z news-content entry page and extract article URLs."""
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
				if full not in self._seen_links:
					self._seen_links.add(full)
					article_urls.append(full)

		self._listing_urls = article_urls
		self.logger.info("a16z listing found %d URLs from %s", len(article_urls), response.url)

		# news-content now redirects to a Substack embed shell; fetch archive when sparse
		if len(article_urls) <= 12 and ARCHIVE_URL not in response.url:
			self.logger.info("Listing sparse (%d), fetching archive", len(article_urls))
			yield scrapy.Request(
				url=ARCHIVE_URL,
				headers={"User-Agent": USER_AGENT},
				callback=self.parse_listing,
				dont_filter=True,
			)
		else:
			yield from self._yield_article_requests(article_urls)

	def parse_sitemap_index(self, response):
		"""Parse sitemap index and find post sitemaps."""
		try:
			root = ET.fromstring(response.text)
		except Exception as e:
			self.logger.warning("Sitemap index parse failed: %s", e)
			yield from self._yield_article_requests(self._listing_urls)
			return

		ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
		post_sitemaps = []
		for loc in root.findall(".//sm:loc", ns) or root.findall(".//loc"):
			url = (loc.text or "").strip()
			if "post-sitemap" in url or "posts-sitemap" in url:
				post_sitemaps.append(url)

		if post_sitemaps:
			for sm_url in post_sitemaps[:3]:  # limit to 3 most-recent post sitemaps
				yield scrapy.Request(
					url=sm_url,
					headers={"User-Agent": USER_AGENT},
					callback=self.parse_sitemap,
					dont_filter=True,
				)
		else:
			yield from self._yield_article_requests(self._listing_urls)

	def parse_sitemap(self, response):
		"""Parse a post sitemap and extract article URLs published within hours window."""
		try:
			root = ET.fromstring(response.text)
		except Exception as e:
			self.logger.warning("Sitemap parse failed: %s", e)
			return

		ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
		new_urls = []
		for url_el in root.findall(".//sm:url", ns) or root.findall(".//url"):
			loc_el = url_el.find("sm:loc", ns) or url_el.find("loc")
			lastmod_el = url_el.find("sm:lastmod", ns) or url_el.find("lastmod")
			if loc_el is None:
				continue
			url = (loc_el.text or "").strip()
			if not url or url in self._seen_links:
				continue
			# Post sitemaps are reliable — skip _looks_like_article filter
			parsed = urlparse(url)
			if not any(h in parsed.netloc.lower() for h in ("a16z.com", "a16zcrypto.com")):
				continue

			# Check lastmod date if available
			if lastmod_el is not None and lastmod_el.text:
				dt = self._parse_dt(lastmod_el.text.strip())
				if dt and dt < self.cutoff:
					continue

			self._seen_links.add(url)
			new_urls.append(url)

		self.logger.info("Sitemap added %d URLs (total listing+sitemap=%d)", len(new_urls), len(self._listing_urls) + len(new_urls))
		yield from self._yield_article_requests(self._listing_urls + new_urls)
		self._listing_urls = []  # prevent duplicate yields if multiple sitemaps

	def _yield_article_requests(self, urls):
		for url in urls:
			self._api_total_posts += 1
			yield scrapy.Request(
				url=url,
				callback=self.parse_article,
				headers={"User-Agent": USER_AGENT},
				dont_filter=True,
			)
			self._queued_article_requests += 1

	def parse_article(self, response):
		soup = BeautifulSoup(response.text, "html.parser") if BeautifulSoup else None

		# --- Title ---
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
			# Strip "| a16z" suffix
			if title:
				title = re.sub(r'\s*[|\-]\s*a16z\s*$', '', title, flags=re.IGNORECASE).strip()

		# --- Date ---
		date_iso = self._extract_date_iso(soup)
		dt = self._parse_dt(date_iso)

		# Time filter
		if dt and dt < self.cutoff:
			self._time_filtered_posts += 1
			self.logger.debug("Skipping old article: %s (%s)", response.url, date_iso)
			return

		pub_date = dt.strftime("%Y-%m-%d") if dt else ""

		# --- Author ---
		author = self._extract_author(soup)

		# --- Content ---
		content = self._extract_content(soup, response.text)

		self.logger.info(
			"a16z stats: total=%s filtered_by_time=%s queued=%s hours=%s",
			self._api_total_posts,
			self._time_filtered_posts,
			self._queued_article_requests,
			self.hours,
		)

		row = {
			"标题(中文)": "",
			"标题(英文)": title,
			"链接": response.url,
			"网站": "a16z",
			"发布日期": pub_date,
			"作者": author,
			"标签": "",
			"内容摘要(中文)": "",
			"内容摘要(英文)": "",
			"AI摘要(中文)": "",
			"AI摘要(英文)": "",
			"完整内容(英文)": content,
		}

		print("=== a16z ===")
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
		# dateutil fallback
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
		# 1) time/meta tags
		for sel in [
			"time[datetime]",
			"meta[property='article:published_time']",
			"meta[name='pubdate']",
			"meta[name='parsely-pub-date']",
			"meta[name='date']",
		]:
			el = soup.select_one(sel)
			if el:
				raw = el.get("datetime") or el.get("content")
				if raw:
					return raw.strip()
		# 2) JSON-LD
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
		# 1) JSON-LD author
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
				tp = obj.get("@type") or obj.get("type")
				tps = [str(t).lower() for t in (tp if isinstance(tp, list) else [tp]) if t]
				if any(t in ("article", "newsarticle", "blogposting") for t in tps):
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
		# 2) rel=author link
		a = soup.find("a", rel="author")
		if a:
			return a.get_text(" ", strip=True)
		# 3) meta author
		m = soup.find("meta", attrs={"name": "author"})
		if m and m.get("content"):
			return m["content"].strip()
		return ""

	def _extract_content(self, soup, html_text: str) -> str:
		if not soup:
			if not html_text:
				return ""
			return " ".join(scrapy.Selector(text=html_text).css("p::text").getall()).strip()

		# Select main container
		main = (
			soup.select_one("article")
			or soup.select_one("div.post-content, div.article-content, div.single-post, div.entry-content, div.rich-text, div.prose")
			or soup
		)

		# Remove noise tags
		for tag in main.find_all(["script", "style", "nav", "footer", "aside", "header", "iframe", "noscript", "figure", "picture", "svg", "button"]):
			tag.decompose()

		nav_tokens = {
			"news & content", "portfolio", "team", "about", "jobs", "connect",
			"ai", "american dynamism", "bio + health", "consumer", "crypto",
			"enterprise", "fintech", "infrastructure", "seed",
		}
		cut_markers = [
			"views expressed in",
			"should not be construed as",
			"investment advice",
			"disclaimer",
			"discussion about this post",
			"ready for more",
			"commentsrestacks",
			"more comments",
			"read full story",
			"see all",
		]
		banner_cut = [
			"want more a16z",
			"stay up to date",
			"sign up for our",
			"subscribe to our newsletter",
			"sign up for news",
			"subscribe to a16z",
			"discover more from a16z",
			"already have an account",
		]
		skip_exact = {
			"subscribe",
			"sign in",
			"home",
			"comments",
			"restacks",
			"share",
		}

		parts = []
		seen = set()
		started = False
		for el in main.find_all(["p", "h2", "h3", "h4", "li"]):
			if el.find(["img", "svg", "picture", "figure", "button"]):
				continue
			text = el.get_text(" ", strip=True)
			if not text or text in seen:
				continue
			low = text.lower()
			if low in skip_exact:
				continue
			if text.count("|") >= 2:
				continue
			if low.startswith("[image") or "image: image" in low:
				continue
			if not started and low in nav_tokens:
				continue
			if not started and len(text) < 5 and not el.find("a"):
				continue
			if not started and el.name not in {"p", "h2", "h3", "h4"}:
				continue
			if not started and (len(text) > 60 or "." in text):
				started = True
			if not started:
				continue
			if any(m in low for m in cut_markers):
				if parts:
					break
				continue
			if any(b in low for b in banner_cut):
				if parts:
					break
				continue
			if "view " in low and " profile" in low:
				continue
			if "likes" in low and "restacks" in low:
				if parts:
					break
				continue
			if "/comment/" in low:
				if parts:
					break
				continue
			if el.name == "li" and ("working paper" in low or low.startswith("http")):
				continue
			if any(k in low for k in ["sign up", "newsletter"]) and len(low) < 160:
				if parts:
					break
				continue
			if " read more" in low:
				continue
			seen.add(text)
			parts.append(text)

		content_text = " ".join(parts).strip()
		if not content_text:
			# Fallback: relax heading/cut heuristics and keep substantive paragraphs only.
			fallback_parts = []
			for p in main.find_all("p"):
				if p.find(["img", "svg", "picture", "figure", "button"]):
					continue
				text = p.get_text(" ", strip=True)
				if not text:
					continue
				low = text.lower()
				if len(text) < 40:
					continue
				if any(m in low for m in cut_markers + banner_cut):
					continue
				if any(k in low for k in ["subscribe", "discussion about this post", "read full story", "comments"]):
					continue
				fallback_parts.append(text)
				if len(" ".join(fallback_parts)) >= 2000:
					break
			content_text = " ".join(fallback_parts).strip()

		if len(content_text) > 2000:
			content_text = content_text[:2000] + "..."
		return content_text
