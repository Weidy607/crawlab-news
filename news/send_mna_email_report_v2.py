#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone M&A weekly/monthly brief with database-owned news summaries."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pymysql

from news.settings import AI_NEWS_MYSQL


USER_AGENT = "MnaReportV2/1.0"
STAGES = {"已完成交割", "签约待交割", "拟议/要约", "传闻/洽谈"}
SECTOR_LABELS = {
    "AI_SOFTWARE_CLOUD": "人工智能、软件与云服务",
    "SEMICONDUCTORS_INFRA": "半导体与数字基础设施",
    "HEALTHCARE": "医疗健康",
    "INDUSTRIALS": "工业与制造",
    "FINANCIAL_SERVICES": "金融服务",
    "CONSUMER_INTERNET": "消费与互联网",
    "ENERGY_OTHER": "能源及其他",
}
IT_SECTORS = {"AI_SOFTWARE_CLOUD", "SEMICONDUCTORS_INFRA"}
REGIONS = ("中国", "北美", "欧洲", "亚太（不含中国）", "中东与非洲", "拉丁美洲", "跨区域", "未披露")
CHINA_RELATIONS = ("中国境内交易", "中国企业海外并购", "外资收购中国标的", "非中国交易")
AMOUNT_BASES = {"TOTAL_CONSIDERATION", "EQUITY_VALUE", "ENTERPRISE_VALUE"}
KNOWN_ENTITY_ALIASES = (
    {"nvidia", "英伟达", "辉达"},
)


@dataclass
class News:
    news_id: Any
    title: str
    title_zh: str
    source: str
    url: str
    published_at: datetime
    ai_summary_zh: str
    tags: str = ""
    related_sources: List[Tuple[str, str]] = field(default_factory=list)
    content_full: str = ""


@dataclass
class Deal:
    news_id: Any
    title: str
    acquirer: str
    target: str
    amount_display: str
    amount_usd_millions: Optional[float]
    stage: str
    industry: str
    ai_summary_zh: str
    source: str
    url: str
    published_at: date
    importance: int = 1
    sources: List[Tuple[str, str]] = field(default_factory=list)
    deal_evidence: str = ""
    amount_evidence: str = ""
    region: str = "未披露"
    china_relation: str = "非中国交易"
    transaction_date: Optional[date] = None
    amount_currency: str = "USD"
    amount_basis: str = "TOTAL_CONSIDERATION"
    transaction_facts: str = ""
    deal_analysis: str = ""
    narrative_evidence: List[str] = field(default_factory=list)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成全球 IT 并购周报或月报 v2")
    parser.add_argument("--period", choices=("week", "month"), default="week", help="报告周期，默认 week")
    parser.add_argument("--as-of", help="统计截止日 YYYY-MM-DD，默认当前日期")
    parser.add_argument("--start-date", help="自定义统计开始日 YYYY-MM-DD；与 --as-of 配合使用")
    parser.add_argument("--days", type=int, help=argparse.SUPPRESS)  # 兼容旧任务；报告周期以 --period 为准
    parser.add_argument("--max-items", type=int, default=1000)
    parser.add_argument("--candidate-limit", type=int, default=1000)
    parser.add_argument("--db-host", default=os.getenv("MYSQL_HOST", ""))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("MYSQL_PORT", "0")))
    parser.add_argument("--db-user", default=os.getenv("MYSQL_USER", ""))
    parser.add_argument("--db-password", default=os.getenv("MYSQL_PASSWORD", ""))
    parser.add_argument("--db-name", default=os.getenv("MYSQL_DB", ""))
    parser.add_argument("--db-table", default=os.getenv("MYSQL_TABLE", ""))
    parser.add_argument("--analysis-table", default=os.getenv("MNA_ANALYSIS_TABLE", "mna_news_analysis"))
    parser.add_argument("--report-runs-table", default=os.getenv("MNA_REPORT_RUNS_TABLE", "mna_report_runs"))
    parser.add_argument("--refresh-analysis", action="store_true", help="忽略数据库分析缓存并重新调用AI")
    parser.add_argument("--input-json", help="使用 JSON 回放，不读取数据库")
    parser.add_argument("--output", help="HTML 输出路径；默认按报告周期自动命名")
    parser.add_argument("--dry-run", action="store_true", help="只生成 preview，不发送邮件")
    parser.add_argument("--send", action="store_true", help="明确允许发送邮件")
    parser.add_argument("--email-to", default=None)
    parser.add_argument(
        "--email-language", choices=("zh", "en"), default="zh",
        help="--email-to 临时收件人的报告语言，默认 zh",
    )
    parser.add_argument("--smtp-server", default=os.getenv("SMTP_SERVER", "smtp.163.com"))
    parser.add_argument("--smtp-port", type=int, default=int(os.getenv("SMTP_PORT", "465")))
    parser.add_argument("--smtp-user", default=os.getenv("SMTP_USER", ""))
    parser.add_argument("--smtp-pass", default=os.getenv("SMTP_PASS", ""))
    return parser.parse_args(argv)


def select_display_summary(row: Dict[str, Any]) -> str:
    """Return the database-owned Chinese AI summary without fallback."""
    return str(row.get("ai_summary_zh") or "").strip()


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo(os.getenv("MNA_REPORT_TIMEZONE", "Asia/Shanghai")))
    return result.astimezone(timezone.utc)


def report_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("MNA_REPORT_TIMEZONE", "Asia/Shanghai"))


def calculate_period_dates(
    as_of: date, period: str, start_date: Optional[str] = None
) -> Tuple[date, date]:
    """Return an inclusive reporting range ending on ``as_of``."""
    if start_date:
        custom_start = date.fromisoformat(start_date)
        if custom_start > as_of:
            raise ValueError("--start-date 不能晚于 --as-of")
        return custom_start, as_of
    if period == "month":
        return as_of.replace(day=1), as_of
    if period == "week":
        return as_of - timedelta(days=as_of.weekday()), as_of
    raise ValueError(f"不支持的报告周期: {period}")


def period_copy(period: str) -> Dict[str, str]:
    if period == "month":
        return {
            "label": "月报",
            "management": "管理层月度简报",
            "largest": "本月最大交易",
            "footer": "M&A MONTHLY",
            "file": "mna_monthly_report_v2.html",
        }
    return {
        "label": "周报",
        "management": "管理层周度简报",
        "largest": "本周最大交易",
        "footer": "M&A WEEKLY",
        "file": "mna_weekly_report_v2.html",
    }


def to_news(row: Dict[str, Any]) -> Optional[News]:
    title_zh = str(row.get("title_zh") or "").strip()
    title_en = str(row.get("title_en") or "").strip()
    url = str(row.get("url") or "").strip()
    summary = select_display_summary(row)
    if not (title_zh or title_en) or not url or not row.get("publish_date"):
        return None
    return News(
        news_id=row.get("id"), title=title_zh or title_en, title_zh=title_zh,
        source=str(row.get("website") or "").strip(), url=url,
        published_at=parse_datetime(row["publish_date"]), ai_summary_zh=summary,
        tags=str(row.get("tags") or "").strip(),
        content_full=str(row.get("content_full") or "").strip(),
    )


def resolve_db_config(args: argparse.Namespace) -> Dict[str, Any]:
    config = dict(AI_NEWS_MYSQL or {})
    host = args.db_host or config.get("host", "localhost")
    if host == "host.docker.internal":
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            host = "localhost"
    return {
        "host": host, "port": args.db_port or int(config.get("port", 3306)),
        "user": args.db_user or config.get("user", ""), "password": args.db_password or config.get("password", ""),
        "db": args.db_name or config.get("db", ""), "table": args.db_table or config.get("table", "ai_news"),
        "charset": config.get("charset", "utf8mb4"),
    }


