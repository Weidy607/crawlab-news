#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
import json
import os
import re
import socket
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List

try:
    import pymysql
except ImportError:
    pymysql = None


CURRENT_DIR = Path(__file__).resolve().parent

from news.settings import AI_NEWS_MYSQL  # noqa: E402


INVEST_MNA_TAG_WHERE = """
(
    tags LIKE '%%融资%%' OR tags LIKE '%%投资%%' OR tags LIKE '%%风险投资%%'
    OR tags LIKE '%%并购%%' OR tags LIKE '%%收购%%' OR tags LIKE '%%IPO%%'
    OR tags LIKE '%%私募股权%%'
    OR tags LIKE '%%funding%%' OR tags LIKE '%%investment%%' OR tags LIKE '%%venture%%'
    OR tags LIKE '%%acquisition%%' OR tags LIKE '%%merger%%' OR tags LIKE '%%M&A%%'
)
""".strip()

DAILY_WEBSITE_ALLOWLIST = (
    "36 Kr",
    "36kr.com",
    "A16Z",
    "Bessemer Venture Partners",
    "Bloomberg",
    "bvp",
    "Crunchbase",
    "Crunchbase News",
    "Hongshan",
    "RFI",
    "TechCrunch",
    "TechCrunch Venture",
    "TechStartups",
    "Thoma Bravo",
    "thomabravo",
)


def _get_env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def build_runtime_args(**overrides: Any) -> argparse.Namespace:
    values = {
        "hours": _get_env_int("EMAIL_REPORT_HOURS", 24),
        "limit": _get_env_int("EMAIL_REPORT_LIMIT", 1000),
        "db_host": "",
        "db_port": 0,
        "db_user": "",
        "db_password": "",
        "db_name": "",
        "db_table": "",
        "smtp_server": "",
        "smtp_port": 0,
        "smtp_user": "",
        "smtp_pass": "",
        "email_to": "",
        "subject_suffix": os.getenv("EMAIL_REPORT_SUBJECT_SUFFIX", "").strip(),
        "dry_run": _get_env_bool("EMAIL_REPORT_DRY_RUN", False),
    }
    for key, value in overrides.items():
        if value not in (None, ""):
            values[key] = value
    return argparse.Namespace(**values)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 news 工程的 ai_news 表读取投资/并购新闻并发送邮件"
    )
    defaults = build_runtime_args()

    parser.add_argument(
        "--hours",
        type=int,
        default=defaults.hours,
        help=f"读取最近多少小时的新闻，默认 {defaults.hours}（可用 EMAIL_REPORT_HOURS 配置）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=defaults.limit,
        help=f"最多发送多少条新闻，默认 {defaults.limit}（可用 EMAIL_REPORT_LIMIT 配置）",
    )
    parser.add_argument("--db-host", default="", help="覆盖 MySQL host")
    parser.add_argument("--db-port", type=int, default=0, help="覆盖 MySQL port")
    parser.add_argument("--db-user", default="", help="覆盖 MySQL user")
    parser.add_argument("--db-password", default="", help="覆盖 MySQL password")
    parser.add_argument("--db-name", default="", help="覆盖 MySQL database")
    parser.add_argument("--db-table", default="", help="覆盖 MySQL table")
    parser.add_argument("--smtp-server", default="", help="覆盖 SMTP server")
    parser.add_argument("--smtp-port", type=int, default=0, help="覆盖 SMTP port")
    parser.add_argument("--smtp-user", default="", help="覆盖 SMTP user / 发件人邮箱")
    parser.add_argument("--smtp-pass", default="", help="覆盖 SMTP password / 授权码")
    parser.add_argument("--email-to", default="", help="覆盖收件人，多个邮箱用逗号分隔")
    parser.add_argument(
        "--subject-suffix",
        default=defaults.subject_suffix,
        help="手动指定邮件主题后缀；不传时按 hours 自动生成，也可用 EMAIL_REPORT_SUBJECT_SUFFIX 配置",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=defaults.dry_run,
        help="只读取并打印数量，不真正发送邮件",
    )
    return parser.parse_args()


