# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


import os
# useful for handling different item types with a single interface
import re
import sys
import time
from datetime import datetime, timedelta, timezone
import pymongo
import pymysql
from pyquery import PyQuery as pq
import json

from scrapy.exceptions import DropItem
from news_tagging import (METADATA_COLUMN, classify_news_for_storage, ensure_metadata_column, report_row)

from news.items import CommentItem
from news.utils.timefinder import TimeFinder
from urllib.parse import urlparse


# g_dic = json.load(codecs.open('/data/smart-reputation/yu-spider/news/news/utils', 'r', 'utf-8'))


# def loaddict():
#     if not g_dic:
#         g_dic = json.load(codecs.open('./utils/res.txt', 'r', 'utf-8'))
#         return g_dic
from news.items import SearchInfo


def _get_crawlab_mongo_helpers():
    """
    Load Crawlab helpers lazily to avoid hard crash on incompatible SDK/runtime.
    """
    try:
        from crawlab.scrapy.pipelines import save_item as _save_item_mongo
        from crawlab.scrapy.pipelines import get_task_id as _get_task_id
        return _get_task_id, _save_item_mongo
    except Exception as e:
        return None, None


class DefaultValuesPipeline(object):
    @classmethod
    def from_crawler(cls, crawler):
        cls.mongo_uri = crawler.settings.get('MONGO_URI', 'mongodb://localhost:27017')
        cls.mongo_db = crawler.settings.get('MONGO_DB_NAME', 'scrapy_data')
        # 读取mysql配置
        mysql_dict = crawler.settings.get('MYSQL_DICT', {})
        mysql_uri = crawler.settings.get('MYSQL_URI')
        mysql_dict['host'] = mysql_uri
        cls.mysql_config = mysql_dict
        return cls()

    def get_sites(self):
        maps={}
        # print(self.mysql_config)
        self.mysqlconnection = pymysql.connect(**self.mysql_config)
        cursor = self.mysqlconnection.cursor(pymysql.cursors.DictCursor)
        try:
            cursor.execute("select site_name,main_url from smart_ma_site")
            for item in cursor.fetchall():
                url = item['main_url'].replace('http://', "").replace("https://", "").replace("/", "")
                maps[url] = item['site_name']
            return maps
        except pymysql.Error as e:
            print(e)
        finally:
            cursor.close()
        return {}


    def get_search_companys(self):
        for item in self.db['ma_listed_companys'].find():
            yield SearchInfo(item)

    def get_use_proxy(self):
        return ['huxiu','baiduzixun']

    def open_spider(self, spider):
        self.client = pymongo.MongoClient(self.mongo_uri)
        self.db = self.client[self.mongo_db]

        # spider.search_key = self.get_search_key()
        spider.use_proxy_lst = self.get_use_proxy()
        spider.searchInfo = self.get_search_companys()
        spider.sitesInfo = self.get_sites()

    """
    设置 item 的默认值
    """

    def process_item(self, item, spider):
        for field in item.fields:
            item.setdefault(field, None)
        return item


class NewsPipeline:
    def process_item(self, item, spider):
        return item


class HandlePubDatePipeline:
    def process_item(self, item, spider):
        timefinder = TimeFinder(base_date='2020-04-23 00:00:00')
        if type(item) == CommentItem:
            return item
        if item['pub_date']:
            res = timefinder.find_time(item['pub_date'])
            if res and len(res) > 0:
                try:

                    datetime.strptime(res[0][:10], '%Y-%m-%d')
                    now = datetime.now()
                    item['pub_date'] = res[0][:10] + " %s:%s:%s" % (str(now.hour).zfill(2), str(now.minute).zfill(2), str(now.second).zfill(2))
                except Exception as e:
                    item['pub_date'] = None
        return item


class HandleCleanTextPipeline:
    def process_item(self, item, spider):
        if 'content' in item and item['content'] is not None:
            doc = pq(item['content'])
            item['text'] = doc.text()
            try:
                dic = json.loads(item['text'])
                # 如果可以解析那么,一个文档只返回json,就丢弃掉.
                raise DropItem("文档只保存json: %s" % item['text'])
            except:
                pass
        if len(item['text']) < 30:
            raise DropItem("文档差长度<30:%s" % item['text'])

        return item


