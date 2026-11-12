#!/usr/bin/env python3
"""Backfill missing bilingual ai_news titles. Defaults to dry-run."""

import argparse
import re

import pymysql
from pymysql.cursors import DictCursor

from azure_ai import translate_text_azure
from news.settings import AI_NEWS_MYSQL


def usable_translation(value: str, target_lang: str) -> bool:
    text = str(value or "").strip()
    chinese_count = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    return latin_count >= max(4, chinese_count) if target_lang == "en" else chinese_count > 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="回填 ai_news 缺失的中英文标题（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="实际更新数据库")
    parser.add_argument("--limit", type=int, default=500, help="单次最多处理条数")
    parser.add_argument("--direction", choices=("both", "zh-to-en", "en-to-zh"), default="both")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    limit = max(1, min(args.limit, 5000))
    conditions = []
    if args.direction in {"both", "zh-to-en"}:
        conditions.append("(COALESCE(title_en,'')='' AND COALESCE(title_zh,'')<>'')")
    if args.direction in {"both", "en-to-zh"}:
        conditions.append("(COALESCE(title_zh,'')='' AND COALESCE(title_en,'')<>'')")
    config = dict(AI_NEWS_MYSQL)
    table_name = str(config.get("table") or "ai_news")
    if not re.fullmatch(r"[A-Za-z0-9_]+", table_name):
        raise SystemExit("AI_NEWS_MYSQL.table 非法")
    connection = pymysql.connect(
        host=config.get("host"), port=int(config.get("port", 3306)), user=config.get("user"),
        password=config.get("password"), database=config.get("db"), charset=config.get("charset", "utf8mb4"),
        cursorclass=DictCursor, autocommit=False, connect_timeout=10, read_timeout=30, write_timeout=30,
    )
    stats = {"selected": 0, "translated": 0, "skipped": 0, "failed": 0}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id,title_zh,title_en FROM `{table_name}` WHERE {' OR '.join(conditions)} ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            rows = list(cursor.fetchall())
            stats["selected"] = len(rows)
            for row in rows:
                try:
                    title_zh = str(row.get("title_zh") or "").strip()
                    title_en = str(row.get("title_en") or "").strip()
                    if not title_en and title_zh and args.direction in {"both", "zh-to-en"}:
                        translated = translate_text_azure(title_zh, target_lang="en")
                        if usable_translation(translated, "en"):
                            title_en = translated.strip()[:500]
                    if not title_zh and title_en and args.direction in {"both", "en-to-zh"}:
                        translated = translate_text_azure(title_en, target_lang="zh")
                        if usable_translation(translated, "zh"):
                            title_zh = translated.strip()[:500]
                    if title_zh == str(row.get("title_zh") or "").strip() and title_en == str(row.get("title_en") or "").strip():
                        stats["skipped"] += 1
                        continue
                    if args.apply:
                        cursor.execute(
                            f"UPDATE `{table_name}` SET title_zh=%s,title_en=%s,updated_at=NOW() WHERE id=%s",
                            (title_zh, title_en, row["id"]),
                        )
                    else:
                        print(f"[dry-run] id={row['id']} title_zh={title_zh!r} title_en={title_en!r}")
                    stats["translated"] += 1
                except Exception as exc:
                    stats["failed"] += 1
                    print(f"id={row.get('id')} translation failed: {exc}")
            if args.apply:
                connection.commit()
            else:
                connection.rollback()
    finally:
        connection.close()
    print(stats)
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
