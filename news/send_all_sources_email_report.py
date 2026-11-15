#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Separate investment/M&A report covering every source, grouped by source region."""

import argparse
import html
import json
import os
import re
import smtplib
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

import send_email_report as original
from output_paths import report_output_path
from news_tagging import FIELD_MAP, METADATA_COLUMN, filter_tagged_news, has_metadata_column


# Regions describe the source, rather than the location of an event it covers.
SOURCE_REGIONS = {
    "domestic": {
        "names": (
            "36 Kr", "36氪", "Hongshan", "红杉", "红杉中国", "东方财富", "eastmoney",
            "PANews", "RFID世界网", "rfidworld", "碧海新闻", "bihainews", "富途资讯", "futunn",
            "同花顺", "同花顺财经", "tonghuashun", "投资界", "pedaily", "搜狐", "搜狐网",
            "sohu_news", "新浪网", "sina_news", "新浪财经", "sina_finance", "环球旅讯",
            "traveldaily", "观点网", "guandian", "集微网", "laoyaoba",
        ),
        "domains": (
            "36kr.com", "hongshan.com", "eastmoney.com", "panewslab.com", "rfidworld.com.cn",
            "bihainews.com", "futunn.com", "10jqka.com.cn", "pedaily.cn", "sohu.com",
            "sina.com.cn", "sina.cn", "traveldaily.cn", "guandian.cn", "laoyaoba.com",
        ),
    },
    "international": {
        "names": (
            "A16Z", "Andreessen Horowitz", "Bessemer Venture Partners", "bvp", "Bloomberg",
            "Crunchbase", "Crunchbase News", "RFI", "TechCrunch", "TechCrunch News",
            "TechCrunch Venture", "techcrunch_api", "TechStartups", "Thoma Bravo", "VentureBeat",
        ),
        "domains": (
            "a16z.com", "a16zcrypto.com", "a16z.news", "bvp.com", "bloomberg.com",
            "crunchbase.com", "rfi.fr", "techcrunch.com", "techstartups.com",
            "thomabravo.com", "venturebeat.com",
        ),
    },
}
REGION_LABELS = {
    "zh": {"international": "国外新闻", "domestic": "国内新闻", "unknown": "来源地区待确认"},
    "en": {"international": "International News", "domestic": "Domestic News (China)", "unknown": "Unclassified Sources"},
}
CHINA_TZ = timezone(timedelta(hours=8))


def parse_args(argv=None):
    defaults = original.build_runtime_args()
    parser = argparse.ArgumentParser(description="全来源投资/并购日报：按国内外分组，默认生成 HTML 预览")
    parser.add_argument("--hours", type=int, default=defaults.hours)
    parser.add_argument("--limit", type=int, default=int(os.getenv("ALL_SOURCES_EMAIL_REPORT_LIMIT", "0")), help="最多收录条数；默认0，不设上限")
    parser.add_argument("--subject-suffix", default=defaults.subject_suffix)
    for name in ("db-host", "db-user", "db-password", "db-name", "db-table", "smtp-server", "smtp-user", "smtp-pass", "email-to"):
        parser.add_argument("--" + name, default="")
    for name in ("db-port", "smtp-port"):
        parser.add_argument("--" + name, type=int, default=0)
    parser.add_argument("--output", help="自定义 HTML 输出路径；默认保存到工程根目录 output/")
    parser.add_argument("--language", choices=("zh", "en"), default="zh", help="本地 HTML 预览语言")
    parser.add_argument("--input-json", help="从 JSON 新闻列表回放，不查询数据库")
    parser.add_argument("--demo", action="store_true", help="使用明确标注的排版示例数据，不查询数据库；配合 --send 发送排版测试邮件")
    parser.add_argument("--dry-run", action="store_true", default=defaults.dry_run, help="只生成 HTML，不发送")
    parser.add_argument("--send", action="store_true", help="明确发送新增日报；默认仅生成 HTML")
    return parser.parse_args(argv)


