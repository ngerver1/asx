"""Review queue access — the simplest possible UI is this CLI plus SQL views
(SPEC §6); do not gold-plate it."""

from __future__ import annotations

import json

import psycopg


def list_open(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT item_id, kind, doc_id, reason, created_at
               FROM review_items WHERE resolved_at IS NULL
               ORDER BY created_at LIMIT %s""",
            (limit,),
        )
        return cur.fetchall()


def show(conn: psycopg.Connection, item_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review_items WHERE item_id = %s", (item_id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"review item {item_id} not found")
    return row


def format_item(item: dict) -> str:
    lines = [
        f"item {item['item_id']} [{item['kind']}] created {item['created_at']}",
        f"doc: {item['doc_id']}",
        f"reason: {item['reason']}",
    ]
    if item.get("payload"):
        lines.append(json.dumps(item["payload"], indent=2, default=str))
    return "\n".join(lines)
