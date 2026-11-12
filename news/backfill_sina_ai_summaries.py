#!/usr/bin/env python3
"""Safely replace copied summaries with genuine AI summaries.

Rows where either AI summary exactly equals its content summary are eligible.
The old value is kept unless generation succeeds; updates use an optimistic
WHERE condition so concurrently modified rows are not overwritten.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
from typing import Any, Dict, List

import pymysql

from azure_ai import summarize_text_zh, translate_text_azure
from news.settings import AI_NEWS_MYSQL


def parse_args():
    parser = argparse.ArgumentParser(description="分批回填所有来源中被复制的AI摘要")
    parser.add_argument("--batch-size", type=int, default=20, help="本批最多处理数量")
    parser.add_argument("--after-id", type=int, default=0, help="只处理大于该ID的记录")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="调用AI并实际写数据库；默认只列出候选记录，不调用AI",
    )
    parser.add_argument("--db-host", default=os.getenv("MYSQL_HOST", ""))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("MYSQL_PORT", "0")))
    parser.add_argument("--db-user", default=os.getenv("MYSQL_USER", ""))
    parser.add_argument("--db-password", default=os.getenv("MYSQL_PASSWORD", ""))
    parser.add_argument("--db-name", default=os.getenv("MYSQL_DB", ""))
    parser.add_argument("--db-table", default=os.getenv("MYSQL_TABLE", ""))
    return parser.parse_args()


def db_config(args) -> Dict[str, Any]:
    configured = dict(AI_NEWS_MYSQL or {})
    host = args.db_host or configured.get("host", "localhost")
    if host == "host.docker.internal":
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            host = "localhost"
    table = args.db_table or configured.get("table", "ai_news")
    if not re.fullmatch(r"[A-Za-z0-9_]+", str(table)):
        raise ValueError("数据库表名不合法")
    return {
        "host": host,
        "port": args.db_port or int(configured.get("port", 3306)),
        "user": args.db_user or configured.get("user", ""),
        "password": args.db_password or configured.get("password", ""),
        "database": args.db_name or configured.get("db", ""),
        "charset": configured.get("charset", "utf8mb4"),
        "table": table,
    }


def clean_source_content(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    patterns = (
        r"^图左[：:].{0,100}?[；;，,]\s*图右[：:].{0,100}?[。.!！]\s*",
        r"^(?:图片|图像|照片)(?:来源|说明|注)[：:].{0,160}?[。.!！]\s*",
        r"^(?:资料图|配图|示意图)(?:[：:].{0,120}?)?[。.!！]?\s*",
    )
    for pattern in patterns:
        text = re.sub(pattern, "", text, count=1, flags=re.I)
    return text


def is_copied_summary(ai_value: Any, content_value: Any) -> bool:
    ai_text = str(ai_value or "").strip()
    content_text = str(content_value or "").strip()
    return bool(ai_text) and ai_text == content_text


def fetch_candidates(connection, table: str, after_id: int, batch_size: int) -> List[Dict[str, Any]]:
    sql = f"""
        SELECT id, website, title_zh, title_en, content_full,
               content_summary_zh, content_summary_en,
               ai_summary_zh, ai_summary_en, url
        FROM `{table}`
        WHERE id > %s
          AND (
              (TRIM(COALESCE(ai_summary_zh, '')) <> ''
               AND TRIM(ai_summary_zh) = TRIM(COALESCE(content_summary_zh, '')))
              OR
              (TRIM(COALESCE(ai_summary_en, '')) <> ''
               AND TRIM(ai_summary_en) = TRIM(COALESCE(content_summary_en, '')))
          )
        ORDER BY id
        LIMIT %s
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, (after_id, max(1, batch_size)))
        return cursor.fetchall() or []


def update_summary(connection, table: str, row: Dict[str, Any], summary_zh: str, summary_en: str) -> bool:
    sql = f"""
        UPDATE `{table}`
        SET ai_summary_zh=%s, ai_summary_en=%s, updated_at=NOW()
        WHERE id=%s
          AND COALESCE(ai_summary_zh, '')=%s
          AND COALESCE(ai_summary_en, '')=%s
    """
    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                summary_zh,
                summary_en,
                row["id"],
                str(row.get("ai_summary_zh") or ""),
                str(row.get("ai_summary_en") or ""),
            ),
        )
        return cursor.rowcount == 1


def main() -> int:
    args = parse_args()
    cfg = db_config(args)
    table = cfg.pop("table")
    connection = pymysql.connect(
        **cfg,
        autocommit=True,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        rows = fetch_candidates(connection, table, args.after_id, args.batch_size)
        print(f"[backfill] candidates={len(rows)} execute={args.execute}")
        updated = failed = unchanged = 0
        failed_ids = []
        for row in rows:
            title = str(row.get("title_zh") or row.get("title_en") or "")
            zh_copied = is_copied_summary(
                row.get("ai_summary_zh"), row.get("content_summary_zh")
            )
            en_copied = is_copied_summary(
                row.get("ai_summary_en"), row.get("content_summary_en")
            )
            if not args.execute:
                print(
                    f"[candidate] id={row['id']} website={row.get('website', '')!r} "
                    f"copied_zh={zh_copied} copied_en={en_copied} title={title[:60]!r}"
                )
                continue

            summary_zh = "" if zh_copied else str(row.get("ai_summary_zh") or "").strip()
            if not summary_zh:
                source = clean_source_content(row.get("content_full") or "")
                if not source:
                    failed += 1
                    failed_ids.append(row["id"])
                    print(f"[skip] id={row['id']} empty cleaned source")
                    continue
                try:
                    summary_zh = summarize_text_zh(source, max_length=300).strip()
                except Exception as exc:
                    failed += 1
                    failed_ids.append(row["id"])
                    print(f"[skip] id={row['id']} AI error={exc!r}; old summaries preserved")
                    continue
            if not summary_zh:
                failed += 1
                failed_ids.append(row["id"])
                print(f"[skip] id={row['id']} AI returned empty; old summaries preserved")
                continue
            try:
                summary_en = translate_text_azure(summary_zh, target_lang="en").strip()
            except Exception as exc:
                failed += 1
                failed_ids.append(row["id"])
                print(f"[skip] id={row['id']} English translation error={exc!r}; old summaries preserved")
                continue
            if not summary_en:
                failed += 1
                failed_ids.append(row["id"])
                print(f"[skip] id={row['id']} English translation empty; old summaries preserved")
                continue
            print(
                f"[ready] id={row['id']} copied_zh={zh_copied} copied_en={en_copied} "
                f"title={title[:60]!r} zh={summary_zh[:90]!r}"
            )
            if update_summary(connection, table, row, summary_zh, summary_en):
                updated += 1
            else:
                unchanged += 1
                print(f"[skip] id={row['id']} row changed concurrently; not overwritten")
        last_id = rows[-1]["id"] if rows else args.after_id
        print(
            f"[done] inspected={len(rows)} updated={updated} failed={failed} "
            f"unchanged={unchanged} last_id={last_id}"
        )
        if failed_ids:
            print(f"[retry] failed_ids={','.join(map(str, failed_ids))}")
        if rows:
            next_after_id = args.after_id if failed_ids else last_id
            print(f"[next] --after-id {next_after_id}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