def _fetch_email_config_from_db(args: argparse.Namespace) -> Dict[str, Any]:
    mysql_cfg = resolve_mysql_config(args)
    try:
        connection = connect_ai_news_mysql(mysql_cfg)
    except Exception as exc:
        print(f"⚠️ 读取邮件数据库配置失败: {exc}")
        return {}

    try:
        connection.ping(reconnect=True)
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT config_key, config_value FROM system_config WHERE config_key IN (%s, %s, %s, %s, %s, %s)",
                ("SMTP_SERVER", "SMTP_PORT", "SENDER_EMAIL", "SMTP_USER", "SENDER_PASSWORD", "SMTP_PASS"),
            )
            rows = cursor.fetchall()
    except Exception as exc:
        print(f"⚠️ 查询 system_config 失败: {exc}")
        return {}
    finally:
        connection.close()

    config_map = {row.get("config_key"): row.get("config_value") for row in rows or []}
    return {
        "smtp_server": config_map.get("SMTP_SERVER") or "",
        "smtp_port": _safe_int(config_map.get("SMTP_PORT"), 0),
        "smtp_user": config_map.get("SENDER_EMAIL") or config_map.get("SMTP_USER") or "",
        "smtp_pass": config_map.get("SENDER_PASSWORD") or config_map.get("SMTP_PASS") or "",
    }


def resolve_email_config(args: argparse.Namespace) -> Dict[str, Any]:
    db_config = _fetch_email_config_from_db(args)
    smtp_server = args.smtp_server or db_config.get("smtp_server") or os.getenv("SMTP_SERVER", "smtp.163.com")
    smtp_port = args.smtp_port or db_config.get("smtp_port") or int(os.getenv("SMTP_PORT", "465"))
    smtp_user = args.smtp_user or db_config.get("smtp_user") or os.getenv("SMTP_USER", "")
    smtp_pass = args.smtp_pass or db_config.get("smtp_pass") or os.getenv("SMTP_PASS", "")
    recipient_text = (
        args.email_to
        or os.getenv("NEWS_EMAIL_TO")
        or os.getenv("RECIPIENT_EMAILS")
        or os.getenv("RECIPIENT_EMAIL")
        or os.getenv("EMAIL_TO")
        or ""
    )
    recipients = [email.strip() for email in recipient_text.split(",") if email.strip()]
    return {
        "smtp_server": smtp_server,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_pass": smtp_pass,
        "recipients": recipients,
    }


def get_recipients_by_language(args: argparse.Namespace) -> Dict[str, List[Dict[str, Any]]]:
    recipients = {"zh": [], "en": []}

    # An explicitly supplied recipient list must override database and
    # environment configuration. This is used by one-off Crawlab/CLI runs.
    explicit_recipients = [
        email.strip() for email in str(args.email_to or "").split(",") if email.strip()
    ]
    if explicit_recipients:
        recipients["zh"] = [
            {"email": email, "name": None, "language": "zh"}
            for email in explicit_recipients
        ]
        return recipients

    mysql_cfg = resolve_mysql_config(args)

    try:
        connection = connect_ai_news_mysql(mysql_cfg)
    except Exception as exc:
        print(f"⚠️ 读取邮件收件人失败: {exc}")
        connection = None

    if connection is not None:
        try:
            connection.ping(reconnect=True)
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT email, name, language FROM email_recipients WHERE is_active = TRUE ORDER BY id"
                )
                db_recipients = cursor.fetchall() or []
            if db_recipients:
                for recipient in db_recipients:
                    lang = str(recipient.get("language") or "zh").strip().lower()
                    if lang not in recipients:
                        lang = "zh"
                    recipients[lang].append(
                        {
                            "email": recipient.get("email", ""),
                            "name": recipient.get("name"),
                            "language": lang,
                        }
                    )
                return recipients
        except Exception as exc:
            print(f"⚠️ 查询 email_recipients 失败: {exc}")
        finally:
            connection.close()

    env_recipients = resolve_email_config(args).get("recipients", [])
    for email in env_recipients:
        recipients["zh"].append({"email": email, "name": None, "language": "zh"})
    return recipients


def email_enabled(args: argparse.Namespace) -> bool:
    email_cfg = resolve_email_config(args)
    recipients_by_lang = get_recipients_by_language(args)
    return bool(
        email_cfg["smtp_server"]
        and email_cfg["smtp_port"]
        and email_cfg["smtp_user"]
        and email_cfg["smtp_pass"]
        and any(recipients_by_lang.values())
    )


def _extract_source_label(website: str, link: str) -> str:
    text = (website or "").strip()
    if text:
        if text.startswith("http://") or text.startswith("https://"):
            text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
            text = text.split("/", 1)[0]
        return text

    if link:
        cleaned = re.sub(r"^https?://", "", link, flags=re.IGNORECASE)
        return cleaned.split("/", 1)[0]
    return "未知来源"