def now_china():
    return datetime.now(CHINA_TZ)


def normalize_source(value: str) -> str:
    return "".join(char for char in value.casefold() if char not in " _-\t\r\n")


def source_region(row: Dict[str, Any]) -> str:
    website = str(row.get("网站") or "").strip()
    for region, sources in SOURCE_REGIONS.items():
        if normalize_source(website) in {normalize_source(name) for name in sources["names"]}:
            return region

    for value in (website, str(row.get("链接") or "")):
        try:
            host = (urlsplit(value if "://" in value else "//" + value).hostname or "").lower()
        except ValueError:
            continue
        for region, sources in SOURCE_REGIONS.items():
            if any(host == domain or host.endswith("." + domain) for domain in sources["domains"]):
                return region
        if host.endswith(".cn"):
            return "domestic"
    return "unknown"


def group_rows_by_region(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {region: [] for region in REGION_LABELS["zh"]}
    for row in rows:
        groups[source_region(row)].append(row)
    return {region: original.group_rows_by_source(items) for region, items in groups.items()}


def fetch_news(args) -> List[Dict[str, Any]]:
    """Read every source, including unlabelled rows so they cannot silently disappear."""
    mysql_cfg = original.resolve_mysql_config(args)
    fields = dict(FIELD_MAP)
    query = f"""
        SELECT {', '.join(fields)} FROM `{mysql_cfg['table']}`
        WHERE publish_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
          AND publish_date < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
        ORDER BY publish_date DESC
    """
    params = [args.hours // 24]
    connection = original.connect_ai_news_mysql(mysql_cfg)
    try:
        connection.ping(reconnect=True)
        if not has_metadata_column(connection, mysql_cfg["table"]):
            raise RuntimeError("数据库尚未补充日报标签，请先运行 backfill_news_tags.py --execute")
        query = query.replace("SELECT ", f"SELECT {METADATA_COLUMN}, ", 1)
        with connection.cursor(original.pymysql.cursors.DictCursor) as cursor:
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
    finally:
        connection.close()
    return [{**{label: str(row.get(field) or "") for field, label in fields.items()},
             METADATA_COLUMN: row.get(METADATA_COLUMN)} for row in rows]


def report_title(args, language: str = "zh") -> str:
    demo = bool(getattr(args, "demo", False))
    suffix = str(args.subject_suffix or "").strip()
    if language == "en":
        title = ("[Layout Test] " if demo else "") + "Global Investment Research Daily"
    else:
        title = ("【排版测试】" if demo else "") + "全球投研日报"
    return title + (f" - {suffix}" if suffix else "")


def summary_points(summary: str) -> List[str]:
    """Conservative fallback: retain existing paragraphs and explicit list items."""
    points = []
    for line in summary.splitlines():
        line = line.strip()
        if not line:
            continue
        bullet = re.match(r"^(?:[-*]\s+|[•●]\s*)", line)
        if bullet:
            point = line[bullet.end():].strip()
            if point:
                points.append(point)
            continue
        points.append(line)
    return points


def summary_fields(language):
    primary, secondary = ("英文", "中文") if language == "en" else ("中文", "英文")
    return [f"{kind}摘要({lang})" for lang in (primary, secondary) for kind in ("AI", "内容")]


def summary_source_text(value):
    return "\n".join(summary_points(str(value or "")))


def has_incomplete_summary_boundary(points):
    return any(point.rstrip().rstrip('”’」』）】\"\'').endswith((",", "，", ":", "：", "、")) for point in points[:-1])


def validate_summary_review(row, language, review):
    """AI may only partition a selected DB summary; verify every non-space character."""
    if not isinstance(review, dict) or review.get("status") not in {"ok", "stored", "incomplete", "unavailable"}:
        raise ValueError("摘要审核状态无效")
    field = review.get("source_field")
    if field not in summary_fields(language):
        raise ValueError("摘要审核来源字段无效")
    source = summary_source_text(row.get(field))
    if review.get("source_text") != source:
        raise ValueError("摘要来源已变更，须重新审核")
    points = review.get("points")
    if not isinstance(points, list) or any(not isinstance(p, str) or not p.strip() for p in points):
        raise ValueError("摘要分点格式无效")
    if review["status"] != "unavailable":
        compact = lambda text: re.sub(r"\s+", "", text)
        if not points or compact("".join(points)) != compact(source):
            raise ValueError("摘要分点存在遗漏、改写、增添或顺序变化")
        if review["status"] != "stored" and has_incomplete_summary_boundary(points):
            raise ValueError("摘要分点切在逗号或冒号处，形成残句")
    elif points:
        raise ValueError("无有效摘要时不得返回分点")
    return review


def prepare_report_summaries(rows, languages=("zh",)):
    """Use the complete paragraph/bullet boundaries already stored by the crawler."""
    output = []
    for row in rows:
        reviews = {}
        for language in dict.fromkeys(languages):
            fields = summary_fields(language)
            field = next((field for field in fields if str(row.get(field) or "").strip()), fields[0])
            points = summary_points(str(row.get(field) or ""))
            reviews[language] = {"status": "stored" if points else "unavailable", "source_field": field,
                                 "source_text": summary_source_text(row.get(field)), "points": points,
                                 "reason": "直接使用数据库摘要段落，不调用AI"}
        output.append(dict(row, report_summary=reviews))
    return output


def article_summary(row, language):
    review = (row.get("report_summary") or {}).get(language)
    try:
        validate_summary_review(row, language, review)
        return review["points"], review["status"]
    except (ValueError, TypeError):
        return summary_points(str(original._pick_summary(row, language) or "")), "fallback"


def build_report_markdown(rows: List[Dict[str, Any]], args, language: str = "zh") -> str:
    english = language == "en"
    groups = group_rows_by_region(rows)
    labels = REGION_LABELS[language]
    today = datetime.now().strftime("%Y-%m-%d")
    counts = " | ".join(f"{labels[region]}: {len(items)}" for region, items in groups.items() if items or region != "unknown")
    content = [f"# {report_title(args, language)} - {today}", "", f"*{counts}*", ""]
    content += [
        "Grouped by source location; investment, M&A and relevant industry developments."
        if english else "按新闻源所属地区分组；收录IT、AI、软件/SaaS行业的投融资、并购及相关产业动态。",
        "",
    ]
    index = 0
    for region, items in groups.items():
        if not items and region == "unknown":
            continue
        content += [f"## {labels[region]} ({len(items)})", ""]
        if not items:
            content += ["No matching news." if english else "本期暂无符合条件的新闻。", ""]
        for row in items:
            index += 1
            title = (row.get("标题(英文)") if english else row.get("标题(中文)")) or row.get("标题(中文)") or row.get("标题(英文)") or ("Untitled News" if english else "未命名新闻")
            source = original._extract_source_label(str(row.get("网站") or ""), str(row.get("链接") or ""))
            tags = str(row.get("标签") or "")
            if english:
                tags = original._translate_tags_to_english(tags)
            info = [f"**{'Source' if english else '来源'}**: {source}", f"**{'Date' if english else '日期'}**: {row.get('发布日期', '')}"]
            if tags:
                info.append(f"**{'Category' if english else '分类'}**: {tags}")
            content += [f"### {index}. {title}", "", " | ".join(info), ""]
            if row.get("链接"):
                content += [f"[{'Read More' if english else '查看原文'}]({row['链接']})", ""]
            points, status = article_summary(row, language)
            if points:
                content += [f"**{'Summary' if english else '摘要'}:**", ""]
                content.extend(f"- {point}" for point in points)
                content.append("")
            if status in {"incomplete", "unavailable"}:
                content += ["Summary information is incomplete; please consult the source." if english else "摘要资料不完整，请结合原文阅读。", ""]
    content += ["---", "", f"{'Total Items' if english else '总条数'}: {len(rows)}"]
    return "\n".join(content)


def render_news_item(index, row, language):
    english = language == "en"
    title = (row.get("标题(英文)") if english else row.get("标题(中文)")) or row.get("标题(中文)") or row.get("标题(英文)") or ("Untitled News" if english else "未命名新闻")
    url = str(row.get("链接") or "").strip()
    safe_url = html.escape(url, quote=True) if urlsplit(url).scheme in {"https", "http"} else ""
    source = original._extract_source_label(str(row.get("网站") or ""), url)
    title_html = html.escape(str(title))
    if safe_url:
        title_html = f"<a href='{safe_url}' target='_blank' rel='noopener noreferrer' style='color:#173b67;text-decoration:none'>{title_html}</a>"
    points, summary_status = article_summary(row, language)
    summary_html = (
        "<ul class='summary-points' style='margin:0;padding:0 0 0 22px;list-style-type:disc;list-style-position:outside;color:#202020;font-size:15px;font-weight:400;line-height:1.85'>"
        + "".join(f"<li style='margin:0 0 8px;padding-left:2px'>{html.escape(point)}</li>" for point in points)
        + "</ul>"
    ) if points else ""
    if not summary_html:
        summary_html = "<p style='color:#8292a5;font-size:14px'>" + ("No summary available." if english else "暂无摘要。") + "</p>"
    if summary_status in {"incomplete", "unavailable"}:
        summary_html += "<p style='margin:8px 0 0;color:#8292a5;font-size:12px'>" + ("Summary information is incomplete; please consult the source." if english else "摘要资料不完整，请结合原文阅读。") + "</p>"
    tags = str(row.get("标签") or "")
    if english:
        tags = original._translate_tags_to_english(tags)
    tag_html = "".join(f"<span style='display:inline-block;margin:0 6px 5px 0;padding:3px 8px;background:#eef5fb;color:#456986;font-size:11px;line-height:1.5'>{html.escape(tag.strip())}</span>" for tag in re.split(r"[|,，;；]", tags) if tag.strip())
    source_link = f"<a href='{safe_url}' target='_blank' rel='noopener noreferrer' style='color:#1d5f9f;text-decoration:none;font-weight:700'>{html.escape(source)} ↗</a>" if safe_url else html.escape(source)
    return (
        "<table class='news-card' role='presentation' width='100%' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;border-bottom:1px solid #d8e3ee'>"
        "<tr><td style='padding:20px 0 24px'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'><tr>"
        f"<td width='38' valign='top' style='width:38px;padding-top:3px;color:#2a67a2;font-size:15px;font-weight:800'>{index:02d}</td>"
        f"<td style='font-size:20px;font-weight:700;line-height:1.55;color:#173b67;overflow-wrap:anywhere'>{title_html}</td></tr></table>"
        "<div style='margin:14px 0;padding:10px 12px;background:#eef5fb;border:1px solid #d4e3f1;color:#52677d;font-size:12px;line-height:1.7'>"
        f"<b>{'Source' if english else '来源'}：</b>{source_link}"
        f"<span style='padding:0 10px;color:#a2afbd'>｜</span>{'Published' if english else '发布日期'}：{html.escape(str(row.get('发布日期') or '—'))}</div>"
        + (f"<div style='margin-bottom:12px'>{tag_html}</div>" if tag_html else "")
        + summary_html + "</td></tr></table>"
    )


def render_section(number, title, content):
    return (
        f"<tr id='section-{number}' class='report-section'><td class='section-padding' style='padding:36px 42px 0'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>"
        f"<tr><td class='chapter-title' style='padding:17px 20px;background:#173b67;color:#fff;font-size:22px;font-weight:700;line-height:1.4'>{number}｜{html.escape(title)}</td></tr>"
        f"<tr><td style='padding-top:16px'>{content}</td></tr></table></td></tr>"
    )


def build_report_html(rows, args, language="zh"):
    """Use the v2 brief's visual language without its transaction extraction rules."""
    english = language == "en"
    groups = group_rows_by_region(rows)
    labels = REGION_LABELS[language]
    generated = now_china()
    title = report_title(args, language)
    source_counts = Counter(original._extract_source_label(str(row.get("网站") or ""), str(row.get("链接") or "")) for row in rows)
    demo = bool(getattr(args, "demo", False))
    overview_title = "Report Overview" if english else "本期资讯概览"
    sections = [("01", overview_title, len(rows))] + [(f"{index:02d}", labels[region], len(items)) for index, (region, items) in enumerate(groups.items(), 2) if items or region != "unknown"]
    kpis = [(len(rows), "News Items" if english else "收录新闻"), (len(groups["international"]), "International" if english else "国外新闻"), (len(groups["domestic"]), "Domestic" if english else "国内新闻"), (len(source_counts), "Sources" if english else "覆盖来源")]
    kpi_cells = "".join(f"<td width='25%' align='center' style='width:25%;padding:24px 4px;border-right:{'0' if i == 3 else '1px solid #e4eaf1'}'><div style='color:#15355f;font-size:35px;font-weight:700;line-height:1.1'>{value}</div><div style='margin-top:8px;color:#64748b;font-size:12px'>{label}</div></td>" for i, (value, label) in enumerate(kpis))
    source_list = " · ".join(f"{html.escape(source)} ({count})" for source, count in source_counts.most_common()) or "—"
    overview = (
        f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='border:1px solid #dbe5f1'><tr>{kpi_cells}</tr></table>"
        "<div style='margin-top:18px;padding:16px 18px;border-left:5px solid #4b78a9;background:#eef5fb'>"
        f"<div style='color:#173b67;font-size:15px;font-weight:700'>{'Source Coverage' if english else '来源覆盖'}</div>"
        f"<div style='margin-top:8px;color:#52677d;font-size:13px;line-height:1.85;overflow-wrap:anywhere'>{source_list}</div></div>"
    )
    toc = "".join(f"<tr><td width='42' style='padding:9px 0;border-bottom:1px solid #e7edf3;color:#4b78a9;font-size:12px;font-weight:700'>{number}</td><td style='padding:9px 0;border-bottom:1px solid #e7edf3'><a href='#section-{number}' style='color:#173b67;font-size:14px;font-weight:600;text-decoration:none'>{html.escape(label)}</a></td><td align='right' style='padding:9px 0;border-bottom:1px solid #e7edf3;color:#718096;font-size:12px'>{count} {'items' if english else '条'}</td></tr>" for number, label, count in sections)
    chapters = render_section("01", overview_title, overview)
    article_index = 0
    for region_index, (region, items) in enumerate(groups.items(), 2):
        if not items and region == "unknown":
            continue
        parts = []
        previous_source = None
        for row in items:
            source = original._extract_source_label(str(row.get("网站") or ""), str(row.get("链接") or ""))
            if source != previous_source:
                parts.append(f"<div class='source-heading' style='margin:24px 0 0;padding:9px 12px;background:#eaf2f9;border-left:4px solid #4b78a9;color:#173b67;font-size:15px;font-weight:700'>{html.escape(source)}</div>")
                previous_source = source
            article_index += 1
            parts.append(render_news_item(article_index, row, language))
        if not items:
            parts.append("<p style='padding:10px 0;color:#8292a5;font-size:14px'>" + ("No matching news." if english else "本期暂无符合条件的新闻。") + "</p>")
        chapters += render_section(f"{region_index:02d}", f"{labels[region]}｜{len(items)} {'items' if english else '条'}", "".join(parts))
    limit_note = (f"up to {args.limit} items" if args.limit else "all matching items, with no item limit") if english else (f"最多收录 {args.limit} 条" if args.limit else "完整收录符合条件的新闻，不设条数上限")
    note = (
        f"All sources are eligible; coverage includes IT/AI/software/SaaS investment, funding, M&A and relevant business, technology and policy developments. Incidental AI/Token mentions do not qualify. Mixed roundups retain relevant entries only. Grouped by source location. The date window starts at database midnight minus {args.hours // 24} day(s); newest first, {limit_note}."
        if english else f"覆盖所有新闻源，收录IT、AI、软件/SaaS行业的投融资、并购及对投资研究有直接价值的业务、技术、政策动态。仅提及AI、Token、数字人等应用的消费营销及其他非IT内容不纳入；混合行业汇总仅保留相关条目。国内外按新闻源所属地区归类；同一来源的新闻集中排列。从数据库当天零点往前 {args.hours // 24} 天起，按发布日期倒序，{limit_note}。"
    )
    demo_banner = ("Layout sample · illustrative content, not real news" if english else "排版样例 · 以下为示例内容，不代表真实新闻") if demo else ""
    return (
        "<!doctype html><html lang='" + ("en" if english else "zh-CN") + "'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body,table,td,div,p,a,span{font-family:Inter,'Source Han Sans SC','Noto Sans CJK SC','Microsoft YaHei',Arial,sans-serif;}body{font-size:15px;line-height:1.65}table{box-sizing:border-box}"
        ".news-card{break-inside:avoid}.source-heading{break-after:avoid}.chapter-title{break-after:avoid}"
        "@media(max-width:600px){.section-padding{padding-left:20px!important;padding-right:20px!important}.report-title{font-size:25px!important}.page-header{padding:30px 20px!important}.news-card td{overflow-wrap:anywhere}.chapter-title{font-size:19px!important}.toc-cell{padding-left:20px!important;padding-right:20px!important}}"
        "@media print{@page{size:A4;margin:14mm 10mm}body{background:#fff!important}.email-shell{width:100%!important;box-shadow:none!important}.outer-cell{padding:0!important}.report-section{break-inside:auto}a{color:inherit!important}}"
        "</style></head><body style='margin:0;padding:0;background:#eef2f6;color:#293b4d'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'><tr><td class='outer-cell' align='center' style='padding:24px 8px'>"
        "<table id='report-shell' class='email-shell' role='presentation' width='820' cellspacing='0' cellpadding='0' style='width:820px;max-width:100%;background:#fff;box-shadow:0 8px 26px rgba(21,53,95,.08)'>"
        "<tr><td class='page-header' align='center' style='padding:37px 28px 34px;background:#15355f;color:#fff'>"
        "<div style='color:#bdd3ed;font-size:9px;font-weight:700;letter-spacing:2px'>INVESTMENT &amp; M&amp;A INTELLIGENCE</div>"
        f"<div class='report-title' style='margin-top:12px;font-size:30px;font-weight:600;line-height:1.45'>{html.escape(title)}</div>"
        f"<div style='margin-top:8px;color:#d8e6f7;font-size:12px;letter-spacing:1px'>{'ALL SOURCES · DOMESTIC & INTERNATIONAL' if english else '全来源覆盖 · 国内与国外资讯'}</div>"
        f"<div style='margin-top:20px;font-size:19px;font-weight:700'>{generated:%Y-%m-%d}</div>"
        f"<div style='margin-top:6px;color:#bbcee4;font-size:11px'>{'Generated' if english else '生成于'} {generated:%Y-%m-%d %H:%M}（GMT+8）</div></td></tr>"
        + (f"<tr><td style='padding:12px 24px;background:#fff4dd;color:#8a5c15;text-align:center;font-size:13px'>{demo_banner}</td></tr>" if demo else "")
        + "<tr><td class='toc-cell' style='padding:28px 42px 0'>"
        f"<div style='margin-bottom:8px;color:#173b67;font-size:16px;font-weight:700'>{'Contents' if english else '目录'}</div>"
        f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>{toc}</table></td></tr>"
        + chapters
        + f"<tr><td class='section-padding' style='padding:30px 42px 0'><div style='padding:15px;background:#f7f9fb;color:#59697a;font-size:11.5px;line-height:1.85'><b>{'Coverage' if english else '收录口径'}：</b>{note}</div></td></tr>"
        f"<tr><td align='center' style='padding:24px 24px 28px;color:#8292a5;font-size:9px'>FOXIT STRATEGIC INTELLIGENCE · ALL-SOURCE DAILY BRIEF · {generated:%Y-%m-%d}</td></tr>"
        "</table></td></tr></table></body></html>"
    )


def demo_rows():
    """Clearly fictional copy for inspecting short/long titles and summaries."""
    examples = [
        ("东方财富", "示例｜产业基金参与新一轮融资，支持企业拓展核心业务", "示例内容：项目A完成新一轮融资，本条用于查看标题、来源、日期、标签与摘要之间的间距。\n第二段摘要用于检查长内容在邮件中的阅读效果，以及不同段落之间的留白。", "融资,投资"),
        ("投资界", "示例｜科技企业完成战略融资", "示例内容：项目B引入战略投资方。本条用于检查较短标题与单段摘要的显示效果。", "战略投资"),
        ("Hongshan", "示例｜企业服务公司启动新一轮资本合作，计划扩大研发投入与产品应用范围", "示例内容：项目C公布融资安排。资金将用于产品研发、市场拓展和团队建设。此处仅为排版占位，不是实际新闻。", "风险投资,融资"),
        ("TechCrunch", "示例｜海外软件公司宣布融资计划，布局下一阶段业务增长", "示例内容：项目D获得新一轮投资。本条用于查看国外来源的新闻样式。\n摘要沿用数据库内容呈现，排版不会自动生成交易金额或市场判断。", "融资,软件"),
        ("TechCrunch", "示例｜投资机构发布企业软件市场观察", "示例内容：本条用于检查同一来源下连续两篇文章的分隔线、编号以及段落样式。", "投资"),
        ("Crunchbase", "示例｜投资方关注新兴技术企业的融资机会", "示例内容：项目E公布资本市场相关动态，用于检查不同国外来源之间的分组标题。", "风险投资"),
    ]
    return [{"网站": source, "标题(中文)": title, "标题(英文)": "Layout sample: " + str(index), "AI摘要(中文)": summary, "AI摘要(英文)": "Illustrative layout content, not real news.", "发布日期": now_china().date().isoformat(), "标签": tags} for index, (source, title, summary, tags) in enumerate(examples, 1)]


def send_report(rows: List[Dict[str, Any]], args, email_config=None) -> bool:
    config = dict(email_config if email_config is not None else original.resolve_email_config(args))
    for key in ("smtp_server", "smtp_user"):
        config[key] = str(config[key]).strip()
    recipients_by_language = original.get_recipients_by_language(args)
    success_count = 0
    total_count = 0
    for language, recipients in recipients_by_language.items():
        emails = [recipient["email"] for recipient in recipients if recipient.get("email")]
        if not emails:
            continue
        total_count += len(emails)
        message = MIMEMultipart()
        message["From"] = config["smtp_user"]
        message["To"] = ", ".join(emails)
        message["Subject"] = report_title(args, language)
        report_html = build_report_html(rows, args, language)
        message.attach(MIMEText(report_html, "html", "utf-8"))
        attachment = MIMEText(report_html, "html", "utf-8")
        attachment.add_header("Content-Disposition", "attachment", filename=f"all_sources_daily_{now_china():%Y-%m-%d}{'_en' if language == 'en' else ''}.html")
        message.attach(attachment)
        try:
            smtp_class = smtplib.SMTP_SSL if config["smtp_port"] == 465 else smtplib.SMTP
            with smtp_class(config["smtp_server"], config["smtp_port"], timeout=30) as server:
                if config["smtp_port"] == 587:
                    server.starttls()
                server.login(config["smtp_user"], config["smtp_pass"])
                refused = server.send_message(message)
            success_count += len(emails) - len(refused)
            print(f"{language.upper()} 全来源日报: {len(emails) - len(refused)}/{len(emails)} 人发送成功")
        except Exception as exc:
            print(f"全来源日报发送失败 ({language}): {exc}")
    print(f"全来源日报发送结果: {success_count}/{total_count} 人成功")
    return total_count > 0 and success_count == total_count


def run_email_report(args) -> int:
    send_requested = bool(getattr(args, "send", False) and not args.dry_run)
    print(f"全来源投资/并购报告: hours={args.hours} limit={args.limit} dry_run={args.dry_run}")
    print("发送模式：按数据库标签筛选后生成HTML并自动发送邮件（不调用AI）" if send_requested else "预览模式：仅生成HTML文件", flush=True)
    print(f"保留原时间口径：从数据库当天零点往前 {args.hours // 24} 天起，按发布日期倒序")
    if args.hours <= 0 or args.limit < 0:
        print("--hours 必须大于 0，--limit 不得小于 0（0 表示完整收录）")
        return 1
    if getattr(args, "demo", False) and getattr(args, "input_json", None):
        print("--demo 不能与 --input-json 同时使用")
        return 1
    try:
        if getattr(args, "demo", False):
            rows = demo_rows()
        elif getattr(args, "input_json", None):
            rows = json.loads(Path(args.input_json).read_text(encoding="utf-8-sig"))
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("JSON 须为新闻对象列表，字段与数据库查询结果转换后的字段一致")
        else:
            rows = fetch_news(args)
        reviews = []
        if not getattr(args, "demo", False):
            candidate_count = len(rows)
            rows, reviews = filter_tagged_news(rows)
            print(f"数据库标签筛选结果: 候选 {candidate_count} 条，符合IT/AI/软件/SaaS {len(rows)} 条，排除 {candidate_count - len(rows)} 条", flush=True)
            if args.limit:
                rows = rows[:args.limit]
        language = getattr(args, "language", "zh")
        if not getattr(args, "demo", False):
            languages = [language]
            if getattr(args, "send", False) and not args.dry_run:
                languages.extend(lang for lang, recipients in original.get_recipients_by_language(args).items() if recipients)
            rows = prepare_report_summaries(rows, languages)
        filename_prefix = "all_sources_daily" if send_requested else "all_sources_daily_preview"
        output = report_output_path(
            f"{filename_prefix}_{now_china():%Y-%m-%d}{'_en' if language == 'en' else ''}.html",
            getattr(args, "output", None),
        )
        output.write_text(build_report_html(rows, args, language), encoding="utf-8")
        if not getattr(args, "demo", False):
            summary_path = output.with_name(output.stem + "_summary_review.json")
            summary_path.write_text(json.dumps([
                {"title": row.get("标题(中文)") or row.get("标题(英文)"), "url": row.get("链接"),
                 "report_summary": row.get("report_summary", {})} for row in rows
            ], ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"摘要分点审核明细: {summary_path}")
        if reviews:
            review_path = output.with_name(output.stem + "_industry_review.json")
            review_path.write_text(json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"行业筛选明细: {review_path}")
        print(f"HTML 日报已生成: {output}")
    except Exception as exc:
        print(f"全来源日报生成失败，尚未发送邮件: {exc}")
        return 1
    for region, items in group_rows_by_region(rows).items():
        print(f"{REGION_LABELS['zh'][region]}: {len(items)} 条")
    if not rows:
        print("本期暂无符合条件的新闻，将按发送模式发送无新闻通知邮件")
    if args.dry_run or not getattr(args, "send", False):
        print("预览模式，不发送邮件；发送需显式使用 --send")
        return 0
    if not original.email_enabled(args):
        print("邮件配置不完整，请沿用原日报的 SMTP 和收件人配置")
        return 1
    print("开始发送HTML日报，正文和附件均使用完整报告", flush=True)
    return 0 if send_report(rows, args) else 1


if __name__ == "__main__":
    raise SystemExit(run_email_report(parse_args()))