class HandleCleanTitlePipline:
    def process_item(self, item, spider):
        if 'title' in item and item['title']:
            doc = pq(item['title'])
            item['title'] = doc.text()
        return item



class HandleStatusPipeline:
    def process_item(self, item, spider):
        item['status'] = 0
        return item


class MongoPipeline(object):
    # def __init__(self, mongo_uri, mongo_db):
    #     self.mongo_uri = mongo_uri
    #     self.mongo_db = mongo_db
    #     self.client = None
    #     self.db = None

    @classmethod
    def from_crawler(cls, crawler):
        cls.mongo_uri = crawler.settings.get('MONGO_URI', 'mongodb://localhost:27017')
        cls.mongo_db = crawler.settings.get('MONGO_DB_NAME', 'scrapy_data')
        return cls()
        # return cls(mongo_uri=crawler.settings.get('MONGO_URI'),
        #            mongo_db=crawler.settings.get('MONGO_DB')
        #            )

    def open_spider(self, spider):
        self.client = pymongo.MongoClient(self.mongo_uri)
        self.db = self.client[self.mongo_db]

    def get_collection(self, name):
        col = self.db[name]
        try:
            col.ensure_index([("url", pymongo.ASCENDING), ("unique", True), ("dropDups", True)])
        except Exception as e:
            pass
        # self.logger.info(u'Ensuring index for key {0}'.format(
        #         self.config['unique_key']))
        return col

    def process_item(self, item, spider):
        dic = dict(item)
        dic['update_time'] = round(time.time())
        # dic['spider_name'] = spider.spider_name
        res = urlparse(dic['url'])

        dic['spider_name'] = spider.sitesInfo.get(res.netloc,spider.spider_name)

        # if '301 Moved Permanently' or 'You need to enable JavaScript to' in dic["content"]:
        #     sys.stdout.write(dic["content"])
        #     sys.stdout.write("301--=======---mongo ===="*20)
        #     return item
        if 'You need to enable Javascript to' in dic['content']:
            return item
        # try:
        #     dic = json.loads(dic["content"])

        # except:
        #     pass
        # self.db[spider.name].insert()
        # db.getCollection('user').update({"name":"user3"}, {"$set":{"age":21,'update_date':'33'}, "$setOnInsert":{"sex":"female","index":5,'create_date':'222'}}, {"upsert":"true"})
        # dic['update_date'] = int(round(time.time()))
        # dic['create_date'] = int(round(time.time()))

        get_col, save_item_mongo = _get_crawlab_mongo_helpers()
        col = get_col() if get_col else self.get_collection(spider.name)
        if col.find_one({'title': dic['title']}):
            return item

        # crawlab 上启动的爬虫
        if os.environ.get('CRAWLAB_TASK_ID') and os.environ.get('CRAWLAB_COLLECTION') and get_col and save_item_mongo:
            dedup_field = os.environ.get('CRAWLAB_DEDUP_FIELD')
            if not dedup_field:
                dedup_field = 'url'
            col = get_col()
            if not col.find_one({dedup_field: dic.get(dedup_field)}):
                dic['create_time'] = round(time.time())
                save_item_mongo(dic)
        else:
            col = self.get_collection(spider.name)
            col.update({'url': dic['url']},
                       {'$set': dic, '$setOnInsert': {'create_time': round(time.time())}}, upsert=True)

        return item

    def close_spider(self, spider):
        self.client.close()