def _translate_tags_to_english(tags: str) -> str:
    tag_map = {
        "人工智能": "AI",
        "投资": "Investment",
        "融资": "Funding",
        "并购": "M&A",
        "收购": "Acquisition",
        "私募股权": "Private Equity",
        "风险投资": "Venture Capital",
        "独角兽": "Unicorn",
        "营收": "Revenue",
        "机器人": "Robotics",
    }
    pieces = [piece.strip() for piece in re.split(r"[|,，;；]", tags or "") if piece.strip()]
    translated = [tag_map.get(piece, piece) for piece in pieces]
    return ", ".join(translated)


def _pick_summary(row: Dict[str, Any], language: str) -> str:
    if language == "en":
        return (
            row.get("AI摘要(英文)")
            or row.get("内容摘要(英文)")
            or row.get("AI摘要(中文)")
            or row.get("内容摘要(中文)")
            or ""
        )
    return (
        row.get("AI摘要(中文)")
        or row.get("内容摘要(中文)")
        or row.get("AI摘要(英文)")
        or row.get("内容摘要(英文)")
        or ""
    )


def build_period_name_en(hours: int, subject_suffix: str) -> str:
    if subject_suffix and subject_suffix.isascii():
        return subject_suffix
    if hours == 24:
        return "Daily Report"
    if hours == 168:
        return "Weekly Report"
    if hours == 720:
        return "Monthly Report"
    return f"{hours}-Hour Report"


def generate_core_insight(rows: List[Dict[str, Any]], period_name: str, language: str = "zh") -> str:
    if not rows:
        if language == "en":
            return f"No significant AI M&A news for this {period_name.lower()}."
        return f"本{period_name}暂无重要AI并购资讯。"

    all_content = []
    for row in rows:
        if language == "en":
            content = " ".join(
                [
                    str(row.get("标题(英文)", "")),
                    str(row.get("AI摘要(英文)", "")),
                    str(row.get("内容摘要(英文)", "")),
                    str(row.get("标签", "")),
                ]
            )
        else:
            content = " ".join(
                [
                    str(row.get("标题(中文)", "")),
                    str(row.get("AI摘要(中文)", "")),
                    str(row.get("内容摘要(中文)", "")),
                    str(row.get("标签", "")),
                ]
            )
        all_content.append(content)

    combined_text = " ".join(all_content)
    ai_count = sum(1 for row in rows if "AI领域" in str(row.get("标签", "")))
    mna_count = sum(1 for row in rows if "投融资" in str(row.get("标签", "")))

    if language == "en":
        trend_text = (
            f"This {period_name.lower()} identified {len(rows)} AI M&A related news items, "
            if len(rows) >= 3
            else f"AI M&A activity remains relatively stable this {period_name.lower()}, "
        )
        if mna_count > ai_count:
            trend_text += "mainly focused on investment and acquisition dynamics."
        elif ai_count > mna_count:
            trend_text += "with AI technology development and innovation as the key focus."
        else:
            trend_text += "balancing AI technology development and investment activities."

        first_title = rows[0].get("标题(英文)") or rows[0].get("标题(中文)") or "the latest coverage"
        if any(token in combined_text.lower() for token in ["funding", "investment", "valuation"]):
            trend_text += f" News such as \"{first_title}\" indicates that investment enthusiasm in the AI sector continues, with capital markets maintaining a positive attitude towards AI technology commercialization."
        elif any(token in combined_text.lower() for token in ["acquisition", "merger"]):
            trend_text += f" Reports like \"{first_title}\" show that major tech companies are accelerating AI capability building through M&A, with clear industry consolidation trends."
        else:
            trend_text += f" News represented by \"{first_title}\" reflects the continued penetration and application expansion of AI technology across industries."
        return trend_text

    trend_text = (
        f"本{period_name}共筛选出 {len(rows)} 条AI并购相关资讯，"
        if len(rows) >= 3
        else f"本{period_name}AI并购领域资讯相对平稳，"
    )
    if mna_count > ai_count:
        trend_text += "主要集中在投融资并购动态方面。"
    elif ai_count > mna_count:
        trend_text += "AI技术发展和创新成为关注焦点。"
    else:
        trend_text += "AI技术发展与投融资活动并重。"

    first_title = rows[0].get("标题(中文)") or rows[0].get("标题(英文)") or "最新资讯"
    if any(token in combined_text for token in ["融资", "投资", "估值"]):
        trend_text += f" 从「{first_title}」等资讯可以看出，AI领域的投资热度依然持续，资本市场对AI技术商业化应用保持积极态度。"
    elif any(token in combined_text for token in ["收购", "并购"]):
        trend_text += f" 从「{first_title}」等资讯显示，大型科技公司正通过并购方式加速AI能力建设，行业整合趋势明显。"
    else:
        trend_text += f" 以「{first_title}」为代表的资讯反映了AI技术在各行业的持续渗透和应用拓展。"
    return trend_text


