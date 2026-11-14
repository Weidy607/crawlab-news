#!/usr/bin/env python3
"""Backfill ingestion labels; report generation itself never invokes AI."""
import argparse
import json

import send_email_report as original
from news_tagging import (
    FIELD_MAP, METADATA_COLUMN, classify_news_for_storage, ensure_metadata_column,
    has_metadata_column, read_stored_classification, report_row,
)
from output_paths import report_output_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="补全所有来源的AI语义标签及混合汇总日报条目；默认只读盘点")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=0, help="待补标数量上限，0不设限")
    parser.add_argument("--force", action="store_true", help="重新分类已有有效新标签的记录")
    parser.add_argument("--execute", action="store_true", help="调用AI并写入数据库；不会发送邮件")
    parser.add_argument("--output", help="审核JSON路径，默认根output/下")
    for name in ("db-host", "db-user", "db-password", "db-name", "db-table"):
        parser.add_argument("--" + name, default="")
    parser.add_argument("--db-port", type=int, default=0)
    return parser.parse_args(argv)


def fetch_candidates(connection, table, args):
    metadata = METADATA_COLUMN if has_metadata_column(connection, table) else f"NULL AS {METADATA_COLUMN}"
    with connection.cursor(original.pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            f"SELECT id, {', '.join(FIELD_MAP)}, {metadata} FROM `{table}` "
            "WHERE publish_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
            "AND publish_date < DATE_ADD(CURDATE(), INTERVAL 1 DAY) ORDER BY publish_date DESC, id",
            (args.hours // 24,),
        )
        rows = cursor.fetchall()
    candidates = []
    for row in rows:
        if not args.force:
            try:
                read_stored_classification(dict(report_row(row), **{METADATA_COLUMN: row.get(METADATA_COLUMN)}))
                continue
            except (ValueError, TypeError, KeyError):
                pass
        candidates.append(row)
    return candidates[:args.limit] if args.limit else candidates


def save_labels(connection, table, row, tags, metadata):
    # Compare every source field and previous label payload, not just a timestamp.
    # A parallel crawl must never receive classification for an older article body.
    compared = list(FIELD_MAP) + [METADATA_COLUMN]
    where = " AND ".join(f"BINARY COALESCE(`{field}`, '') = BINARY %s" for field in compared)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE `{table}` SET tags=%s, `{METADATA_COLUMN}`=%s, updated_at=NOW() WHERE id=%s AND {where}",
            (tags, json.dumps(metadata, ensure_ascii=False), row["id"],
             *(str(row.get(field) or "") for field in compared)),
        )
        changed = cursor.rowcount == 1
    connection.commit()
    return changed


def run(args):
    if args.hours <= 0 or args.limit < 0:
        raise ValueError("hours须大于0，limit不得小于0")
    config = original.resolve_mysql_config(args)
    connection = original.connect_ai_news_mysql(config)
    audit = []
    output = report_output_path("news_tag_backfill_review.json", args.output)
    try:
        rows = fetch_candidates(connection, config["table"], args)
        print(f"待补标: {len(rows)} 条；" + ("执行AI分类并写库，不发送邮件" if args.execute else "只读盘点，不调用AI、不写数据库"), flush=True)
        if args.execute:
            ensure_metadata_column(connection, config["table"])
        for index, row in enumerate(rows, 1):
            item = {"id": row["id"], "title": row.get("title_zh") or row.get("title_en"), "url": row.get("url"), "old_tags": row.get("tags")}
            if args.execute:
                tags, metadata = classify_news_for_storage(report_row(row))
                saved = save_labels(connection, config["table"], row, tags, metadata)
                item.update(tags=tags, metadata=metadata, saved=saved)
                print(f"{index}/{len(rows)} id={row['id']} status={metadata['status']} saved={saved}", flush=True)
            audit.append(item)
            output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        if not rows:
            output.write_text("[]", encoding="utf-8")
    finally:
        connection.close()
    print(f"审核结果: {output}")
    return 1 if any(not item.get("saved") or item.get("metadata", {}).get("status") != "ok" for item in audit) and args.execute else 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