def load_mna_news(args: argparse.Namespace, start_at: datetime, end_at: datetime) -> List[News]:
    if args.input_json:
        rows = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
        return deduplicate_news([news for row in rows if (news := to_news(row))])
    config = resolve_db_config(args)
    if not re.fullmatch(r"[A-Za-z0-9_]+", str(config["table"])):
        raise ValueError("数据库表名不合法")
    connection = pymysql.connect(
        host=config["host"], port=config["port"], user=config["user"], password=config["password"],
        database=config["db"], charset=config["charset"], autocommit=True,
        connect_timeout=10, read_timeout=30, cursorclass=pymysql.cursors.DictCursor,
    )
    terms = ("并购", "收购", "合并", "M&A", "acquisition", "merger")
    where = " OR ".join(["tags LIKE %s"] * len(terms))
    local_zone = report_timezone()
    params = (start_at.astimezone(local_zone).replace(tzinfo=None), end_at.astimezone(local_zone).replace(tzinfo=None), *[f"%{term}%" for term in terms], args.candidate_limit)
    query = f"""
        SELECT id, title_en, title_zh, url, website, publish_date, tags,
               ai_summary_zh, content_full
        FROM `{config['table']}`
        WHERE publish_date >= %s AND publish_date < %s AND ({where})
        ORDER BY publish_date DESC LIMIT %s
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall() or []
    finally:
        connection.close()
    news = [item for row in rows if (item := to_news(row))]
    print(f"[v2] database_rows={len(rows)} usable_news={len(news)}")
    return deduplicate_news(news)


def normalized_words(text: str) -> set[str]:
    aliases = {"acquired": "acquire", "acquires": "acquire", "acquisition": "acquire", "merges": "merge"}
    return {aliases.get(word, word) for word in re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower())}


def similar_news(left: News, right: News) -> bool:
    a, b = normalized_words(left.title), normalized_words(right.title)
    return bool(a and b and len(a & b) / len(a | b) >= 0.68)


def deduplicate_news(news: Iterable[News]) -> List[News]:
    result: List[News] = []
    for item in sorted(news, key=lambda row: row.published_at, reverse=True):
        match = next((saved for saved in result if similar_news(item, saved)), None)
        if match is None:
            result.append(item)
            continue
        reference = (item.source or urlparse(item.url).netloc, item.url)
        if reference not in match.related_sources and reference != (match.source, match.url):
            match.related_sources.append(reference)
    return result


def parse_ai_deals(content: str) -> List[Dict[str, Any]]:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI 返回内容中没有 JSON 对象")
    parsed = json.loads(content[start:end + 1])
    deals = parsed.get("deals")
    if not isinstance(deals, list):
        raise ValueError("AI 返回 JSON 缺少 deals 数组")
    if not all(isinstance(item, dict) for item in deals):
        raise ValueError("AI 返回的 deals 包含非对象元素")
    return deals


def call_ai_for_classification(news: Sequence[News], include_excluded: bool = False) -> List[Dict[str, Any]]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
    if not key or not base_url:
        raise RuntimeError("未配置 OPENAI_API_KEY / OPENAI_BASE_URL")
    try:
        batch_size = int(os.getenv("MNA_REPORT_AI_BATCH_SIZE", "6"))
    except ValueError as exc:
        raise ValueError("MNA_REPORT_AI_BATCH_SIZE 必须是整数") from exc
    if batch_size <= 0:
        raise ValueError("MNA_REPORT_AI_BATCH_SIZE 必须大于 0")

    output_format = {
        "deals": [{
            "id": 0, "acquirer": "", "target": "", "stage": "已完成交割|签约待交割|拟议/要约|传闻/洽谈",
            "industry": "AI_SOFTWARE_CLOUD|SEMICONDUCTORS_INFRA|HEALTHCARE|INDUSTRIALS|FINANCIAL_SERVICES|CONSUMER_INTERNET|ENERGY_OTHER",
            "importance": 1, "deal_evidence": "原文中同时证明收购方、标的和并购动作的完整原句",
            "amount_evidence": "原文中的交易对价原文；未披露则为空字符串",
            "amount_display": "标准化后的中文金额短语；例如约6.5亿加元或约60亿美元",
            "amount_currency": "USD|CAD|CNY|EUR|GBP|HKD|JPY|其他ISO币种|UNKNOWN",
            "amount_value_millions": "按原币种折算为百万单位的数值；不得换算币种；未披露则为null",
            "amount_is_approximate": "原文含约、roughly、about、approximately等约数表述时为true，否则为false",
            "amount_basis": "TOTAL_CONSIDERATION|EQUITY_VALUE|ENTERPRISE_VALUE|PER_SHARE_ONLY|VALUATION_ONLY|UNDISCLOSED",
            "transaction_date": "YYYY-MM-DD；原文未明确则为空字符串",
            "transaction_date_evidence": "原文中支持交易日期的完整原句；未明确则为空字符串",
            "region": "中国|北美|欧洲|亚太（不含中国）|中东与非洲|拉丁美洲|跨区域|未披露",
            "region_evidence": "支持标的所属地区判断的原文；无法核验则为空字符串",
            "region_confidence": "high|medium|low",
            "china_relation": "中国境内交易|中国企业海外并购|外资收购中国标的|非中国交易",
            "china_evidence": "支持中国交易分类的原文；非中国或无法核验则为空字符串",
            "china_confidence": "high|medium|low",
            "transaction_facts": "基于正文撰写的120–220字中文交易事实",
            "deal_analysis": "基于正文撰写的180–320字中文交易解读",
            "narrative_evidence": ["逐字复制正文中支持事实、财务数据和分析判断的关键原句"],
        }],
        "excluded": [{"id": 1, "reason": "非IT行业|非控制权交易|交易双方不明确|原文证据不足|其他明确原因"}],
    }
    return request_ai_classification_batches(
        news, include_excluded, key, base_url, batch_size, output_format,
    )


def validate_table_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", str(value or "")):
        raise ValueError(f"数据库表名不合法: {value!r}")
    return str(value)


def open_db_connection(args: argparse.Namespace):
    config = resolve_db_config(args)
    return pymysql.connect(
        host=config["host"], port=config["port"], user=config["user"], password=config["password"],
        database=config["db"], charset=config["charset"], autocommit=True,
        connect_timeout=10, read_timeout=30, cursorclass=pymysql.cursors.DictCursor,
    )


def analysis_rule_version() -> str:
    return os.getenv("MNA_ANALYSIS_RULE_VERSION", "mna-body-v6-20260907").strip() or "mna-body-v6-20260907"


def news_content_hash(item: News) -> str:
    payload = "\n".join((item.title, item.content_full, item.ai_summary_zh, item.tags))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_analysis_tables(args: argparse.Namespace) -> None:
    analysis_table = validate_table_name(args.analysis_table)
    runs_table = validate_table_name(args.report_runs_table)
    connection = open_db_connection(args)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS `{analysis_table}` (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    news_id BIGINT NOT NULL,
                    content_hash CHAR(64) NOT NULL,
                    rule_version VARCHAR(80) NOT NULL,
                    included TINYINT(1) NOT NULL DEFAULT 0,
                    exclusion_reason VARCHAR(500) NOT NULL DEFAULT '',
                    analysis_json LONGTEXT NOT NULL,
                    model_name VARCHAR(120) NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_mna_news_analysis (news_id, content_hash, rule_version),
                    KEY idx_mna_news_rule (news_id, rule_version)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS `{runs_table}` (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    period_type VARCHAR(16) NOT NULL,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    rule_version VARCHAR(80) NOT NULL,
                    candidate_count INT NOT NULL,
                    included_count INT NOT NULL,
                    international_count INT NOT NULL,
                    china_count INT NOT NULL,
                    cache_hits INT NOT NULL DEFAULT 0,
                    ai_analyzed INT NOT NULL DEFAULT 0,
                    report_hash CHAR(64) NOT NULL,
                    report_html LONGTEXT NULL,
                    output_path VARCHAR(1000) NOT NULL DEFAULT '',
                    validation_status VARCHAR(32) NOT NULL DEFAULT 'passed',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    KEY idx_mna_report_period (start_date, end_date, rule_version)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # CREATE TABLE IF NOT EXISTS does not add columns to an existing
            # installation. Keep historical rows nullable and persist the full
            # immutable HTML snapshot for every new report run.
            cursor.execute(f"SHOW COLUMNS FROM `{runs_table}` LIKE %s", ("report_html",))
            if cursor.fetchone() is None:
                cursor.execute(f"ALTER TABLE `{runs_table}` ADD COLUMN report_html LONGTEXT NULL AFTER report_hash")
    finally:
        connection.close()


def request_ai_classification_batches(
    news: Sequence[News],
    include_excluded: bool,
    key: str,
    base_url: str,
    batch_size: int,
    output_format: Dict[str, Any],
) -> List[Dict[str, Any]]:
    import requests

    all_deals: List[Dict[str, Any]] = []
    total_batches = (len(news) + batch_size - 1) // batch_size
    for batch_number, offset in enumerate(range(0, len(news), batch_size), 1):
        try:
            content_chars = max(2000, int(os.getenv("MNA_REPORT_CONTENT_CHARS", "12000")))
        except ValueError as exc:
            raise ValueError("MNA_REPORT_CONTENT_CHARS 必须是整数") from exc
        candidates = [
            {
                "id": index,
                "title": item.title,
                "body": item.content_full[:content_chars],
                "existing_summary": item.ai_summary_zh,
                "tags": item.tags,
            }
            for index, item in enumerate(news[offset:offset + batch_size], start=offset)
        ]
        prompt = (
            "你是严谨的并购交易核验与研究分析员。请根据候选新闻正文完成交易结构化、事实整理和交易解读。"
            "仅纳入本篇新闻正在报道或更新的控制权收购、合并交易；不得纳入融资、少数股权投资、合作、资产购买意向不明、"
            "仅把历史并购当背景、标题含并购词但正文不能确认交易双方的新闻。"
            "本报告只收录 IT 行业并购：包括人工智能、企业/消费软件、SaaS、云服务、网络安全、数据与开发工具、"
            "半导体、芯片设计与制造设备、数据中心、服务器、通信网络和数字基础设施。"
            "医疗健康、工业制造、传统金融、消费零售、房地产、能源资源、传统媒体等非 IT 行业一律不要返回；"
            "仅因企业使用软件或 AI，不足以将其归入 IT 行业，必须以交易标的的主营业务为准。"
            "acquirer 和 target 必须逐字取自标题或摘要，不得推断、翻译、补全或混淆双方。"
            "deal_evidence 必须逐字复制标题或摘要中的一个完整原句，且该句必须同时明确收购方、标的和收购/合并动作；"
            "无法提供这种原句就不要返回该新闻。"
            "金额必须依靠语义判断，不得把每股价格、普通公司估值、融资额、营收或其他交易金额当成本次交易总金额。"
            "本次交易明确披露的总对价、完全摊薄股权价值或企业价值均可作为交易金额；amount_basis 必须准确区分其口径。"
            "amount_evidence 必须逐字复制包含该金额及其口径的原文；amount_display 必须将金额统一写成简洁中文，"
            "例如 roughly $6 billion 应写为约60亿美元，不得保留 roughly、about、approximately、billion、million 等英文金额表达。"
            "amount_value_millions 只换算到原币种的百万单位，不做汇率换算；只有每股价格或普通估值时分别标记 PER_SHARE_ONLY 或 VALUATION_ONLY，"
            "并将 amount_display、amount_evidence 留空、amount_value_millions 设为 null。未披露则标记 UNDISCLOSED。"
            "transaction_date 只填写原文明示的签约、宣布或交割日期，并逐字提供 transaction_date_evidence；无法核验则均返回空字符串。"
            "transaction_facts 和 deal_analysis 必须依据候选中的 body 撰写中文内容。transaction_facts 只写交易时间线、双方、"
            "交易范围、对价、持股比例、支付方式、审批和交割状态，去除次要背景和重复表述，控制在120至220字。"
            "deal_analysis 只写正文能够支持的标的业务、核心收购逻辑、主要协同关系和行业意义，控制在180至320字，"
            "避免重复交易事实、逐项罗列产品或堆砌财务数据。不得使用正文之外的知识，不得虚构数据。"
            "报告正文必须直接陈述事实和分析，不得出现‘从正文可见’、‘正文可见’、‘正文指出’、‘正文还指出’、‘根据正文’、"
            "‘新闻显示’、‘报道显示’等描述信息来源或分析过程的套话，也不要向读者解释某个判断来自正文。"
            "narrative_evidence 必须逐字复制 body 中支撑关键事实、财务数字和分析判断的原句，最多8条；没有正文依据的内容必须省略。"
            "如果 body 为空，可以用 existing_summary 判断是否为并购及提取基础字段，但 transaction_facts、deal_analysis 和 narrative_evidence 必须留空。"
            "每个候选 id 必须且只能出现一次：纳入的放入 deals，不纳入的放入 excluded 并给出明确原因，不得遗漏任何候选。"
            "industry 必须按交易标的主营业务归入给定七类之一。region 按交易标的总部或主要经营地分类。"
            "中国交易是全球交易的子集，不得从结果中剔除。只有原文能证明双方主要经营主体所在地时才可标记为中国境内交易、"
            "中国企业海外并购或外资收购中国标的，并逐字提供 china_evidence。若原文未写所在地，仅可对总部所在地广为人知、"
            "无歧义的企业使用稳定公开知识补全，并将对应 confidence 设为 high、evidence 留空；任何不确定情况必须将 region 填未披露，"
            "china_relation 填非中国交易，confidence 填 low。不得根据新闻语言、姓名或模糊印象猜测国别。"
            "只返回一个合法 JSON 对象；不要 Markdown 代码块、overview、summary、注释或尾随逗号。\n"
            f"候选：{json.dumps(candidates, ensure_ascii=False)}\n"
            f"格式示例：{json.dumps(output_format, ensure_ascii=False)}"
        )
        payload = {
            "model": os.getenv("OPENAI_MODEL", "gpt-5.3-codex"),
            "messages": [
                {"role": "system", "content": "你只依据提供的新闻正文进行并购核验和分析，并输出严格合法的 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "max_completion_tokens": int(os.getenv("MNA_REPORT_AI_MAX_TOKENS", "12000")),
        }
        print(f"[v2] ai_batch={batch_number}/{total_batches} candidates={len(candidates)}")
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=(10, 90),
            )
            response.raise_for_status()
            content = str(response.json()["choices"][0]["message"]["content"] or "")
            try:
                batch_deals = parse_ai_deals(content)
                parsed = json.loads(content[content.find("{"):content.rfind("}") + 1])
                excluded = parsed.get("excluded") or []
                if include_excluded:
                    if not isinstance(excluded, list) or not all(isinstance(item, dict) for item in excluded):
                        raise ValueError("AI 返回 JSON 的 excluded 不是对象数组")
                    deal_ids = [int(item.get("id", -1)) for item in batch_deals]
                    excluded_ids = [int(item.get("id", -1)) for item in excluded]
                    expected_ids = list(range(offset, min(offset + batch_size, len(news))))
                    if sorted(deal_ids + excluded_ids) != expected_ids or len(set(deal_ids + excluded_ids)) != len(expected_ids):
                        raise ValueError("AI 未对本批每个候选给出唯一的纳入或排除结论")
                    all_deals.extend([{**item, "_included": True} for item in batch_deals])
                    all_deals.extend([
                        {"id": int(item["id"]), "_included": False, "exclusion_reason": str(item.get("reason") or "AI明确排除")}
                        for item in excluded
                    ])
                else:
                    all_deals.extend(batch_deals)
                last_error = None
                break
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                last_error = exc
                print(f"[v2] ai_batch={batch_number} invalid_json attempt={attempt}/3: {exc}")
        if last_error is not None:
            raise RuntimeError(f"AI 第 {batch_number}/{total_batches} 批连续 3 次返回无效 JSON") from last_error
    return all_deals


def load_analysis_cache(args: argparse.Namespace, news: Sequence[News]) -> Dict[int, Dict[str, Any]]:
    if args.refresh_analysis or not news:
        return {}
    table = validate_table_name(args.analysis_table)
    version = analysis_rule_version()
    expected = {
        int(item.news_id): news_content_hash(item)
        for item in news
        if str(item.news_id).lstrip("-").isdigit()
    }
    if not expected:
        return {}
    cached: Dict[int, Dict[str, Any]] = {}
    connection = open_db_connection(args)
    try:
        ids = list(expected)
        with connection.cursor() as cursor:
            for offset in range(0, len(ids), 500):
                subset = ids[offset:offset + 500]
                placeholders = ",".join(["%s"] * len(subset))
                cursor.execute(
                    f"SELECT news_id, content_hash, included, exclusion_reason, analysis_json "
                    f"FROM `{table}` WHERE rule_version=%s AND news_id IN ({placeholders})",
                    (version, *subset),
                )
                for row in cursor.fetchall() or []:
                    news_id = int(row["news_id"])
                    if row.get("content_hash") != expected.get(news_id):
                        continue
                    try:
                        analysis = json.loads(str(row.get("analysis_json") or "{}"))
                    except json.JSONDecodeError:
                        continue
                    cached[news_id] = {
                        "included": bool(row.get("included")),
                        "exclusion_reason": str(row.get("exclusion_reason") or ""),
                        "analysis": analysis if isinstance(analysis, dict) else {},
                    }
    finally:
        connection.close()
    return cached


def save_analysis_cache(
    args: argparse.Namespace,
    entries: Sequence[Tuple[News, bool, str, Dict[str, Any]]],
) -> None:
    if not entries:
        return
    table = validate_table_name(args.analysis_table)
    version = analysis_rule_version()
    model = os.getenv("OPENAI_MODEL", "gpt-5.3-codex")
    values = []
    for item, included, reason, analysis in entries:
        try:
            news_id = int(item.news_id)
        except (TypeError, ValueError):
            continue
        values.append((
            news_id, news_content_hash(item), version, int(included), reason,
            json.dumps(analysis, ensure_ascii=False, separators=(",", ":")), model,
        ))
    if not values:
        return
    connection = open_db_connection(args)
    try:
        with connection.cursor() as cursor:
            cursor.executemany(
                f"""
                INSERT INTO `{table}`
                    (news_id, content_hash, rule_version, included, exclusion_reason, analysis_json, model_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    included=VALUES(included), exclusion_reason=VALUES(exclusion_reason),
                    analysis_json=VALUES(analysis_json), model_name=VALUES(model_name), updated_at=CURRENT_TIMESTAMP
                """,
                values,
            )
    finally:
        connection.close()


def classify_with_analysis_cache(
    args: argparse.Namespace, news: Sequence[News]
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    cached = load_analysis_cache(args, news)
    rows: List[Dict[str, Any]] = []
    missing_news: List[News] = []
    missing_global_indexes: List[int] = []
    cache_hits = 0
    for index, item in enumerate(news):
        try:
            news_id = int(item.news_id)
        except (TypeError, ValueError):
            news_id = -1
        saved = cached.get(news_id)
        if saved is None:
            missing_news.append(item)
            missing_global_indexes.append(index)
            continue
        cache_hits += 1
        if saved["included"]:
            analysis = dict(saved["analysis"])
            analysis["id"] = index
            rows.append(analysis)

    fresh_rows = call_ai_for_classification(missing_news, include_excluded=True) if missing_news else []
    fresh_by_local_index: Dict[int, Dict[str, Any]] = {}
    for row in fresh_rows:
        try:
            local_index = int(row.get("id", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= local_index < len(missing_news):
            fresh_by_local_index[local_index] = row

    cache_entries: List[Tuple[News, bool, str, Dict[str, Any]]] = []
    for local_index, item in enumerate(missing_news):
        fresh = fresh_by_local_index.get(local_index)
        if fresh is None:
            raise RuntimeError(f"AI未返回候选索引 {local_index} 的完整结论")
        if not bool(fresh.get("_included", True)):
            cache_entries.append((item, False, str(fresh.get("exclusion_reason") or "AI明确排除"), {}))
            continue
        stored = dict(fresh)
        stored.pop("id", None)
        stored.pop("_included", None)
        cache_entries.append((item, True, "", stored))
        current = dict(stored)
        current["id"] = missing_global_indexes[local_index]
        rows.append(current)
    save_analysis_cache(args, cache_entries)
    rows.sort(key=lambda row: int(row.get("id", -1)))
    return rows, {"cache_hits": cache_hits, "ai_analyzed": len(missing_news)}


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def exact_source_excerpt(excerpt: str, source_text: str) -> bool:
    return bool(excerpt and compact_text(excerpt) in compact_text(source_text))


def format_amount_display(currency: str, value_millions: float, approximate: bool) -> str:
    currency_labels = {
        "USD": "美元", "CAD": "加元", "CNY": "元人民币", "EUR": "欧元",
        "GBP": "英镑", "HKD": "港元", "JPY": "日元",
    }
    label = currency_labels.get(currency)
    if not label:
        return ""
    if value_millions >= 100:
        number = value_millions / 100
        unit = "亿"
    else:
        number = value_millions * 100
        unit = "万"
    number_text = f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{'约' if approximate else ''}{number_text}{unit}{label}"


def normalize_ai_amount(row: Dict[str, Any], source_text: str, news_id: Any) -> Tuple[str, Optional[float], str, str, str]:
    """Accept an AI-understood deal amount only when its source excerpt is verifiable."""
    evidence = str(row.get("amount_evidence") or "").strip()
    display = str(row.get("amount_display") or "").strip()
    currency = str(row.get("amount_currency") or "UNKNOWN").strip().upper()
    basis = str(row.get("amount_basis") or "UNDISCLOSED").strip().upper()
    raw_value = row.get("amount_value_millions")

    if basis not in AMOUNT_BASES:
        return "未披露", None, "", "UNKNOWN", basis or "UNDISCLOSED"
    if not evidence or not exact_source_excerpt(evidence, source_text):
        print(f"[clean] id={news_id}: AI金额证据不是标题/摘要原文，按未披露处理")
        return "未披露", None, "", "UNKNOWN", "UNDISCLOSED"
    try:
        value_millions = float(raw_value)
    except (TypeError, ValueError):
        print(f"[clean] id={news_id}: AI金额数值无效，按未披露处理")
        return "未披露", None, "", "UNKNOWN", "UNDISCLOSED"
    if value_millions <= 0:
        print(f"[clean] id={news_id}: AI金额数值必须大于0，按未披露处理")
        return "未披露", None, "", "UNKNOWN", "UNDISCLOSED"

    raw_approximate = row.get("amount_is_approximate")
    if isinstance(raw_approximate, bool):
        approximate = raw_approximate
    else:
        approximation_text = f"{display} {evidence}".lower()
        approximate = any(marker in approximation_text for marker in ("约", "roughly", "about", "approximately", "approx."))
    localized_display = format_amount_display(currency, value_millions, approximate) or display
    if not localized_display:
        print(f"[clean] id={news_id}: AI展示金额为空，按未披露处理")
        return "未披露", None, "", "UNKNOWN", "UNDISCLOSED"
    usd_millions = value_millions if currency == "USD" else None
    return localized_display, usd_millions, evidence, currency, basis


def normalize_deal(news: News, row: Dict[str, Any]) -> Optional[Deal]:
    acquirer, target = str(row.get("acquirer") or "").strip(), str(row.get("target") or "").strip()
    stage = str(row.get("stage") or "").strip()
    industry = str(row.get("industry") or "ENERGY_OTHER").strip()
    if not acquirer or not target or target in {"未披露", "待核验"} or stage not in STAGES:
        return None
    if industry not in IT_SECTORS:
        print(f"[clean] excluded id={news.news_id}: 非IT行业 ({SECTOR_LABELS.get(industry, industry or '未分类')})")
        return None
    source_text = f"{news.title}\n{news.content_full}\n{news.ai_summary_zh}"
    deal_evidence = str(row.get("deal_evidence") or "").strip()
    if not exact_source_excerpt(deal_evidence, source_text):
        print(f"[clean] excluded id={news.news_id}: 并购证据不是标题/摘要原文")
        return None
    if not exact_source_excerpt(acquirer, source_text) or not exact_source_excerpt(target, source_text):
        print(f"[clean] excluded id={news.news_id}: 收购方或标的无法从标题/摘要核验")
        return None
    amount_display, amount_value, amount_evidence, amount_currency, amount_basis = normalize_ai_amount(
        row, source_text, news.news_id
    )
    region = str(row.get("region") or "未披露").strip()
    region_evidence = str(row.get("region_evidence") or "").strip()
    region_confidence = str(row.get("region_confidence") or "low").strip().lower()
    region_verified = exact_source_excerpt(region_evidence, source_text) or region_confidence == "high"
    if region not in REGIONS or (region != "未披露" and not region_verified):
        region = "未披露"
    china_relation = str(row.get("china_relation") or "非中国交易").strip()
    china_evidence = str(row.get("china_evidence") or "").strip()
    china_confidence = str(row.get("china_confidence") or "low").strip().lower()
    china_verified = exact_source_excerpt(china_evidence, source_text) or china_confidence == "high"
    if china_relation not in CHINA_RELATIONS or (
        china_relation != "非中国交易" and not china_verified
    ):
        china_relation = "非中国交易"
    importance = max(1, min(5, int(row.get("importance", 1))))
    transaction_date: Optional[date] = None
    transaction_date_text = str(row.get("transaction_date") or "").strip()
    transaction_date_evidence = str(row.get("transaction_date_evidence") or "").strip()
    if transaction_date_text and exact_source_excerpt(transaction_date_evidence, source_text):
        try:
            transaction_date = date.fromisoformat(transaction_date_text)
        except ValueError:
            print(f"[clean] id={news.news_id}: 交易日期格式无效，改用报道日期")
    transaction_facts = str(row.get("transaction_facts") or "").strip()
    deal_analysis = str(row.get("deal_analysis") or "").strip()
    raw_narrative_evidence = row.get("narrative_evidence") or []
    if not isinstance(raw_narrative_evidence, list):
        raw_narrative_evidence = []
    requested_evidence = [str(item).strip() for item in raw_narrative_evidence[:8] if str(item).strip()]
    narrative_evidence = [item for item in requested_evidence if exact_source_excerpt(item, news.content_full)]
    if (transaction_facts or deal_analysis) and (
        not news.content_full or not narrative_evidence or len(narrative_evidence) != len(requested_evidence)
    ):
        print(f"[clean] id={news.news_id}: 深度分析证据不完整，回退到数据库摘要")
        transaction_facts, deal_analysis, narrative_evidence = "", "", []
    # The model already receives length guidance for these fields. Do not
    # truncate them again here: that permanently removed verified narrative.
    transaction_facts = transaction_facts.strip()
    deal_analysis = deal_analysis.strip()
    return Deal(news.news_id, news.title, acquirer, target, amount_display, amount_value, stage,
                industry if industry in SECTOR_LABELS else "ENERGY_OTHER", news.ai_summary_zh,
                news.source or urlparse(news.url).netloc, news.url, news.published_at.astimezone(report_timezone()).date(), importance,
                [(news.source or urlparse(news.url).netloc, news.url), *news.related_sources], deal_evidence, amount_evidence,
                region, china_relation, transaction_date, amount_currency, amount_basis,
                transaction_facts, deal_analysis, narrative_evidence)


def entity_aliases(name: str) -> set[str]:
    """Return conservative aliases for party names without fuzzy substring matching."""
    value = str(name or "").casefold()
    value = re.sub(
        r"(?:的)?(?:母公司|母集团|母企|parent\s+company|parent|旗下公司|owned\s+by)",
        "|",
        value,
        flags=re.I,
    )
    value = re.sub(r"[（(\[【{/、,，;；:：·]", "|", value)
    value = re.sub(r"[）)\]】}]", "|", value)
    aliases: set[str] = set()
    generic_suffixes = re.compile(
        r"(?:\b(?:incorporated|corporation|company|holdings?|limited|ltd|corp|co|group|plc|llc|ai)\b|"
        r"股份有限公司|有限责任公司|有限公司|控股集团|控股|集团|公司)$",
        re.I,
    )
    for part in value.split("|"):
        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", part)
        if compact:
            aliases.add(compact)
        stripped = part.strip()
        previous = None
        while stripped and stripped != previous:
            previous = stripped
            stripped = generic_suffixes.sub("", stripped).strip()
        stripped = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", stripped)
        if len(stripped) >= 3:
            aliases.add(stripped)
    for known_group in KNOWN_ENTITY_ALIASES:
        if aliases & known_group:
            aliases.update(known_group)
    return aliases


def same_entity(left: str, right: str) -> bool:
    return bool(entity_aliases(left) & entity_aliases(right))


def same_deal(left: Deal, right: Deal) -> bool:
    if not (same_entity(left.acquirer, right.acquirer) and same_entity(left.target, right.target)):
        return False
    # Announcements often move from rumor to signing/completion over several
    # consecutive days. Treat nearby verified dates as follow-up coverage of
    # the same buyer/target pair, while retaining distant repeat acquisitions.
    if left.transaction_date and right.transaction_date:
        return abs((left.transaction_date - right.transaction_date).days) <= 7
    return True


def merge_duplicate_deal(saved: Deal, duplicate: Deal) -> None:
    for source in duplicate.sources:
        if source not in saved.sources:
            saved.sources.append(source)
    if len(duplicate.target) > len(saved.target) and any(
        marker in duplicate.target.casefold() for marker in ("母公司", "parent", "（", "(")
    ):
        saved.target = duplicate.target
    stage_order = {"传闻/洽谈": 1, "拟议/要约": 2, "签约待交割": 3, "已完成交割": 4}
    duplicate_has_amount = (
        bool(duplicate.amount_display)
        and duplicate.amount_display != "未披露"
        and duplicate.amount_basis in AMOUNT_BASES
    )
    saved_has_amount = (
        bool(saved.amount_display)
        and saved.amount_display != "未披露"
        and saved.amount_basis in AMOUNT_BASES
    )
    if duplicate_has_amount and (
        not saved_has_amount
        or stage_order.get(duplicate.stage, 0) > stage_order.get(saved.stage, 0)
        or (
            stage_order.get(duplicate.stage, 0) == stage_order.get(saved.stage, 0)
            and duplicate.published_at > saved.published_at
        )
    ):
        saved.amount_display = duplicate.amount_display
        saved.amount_usd_millions = duplicate.amount_usd_millions
        saved.amount_evidence = duplicate.amount_evidence
        saved.amount_currency = duplicate.amount_currency
        saved.amount_basis = duplicate.amount_basis
    if stage_order.get(duplicate.stage, 0) > stage_order.get(saved.stage, 0):
        saved.stage = duplicate.stage
    if not re.search(r"[\u4e00-\u9fff]", saved.acquirer) and re.search(r"[\u4e00-\u9fff]", duplicate.acquirer):
        saved.acquirer = duplicate.acquirer
    saved.importance = max(saved.importance, duplicate.importance)
    saved.published_at = max(saved.published_at, duplicate.published_at)
    if saved.transaction_date is None and duplicate.transaction_date is not None:
        saved.transaction_date = duplicate.transaction_date
    if len(duplicate.deal_evidence) > len(saved.deal_evidence):
        saved.deal_evidence = duplicate.deal_evidence
    if len(duplicate.transaction_facts) > len(saved.transaction_facts):
        saved.transaction_facts = duplicate.transaction_facts
    if len(duplicate.deal_analysis) > len(saved.deal_analysis):
        saved.deal_analysis = duplicate.deal_analysis
    for evidence in duplicate.narrative_evidence:
        if evidence not in saved.narrative_evidence:
            saved.narrative_evidence.append(evidence)
    if saved.region == "未披露" and duplicate.region != "未披露":
        saved.region = duplicate.region
    if saved.china_relation == "非中国交易" and duplicate.china_relation != "非中国交易":
        saved.china_relation = duplicate.china_relation


def deduplicate_deals(deals: Iterable[Deal]) -> List[Deal]:
    result: List[Deal] = []
    for deal in deals:
        saved = next(
            (
                item for item in result
                if same_deal(item, deal)
            ),
            None,
        )
        if saved is None:
            result.append(deal)
            continue
        print(f"[dedupe] merged {deal.acquirer} → {deal.target} into {saved.acquirer} → {saved.target}")
        merge_duplicate_deal(saved, deal)
    return result


def rank_deals(deals: Iterable[Deal]) -> List[Deal]:
    return sorted(deduplicate_deals(deals), key=lambda deal: (deal.importance, deal.amount_usd_millions or 0, deal.published_at), reverse=True)


def is_verifiable_transaction_amount(deal: Deal) -> bool:
    """Only source-verified USD deal-value bases participate in totals and rankings."""
    return (
        deal.amount_usd_millions is not None
        and deal.amount_currency == "USD"
        and deal.amount_basis in AMOUNT_BASES
    )


def calculate_kpis(deals: Sequence[Deal]) -> Dict[str, Any]:
    return {
        "total": len(deals), "completed": sum(d.stage == "已完成交割" for d in deals),
        "signed": sum(d.stage == "签约待交割" for d in deals), "proposed": sum(d.stage == "拟议/要约" for d in deals),
        "rumored": sum(d.stage == "传闻/洽谈" for d in deals),
        "amount": sum(d.amount_usd_millions or 0 for d in deals if is_verifiable_transaction_amount(d)),
    }


def validate_report_reconciliation(deals: Sequence[Deal]) -> None:
    """Fail fast when report totals cannot reconcile after cleaning and deduplication."""
    total = len(deals)
    international = [deal for deal in deals if not is_china_deal(deal)]
    china = [deal for deal in deals if is_china_deal(deal)]
    if len(international) + len(china) != total:
        raise ValueError("统计勾稽失败：全球交易数不等于国际交易数与中国相关交易数之和")

    kpis = calculate_kpis(deals)
    stage_total = kpis["completed"] + kpis["signed"] + kpis["proposed"] + kpis["rumored"]
    if stage_total != total:
        raise ValueError(f"统计勾稽失败：阶段分布合计 {stage_total} 笔，全球交易共 {total} 笔")

    sector_total = sum(count for _, count in distribution_counts(deals, "industry", tuple(SECTOR_LABELS)))
    if sector_total != total:
        raise ValueError(f"统计勾稽失败：行业分布合计 {sector_total} 笔，全球交易共 {total} 笔")

    for index, deal in enumerate(deals):
        if any(
            same_deal(deal, other)
            for other in deals[index + 1:]
        ):
            raise ValueError(f"统计勾稽失败：仍存在重复交易 {deal.acquirer} → {deal.target}")

    top_ten = top_disclosed_deals(deals, 10)
    for index, deal in enumerate(top_ten):
        if not is_verifiable_transaction_amount(deal):
            raise ValueError(f"统计勾稽失败：Top 10 包含不可核验金额 {deal.acquirer} → {deal.target}")
        if any(
            same_deal(deal, other)
            for other in top_ten[index + 1:]
        ):
            raise ValueError(f"统计勾稽失败：Top 10 存在重复交易 {deal.acquirer} → {deal.target}")


def format_usd_total(millions: float) -> str:
    if not millions:
        return "—"
    if millions >= 1_000_000:
        return f"${millions / 1_000_000:g}T+"
    if millions >= 1000:
        return f"${millions / 1000:g}B+"
    return f"${millions:g}M+"


def emphasize_metrics(text: str) -> str:
    escaped = html.escape(str(text))
    pattern = r"(\$[\d,.]+(?:[TBM])?\+?|\d+(?:\.\d+)?\s*笔|[^，。；：]{1,40}\s*→\s*[^，。；：]{1,40})"
    return re.sub(pattern, r"<strong style='color:#c05a2b;font-weight:700'>\1</strong>", escaped)


def distribution_counts(deals: Sequence[Deal], attribute: str, order: Sequence[str]) -> List[Tuple[str, int]]:
    counts: Dict[str, int] = {}
    for deal in deals:
        value = str(getattr(deal, attribute))
        counts[value] = counts.get(value, 0) + 1
    return [(name, counts[name]) for name in order if counts.get(name)]


def top_disclosed_deals(deals: Sequence[Deal], limit: int = 10) -> List[Deal]:
    disclosed = [deal for deal in deals if is_verifiable_transaction_amount(deal)]
    return sorted(disclosed, key=lambda deal: deal.amount_usd_millions or 0, reverse=True)[:limit]


def format_mom(current: float, previous: float, unit: str = "") -> str:
    if previous <= 0:
        return f"无法计算（本期 {current:g}{unit}，上期为0）"
    change = (current - previous) / previous
    direction = "上升" if change > 0 else "下降" if change < 0 else "持平"
    return f"{direction} {abs(change):.1%}"


def render_highlights(deals: Sequence[Deal], previous_deals: Optional[Sequence[Deal]] = None) -> str:
    if not deals:
        points = [("交易规模", "本期未发现能够从标题或摘要原文核验的 IT 并购交易。")]
    else:
        kpis = calculate_kpis(deals)
        sectors = distribution_counts(deals, "industry", tuple(SECTOR_LABELS))[:]
        sectors.sort(key=lambda item: (-item[1], item[0]))
        hot = "、".join(f"{SECTOR_LABELS[key]}（{count}笔）" for key, count in sectors[:3]) or "未披露"
        points = [
            ("交易规模", f"本期共核验 {kpis['total']} 笔 IT 并购交易，已披露美元对价合计 {format_usd_total(kpis['amount'])}。"),
            ("交易阶段", f"已完成 {kpis['completed']} 笔，签约待交割 {kpis['signed']} 笔，拟议或洽谈 {kpis['proposed'] + kpis['rumored']} 笔。"),
            ("热点行业", f"{hot}。全球统计包含中国相关交易。"),
        ]
        largest = top_disclosed_deals(deals, 1)
        representative = largest[0] if largest else deals[0]
        points.append((
            "代表交易",
            f"{representative.acquirer} → {representative.target}，交易金额 {representative.amount_display}，阶段为{representative.stage}。",
        ))
        if previous_deals is not None:
            previous_kpis = calculate_kpis(previous_deals)
            points.append((
                "环比变化",
                f"交易数量环比{format_mom(kpis['total'], previous_kpis['total'], '笔')}；"
                f"已披露金额环比{format_mom(kpis['amount'], previous_kpis['amount'], '百万美元')}。",
            ))
    blocks = "".join(
        "<div class='insight-card' style='margin-bottom:12px;padding:16px 18px;border-left:5px solid #4b78a9;background:#eef5fb'>"
        f"<div style='color:#173b67;font-size:15px;font-weight:700;line-height:1.4'>{html.escape(label)}｜</div>"
        f"<div style='margin-top:7px;color:#29343f;font-size:15px;font-weight:400;line-height:1.65'>{emphasize_metrics(point)}</div>"
        "</div>"
        for label, point in points
    )
    return blocks


def render_market_interpretations(deals: Sequence[Deal]) -> Tuple[str, str]:
    if not deals:
        empty = "本期暂无可核验交易，无法形成分布解读。"
        return empty, empty

    known_regions = [(name, count) for name, count in distribution_counts(deals, "region", REGIONS) if name != "未披露"]
    known_regions.sort(key=lambda item: (-item[1], item[0]))
    if known_regions:
        region_leaders = "、".join(f"{name}（{count}笔）" for name, count in known_regions[:2])
        region_text = f"已核验地区的交易主要集中在{region_leaders}。"
    else:
        region_text = "标的地区均未获得足够证据，暂不判断地区集中度。"

    disclosed = top_disclosed_deals(deals, 1)
    total_amount = calculate_kpis(deals)["amount"]
    if disclosed and total_amount:
        largest = disclosed[0]
        share = (largest.amount_usd_millions or 0) / total_amount
        region_text += (
            f"最大披露交易为 {largest.acquirer} → {largest.target}（{largest.amount_display}），"
            f"约占本期已披露金额的 {share:.0%}，对总额影响显著。"
        )

    sector_rows = distribution_counts(deals, "industry", tuple(SECTOR_LABELS))
    sector_rows.sort(key=lambda item: (-item[1], item[0]))
    count_leader, count_value = sector_rows[0]
    sector_amounts = {
        sector: sum((deal.amount_usd_millions or 0) for deal in deals if deal.industry == sector and is_verifiable_transaction_amount(deal))
        for sector, _ in sector_rows
    }
    amount_leader = max(sector_amounts, key=sector_amounts.get) if sector_amounts and max(sector_amounts.values()) > 0 else None
    sector_text = f"交易数量主要集中在{SECTOR_LABELS[count_leader]}（{count_value}笔）。"
    if amount_leader:
        sector_text += f"已披露金额主要由{SECTOR_LABELS[amount_leader]}贡献。"
        if amount_leader != count_leader:
            sector_text += f"{SECTOR_LABELS[count_leader]}交易数量较多，但单笔披露规模相对较小。"
    return region_text, sector_text


def truncate_text(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip("，,；;。 ") + "…"


def clean_narrative_wording(text: str) -> str:
    """Remove source-process boilerplate while preserving the substantive statement."""
    value = str(text or "").strip()
    for phrase in (
        "从新闻正文可见，", "从新闻正文可见：", "从正文可见，", "从正文可见：",
        "新闻正文还指出，", "新闻正文还指出：", "新闻正文指出，", "新闻正文指出：",
        "正文还指出，", "正文还指出：", "正文指出，", "正文指出：",
        "根据新闻正文，", "根据新闻正文：", "根据正文，", "根据正文：",
        "新闻还显示，", "新闻还显示：", "新闻显示，", "新闻显示：",
        "报道还显示，", "报道还显示：", "报道显示，", "报道显示：",
    ):
        value = value.replace(phrase, "")
    return value.strip()


def split_sentences(text: str) -> List[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return [
        re.sub(r"\s+", " ", sentence).strip()
        for sentence in re.split(r"(?<=[。！？!?；;])\s*|\n+", normalized)
        if sentence.strip()
    ]


def build_deal_narrative(deal: Deal) -> Tuple[str, str]:
    """Prefer AI-structured, body-grounded facts/analysis; retain summary fallback."""
    if deal.transaction_facts or deal.deal_analysis:
        return (
            clean_narrative_wording(deal.transaction_facts),
            clean_narrative_wording(deal.deal_analysis),
        )
    source_sentences = split_sentences(deal.ai_summary_zh)
    candidates = [(deal.deal_evidence, False), *((sentence, True) for sentence in source_sentences)]
    unique: List[Tuple[str, bool]] = []
    seen: set[str] = set()
    for sentence, from_summary in candidates:
        key = compact_text(sentence)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append((sentence.strip(), from_summary))

    fact_parts: List[str] = []
    analysis_parts: List[str] = []
    for sentence, from_summary in unique:
        fact_length = len("".join(fact_parts))
        if fact_length < 100:
            # Keep complete sentences; character-boundary splitting used to
            # manufacture an ellipsis and could discard evidence text.
            fact_parts.append(sentence)
            continue
        if from_summary:
            analysis_parts.append(sentence)
    fact = clean_narrative_wording("".join(fact_parts))
    analysis = clean_narrative_wording("".join(analysis_parts))
    return fact or clean_narrative_wording(deal.deal_evidence), analysis


def emphasize_deal_text(text: str, deal: Deal) -> str:
    result = html.escape(str(text or ""))
    values = sorted(
        {deal.acquirer, deal.target, deal.amount_display, deal.amount_evidence} - {"", "未披露"},
        key=len,
        reverse=True,
    )
    for value in values:
        escaped = html.escape(value)
        result = result.replace(escaped, f"<strong style='color:#173b67;font-weight:700'>{escaped}</strong>")
    return result


def split_display_paragraphs(text: str, limit: int = 175) -> List[str]:
    paragraphs: List[str] = []
    current = ""
    for sentence in split_sentences(text):
        if current and len(current) + len(sentence) > limit:
            paragraphs.append(current)
            current = ""
        while len(sentence) > limit:
            paragraphs.append(sentence[:limit])
            sentence = sentence[limit:]
        current += sentence
    if current:
        paragraphs.append(current)
    return paragraphs or [str(text or "")]


def display_deal_date(deal: Deal) -> str:
    return (deal.transaction_date or deal.published_at).isoformat()


def render_deal_item(index: int, deal: Deal, id_prefix: str = "key-deal") -> str:
    title = html.escape(deal.title or f"{deal.acquirer}收购{deal.target}")
    fact, analysis = build_deal_narrative(deal)
    source_links = []
    for source, url in deal.sources[:3]:
        if not url:
            continue
        source_links.append(
            f"<a href='{html.escape(url, quote=True)}' target='_blank' "
            "style='color:#1d5f9f;text-decoration:none;font-weight:700'>"
            f"{html.escape(source or '来源')} ↗</a>"
        )
    sources = "<span style='color:#a2afbd;padding:0 7px'>·</span>".join(source_links) or "—"
    industry = html.escape(SECTOR_LABELS.get(deal.industry, "能源及其他"))
    analysis_text = analysis or "数据库摘要未提供可独立提取的背景或战略解读。"
    fact_html = "".join(
        "<div style='margin-top:8px;color:#29343f;font-size:15px;font-weight:400;line-height:1.65'>"
        + emphasize_deal_text(paragraph, deal)
        + "</div>"
        for paragraph in split_display_paragraphs(fact, 220)
    )
    analysis_html = "".join(
        "<div style='margin-top:8px;color:#29343f;font-size:15px;font-weight:400;line-height:1.65'>"
        + emphasize_deal_text(paragraph, deal)
        + "</div>"
        for paragraph in split_display_paragraphs(analysis_text)
    )
    return (
        "<table class='key-deal-card' role='presentation' width='100%' cellspacing='0' cellpadding='0' "
        f"id='{html.escape(id_prefix, quote=True)}-{index:02d}' style='width:100%;border-collapse:collapse;border-bottom:1px solid #d8e3ee'>"
        "<tr><td valign='top' style='padding:18px 0 24px'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='width:100%;margin-bottom:10px;border-collapse:collapse'>"
        "<tr>"
        f"<td class='key-deal-index' width='38' valign='middle' style='width:38px;padding:0;color:#2a67a2;font-size:13px;font-weight:800;line-height:1.4'>{index:02d}</td>"
        f"<td valign='middle' style='padding:0;color:#315f8d;font-size:13px;font-weight:700;line-height:1.4'>重点交易 · {industry}</td>"
        "</tr></table>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;background:#eef5fb;border:1px solid #d4e3f1'>"
        "<tr>"
        "<td width='34%' valign='middle' style='width:34%;padding:13px 12px'>"
        "<div style='color:#6b7f93;font-size:11px;font-weight:500;letter-spacing:.8px'>收购方</div>"
        f"<div style='margin-top:5px;color:#123b65;font-size:18px;font-weight:700;line-height:1.35'>{html.escape(deal.acquirer)}</div></td>"
        "<td width='7%' align='center' valign='middle' style='width:7%;color:#2f6fa7;font-size:21px;font-weight:700'>→</td>"
        "<td width='34%' valign='middle' style='width:34%;padding:13px 12px'>"
        "<div style='color:#6b7f93;font-size:11px;font-weight:500;letter-spacing:.8px'>被收购方 / 标的</div>"
        f"<div style='margin-top:5px;color:#123b65;font-size:18px;font-weight:700;line-height:1.35'>{html.escape(deal.target)}</div></td>"
        "<td width='25%' align='right' valign='middle' style='width:25%;padding:13px 12px;border-left:1px solid #c8dbea;background:#e4eff8'>"
        "<div style='color:#6b7f93;font-size:11px;font-weight:500;letter-spacing:.8px'>交易金额</div>"
        f"<div style='margin-top:5px;color:#c05a2b;font-size:18px;font-weight:700;line-height:1.3'>{html.escape(deal.amount_display or '未披露')}</div></td>"
        "</tr><tr><td colspan='4' style='padding:9px 12px;border-top:1px solid #d4e3f1;color:#52677d;font-size:11px;font-weight:500'>"
        f"<b style='color:#31516f'>阶段：</b>{html.escape(deal.stage)}"
        f"&nbsp;&nbsp;<span style='color:#8ca0b5'>｜</span>&nbsp;&nbsp;<b style='color:#31516f'>日期：</b>{display_deal_date(deal)}</td></tr></table>"
        f"<div style='margin-top:12px;color:#52677d;font-size:12px;line-height:1.65'><span style='color:#8292a5'>新闻标题：</span>{title}</div>"
        + (
            "<div style='margin-top:18px;color:#173b67;font-size:15px;font-weight:800'>交易事实</div>"
            + "<div class='narrative-content narrative-facts'>" + fact_html + "</div>"
            + "<button class='narrative-toggle' type='button' data-narrative-toggle hidden>展开全文</button>"
            if fact
            else ""
        )
        + "<div style='margin-top:20px;color:#173b67;font-size:15px;font-weight:800'>交易解读</div>"
        + "<div class='narrative-content narrative-analysis'>" + analysis_html + "</div>"
        + "<button class='narrative-toggle' type='button' data-narrative-toggle hidden>展开全文</button>"
        + f"<div style='margin-top:13px;color:#718096;font-size:12px;font-weight:500;line-height:1.6'><span style='margin-right:8px'>来源</span>{sources}</div>"
        + "</td></tr></table>"
    )


def render_header(start_date: date, end_date: date, generated_at: str, period: str) -> str:
    copy = period_copy(period)
    period_heading = f"{end_date:%Y年%m月}" if period == "month" else f"{start_date} — {end_date}"
    period_detail = (
        f"<div style='margin-top:6px;color:#bbcee4;font-size:11px'>{start_date} — {end_date}</div>"
        if period == "month" else ""
    )
    return (
        "<tr><td align='center' style='padding:37px 28px 34px;background:#15355f;color:#fff'>"
        "<div style='color:#bdd3ed;font-size:9px;font-weight:700;letter-spacing:2px'>GLOBAL TECHNOLOGY M&amp;A INTELLIGENCE</div>"
        f"<div style='margin-top:10px;font-size:32px;font-weight:600;letter-spacing:.4px'>全球IT并购{copy['label']}</div>"
        f"<div style='margin-top:6px;color:#d8e6f7;font-size:12px;font-weight:500;letter-spacing:1.2px'>{copy['management']}</div>"
        f"<div style='margin-top:20px;font-size:19px;font-weight:700'>{period_heading}</div>"
        + period_detail
        + f"<div style='margin-top:6px;color:#bbcee4;font-size:11px'>生成于 {html.escape(generated_at)}（GMT+8）</div>"
        "</td></tr>"
    )


def render_kpis(kpis: Dict[str, Any]) -> str:
    items = [
        (kpis["total"], "纳入交易"),
        (kpis["completed"], "已完成"),
        (kpis["signed"], "签约待交割"),
        (kpis["proposed"] + kpis["rumored"], "拟议 / 洽谈"),
        (format_usd_total(kpis["amount"]), "已披露美元金额"),
    ]
    cells = "".join(
        f"<td width='20%' align='center' valign='middle' style='width:20%;height:98px;padding:27px 5px 24px;border-right:{'0' if index == 4 else '1px solid #e4eaf1'}'>"
        f"<div style='color:#15355f;font-size:35px;font-weight:700;line-height:1.05'>{html.escape(str(value))}</div>"
        f"<div style='margin-top:7px;color:#64748b;font-size:11px;font-weight:500;line-height:1.4'>{html.escape(label)}</div></td>"
        for index, (value, label) in enumerate(items)
    )
    return f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='border:1px solid #dbe5f1'><tr>{cells}</tr></table>"


def render_distribution(
    title: str, rows: Sequence[Tuple[str, int]], total: int,
    labels: Optional[Dict[str, str]] = None, show_bars: bool = False,
) -> str:
    body = "".join(
        "<tr>"
        f"<td style='padding:10px 5px;border-bottom:1px solid #edf1f5;color:#40566e;font-size:13px'>{html.escape((labels or {}).get(name, name))}</td>"
        f"<td width='42' align='right' style='width:42px;padding:10px 5px;border-bottom:1px solid #edf1f5;color:#173b67;font-size:13px;font-weight:700'>{count}</td>"
        f"<td width='48' align='right' style='width:48px;padding:10px 0 10px 5px;border-bottom:1px solid #edf1f5;color:#718096;font-size:12px'>{count / total:.0%}</td>"
        "</tr>"
        + (
            "<tr><td colspan='3' style='padding:0 5px 8px'>"
            "<div style='height:5px;background:#edf2f6'>"
            f"<div style='height:5px;width:{max(2, round(count / total * 100))}%;background:#5f8db8'></div>"
            "</div></td></tr>"
            if show_bars else ""
        )
        for name, count in rows
    ) or "<tr><td style='padding:10px;color:#8292a5;font-size:10px'>暂无数据</td></tr>"
    return (
        f"<div style='margin-bottom:8px;color:#173b67;font-size:15px;font-weight:600'>{html.escape(title)}</div>"
        f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='border-collapse:collapse'>{body}</table>"
    )


def render_compact_table(deals: Sequence[Deal], start_index: int = 1) -> str:
    if not deals:
        return "<p style='margin:8px 0;color:#8292a5;font-size:10px'>暂无符合条件的交易。</p>"
    rows = []
    for index, deal in enumerate(deals, start_index):
        rows.append(
            "<tr>"
            f"<td width='30' valign='top' style='width:30px;padding:11px 6px 11px 0;border-bottom:1px solid #e7edf3;color:#2a67a2;font-size:10px;font-weight:800'>{index:02d}</td>"
            "<td valign='top' style='padding:10px 6px;border-bottom:1px solid #e7edf3'>"
            f"<div style='color:#173b67;font-size:14px;font-weight:700;line-height:1.5'><a href='{html.escape(deal.url, quote=True)}' target='_blank' style='color:#173b67;text-decoration:none'>{html.escape(deal.acquirer)} → {html.escape(deal.target)}</a></div>"
            f"<div style='margin-top:5px;color:#718096;font-size:12px;font-weight:500'>{html.escape(SECTOR_LABELS.get(deal.industry, '能源及其他'))} · {html.escape(deal.stage)}</div></td>"
            f"<td width='100' align='right' valign='top' style='width:100px;padding:10px 0 10px 6px;border-bottom:1px solid #e7edf3;color:#c05a2b;font-size:14px;font-weight:700'>{html.escape(deal.amount_display or '未披露')}"
            f"<div style='margin-top:5px;color:#718096;font-size:12px;font-weight:500'>{display_deal_date(deal)}</div></td>"
            "</tr>"
        )
    return f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='border-collapse:collapse'>{''.join(rows)}</table>"


def build_compact_fact(deal: Deal) -> str:
    fact, analysis = build_deal_narrative(deal)
    combined = fact
    if len(combined) < 80 and analysis:
        combined = " ".join(part for part in (combined, analysis) if part)
    # This section is explicitly the complete transaction list.  Keep the
    # selected narrative intact instead of replacing its tail with an ellipsis.
    return combined or deal.deal_evidence or deal.title


def render_compact_deal_card(index: int, deal: Deal) -> str:
    source_links = []
    for source, url in deal.sources[:2]:
        if url:
            source_links.append(
                f"<a href='{html.escape(url, quote=True)}' target='_blank' style='color:#2a67a2;text-decoration:none;font-weight:600'>"
                f"{html.escape(source or '来源')} ↗</a>"
            )
    sources = "<span style='padding:0 6px;color:#a2afbd'>·</span>".join(source_links) or "—"
    return (
        "<div class='compact-deal-card' style='margin-bottom:12px;padding:14px 16px;border:1px solid #dbe5ef;border-left:4px solid #6e9bc6;"
        "background:#fff;page-break-inside:avoid'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse'>"
        "<tr>"
        f"<td class='compact-index' width='38' height='24' valign='middle' style='width:38px;height:24px;padding:0 10px 0 0;color:#2a67a2;font-size:16px;font-weight:800;line-height:24px;mso-line-height-rule:exactly'>{index:02d}</td>"
        f"<td height='24' valign='middle' style='height:24px;padding:0;color:#173b67;font-size:16px;font-weight:700;line-height:24px;mso-line-height-rule:exactly'>{html.escape(deal.acquirer)} → {html.escape(deal.target)}</td>"
        f"<td width='115' height='24' align='right' valign='middle' style='width:115px;height:24px;padding:0 0 0 12px;color:#c05a2b;font-size:17px;font-weight:700;line-height:24px;mso-line-height-rule:exactly'>{html.escape(deal.amount_display or '未披露')}</td>"
        "</tr><tr>"
        "<td width='38' style='width:38px;padding:0'>&nbsp;</td>"
        f"<td colspan='2' style='padding:5px 0 0;color:#52677d;font-size:12px;font-weight:500;line-height:18px'>{html.escape(SECTOR_LABELS.get(deal.industry, 'IT其他'))}"
        + (f"&nbsp;｜&nbsp;{html.escape(deal.china_relation)}" if is_china_deal(deal) else "")
        + f"&nbsp;｜&nbsp;{html.escape(deal.stage)}&nbsp;｜&nbsp;{display_deal_date(deal)}</td>"
        "</tr></table>"
        f"<div style='margin-top:9px;color:#29343f;font-size:14.5px;font-weight:400;line-height:1.65'>{html.escape(build_compact_fact(deal))}</div>"
        f"<div style='margin-top:8px;color:#718096;font-size:12px;font-weight:500'>来源：{sources}</div>"
        "</div>"
    )


def render_grouped_compact_cards(deals: Sequence[Deal], start_index: int = 1) -> str:
    if not deals:
        return "<p style='margin:8px 0;color:#8292a5;font-size:12px'>暂无其他交易。</p>"
    parts: List[str] = []
    display_index = start_index
    for sector, label in SECTOR_LABELS.items():
        subset = [deal for deal in deals if deal.industry == sector]
        if not subset:
            continue
        parts.append(
            f"<div class='industry-heading' style='margin:{'0' if not parts else '30px'} 0 14px;padding:8px 12px;background:#eaf2f9;border-left:4px solid #4b78a9;"
            f"color:#173b67;font-size:16px;font-weight:600;line-height:1.3'>{html.escape(label)}｜{len(subset)}笔</div>"
        )
        for deal in subset:
            parts.append(render_compact_deal_card(display_index, deal))
            display_index += 1
    return "".join(parts)


def render_overview(deals: Sequence[Deal]) -> str:
    kpis = calculate_kpis(deals)
    region_rows = distribution_counts(deals, "region", REGIONS)
    sector_rows = distribution_counts(deals, "industry", tuple(SECTOR_LABELS))
    sector_rows.sort(key=lambda item: (-item[1], item[0]))
    top_ten = top_disclosed_deals(deals, 10)
    unknown_regions = sum(deal.region == "未披露" for deal in deals)
    region_insight, sector_insight = render_market_interpretations(deals)
    region_note = (
        f"<div style='margin-top:9px;color:#718096;font-size:12px;line-height:1.6'>注：仍有 {unknown_regions} 笔交易的标的地区待核验，地区分布仅作辅助参考。</div>"
        if unknown_regions else ""
    )
    stage_items = [
        ("已完成交割", kpis["completed"]),
        ("签约待交割", kpis["signed"]),
        ("拟议 / 要约", kpis["proposed"]),
        ("传闻 / 洽谈", kpis["rumored"]),
    ]
    stage_rows = "".join(
        "<tr>"
        f"<td width='105' style='width:105px;padding:8px 8px 8px 0;color:#40566e;font-size:13px'>{html.escape(label)}</td>"
        "<td style='padding:8px'><div style='height:10px;background:#edf2f6'>"
        f"<div style='height:10px;width:{max(2, round(count / (len(deals) or 1) * 100))}%;background:#4b78a9'></div></div></td>"
        f"<td width='70' align='right' style='width:70px;padding:8px 0;color:#173b67;font-size:13px;font-weight:700'>{count}笔&nbsp; {count / (len(deals) or 1):.0%}</td>"
        "</tr>"
        for label, count in stage_items
    )
    return (
        render_kpis(kpis)
        + "<div style='margin-top:18px;margin-bottom:7px;color:#173b67;font-size:12px;font-weight:800'>交易阶段分布</div>"
        + f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='padding:8px 12px;border:1px solid #e7edf3'>{stage_rows}</table>"
        + "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='margin-top:22px;border-collapse:collapse'><tr>"
        + "<td width='48%' valign='top' style='width:48%;padding-right:14px'>"
        + render_distribution("全球地区分布（按标的）", region_rows, len(deals) or 1)
        + region_note
        + f"<div style='margin-top:11px;padding:10px 11px;background:#f4f8fb;color:#40566e;font-size:12.5px;line-height:1.65'>{html.escape(region_insight)}</div>"
        + "</td><td width='4%' style='width:4%'>&nbsp;</td>"
        + "<td width='48%' valign='top' style='width:48%;padding-left:14px'>"
        + render_distribution("行业分布", sector_rows, len(deals) or 1, SECTOR_LABELS, show_bars=True)
        + f"<div style='margin-top:11px;padding:10px 11px;background:#f4f8fb;color:#40566e;font-size:12.5px;line-height:1.65'>{html.escape(sector_insight)}</div>"
        + "</td></tr></table>"
        + "<div style='margin-top:25px;color:#173b67;font-size:15px;font-weight:600'>交易金额 Top 10</div>"
        + "<div style='margin-top:5px;margin-bottom:10px;color:#718096;font-size:12px;line-height:1.6'>"
          "按公开披露且可核验的美元交易金额排序；未披露金额及仅披露估值的交易不参与排名。</div>"
        + render_compact_table(top_ten)
    )


def render_key_transactions(deals: Sequence[Deal], limit: int = 15, id_prefix: str = "key-deal") -> str:
    selected = list(deals[:limit])
    if not selected:
        return "<p style='margin:0;color:#8292a5;font-size:11px'>暂无重点交易。</p>"
    grouped: Dict[str, List[Deal]] = {key: [] for key in SECTOR_LABELS}
    for deal in selected:
        sector = deal.industry if deal.industry in SECTOR_LABELS else "ENERGY_OTHER"
        grouped[sector].append(deal)
    parts: List[str] = []
    display_index = 1
    for sector, label in SECTOR_LABELS.items():
        sector_deals = grouped.get(sector) or []
        if not sector_deals:
            continue
        parts.append(
            f"<div class='industry-heading' style='margin-top:{'0' if not parts else '34px'};padding:9px 12px;background:#eaf2f9;border-left:5px solid #2f6fa7;color:#173b67;"
            f"font-size:15px;font-weight:700;letter-spacing:.2px'>{html.escape(label)}｜{len(sector_deals)}笔</div>"
        )
        for deal in sector_deals:
            parts.append(render_deal_item(display_index, deal, id_prefix))
            display_index += 1
    return "".join(parts)


def is_china_deal(deal: Deal) -> bool:
    return deal.china_relation != "非中国交易" or deal.region == "中国"


def render_subsection(title: str, content: str, note: str = "", anchor_id: str = "") -> str:
    anchor = f" id='{html.escape(anchor_id, quote=True)}'" if anchor_id else ""
    return (
        f"<div{anchor} style='margin-top:30px;padding:12px 14px;background:#eaf2f9;border-left:5px solid #2f6fa7'>"
        f"<span style='color:#173b67;font-size:16px;font-weight:600'>{html.escape(title)}</span>"
        + (f"<span style='padding-left:9px;color:#64748b;font-size:11px;font-weight:500'>{html.escape(note)}</span>" if note else "")
        + "</div>"
        + f"<div style='padding-top:14px'>{content}</div>"
    )


def render_international_transactions(deals: Sequence[Deal]) -> str:
    key_limit = min(15, len(deals))
    key_deals = deals[:key_limit]
    other_deals = deals[key_limit:]
    return (
        render_subsection(
            f"03A｜重点交易深度解读（{key_limit}笔）",
            render_key_transactions(key_deals, key_limit),
            anchor_id="international-key",
        )
        + render_subsection(
            f"03B｜其他国际交易（{len(other_deals)}笔）",
            render_grouped_compact_cards(other_deals, key_limit + 1),
            "全部按行业列示",
            anchor_id="international-other",
        )
    )


def render_china_topic(deals: Sequence[Deal]) -> str:
    china_deals = [deal for deal in deals if is_china_deal(deal)]
    if not china_deals:
        return (
            "<div style='padding:14px;background:#f7f9fb;color:#64748b;font-size:11px;line-height:1.7'>"
            "本期未发现能够依据标题或摘要原文核验的中国相关交易。</div>"
        )
    amount = calculate_kpis(china_deals)["amount"]
    sectors = distribution_counts(china_deals, "industry", tuple(SECTOR_LABELS))
    sectors.sort(key=lambda item: (-item[1], item[0]))
    main_sectors = "、".join(SECTOR_LABELS[key] for key, _ in sectors[:2]) or "未披露"
    disclosed = top_disclosed_deals(china_deals, 1)
    representative = disclosed[0] if disclosed else china_deals[0]
    overview = [
        (len(china_deals), "中国相关交易"),
        (format_usd_total(amount), "已披露美元金额"),
        (main_sectors, "主要行业"),
        (f"{representative.acquirer} → {representative.target}", "代表交易"),
    ]
    cells = "".join(
        f"<td width='25%' height='116' align='center' valign='middle' style='width:25%;height:116px;padding:16px 12px;border-right:{'0' if index == 3 else '1px solid #dbe5f1'}'>"
        f"<div style='color:#173b67;font-size:{'15px' if index >= 2 else '24px'};font-weight:700;line-height:1.55'>{html.escape(str(value))}</div>"
        f"<div style='margin-top:7px;color:#718096;font-size:12px;font-weight:500;line-height:1.4'>{html.escape(label)}</div></td>"
        for index, (value, label) in enumerate(overview)
    )
    parts = [
        f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='border:1px solid #dbe5f1'><tr>{cells}</tr></table>"
    ]
    relation_counts = {relation: sum(deal.china_relation == relation for deal in china_deals) for relation in CHINA_RELATIONS[:3]}
    features = [
        f"规模：共 {len(china_deals)} 笔中国相关交易，已披露美元对价合计 {format_usd_total(amount)}。",
        "结构："
        + "、".join(f"{relation} {relation_counts[relation]} 笔" for relation in CHINA_RELATIONS[:3])
        + "。",
        f"特点：主要集中于{main_sectors}；代表交易为 {representative.acquirer} → {representative.target}，金额 {representative.amount_display}。",
    ]
    feature_rows = "".join(
        "<tr>"
        f"<td width='26' valign='top' style='width:26px;padding:8px 8px 8px 0;color:#2f6fa7;font-size:12px;font-weight:700'>{index:02d}</td>"
        f"<td valign='top' style='padding:8px 0;color:#52677d;font-size:13.5px;line-height:1.65'>{html.escape(feature)}</td></tr>"
        for index, feature in enumerate(features, 1)
    )
    parts.append(
        "<div style='margin-top:20px;color:#173b67;font-size:15px;font-weight:600'>中国市场特点</div>"
        f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>{feature_rows}</table>"
    )
    key_limit = min(5, len(china_deals))
    parts.append(render_subsection(
        "中国重点交易",
        render_key_transactions(china_deals[:key_limit], key_limit, "china-key-deal"),
        f"{key_limit}笔",
        anchor_id="china-key",
    ))
    other_deals = china_deals[key_limit:]
    parts.append(render_subsection(
        f"其他中国交易完整清单（{len(other_deals)}笔）",
        render_grouped_compact_cards(other_deals, key_limit + 1),
        "全部按行业列示",
        anchor_id="china-other",
    ))
    return "".join(parts)


def render_section(index: str, title: str, content: str) -> str:
    return (
        f"<tr id='section-{html.escape(index)}' class='report-section section-{html.escape(index)}' data-chapter='{html.escape(index + '｜' + title, quote=True)}'><td style='padding:42px 42px 0'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>"
        "<tr>"
        f"<td class='chapter-title' align='left' valign='middle' style='padding:17px 20px;background:#173b67;color:#fff;text-align:left'>"
        f"<span style='font-size:22px;font-weight:700;letter-spacing:.3px'>{html.escape(index)}｜{html.escape(title)}</span></td>"
        f"</tr><tr><td style='padding-top:16px'>{content}</td></tr></table></td></tr>"
    )


def render_china_section(china_deals: Sequence[Deal], global_total: int) -> str:
    amount = calculate_kpis(china_deals)["amount"]
    share = len(china_deals) / global_total if global_total else 0
    title = (
        f"<tr id='section-04' class='report-section china-section' data-chapter='04｜中国相关交易｜共{len(china_deals)}笔'>"
        "<td style='padding:42px 42px 0'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse'>"
        "<tr><td class='chapter-title' style='padding:17px 20px;background:#173b67;color:#fff'>"
        f"<div style='font-size:22px;font-weight:700;line-height:1.35'>04｜中国相关交易｜共{len(china_deals)}笔</div>"
        "</td></tr>"
        "<tr><td style='padding:10px 14px;background:#eaf2f9;color:#52677d'>"
        "<div style='font-size:12px;font-weight:500;line-height:1.5'>包括中国境内交易、中国企业海外并购及外资收购中国标的。</div>"
        f"<div style='margin-top:5px;font-size:13px;font-weight:600;line-height:1.5'>{len(china_deals)}笔"
        f"<span style='padding:0 10px;color:#8fb1d0'>|</span>占全球IT交易 {share:.0%}"
        f"<span style='padding:0 10px;color:#8fb1d0'>|</span>已披露美元金额 {html.escape(format_usd_total(amount))}</div>"
        "</td></tr>"
        "<tr><td style='padding-top:20px'>"
        + render_china_topic(china_deals)
        + "</td></tr></table></td></tr>"
    )
    return title


def render_toc(international_count: int, china_count: int) -> str:
    items = [
        ("01", "本期重点解读", "#section-01", ""),
        ("02", "全球并购概览", "#section-02", ""),
        ("03", "国际交易", "#section-03", f"{international_count}笔"),
        ("03A", "重点交易深度解读", "#international-key", f"{min(15, international_count)}笔"),
        ("03B", "其他国际交易", "#international-other", f"{max(0, international_count - 15)}笔"),
        ("04", "中国相关交易", "#section-04", f"{china_count}笔"),
    ]
    rows = "".join(
        "<tr>"
        f"<td width='52' style='padding:8px 0;border-bottom:1px solid #e7edf3;color:#4b78a9;font-size:12px;font-weight:700'>{number}</td>"
        f"<td style='padding:8px 0;border-bottom:1px solid #e7edf3'><a href='{href}' style='color:#173b67;font-size:14px;font-weight:600;text-decoration:none'>{title}</a></td>"
        f"<td width='70' align='right' style='padding:8px 0;border-bottom:1px solid #e7edf3;color:#718096;font-size:12px'>{count}</td>"
        "</tr>"
        for number, title, href, count in items
    )
    return (
        "<tr><td style='padding:28px 42px 4px'>"
        "<div style='margin-bottom:8px;color:#173b67;font-size:16px;font-weight:700'>目录</div>"
        f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='border-collapse:collapse'>{rows}</table>"
        "</td></tr>"
    )


def render_email_html(
    deals: Sequence[Deal], start_date: date, end_date: date, generated_at: str, period: str = "week",
    previous_deals: Optional[Sequence[Deal]] = None,
) -> str:
    # Keep the rendering boundary strict as well as the ingestion boundary. This
    # prevents callers that construct Deal objects directly from leaking non-IT
    # transactions into global totals or either detail section.
    ordered = rank_deals([deal for deal in deals if deal.industry in IT_SECTORS])
    validate_report_reconciliation(ordered)
    previous_ordered: Optional[List[Deal]] = None
    if period == "month" and previous_deals is not None:
        previous_ordered = rank_deals([deal for deal in previous_deals if deal.industry in IT_SECTORS])
        validate_report_reconciliation(previous_ordered)
    copy = period_copy(period)
    international_deals = [deal for deal in ordered if not is_china_deal(deal)]
    china_deals = [deal for deal in ordered if is_china_deal(deal)]
    methodology = (
        "<tr><td style='padding:30px 42px 0'><div style='padding:15px;background:#f7f9fb;color:#59697a;font-size:11.5px;line-height:1.7'>"
        "<div><b style='color:#40566e'>统计口径：</b>本报告统计期内公开披露的并购、控股权收购及合并交易，包括已完成、签约待交割和拟议/洽谈交易。"
        "同一交易的多篇报道合并计算。交易金额仅汇总具有可核验公开对价的交易。</div>"
        "<div style='margin-top:7px'>仅收录 IT 行业交易；非 IT 行业、融资、合作及少数股权投资不纳入。"
        "收购方、标的、金额、交易事实和解读必须能够由新闻正文或标题中的原文证据支持；仅披露估值的交易不计入金额汇总和 Top 10。</div>"
        f"<div style='margin-top:7px'><b style='color:#40566e'>数据来源：</b>公司公告、监管文件及公开媒体报道；数据截止{end_date.year}年{end_date.month}月{end_date.day}日。</div>"
        "<div style='margin-top:7px'><b style='color:#40566e'>金额换算：</b>仅汇总原文明确披露的美元交易对价；其他币种未作汇率换算。</div>"
        "<div style='margin-top:7px'><b style='color:#40566e'>免责声明：</b>本报告仅供内部研究参考，不构成投资或决策建议。</div>"
        "</div></td></tr>"
    )
    font_stack = "Inter,Source Han Sans SC,Noto Sans CJK SC,Microsoft YaHei,Arial,sans-serif"
    print_period = f"全球IT并购{copy['label']}｜{start_date:%Y年%m月}"
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style type='text/css'>body,table,td,th,div,p,a,span,b,strong,h1,h2,h3,li{font-family:Inter,'Source Han Sans SC','Noto Sans CJK SC','Microsoft YaHei',Arial,sans-serif!important;}body{font-size:15px;font-weight:400;line-height:1.65;color:#29343f;}</style>"
        "<style type='text/css'>"
        ".print-header,.print-footer{display:none;}"
        ".report-zoom-controls{position:fixed;right:18px;bottom:18px;z-index:9999;display:flex;align-items:center;gap:4px;padding:6px;background:rgba(255,255,255,.96);border:1px solid #cbd8e6;border-radius:8px;box-shadow:0 4px 18px rgba(21,53,95,.18);font-family:Arial,sans-serif!important;}"
        ".report-zoom-controls[hidden]{display:none!important;}.report-zoom-controls button{min-width:34px;height:32px;padding:0 9px;border:1px solid #cbd8e6;border-radius:5px;background:#fff;color:#173b67;font-size:14px;font-weight:700;cursor:pointer;}.report-zoom-controls button:hover{background:#eef5fb;}.report-zoom-value{min-width:48px;color:#52677d;font-size:12px;text-align:center;}"
        ".report-interactive .narrative-content.is-collapsed{position:relative;overflow:hidden;}.report-interactive .narrative-facts.is-collapsed{max-height:128px;}.report-interactive .narrative-analysis.is-collapsed{max-height:158px;}"
        ".report-interactive .narrative-content.is-collapsed:after{content:'';position:absolute;left:0;right:0;bottom:0;height:36px;background:linear-gradient(to bottom,rgba(255,255,255,0),#fff);pointer-events:none;}"
        ".narrative-toggle{margin-top:8px;padding:3px 0;border:0;background:transparent;color:#1d5f9f;font-size:12px;font-weight:700;cursor:pointer;}.narrative-toggle:hover{text-decoration:underline;}.narrative-toggle[hidden]{display:none!important;}"
        "@media print{@page{size:A4;margin:16mm 12mm 18mm;}body{background:#fff!important;}"
        ".report-zoom-controls,.narrative-toggle{display:none!important;}.narrative-content{max-height:none!important;overflow:visible!important;}"
        ".print-header{display:block;position:fixed;top:-11mm;left:0;right:0;color:#52677d;font-size:9pt;border-bottom:1px solid #dbe5ef;padding-bottom:2mm;}"
        ".print-footer{display:block;position:fixed;bottom:-12mm;left:0;right:0;text-align:right;color:#8292a5;font-size:9pt;}"
        ".print-footer:after{content:'第 ' counter(page) ' 页';}.chapter-title{string-set:reportChapter content(text);}.print-chapter:after{content:'｜' string(reportChapter);}.report-section{break-before:page;page-break-before:always;}"
        ".section-01{break-before:auto;page-break-before:auto;}.china-section{break-before:page;page-break-before:always;}"
        ".key-deal-card,.insight-card,.compact-deal-card{break-inside:avoid;page-break-inside:avoid;}"
        ".industry-heading{break-after:avoid;page-break-after:avoid;}"
        "a{word-break:break-all;}.email-shell{width:100%!important;box-shadow:none!important;}}"
        "</style>"
        "<!--[if mso]><style type='text/css'>body,table,td,th,div,p,a,span,b,strong,h1,h2,h3,li{font-family:Arial,sans-serif!important;}</style><![endif]-->"
        f"</head><body style='margin:0;padding:0;background:#eef2f6;font-family:{font_stack};color:#293b4d'>"
        "<div id='report-zoom-controls' class='report-zoom-controls' hidden aria-label='报表缩放控件'>"
        "<button type='button' data-zoom-action='out' title='缩小' aria-label='缩小'>−</button>"
        "<span id='report-zoom-value' class='report-zoom-value'>100%</span>"
        "<button type='button' data-zoom-action='in' title='放大' aria-label='放大'>＋</button>"
        "<button type='button' data-zoom-action='reset' title='还原为 100%'>100%</button>"
        "<button type='button' data-zoom-action='fit' title='适应窗口宽度'>适宽</button></div>"
        f"<div class='print-header'>{html.escape(print_period)}<span class='print-chapter'></span></div><div class='print-footer'></div>"
        f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='font-family:{font_stack}'><tr><td align='center' style='padding:24px 8px'>"
        f"<table id='report-shell' class='email-shell' role='presentation' width='820' cellspacing='0' cellpadding='0' style='width:820px;max-width:100%;background:#fff;box-shadow:0 8px 26px rgba(21,53,95,.08);font-family:{font_stack}'>"
        + render_header(start_date, end_date, generated_at, period)
        + render_toc(len(international_deals), len(china_deals))
        + render_section("01", "本期重点解读", render_highlights(ordered, previous_ordered))
        + render_section("02", f"全球并购概览｜共{len(ordered)}笔", render_overview(ordered))
        + render_section("03", f"国际交易｜共{len(international_deals)}笔", render_international_transactions(international_deals))
        + render_china_section(china_deals, len(ordered))
        + methodology
        + f"<tr><td align='center' style='padding:24px 42px 28px;color:#8292a5;font-size:9px'>FOXIT STRATEGIC INTELLIGENCE · {copy['footer'].replace('&', '&amp;')} · {start_date} — {end_date}</td></tr>"
        + "</table></td></tr></table>"
        + "<script>(function(){var shell=document.getElementById('report-shell'),controls=document.getElementById('report-zoom-controls'),value=document.getElementById('report-zoom-value');if(!shell||!controls||!value)return;var zoom=1;function apply(next){zoom=Math.max(.5,Math.min(1.5,next));shell.style.zoom=String(zoom);value.textContent=Math.round(zoom*100)+'%';}function fit(){apply(Math.min(1,(window.innerWidth-32)/820));}controls.addEventListener('click',function(event){var action=event.target.getAttribute('data-zoom-action');if(action==='out')apply(zoom-.1);else if(action==='in')apply(zoom+.1);else if(action==='reset')apply(1);else if(action==='fit')fit();});document.body.classList.add('report-interactive');document.querySelectorAll('[data-narrative-toggle]').forEach(function(button){var content=button.previousElementSibling;if(!content)return;content.classList.add('is-collapsed');button.hidden=false;button.addEventListener('click',function(){var collapsed=content.classList.toggle('is-collapsed');button.textContent=collapsed?'展开全文':'收起';});});controls.hidden=false;})();</script>"
        + "</body></html>"
    )


def load_mna_email_recipients_by_language(
    args: argparse.Namespace,
) -> Dict[str, List[str]]:
    """Load enabled M&A subscribers grouped by their preferred language."""
    grouped: Dict[str, List[str]] = {"zh": [], "en": []}
    explicit = [
        item.strip()
        for item in re.split(r"[,;]", str(getattr(args, "email_to", None) or ""))
        if item.strip()
    ]
    if explicit:
        language = str(getattr(args, "email_language", "zh") or "zh").lower()
        grouped[language if language in grouped else "zh"] = list(dict.fromkeys(explicit))
        return grouped

    connection = None
    try:
        connection = open_db_connection(args)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT email, language FROM email_recipients "
                "WHERE is_active = TRUE AND receive_mna_report = TRUE "
                "AND language IN ('zh', 'en') ORDER BY id"
            )
            rows = cursor.fetchall() or []
        for row in rows:
            language = str(row.get("language") or "zh").strip().lower()
            email = str(row.get("email") or "").strip()
            if language in grouped and email:
                grouped[language].append(email)
        return {language: list(dict.fromkeys(items)) for language, items in grouped.items()}
    except Exception as exc:
        raise RuntimeError(
            "读取 email_recipients 失败；为避免绕过 receive_mna_report 设置，本次停止发送"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def load_mna_email_recipients(
    args: argparse.Namespace, language: str = "zh",
) -> List[str]:
    """Return M&A subscribers for one language."""
    return load_mna_email_recipients_by_language(args).get(language, [])


class _HtmlTextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.skip_depth = 0
        self.texts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"style", "script"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"style", "script"}:
            self.skip_depth = max(0, self.skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and re.search(r"[\u4e00-\u9fff]", data):
            self.texts.append(data)


class _HtmlTextReplacer(HTMLParser):
    def __init__(self, translations: Dict[str, str]) -> None:
        super().__init__(convert_charrefs=False)
        self.translations = translations
        self.parts: List[str] = []
        self.skip_depth = 0

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.parts.append(self.get_starttag_text())
        if tag.lower() in {"style", "script"}:
            self.skip_depth += 1

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")
        if tag.lower() in {"style", "script"}:
            self.skip_depth = max(0, self.skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.skip_depth or data not in self.translations:
            self.parts.append(data)
        else:
            self.parts.append(html.escape(self.translations[data], quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")


def translate_report_texts(texts: Sequence[str]) -> Dict[str, str]:
    """Translate visible report strings to institutional English."""
    import requests

    unique_texts = list(dict.fromkeys(texts))
    if not unique_texts:
        return {}
    key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
    if not key or not base_url:
        raise RuntimeError("生成英文报告需要 OPENAI_API_KEY / OPENAI_BASE_URL")

    translated: Dict[str, str] = {}

    def translate_batch(items: List[str]) -> None:
        # IDs are intentionally local to each batch. Some compatible APIs
        # renumber IDs from zero even when the input uses global indices.
        source = [{"id": index, "text": value} for index, value in enumerate(items)]
        prompt = (
            "Translate every text value into polished, concise English for an institutional M&A report. "
            "Preserve company names, numbers, currencies, dates, arrows and factual meaning. "
            "Do not add facts, commentary or HTML. Return strict JSON as "
            "{\"translations\":[{\"id\":0,\"text\":\"...\"}]} with every id exactly once.\n"
            + json.dumps(source, ensure_ascii=False)
        )
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                response = requests.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": os.getenv("OPENAI_MODEL", "gpt-5.3-codex"),
                        "messages": [
                            {"role": "system", "content": "You are a precise financial translator."},
                            {"role": "user", "content": prompt},
                        ],
                        "max_completion_tokens": int(os.getenv("MNA_REPORT_TRANSLATION_MAX_TOKENS", "12000")),
                    },
                    timeout=(10, 120),
                )
                response.raise_for_status()
                content = str(response.json()["choices"][0]["message"]["content"] or "")
                start, end = content.find("{"), content.rfind("}")
                if start < 0 or end < start:
                    raise ValueError("英文报告翻译未返回 JSON")
                rows = json.loads(content[start:end + 1]).get("translations")
                if not isinstance(rows, list):
                    raise ValueError("英文报告翻译结果缺少 translations 数组")
                values = {int(row["id"]): str(row["text"]) for row in rows}
                expected = set(range(len(items)))
                if set(values) != expected:
                    missing = sorted(expected - set(values))
                    raise ValueError(f"英文报告翻译缺少段落 ID: {missing}")
                for index, original in enumerate(items):
                    translated[original] = values[index]
                return
            except Exception as exc:
                last_error = exc
                print(f"[v2] English translation retry {attempt}/3: {exc}")

        # If a provider still truncates or omits entries, split the failed
        # batch and retry smaller independent requests.
        if len(items) > 1:
            midpoint = len(items) // 2
            translate_batch(items[:midpoint])
            translate_batch(items[midpoint:])
            return
        raise RuntimeError("英文报告单段翻译连续失败") from last_error

    try:
        batch_limit = max(1000, int(os.getenv("MNA_REPORT_TRANSLATION_BATCH_CHARS", "4000")))
    except ValueError as exc:
        raise ValueError("MNA_REPORT_TRANSLATION_BATCH_CHARS 必须是整数") from exc
    batch: List[str] = []
    batch_chars = 0
    for value in unique_texts:
        if batch and batch_chars + len(value) > batch_limit:
            translate_batch(batch)
            batch, batch_chars = [], 0
        batch.append(value)
        batch_chars += len(value)
    if batch:
        translate_batch(batch)
    return translated


def render_english_report(report_html: str) -> str:
    collector = _HtmlTextCollector()
    collector.feed(report_html)
    replacer = _HtmlTextReplacer(translate_report_texts(collector.texts))
    replacer.feed(report_html)
    output = "".join(replacer.parts).replace("<html lang='zh-CN'>", "<html lang='en'>")
    replacements = {
        "报表缩放控件": "Report zoom controls",
        "title='缩小' aria-label='缩小'": "title='Zoom out' aria-label='Zoom out'",
        "title='放大' aria-label='放大'": "title='Zoom in' aria-label='Zoom in'",
        "title='还原为 100%'": "title='Reset to 100%'",
        "title='适应窗口宽度'": "title='Fit to window width'",
        "content:'第 ' counter(page) ' 页'": "content:'Page ' counter(page)",
        "展开全文": "Read more",
        "收起": "Collapse",
    }
    for source, target in replacements.items():
        output = output.replace(source, target)
    return output


def send_email(
    args: argparse.Namespace, subject: str, report_html: str,
    recipients: Optional[Sequence[str]] = None, language: str = "zh",
) -> None:
    recipients = list(recipients) if recipients is not None else load_mna_email_recipients(args, language)
    if not recipients or not args.smtp_user or not args.smtp_pass:
        raise ValueError(
            "发送邮件需要 email_recipients 中启用的并购报告收件人（或 --email-to）、"
            "SMTP_USER 和 SMTP_PASS"
        )
    message = MIMEMultipart("mixed")
    message["Subject"], message["From"], message["To"] = subject, args.smtp_user, ", ".join(recipients)
    message["Date"], message["Message-ID"] = formatdate(localtime=True), make_msgid()
    body = MIMEMultipart("alternative")
    plain_text = (
        "Please view this report in an HTML-capable email client or download the attached HTML report."
        if language == "en"
        else f"请使用支持 HTML 的邮件客户端查看{subject.split('｜', 1)[0]}，或下载随附的 HTML 报告。"
    )
    body.attach(MIMEText(plain_text, "plain", "utf-8"))
    body.attach(MIMEText(report_html, "html", "utf-8"))
    message.attach(body)

    safe_subject = re.sub(r'[\\/:*?"<>|｜]+', "_", subject).strip(" ._")
    attachment_name = f"{safe_subject or '全球IT并购报告'}.html"
    attachment = MIMEText(report_html, "html", "utf-8")
    attachment.add_header(
        "Content-Disposition", "attachment", filename=("utf-8", "", attachment_name)
    )
    message.attach(attachment)
    if args.smtp_port == 465:
        with smtplib.SMTP_SSL(args.smtp_server, args.smtp_port, context=ssl.create_default_context(), timeout=30) as server:
            server.login(args.smtp_user, args.smtp_pass); server.sendmail(args.smtp_user, recipients, message.as_string())
    else:
        with smtplib.SMTP(args.smtp_server, args.smtp_port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context()); server.login(args.smtp_user, args.smtp_pass); server.sendmail(args.smtp_user, recipients, message.as_string())


def classify_verified_deals(
    news: Sequence[News], label: str = "current", args: Optional[argparse.Namespace] = None,
    stats_out: Optional[Dict[str, int]] = None,
) -> List[Deal]:
    if args is not None and not args.input_json:
        rows, cache_stats = classify_with_analysis_cache(args, news)
    else:
        rows = call_ai_for_classification(news)
        cache_stats = {"cache_hits": 0, "ai_analyzed": len(news)}
    non_it_rows = sum(str(row.get("industry") or "") not in IT_SECTORS for row in rows)
    verified_deals: List[Deal] = []
    for row in rows:
        try:
            news_index = int(row.get("id", -1))
        except (TypeError, ValueError):
            print(f"[clean] excluded invalid news id: {row.get('id')!r}")
            continue
        if not 0 <= news_index < len(news):
            print(f"[clean] excluded out-of-range news id: {news_index}")
            continue
        deal = normalize_deal(news[news_index], row)
        if deal is not None:
            verified_deals.append(deal)
    deals = rank_deals(verified_deals)
    validate_report_reconciliation(deals)
    print(
        f"[v2] period={label} ai_deals={len(rows)} evidence_verified={len(verified_deals)} "
        f"non_it_excluded={non_it_rows} duplicates_merged={len(verified_deals) - len(deals)} "
        f"excluded={len(news) - len(verified_deals)} final_deals={len(deals)} "
        f"cache_hits={cache_stats['cache_hits']} ai_analyzed={cache_stats['ai_analyzed']}"
    )
    if stats_out is not None:
        stats_out.update({
            "candidate_count": len(news), "included_count": len(deals),
            "cache_hits": cache_stats["cache_hits"], "ai_analyzed": cache_stats["ai_analyzed"],
            "duplicates_merged": len(verified_deals) - len(deals),
        })
    return deals


def save_report_run(
    args: argparse.Namespace, start_date: date, end_date: date, deals: Sequence[Deal],
    report_html: str, output: Path, stats: Dict[str, int],
) -> None:
    if args.input_json:
        return
    table = validate_table_name(args.report_runs_table)
    international_count = sum(not is_china_deal(deal) for deal in deals)
    china_count = sum(is_china_deal(deal) for deal in deals)
    report_hash = hashlib.sha256(report_html.encode("utf-8")).hexdigest()
    connection = open_db_connection(args)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO `{table}`
                    (period_type, start_date, end_date, rule_version, candidate_count, included_count,
                     international_count, china_count, cache_hits, ai_analyzed, report_hash, report_html,
                     output_path, validation_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'passed')
                """,
                (
                    args.period, start_date, end_date, analysis_rule_version(),
                    stats.get("candidate_count", 0), len(deals), international_count, china_count,
                    stats.get("cache_hits", 0), stats.get("ai_analyzed", 0), report_hash,
                    report_html, str(output),
                ),
            )
    finally:
        connection.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.input_json:
        ensure_analysis_tables(args)
    zone = report_timezone()
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(zone).date()
    start_date, end_date = calculate_period_dates(as_of, args.period, args.start_date)
    start_at = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    news = load_mna_news(args, start_at, end_at)
    current_stats: Dict[str, int] = {}
    deals = classify_verified_deals(news, args=args, stats_out=current_stats)

    previous_deals: Optional[List[Deal]] = None
    if args.period == "month" and not args.input_json:
        previous_end = start_date - timedelta(days=1)
        previous_start = previous_end.replace(day=1)
        previous_start_at = datetime.combine(previous_start, time.min, tzinfo=zone).astimezone(timezone.utc)
        previous_end_at = datetime.combine(previous_end + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
        try:
            previous_news = load_mna_news(args, previous_start_at, previous_end_at)
            previous_deals = classify_verified_deals(previous_news, "previous_month", args=args)
            print(f"[v2] mom_comparison={previous_start}..{previous_end} deals={len(previous_deals)}")
        except Exception as exc:
            print(f"[v2] mom_comparison omitted: previous month could not be verified with the same methodology: {exc}")
    copy = period_copy(args.period)
    output = Path(args.output or copy["file"]).resolve()
    report_deals = deals[:args.max_items]
    report_previous_deals = previous_deals[:args.max_items] if previous_deals is not None else None
    report_html = render_email_html(
        report_deals, start_date, end_date,
        datetime.now(zone).strftime("%Y-%m-%d %H:%M"), args.period,
        previous_deals=report_previous_deals,
    )
    output.write_text(report_html, encoding="utf-8")
    save_report_run(args, start_date, end_date, report_deals, report_html, output, current_stats)
    print(f"[v2] preview={output} deals={len(report_deals)}")
    if args.send and not args.dry_run:
        recipients = load_mna_email_recipients_by_language(args)
        if not any(recipients.values()):
            raise ValueError("email_recipients 中没有启用的并购报告收件人")
        print(f"[v2] Chinese recipients: {', '.join(recipients['zh']) or '(none)'}")
        print(f"[v2] English recipients: {', '.join(recipients['en']) or '(none)'}")
        english_html: Optional[str] = None
        english_output: Optional[Path] = None
        if recipients["en"]:
            # Finish English generation before sending either language so a
            # translation failure cannot leave the run half-sent.
            english_html = render_english_report(report_html)
            english_output = output.with_name(f"{output.stem}_en{output.suffix}")
            english_output.write_text(english_html, encoding="utf-8")
        if recipients["zh"]:
            send_email(
                args,
                f"全球IT并购{copy['label']}｜{start_date:%Y.%m.%d}-{end_date:%m.%d}｜{len(report_deals)}笔",
                report_html,
                recipients=recipients["zh"],
                language="zh",
            )
            print(f"[v2] Chinese email sent successfully: {len(recipients['zh'])} recipient(s)")
        if recipients["en"] and english_html is not None and english_output is not None:
            period_label = "Monthly Report" if args.period == "month" else "Weekly Report"
            send_email(
                args,
                f"Global IT M&A {period_label} | {start_date:%Y.%m.%d}-{end_date:%m.%d} | {len(report_deals)} Deals",
                english_html,
                recipients=recipients["en"],
                language="en",
            )
            print(
                f"[v2] English email sent successfully: {len(recipients['en'])} recipient(s); "
                f"preview={english_output}"
            )
    else:
        print("[v2] email not sent; use --send explicitly after preview review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