def format_summary_as_points(summary: str) -> List[str]:
    if not summary:
        return []

    sentences = re.split(r"[。\n]", summary.strip())
    points = []
    for sentence in sentences:
        sentence = re.sub(r"^[\d\-•\s]+", "", sentence.strip())
        if sentence and len(sentence) > 10:
            points.append(sentence)
    if len(points) < 2:
        return [summary.strip()]
    return points[:5]


def extract_domain(website: str) -> str:
    if not website:
        return "未知来源"
    domain_map = {
        "TechCrunch": "techcrunch.com",
        "Crunchbase": "crunchbase.com",
        "VentureBeat": "venturebeat.com",
        "TechCrunch News": "techcrunch.com",
    }
    if website in domain_map:
        return domain_map[website]
    if "." in website:
        return website
    return website


def group_rows_by_source(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped_rows: List[Dict[str, Any]] = []
    source_order: List[str] = []
    buckets: Dict[str, List[Dict[str, Any]]] = {}

    for row in rows:
        source = extract_domain(str(row.get("网站", "")))
        if source not in buckets:
            buckets[source] = []
            source_order.append(source)
        buckets[source].append(row)

    for source in source_order:
        grouped_rows.extend(buckets[source])
    return grouped_rows


def markdown_to_html(markdown_content: str) -> str:
    try:
        import markdown

        html = markdown.markdown(markdown_content, extensions=["tables"])
        css = """
        <style>
        body {
            font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
            font-size: 14px;
            line-height: 1.6;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            color: #333;
            background-color: #fff;
        }
        h1 {
            font-size: 20px;
            font-weight: 600;
            color: #2c3e50;
            text-align: center;
            margin: 0 0 25px 0;
            padding: 15px 0;
            border-bottom: 2px solid #3498db;
        }
        h2 {
            font-size: 16px;
            font-weight: 600;
            color: #34495e;
            margin: 25px 0 15px 0;
            padding: 5px 0 5px 10px;
            border-left: 3px solid #3498db;
            background: #f8f9fa;
        }
        h3 {
            font-size: 15px;
            font-weight: 600;
            color: #2980b9;
            margin: 20px 0 8px 0;
        }
        p {
            font-size: 14px;
            margin: 8px 0;
            line-height: 1.5;
        }
        ul {
            padding-left: 20px;
            margin: 10px 0;
        }
        li {
            font-size: 14px;
            margin-bottom: 6px;
            line-height: 1.5;
        }
        a {
            color: #3498db;
            text-decoration: none;
        }
        a:hover {
            text-decoration: underline;
        }
        hr {
            border: none;
            border-top: 1px solid #ddd;
            margin: 20px 0;
        }
        em {
            font-size: 13px;
            color: #7f8c8d;
            font-style: italic;
        }
        strong {
            color: #2c3e50;
            font-weight: 600;
        }
        @media (max-width: 600px) {
            body { padding: 15px; font-size: 13px; }
            h1 { font-size: 18px; }
            h2 { font-size: 15px; }
            h3 { font-size: 14px; }
        }
        </style>
        """
        return f"<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'>{css}</head><body>{html}</body></html>"
    except ImportError:
        html = markdown_content.replace("\n\n", "</p><p>")
        html = html.replace("\n", "<br>")
        html = re.sub(r"# (.*?)(?=</p>|<br>|$)", r"<h1>\1</h1>", html)
        html = re.sub(r"## (.*?)(?=</p>|<br>|$)", r"<h2>\1</h2>", html)
        html = re.sub(r"### (.*?)(?=</p>|<br>|$)", r"<h3>\1</h3>", html)
        html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", html)
        html = re.sub(r"\*(.*?)\*", r"<em>\1</em>", html)
        html = re.sub(r"\[(.*?)\]\((.*?)\)", r"<a href=\"\2\">\1</a>", html)
        return f"<html><head><meta charset='utf-8'></head><body><p>{html}</p></body></html>"


def build_mna_digest_list_markdown(rows: List[Dict[str, Any]], period_name: str, language: str = "zh") -> str:
    if not rows:
        return f"No AI M&A related news for this {period_name.lower()}." if language == "en" else f"本{period_name}暂无AI领域并购相关资讯。"

    today = datetime.now().strftime("%Y-%m-%d")
    content: List[str] = []

    if language == "en":
        content.append(f"# AI M&A Intelligence Monitor {period_name} - {today}")
        content.append("")
        content.append(f"*{len(rows)} AI/Investment/M&A related news items*")
        content.append("")
        content.append("## Key Insights")
        content.append("")
        content.append(generate_core_insight(rows, period_name, language))
        content.append("")
        content.append("---")
        content.append("")
        content.append("## AI M&A News Digest")
        content.append("")
        for index, row in enumerate(rows, 1):
            title = row.get("标题(英文)") or row.get("标题(中文)") or "Untitled News"
            link = row.get("链接", "")
            source_domain = extract_domain(str(row.get("网站", "")))
            pub_date = row.get("发布日期", "")
            translated_tags = _translate_tags_to_english(str(row.get("标签", "")))
            summary = row.get("AI摘要(英文)") or row.get("内容摘要(英文)") or row.get("AI摘要(中文)") or row.get("内容摘要(中文)")

            content.append(f"### {index}. {title}")
            content.append("")
            info_parts = [f"**Source**: {source_domain}", f"**Date**: {pub_date}"]
            if translated_tags:
                info_parts.append(f"**Category**: {translated_tags}")
            content.append(" | ".join(info_parts))
            content.append("")
            content.append(f"**Link**: [Read More]({link})")
            content.append("")
            if summary:
                content.append("**Summary:**")
                content.append("")
                for point in format_summary_as_points(str(summary)):
                    content.append(f"- {point}")
            content.append("")
        websites = sorted({extract_domain(str(row.get("网站", ""))) for row in rows if row.get("网站")})
        content.append("---")
        content.append("")
        content.append("**Report Information**")
        content.append("")
        content.append(f"- **Data Sources**: {' | '.join(websites)}")
        content.append(f"- **Generated**: {today}")
        content.append(f"- **Total Items**: {len(rows)} AI M&A news")
        return "\n".join(content)

    content.append(f"# AI领域并购资讯监控{period_name} - {today}")
    content.append("")
    content.append(f"*共{len(rows)}条AI/投资/并购相关新闻*")
    content.append("")
    content.append("## 核心洞察")
    content.append("")
    content.append(generate_core_insight(rows, period_name, language))
    content.append("")
    content.append("---")
    content.append("")
    content.append("## AI并购新闻摘要")
    content.append("")
    for index, row in enumerate(rows, 1):
        title = row.get("标题(中文)") or row.get("标题(英文)") or "未命名新闻"
        link = row.get("链接", "")
        source_domain = extract_domain(str(row.get("网站", "")))
        pub_date = row.get("发布日期", "")
        tags = str(row.get("标签", ""))
        summary = row.get("AI摘要(中文)") or row.get("内容摘要(中文)") or row.get("AI摘要(英文)") or row.get("内容摘要(英文)")

        content.append(f"### {index}. {title}")
        content.append("")
        info_parts = [f"**来源**: {source_domain}", f"**日期**: {pub_date}"]
        if tags:
            info_parts.append(f"**分类**: {tags}")
        content.append(" | ".join(info_parts))
        content.append("")
        content.append(f"**链接**: [查看原文]({link})")
        content.append("")
        if summary:
            content.append("**摘要:**")
            content.append("")
            for point in format_summary_as_points(str(summary)):
                content.append(f"- {point}")
        content.append("")
    websites = sorted({extract_domain(str(row.get("网站", ""))) for row in rows if row.get("网站")})
    content.append("---")
    content.append("")
    content.append("**报告信息**")
    content.append("")
    content.append(f"- **数据来源**: {' | '.join(websites)}")
    content.append(f"- **生成时间**: {today}")
    content.append(f"- **总条数**: {len(rows)} 条AI并购新闻")
    return "\n".join(content)


def build_email_html(rows: List[Dict[str, Any]], period_name: str, language: str = "zh") -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    if language == "en":
        header_title = f"AI M&A Intelligence Monitor {period_name}"
        meta_text = f"Generated: {today} | News Count: {len(rows)}"
        section_title = "News Digest"
        source_label = "Source"
        date_label = "Date"
        tag_label = "Tags"
        footer_text = "This email was generated by the standalone news email report task."
    else:
        header_title = f"AI领域并购资讯监控{period_name}"
        meta_text = f"生成日期：{today} | 新闻数量：{len(rows)} 条"
        section_title = "新闻列表"
        source_label = "来源"
        date_label = "日期"
        tag_label = "标签"
        footer_text = "本邮件由 news 工程独立脚本生成发送。"

    sections = [
        "<html><head><meta charset='utf-8'>",
        "<style>",
        "body{font-family:'Microsoft YaHei','Segoe UI',Arial,sans-serif;font-size:14px;line-height:1.65;color:#222;margin:0;padding:24px;background:#f5f7fa;}",
        ".container{max-width:880px;margin:0 auto;background:#fff;padding:28px 32px;border-radius:14px;box-shadow:0 8px 24px rgba(15,23,42,.08);}",
        "h1{font-size:24px;margin:0 0 8px;color:#102a43;} h2{font-size:18px;margin:28px 0 12px;color:#0b7285;}",
        ".meta{color:#52606d;margin-bottom:20px;} .item{padding:18px 0;border-top:1px solid #e9ecef;} .item:first-of-type{border-top:none;}",
        ".title{font-size:17px;font-weight:700;color:#1f2933;margin-bottom:8px;} .info{color:#52606d;font-size:13px;margin-bottom:8px;}",
        ".summary{margin:10px 0 0;color:#243b53;} a{color:#0b7285;text-decoration:none;} a:hover{text-decoration:underline;}",
        ".footer{margin-top:24px;padding-top:16px;border-top:1px solid #e9ecef;color:#7b8794;font-size:12px;}",
        "</style></head><body><div class='container'>",
        f"<h1>{header_title}</h1>",
        f"<div class='meta'>{meta_text}</div>",
        f"<h2>{section_title}</h2>",
    ]

    for index, row in enumerate(rows, start=1):
        title = row.get("标题(英文)") if language == "en" else row.get("标题(中文)")
        fallback_title = row.get("标题(中文)") if language == "en" else row.get("标题(英文)")
        title = title or fallback_title or ("Untitled News" if language == "en" else "未命名新闻")
        title = str(title).strip()
        link = str(row.get("链接") or "").strip()
        website = _extract_source_label(str(row.get("网站") or ""), link)
        publish_date = str(row.get("发布日期") or "").strip() or "未知日期"
        if language == "en":
            publish_date = publish_date or "Unknown Date"
        tags = str(row.get("标签") or "").strip()
        if language == "en":
            tags = _translate_tags_to_english(tags)
        tags = tags or ("Uncategorized" if language == "en" else "未分类")
        summary = str(_pick_summary(row, language)).strip() or ("No summary available" if language == "en" else "无摘要")

        sections.append("<div class='item'>")
        if link:
            sections.append(f"<div class='title'>{index}. <a href='{link}' target='_blank' rel='noopener noreferrer'>{title}</a></div>")
        else:
            sections.append(f"<div class='title'>{index}. {title}</div>")
        sections.append(f"<div class='info'>{source_label}: {website} | {date_label}: {publish_date} | {tag_label}: {tags}</div>")
        sections.append(f"<div class='summary'>{summary}</div>")
        sections.append("</div>")

    sections.append(f"<div class='footer'>{footer_text}</div>")
    sections.append("</div></body></html>")
    return "".join(sections)


def send_digest_email(rows: List[Dict[str, Any]], period_name: str, args: argparse.Namespace) -> bool:
    email_cfg = resolve_email_config(args)
    recipients_by_lang = get_recipients_by_language(args)
    content_hash = hashlib.md5(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    today = datetime.now().strftime("%Y-%m-%d")
    success_count = 0
    total_count = 0

    print(f"\n=== 准备发送邮件报告 ===")
    print(f"📊 新闻数量: {len(rows)} 条")
    print(f"🔒 内容哈希: {content_hash[:8]}")

    for lang, recipients in recipients_by_lang.items():
        if not recipients:
            continue

        total_count += len(recipients)
        recipient_emails = [recipient["email"] for recipient in recipients if recipient.get("email")]
        if not recipient_emails:
            continue

        if lang == "en":
            lang_period_name = build_period_name_en(args.hours, args.subject_suffix)
            subject = f"AI M&A Intelligence Monitor {lang_period_name} - {today}"
            markdown_content = build_mna_digest_list_markdown(rows, lang_period_name, language="en")
        else:
            subject = f"AI领域并购资讯监控{period_name} - {today}"
            markdown_content = build_mna_digest_list_markdown(rows, period_name, language="zh")
        html_content = markdown_to_html(markdown_content)

        message = MIMEMultipart()
        message["From"] = email_cfg["smtp_user"]
        message["To"] = ", ".join(recipient_emails)
        message["Subject"] = subject
        message.attach(MIMEText(html_content, "html", "utf-8"))

        print(f"\n--- 发送{lang.upper()}版本邮件 ---")
        print(f"📨 收件人 ({len(recipient_emails)}人): {', '.join(recipient_emails)}")
        print(f"📧 主题: {subject}")
        print(f"🌐 SMTP服务器: {email_cfg['smtp_server']}:{email_cfg['smtp_port']}")

        try:
            if email_cfg["smtp_port"] == 465:
                with smtplib.SMTP_SSL(email_cfg["smtp_server"], email_cfg["smtp_port"], timeout=30) as server:
                    server.login(email_cfg["smtp_user"], email_cfg["smtp_pass"])
                    result = server.send_message(message)
            else:
                with smtplib.SMTP(email_cfg["smtp_server"], email_cfg["smtp_port"], timeout=30) as server:
                    if email_cfg["smtp_port"] == 587:
                        server.starttls()
                    server.login(email_cfg["smtp_user"], email_cfg["smtp_pass"])
                    result = server.send_message(message)

            if result:
                print(f"⚠️ {lang.upper()}版本部分发送失败: {result}")
            else:
                print(f"✅ {lang.upper()}版本邮件发送成功")
                success_count += len(recipient_emails)
        except Exception as exc:
            print(f"❌ {lang.upper()}版本邮件发送异常: {exc}")

    print(f"\n=== 邮件发送总结 ===")
    print(f"✅ 成功发送: {success_count}/{total_count} 人")
    return success_count > 0


def resolve_mysql_config(args: argparse.Namespace) -> Dict[str, Any]:
    mysql_cfg = dict(AI_NEWS_MYSQL or {})
    if args.db_host:
        mysql_cfg["host"] = args.db_host
    if args.db_port:
        mysql_cfg["port"] = args.db_port
    if args.db_user:
        mysql_cfg["user"] = args.db_user
    if args.db_password:
        mysql_cfg["password"] = args.db_password
    if args.db_name:
        mysql_cfg["db"] = args.db_name
    if args.db_table:
        mysql_cfg["table"] = args.db_table

    host = mysql_cfg.get("host") or os.getenv("MYSQL_HOST") or "localhost"
    if host == "host.docker.internal":
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            host = os.getenv("MYSQL_HOST") or "localhost"

    table_name = mysql_cfg.get("table", "ai_news")
    if not re.match(r"^[A-Za-z0-9_]+$", str(table_name)):
        table_name = "ai_news"

    mysql_cfg["host"] = host
    mysql_cfg["table"] = table_name
    mysql_cfg["port"] = int(mysql_cfg.get("port", 3306))
    mysql_cfg["charset"] = mysql_cfg.get("charset", "utf8mb4")
    return mysql_cfg


def build_period_name(hours: int, subject_suffix: str) -> str:
    if subject_suffix:
        return subject_suffix
    if hours == 24:
        return "日报"
    if hours == 168:
        return "周报"
    if hours == 720:
        return "月报"
    return f"{hours}小时报告"


def connect_ai_news_mysql(mysql_cfg: Dict[str, Any]):
    if pymysql is None:
        raise RuntimeError(
            f"当前解释器未安装 PyMySQL: python={sys.executable}. "
            "news 项目现有写库逻辑使用的也是 PyMySQL，请确认 Crawlab 任务节点已安装项目依赖。"
        )

    return pymysql.connect(
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


def fetch_invest_mna_news(args: argparse.Namespace) -> List[Dict[str, Any]]:
    mysql_cfg = resolve_mysql_config(args)
    daily_website_filter = ""
    query_params: List[Any] = [args.hours // 24]
    if args.hours == 24:
        placeholders = ", ".join(["%s"] * len(DAILY_WEBSITE_ALLOWLIST))
        daily_website_filter = f"AND TRIM(website) IN ({placeholders})"
        query_params.extend(DAILY_WEBSITE_ALLOWLIST)

    query = f"""
    SELECT
        title_en,
        title_zh,
        url,
        website,
        publish_date,
        author,
        tags,
        content_summary_en,
        content_summary_zh,
        ai_summary_en,
        ai_summary_zh
    FROM `{mysql_cfg['table']}`
    WHERE publish_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
      AND {INVEST_MNA_TAG_WHERE}
      {daily_website_filter}
    ORDER BY publish_date DESC
    LIMIT %s
    """
    query_params.append(args.limit)

    cutoff_date = f"CURDATE() - {args.hours // 24} day(s)"
    print(
        "连接 ai_news 数据库: "
        f"host={mysql_cfg.get('host')} port={mysql_cfg.get('port')} "
        f"db={mysql_cfg.get('db')} table={mysql_cfg.get('table')}"
    )
    print(f"publish_date 筛选: >= {cutoff_date} (hours={args.hours} → {args.hours // 24} 天)")
    if daily_website_filter:
        print(f"日报 website 白名单 ({len(DAILY_WEBSITE_ALLOWLIST)}): {' | '.join(DAILY_WEBSITE_ALLOWLIST)}")

    connection = connect_ai_news_mysql(mysql_cfg)
    try:
        connection.ping(reconnect=True)
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(query, query_params)
            results = cursor.fetchall()
    finally:
        connection.close()

    converted_results: List[Dict[str, Any]] = []
    for row in results:
        converted_results.append(
            {
                "标题(英文)": row.get("title_en", "") or "",
                "标题(中文)": row.get("title_zh", "") or "",
                "链接": row.get("url", "") or "",
                "网站": row.get("website", "") or "",
                "发布日期": str(row.get("publish_date", "")) if row.get("publish_date") else "",
                "作者": row.get("author", "") or "",
                "标签": row.get("tags", "") or "",
                "内容摘要(英文)": row.get("content_summary_en", "") or "",
                "内容摘要(中文)": row.get("content_summary_zh", "") or "",
                "AI摘要(英文)": row.get("ai_summary_en", "") or "",
                "AI摘要(中文)": row.get("ai_summary_zh", "") or "",
            }
        )
    return converted_results


def run_email_report(args: argparse.Namespace) -> int:
    print(
        "任务参数: "
        f"hours={args.hours} limit={args.limit} "
        f"subject_suffix={args.subject_suffix or '(auto)'} dry_run={args.dry_run}"
    )

    if args.hours <= 0:
        print("❌ --hours 必须大于 0")
        return 1
    if args.limit <= 0:
        print("❌ --limit 必须大于 0")
        return 1

    try:
        invest_mna_news = fetch_invest_mna_news(args)
    except Exception as exc:
        print(f"❌ 读取 ai_news 数据失败: {exc}")
        return 1

    print(f"投资/并购相关新闻: {len(invest_mna_news)} 条")
    if not invest_mna_news:
        print("⚠️ 没有可发送的新闻，跳过邮件发送")
        return 0

    invest_mna_news = group_rows_by_source(invest_mna_news)
    ordered_sources = []
    for row in invest_mna_news:
        source = extract_domain(str(row.get("网站", "")))
        if source not in ordered_sources:
            ordered_sources.append(source)
    print(f"来源分组顺序: {' | '.join(ordered_sources)}")

    period_name = build_period_name(args.hours, args.subject_suffix)
    print(f"邮件周期名称: {period_name}")

    if args.dry_run:
        print("🧪 dry-run 模式，不发送邮件")
        return 0

    if not email_enabled(args):
        print("❌ 邮件功能未配置，无法发送邮件")
        print(
            "需要设置 SMTP_SERVER / SMTP_PORT / SMTP_USER / SMTP_PASS / NEWS_EMAIL_TO；"
            "Crawlab 任务还可选配 EMAIL_REPORT_HOURS / EMAIL_REPORT_LIMIT / EMAIL_REPORT_SUBJECT_SUFFIX / EMAIL_REPORT_DRY_RUN"
        )
        return 1

    success = send_digest_email(invest_mna_news, period_name, args)
    if success:
        print("✅ 邮件报告发送成功")
        return 0

    print("❌ 邮件报告发送失败")
    return 1


def main() -> int:
    args = parse_args()
    return run_email_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
