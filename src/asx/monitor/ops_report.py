"""Weekly operations one-pager (SPEC §13). Generated automatically — if
producing it required manual work, the monitoring would be incomplete."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg

from asx.monitor.checks import run_monitor


def ops_report(conn: psycopg.Connection, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    lines = [f"ASX platform operations report — {now.date().isoformat()}", ""]

    alarms = run_monitor(conn, now)
    lines.append(f"ALARMS: {len(alarms)}")
    for a in alarms:
        lines.append(f"  [{a.check}] {a.detail}")
    lines.append("")

    with conn.cursor() as cur:
        cur.execute(
            """SELECT coalesce(doc_class, '(unclassified)') AS doc_class, count(*) AS n
               FROM documents WHERE fetched_at >= %s
               GROUP BY 1 ORDER BY n DESC""",
            (week_ago,),
        )
        rows = cur.fetchall()
        lines.append(f"DOCUMENTS THIS WEEK: {sum(r['n'] for r in rows)}")
        for r in rows:
            lines.append(f"  {r['doc_class']:20s} {r['n']}")
        lines.append("")

        cur.execute(
            """SELECT parse_status, count(*) AS n FROM documents
               GROUP BY 1 ORDER BY 1"""
        )
        lines.append("PARSE STATUS (all time):")
        for r in cur.fetchall():
            lines.append(f"  {r['parse_status']:16s} {r['n']}")
        lines.append("")

        cur.execute(
            """SELECT count(*) AS open, min(created_at) AS oldest
               FROM review_items WHERE resolved_at IS NULL"""
        )
        r = cur.fetchone()
        oldest = r["oldest"].date().isoformat() if r["oldest"] else "-"
        lines.append(f"REVIEW QUEUE: {r['open']} open, oldest {oldest}")

        cur.execute(
            """SELECT parser_name,
                      count(*) AS parses,
                      count(*) FILTER (WHERE confidence >= 0.9 AND passes_agree) AS auto_ok
               FROM parsed_records WHERE created_at >= %s
               GROUP BY 1""",
            (week_ago,),
        )
        rows = cur.fetchall()
        if rows:
            lines.append("")
            lines.append("PARSER AUTO-ACCEPT RATES (7d):")
            for r in rows:
                rate = r["auto_ok"] / r["parses"] if r["parses"] else 0.0
                lines.append(f"  {r['parser_name']:12s} {r['auto_ok']}/{r['parses']} ({rate:.0%})")

        cur.execute(
            """SELECT count(*) AS checks,
                      count(*) FILTER (WHERE within_tolerance IS FALSE) AS exceptions
               FROM share_reconciliations WHERE checked_at >= %s""",
            (week_ago,),
        )
        r = cur.fetchone()
        lines.append("")
        # Total checks are reported so a dead reconciliation job (0 checks)
        # never reads the same as a clean week (Invariant 7).
        lines.append(f"RECONCILIATIONS (7d): {r['checks']} checks, {r['exceptions']} exceptions")

    return "\n".join(lines)