class TechCrunchMySQLPipeline(object):
    """
    Save techcrunch_api results into ai_news.
    """

    SUPPORTED_SPIDERS = {
        "techcrunch_api",
        "techcrunch_venture",
        "techstartups",
        "a16z",
        "bvp",
        "hongshan",
        "thomabravo",
        "crunchbase",
        "eastmoney",
        "panews",
        "rfidworld",
        "bihainews",
        "futunn",
        "tonghuashun",
        "pedaily",
        "sohu_news",
        "sina_news",
        "sina_finance",
        "traveldaily",
        "guandian",
        "laoyaoba",
    }

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings.get("AI_NEWS_MYSQL", {}))

    def __init__(self, mysql_cfg):
        self.mysql_cfg = mysql_cfg or {}
        self.conn = None
        self.cursor = None
        self._warned_no_conn = False
        self._supports_content_full = False
        self._summarize_text_en = None
        self._summarize_text_zh = None
        self._summarize_news_bilingual = None
        self._translate_text_azure = None
        self._warned_ai_enrich = False
        table_name = self.mysql_cfg.get("table", "ai_news")
        if not re.match(r"^[A-Za-z0-9_]+$", str(table_name)):
            table_name = "ai_news"
        self.table_name = table_name

    def open_spider(self, spider):
        if spider.name not in self.SUPPORTED_SPIDERS:
            return
        self._init_ai_enrichment(spider)
        mysql_cfg = dict(self.mysql_cfg)
        try:
            self.conn = pymysql.connect(
                host=mysql_cfg.get("host"),
                port=int(mysql_cfg.get("port", 3306)),
                user=mysql_cfg.get("user"),
                password=mysql_cfg.get("password"),
                database=mysql_cfg.get("db"),
                charset=mysql_cfg.get("charset", "utf8mb4"),
                autocommit=True,
                connect_timeout=10,
                read_timeout=30,
                write_timeout=30,
            )
            ensure_metadata_column(self.conn, self.table_name)
            self.cursor = self.conn.cursor(pymysql.cursors.DictCursor)
            self.cursor.execute(f"SHOW COLUMNS FROM `{self.table_name}` LIKE 'content_full'")
            self._supports_content_full = bool(self.cursor.fetchone())
            spider.logger.info(
                "TechCrunchMySQLPipeline connected: host=%s port=%s db=%s table=%s content_full=%s",
                mysql_cfg.get("host"),
                mysql_cfg.get("port", 3306),
                mysql_cfg.get("db"),
                self.table_name,
                self._supports_content_full,
            )
        except Exception as e:
            self.conn = None
            self.cursor = None
            spider.logger.error("TechCrunchMySQLPipeline connect failed: %s", e)
            spider.logger.error(
                "Set MYSQL_HOST/PORT/USER/PASSWORD/DB in task env; spider will continue without DB write."
            )

    def close_spider(self, spider):
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()

    def _safe_str(self, value):
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _mojibake_score(value):
        """Score common UTF-8-as-Latin-1/CP1252 corruption markers."""
        text = str(value or "")
        marker_count = len(re.findall(r"[ÂÃâåæçèéï�]", text))
        c1_count = len(re.findall(r"[\x80-\x9f]", text))
        suspicious_pairs = len(re.findall(
            r"(?:Ã.|[âåæçèé][\x80-\xff]|[ÂÃâåæçèé]{2,})",
            text,
        ))
        return marker_count + c1_count * 2 + suspicious_pairs * 2

    @classmethod
    def _repair_mojibake(cls, value):
        """Repair only when a Latin-1/CP1252 round trip clearly improves text."""
        text = str(value or "").strip()
        original_score = cls._mojibake_score(text)
        if not text or original_score < 2:
            return text

        best = text
        best_score = original_score
        best_cjk = len(re.findall(r"[\u3400-\u9fff]", text))
        for encoding in ("latin1", "cp1252"):
            try:
                candidate = text.encode(encoding).decode("utf-8").strip()
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            candidate_score = cls._mojibake_score(candidate)
            candidate_cjk = len(re.findall(r"[\u3400-\u9fff]", candidate))
            if candidate_score < best_score and (
                candidate_score == 0 or candidate_cjk > best_cjk
            ):
                best = candidate
                best_score = candidate_score
                best_cjk = candidate_cjk
        return best

    def _translate_checked(self, text, target_lang, spider):
        if not text or not self._translate_text_azure:
            return ""
        for use_cache in (True, False):
            try:
                try:
                    translated = self._translate_text_azure(
                        text, target_lang=target_lang, use_cache=use_cache
                    )
                except TypeError:
                    translated = self._translate_text_azure(text, target_lang=target_lang)
                translated = self._repair_mojibake(self._safe_str(translated))
                if self._is_usable_translation(translated, target_lang):
                    return translated
            except Exception as exc:
                if not self._warned_ai_enrich:
                    spider.logger.warning("translation to %s failed: %s", target_lang, exc)
                    self._warned_ai_enrich = True
                break
        spider.logger.warning("translation to %s returned mojibake or wrong language", target_lang)
        return ""

    def _parse_publish_date(self, value):
        text = self._safe_str(value)
        if not text:
            return None
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    def _init_ai_enrichment(self, spider):
        """Load shared, grounded bilingual summarization and existing translation/tagging."""
        try:
            from azure_ai import (
                summarize_text_en,
                summarize_text_zh,
                summarize_news_bilingual,
                translate_text_azure,
            )

            self._summarize_text_en = summarize_text_en
            self._summarize_text_zh = summarize_text_zh
            self._summarize_news_bilingual = summarize_news_bilingual
            self._translate_text_azure = translate_text_azure
            spider.logger.info("AI enrichment module loaded: azure_ai")
        except Exception as e:
            self._summarize_text_en = None
            self._summarize_text_zh = None
            self._summarize_news_bilingual = None
            self._translate_text_azure = None
            spider.logger.warning("AI enrichment unavailable, using fallback summaries only: %s", e)

    def _fallback_summary_en(self, content_full, max_chars=300):
        text = self._safe_str(content_full)
        if not text:
            return ""

        normalized = re.sub(r"\s+", " ", text).strip()
        if len(normalized) <= max_chars:
            return normalized

        sentences = re.split(r"(?<=[.!?])\s+", normalized)
        sentences = [s.strip() for s in sentences if s and len(s.strip()) >= 30]
        if not sentences:
            return normalized[:max_chars].rsplit(" ", 1)[0].strip() or normalized[:max_chars]

        stop_words = {
            "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those",
            "is", "are", "was", "were", "be", "been", "being", "to", "of", "in", "on", "for", "with",
            "as", "at", "by", "from", "into", "over", "after", "before", "about", "it", "its", "their",
            "they", "them", "we", "our", "you", "your", "he", "she", "his", "her", "will", "would", "can",
            "could", "should", "may", "might", "do", "does", "did", "have", "has", "had", "not"
        }

        word_counts = {}
        for word in re.findall(r"[A-Za-z][A-Za-z\-']{2,}", normalized.lower()):
            if word in stop_words:
                continue
            word_counts[word] = word_counts.get(word, 0) + 1

        scored = []
        for idx, sentence in enumerate(sentences):
            words = re.findall(r"[A-Za-z][A-Za-z\-']{2,}", sentence.lower())
            if not words:
                continue
            score = sum(word_counts.get(word, 0) for word in words if word not in stop_words)
            if idx == 0:
                score += 3
            if any(token in sentence for token in (":", "%", "$")):
                score += 1
            scored.append((score, idx, sentence))

        if not scored:
            return normalized[:max_chars].rsplit(" ", 1)[0].strip() or normalized[:max_chars]

        picked = []
        total_len = 0
        for _, idx, sentence in sorted(scored, key=lambda item: (-item[0], item[1])):
            projected = total_len + (1 if picked else 0) + len(sentence)
            if projected > max_chars and picked:
                continue
            picked.append((idx, sentence))
            total_len = projected
            if total_len >= max_chars * 0.7 or len(picked) >= 3:
                break

        if not picked:
            first = sentences[0]
            return first[:max_chars].rsplit(" ", 1)[0].strip() or first[:max_chars]

        summary = " ".join(sentence for _, sentence in sorted(picked, key=lambda item: item[0]))
        return summary

    def _enrich_summaries(self, row, spider):
        content_full = self._safe_str(row.get("content_full"))
        shared_attempted = bool(content_full and self._summarize_news_bilingual)
        if shared_attempted:
            # Replace incoming AI fields only with this request's model result.
            # Source quality and editorial decisions come from the model prompt.
            row["ai_summary_zh"] = row["ai_summary_en"] = ""
            try:
                result = self._summarize_news_bilingual(content_full, max_length=300)
                if result.get("status") == "ok" and all(result.get(lang) for lang in ("zh", "en")):
                    row["ai_summary_zh"] = result["zh"]
                    row["ai_summary_en"] = result["en"]
                    if result.get("source_quality") == "incomplete":
                        spider.logger.warning("正文资料不完整，摘要仅依据可验证事实 url=%s reason=%s", row.get("url"), result.get("reason"))
                elif result.get("status") == "insufficient":
                    row["ai_summary_zh"] = row["ai_summary_en"] = ""
                    spider.logger.warning("正文不足，未生成AI摘要 url=%s reason=%s", row.get("url"), result.get("reason"))
                else:
                    spider.logger.warning("双语摘要未生成，未将正文摘录标为AI摘要 url=%s status=%s", row.get("url"), result.get("status"))
            except Exception as exc:
                spider.logger.warning("双语摘要失败 url=%s error=%s", row.get("url"), type(exc).__name__)

        # Only fill missing content-summary fields; retain source-provided descriptions.
        for language in ("zh", "en"):
            if not row.get(f"content_summary_{language}") and row.get(f"ai_summary_{language}"):
                row[f"content_summary_{language}"] = row[f"ai_summary_{language}"]

        if not row.get("content_summary_en"):
            summary_en = ""
            if row.get("content_summary_zh") and self._translate_text_azure:
                summary_en = self._translate_checked(row["content_summary_zh"], "en", spider)
            if not summary_en and content_full and self._summarize_text_en and not shared_attempted:
                try:
                    summary_en = self._safe_str(self._summarize_text_en(content_full, max_length=200))
                except Exception as e:
                    if not self._warned_ai_enrich:
                        spider.logger.warning("summarize_text_en failed, fallback to local summary extraction: %s", e)
                        self._warned_ai_enrich = True
            if not summary_en and content_full:
                summary_en = self._fallback_summary_en(content_full, max_chars=300)
            row["content_summary_en"] = summary_en

        if not row.get("content_summary_zh") and row.get("content_summary_en") and self._translate_text_azure:
            row["content_summary_zh"] = self._translate_checked(
                row["content_summary_en"], "zh", spider
            )

        if not row.get("title_zh") and row.get("title_en") and self._translate_text_azure:
            row["title_zh"] = self._translate_checked(row["title_en"], "zh", spider)[:500]

        if not row.get("title_en") and row.get("title_zh") and self._translate_text_azure:
            row["title_en"] = self._translate_checked(row["title_zh"], "en", spider)[:500]

        # AI fields must contain actual model output. Never relabel source meta
        # descriptions or locally extracted content summaries as AI summaries.
        if not shared_attempted and not row.get("ai_summary_zh") and content_full and self._summarize_text_zh:
            try:
                row["ai_summary_zh"] = self._safe_str(
                    self._summarize_text_zh(content_full, max_length=300)
                )
            except Exception as exc:
                if not self._warned_ai_enrich:
                    spider.logger.warning("summarize_text_zh failed: %s", exc)
                    self._warned_ai_enrich = True

        if not shared_attempted and not row.get("ai_summary_en") and row.get("ai_summary_zh") and self._translate_text_azure:
            row["ai_summary_en"] = self._translate_checked(
                row["ai_summary_zh"], "en", spider
            )

    @staticmethod
    def _is_usable_translation(value, target_lang):
        text = str(value or "").strip()
        if not text:
            return False
        if TechCrunchMySQLPipeline._mojibake_score(text) >= 2:
            return False
        chinese_count = len(re.findall(r"[\u3400-\u9fff]", text))
        latin_count = len(re.findall(r"[A-Za-z]", text))
        if target_lang == "en":
            return latin_count >= max(4, chinese_count * 3)
        return chinese_count > 0

    def _classify_tags(self, row, spider):
        # Always re-evaluate source-provided tags. No keyword fallback, snippet
        # truncation or three-tag limit may determine report eligibility.
        tags, metadata = classify_news_for_storage(report_row(row))
        row[METADATA_COLUMN] = json.dumps(metadata, ensure_ascii=False)
        if metadata["status"] != "ok":
            spider.logger.warning("新闻分类待审核，已保留原文: %s (%s)", row.get("url"), metadata.get("reason"))
        return tags

    def _sync_item_from_row(self, item, row):
        item["标题(中文)"] = row.get("title_zh", "")
        item["标题(英文)"] = row.get("title_en", "")
        item["链接"] = row.get("url", "")
        item["网站"] = row.get("website", "")
        item["发布日期"] = row.get("publish_date").strftime("%Y-%m-%d") if row.get("publish_date") else ""
        item["作者"] = row.get("author", "")
        item["标签"] = row.get("tags", "")
        item["内容摘要(中文)"] = row.get("content_summary_zh", "")
        item["内容摘要(英文)"] = row.get("content_summary_en", "")
        item["AI摘要(中文)"] = row.get("ai_summary_zh", "")
        item["AI摘要(英文)"] = row.get("ai_summary_en", "")
        if row.get("content_full"):
            item["完整内容(英文)"] = row.get("content_full", "")
        item["match_type"] = row.get("match_type", "")

    def _prepare_row(self, data, spider):
        url = self._safe_str(data.get("链接"))
        tags = data.get("标签", "")
        if isinstance(tags, (list, tuple, set)):
            tags = ",".join([self._safe_str(x) for x in tags if self._safe_str(x)])
        elif isinstance(tags, dict):
            tags = json.dumps(tags, ensure_ascii=False)
        else:
            tags = self._safe_str(tags)

        row = {
            "title_zh": self._safe_str(data.get("标题(中文)"))[:500],
            "title_en": self._safe_str(data.get("标题(英文)"))[:500],
            "url": url[:767],
            "website": self._safe_str(data.get("网站"))[:100],
            "publish_date": self._parse_publish_date(data.get("发布日期")),
            "author": self._safe_str(data.get("作者"))[:200],
            "tags": tags,
            "content_summary_zh": self._safe_str(data.get("内容摘要(中文)")),
            "content_summary_en": self._safe_str(data.get("内容摘要(英文)")),
            "ai_summary_zh": self._safe_str(data.get("AI摘要(中文)")),
            "ai_summary_en": self._safe_str(data.get("AI摘要(英文)")),
            "content_full": self._safe_str(data.get("完整内容(英文)")),
            "match_type": self._safe_str(data.get("match_type"))[:100],
        }

        # Keep the article, but never persist a forecast/event date as a future
        # publication date or replace it with the crawl date.
        today = datetime.now(timezone(timedelta(hours=8))).date()
        if row["publish_date"] and row["publish_date"] > today:
            spider.logger.warning(
                "Future publication date needs review; storing NULL: date=%s url=%s",
                row["publish_date"], row["url"],
            )
            row["publish_date"] = None

        for field in (
            "title_zh", "title_en", "author", "tags", "content_summary_zh",
            "content_summary_en", "ai_summary_zh", "ai_summary_en", "content_full",
        ):
            row[field] = self._repair_mojibake(row[field])

        # A corrupted Chinese fallback can look Latin-heavy before repair. Do not
        # preserve it as an English translation; let enrichment translate again.
        if row["title_en"] and row["title_zh"] and not self._is_usable_translation(row["title_en"], "en"):
            row["title_en"] = ""
        if (row["content_summary_en"] and row["content_summary_zh"]
                and not self._is_usable_translation(row["content_summary_en"], "en")):
            row["content_summary_en"] = ""
        if (row["ai_summary_en"] and row["ai_summary_zh"]
                and not self._is_usable_translation(row["ai_summary_en"], "en")):
            row["ai_summary_en"] = ""

        self._enrich_summaries(row, spider)
        row["tags"] = self._classify_tags(row, spider)

        if not row["match_type"]:
            first_tag = ""
            if row["tags"]:
                first_tag = re.split(r"[,，;；]", row["tags"])[0].strip()
            row["match_type"] = (first_tag or spider.name)[:100]
        return row

    def process_item(self, item, spider):
        if spider.name not in self.SUPPORTED_SPIDERS:
            return item
        data = dict(item)
        url = self._safe_str(data.get("链接"))
        if not url:
            raise DropItem("missing url")

        row = self._prepare_row(data, spider)
        # Drop items with no title — a missing title means the article page failed
        # to parse correctly and the row would appear blank in the UI.
        has_title = bool(row.get("title_en") or row.get("title_zh"))
        if not has_title:
            spider.logger.debug("Skipping item with no title: %s", row.get("url"))
            raise DropItem("empty item: no title")
        self._sync_item_from_row(item, row)
        self._save_to_mysql(row, spider)
        return item

    def _save_to_mysql(self, row, spider):
        if not self.conn or not self.cursor:
            if not self._warned_no_conn:
                spider.logger.warning("MySQL unavailable; skip DB insert/update for this run.")
                self._warned_no_conn = True
            return

        if not self._safe_str(row.get("url")):
            return

        self.conn.ping(reconnect=True)
        self.cursor.execute(
            f"SELECT id FROM `{self.table_name}` WHERE url=%s LIMIT 1",
            (row["url"],),
        )
        exists = self.cursor.fetchone()

        if exists:
            if self._supports_content_full:
                self.cursor.execute(
                    f"""
                    UPDATE `{self.table_name}`
                    SET title_zh=%s, title_en=%s, website=%s, publish_date=%s, author=%s,
                        tags=%s, email_report_metadata=%s, content_summary_zh=%s, content_summary_en=%s,
                        ai_summary_zh=%s, ai_summary_en=%s, content_full=%s, match_type=%s,
                        crawl_time=NOW(), updated_at=NOW()
                    WHERE id=%s
                    """,
                    (
                        row["title_zh"], row["title_en"], row["website"], row["publish_date"], row["author"],
                        row["tags"], row[METADATA_COLUMN], row["content_summary_zh"], row["content_summary_en"],
                        row["ai_summary_zh"], row["ai_summary_en"], row["content_full"], row["match_type"],
                        exists["id"],
                    ),
                )
            else:
                self.cursor.execute(
                    f"""
                    UPDATE `{self.table_name}`
                    SET title_zh=%s, title_en=%s, website=%s, publish_date=%s, author=%s,
                        tags=%s, email_report_metadata=%s, content_summary_zh=%s, content_summary_en=%s,
                        ai_summary_zh=%s, ai_summary_en=%s, match_type=%s,
                        crawl_time=NOW(), updated_at=NOW()
                    WHERE id=%s
                    """,
                    (
                        row["title_zh"], row["title_en"], row["website"], row["publish_date"], row["author"],
                        row["tags"], row[METADATA_COLUMN], row["content_summary_zh"], row["content_summary_en"],
                        row["ai_summary_zh"], row["ai_summary_en"], row["match_type"],
                        exists["id"],
                    ),
                )
        else:
            if self._supports_content_full:
                self.cursor.execute(
                    f"""
                    INSERT INTO `{self.table_name}` (
                        title_zh, title_en, url, website, publish_date, author, tags, email_report_metadata,
                        content_summary_zh, content_summary_en, ai_summary_zh, ai_summary_en,
                        content_full, match_type, crawl_time, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW()
                    )
                    """,
                    (
                        row["title_zh"], row["title_en"], row["url"], row["website"], row["publish_date"], row["author"],
                        row["tags"], row[METADATA_COLUMN], row["content_summary_zh"], row["content_summary_en"],
                        row["ai_summary_zh"], row["ai_summary_en"], row["content_full"], row["match_type"],
                    ),
                )
            else:
                self.cursor.execute(
                    f"""
                    INSERT INTO `{self.table_name}` (
                        title_zh, title_en, url, website, publish_date, author, tags, email_report_metadata,
                        content_summary_zh, content_summary_en, ai_summary_zh, ai_summary_en,
                        match_type, crawl_time, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, NOW(), NOW(), NOW()
                    )
                    """,
                    (
                        row["title_zh"], row["title_en"], row["url"], row["website"], row["publish_date"], row["author"],
                        row["tags"], row[METADATA_COLUMN], row["content_summary_zh"], row["content_summary_en"],
                        row["ai_summary_zh"], row["ai_summary_en"], row["match_type"],
                    ),
                )
