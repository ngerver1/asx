"""Standing data-quality checks (SPEC §13, Invariant 7).

Silence is an alarm: zero lodgements in a window is treated as a probable
pipeline failure until a human confirms otherwise. Alerting only on exceptions
raised in code is the prohibited shortcut — these checks assert expected
activity, not just absence of errors. Every run writes a monitor_runs row so a
monitor that stops running is itself visible as a gap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

STUCK_UNPARSED_SLA_HOURS = 24


@dataclass
class Alarm:
    check: str
    detail: str

    def as_dict(self) -> dict:
        return {"check": self.check, "detail": self.detail}


def check_freshness_and_volume(conn: psycopg.Connection, now: datetime) -> list[Alarm]:
    alarms: list[Alarm] = []
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM feed_slos")
        slos = cur.fetchall()
        for slo in slos:
            class_filter = "AND doc_class = %(doc_class)s" if slo["doc_class"] else ""
            params = {"doc_class": slo["doc_class"],
                      "cutoff": now - timedelta(days=slo["window_days"])}
            cur.execute(
                f"""SELECT count(*) AS n, max(fetched_at) AS latest
                    FROM documents WHERE fetched_at >= %(cutoff)s {class_filter}""",
                params,
            )
            row = cur.fetchone()
            if row["n"] == 0:
                alarms.append(Alarm(
                    "volume",
                    f"{slo['feed_name']}: ZERO documents in the last "
                    f"{slo['window_days']}d — treat as pipeline failure until "
                    f"a human confirms a quiet market",
                ))
                continue
            if row["n"] < slo["min_docs_per_window"]:
                alarms.append(Alarm(
                    "volume",
                    f"{slo['feed_name']}: {row['n']} docs in {slo['window_days']}d "
                    f"is below baseline {slo['min_docs_per_window']}",
                ))
            cur.execute(
                f"""SELECT max(fetched_at) AS latest FROM documents
                    WHERE true {class_filter}""",
                params,
            )
            latest = cur.fetchone()["latest"]
            if latest and (now - latest) > timedelta(hours=slo["max_staleness_hours"]):
                alarms.append(Alarm(
                    "freshness",
                    f"{slo['feed_name']}: newest document fetched {latest.isoformat()} "
                    f"exceeds staleness SLO of {slo['max_staleness_hours']}h",
                ))
    return alarms


def check_stuck_documents(conn: psycopg.Connection, now: datetime) -> list[Alarm]:
    """Every fetched document must reach a terminal parse_status (SPEC §5.3)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS n FROM documents
               WHERE parse_status = 'unparsed' AND fetched_at < %s""",
            (now - timedelta(hours=STUCK_UNPARSED_SLA_HOURS),),
        )
        n = cur.fetchone()["n"]
    if n:
        return [Alarm("stuck_documents",
                      f"{n} documents stuck in 'unparsed' beyond {STUCK_UNPARSED_SLA_HOURS}h SLA")]
    return []


def check_review_queue(conn: psycopg.Connection, now: datetime) -> list[Alarm]:
    alarms = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n, min(created_at) AS oldest FROM review_items WHERE resolved_at IS NULL"
        )
        row = cur.fetchone()
    if row["oldest"] and (now - row["oldest"]) > timedelta(days=14):
        alarms.append(Alarm(
            "review_sla",
            f"review queue oldest item from {row['oldest'].date()} exceeds two-week "
            f"SLA — auto-accept is halted for affected parsers ({row['n']} open items)",
        ))
    elif row["oldest"] and (now - row["oldest"]) > timedelta(days=7):
        alarms.append(Alarm(
            "review_sla",
            f"review queue not drained weekly: oldest open item {row['oldest'].date()} "
            f"({row['n']} open items)",
        ))
    return alarms


def check_parser_health(conn: psycopg.Connection, now: datetime) -> list[Alarm]:
    """Review-routing rate per parser, current 30d vs prior 30d. A rising rate
    is a drift alarm; a sharply falling one may mean validation has weakened
    (SPEC §6) — both directions alarm."""
    alarms = []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT parser_name,
                      count(*) FILTER (WHERE created_at >= %(mid)s) AS recent_total,
                      count(*) FILTER (WHERE created_at >= %(mid)s
                                       AND (confidence < 0.9 OR NOT passes_agree)) AS recent_routed,
                      count(*) FILTER (WHERE created_at < %(mid)s AND created_at >= %(start)s) AS prior_total,
                      count(*) FILTER (WHERE created_at < %(mid)s AND created_at >= %(start)s
                                       AND (confidence < 0.9 OR NOT passes_agree)) AS prior_routed
               FROM parsed_records
               GROUP BY parser_name""",
            {"mid": now - timedelta(days=30), "start": now - timedelta(days=60)},
        )
        for row in cur.fetchall():
            if row["recent_total"] < 10 or row["prior_total"] < 10:
                continue  # not enough volume for a trend judgement
            recent_rate = row["recent_routed"] / row["recent_total"]
            prior_rate = row["prior_routed"] / row["prior_total"]
            if recent_rate > prior_rate * 2 and recent_rate > 0.1:
                alarms.append(Alarm(
                    "parser_drift",
                    f"{row['parser_name']}: review-routing rate rose "
                    f"{prior_rate:.0%} -> {recent_rate:.0%} — probable format drift",
                ))
            elif prior_rate > 0.05 and recent_rate < prior_rate / 3:
                alarms.append(Alarm(
                    "parser_drift",
                    f"{row['parser_name']}: review-routing rate fell "
                    f"{prior_rate:.0%} -> {recent_rate:.0%} — check validation "
                    f"has not quietly weakened",
                ))
    return alarms


def run_monitor(conn: psycopg.Connection, now: datetime | None = None) -> list[Alarm]:
    now = now or datetime.now(timezone.utc)
    alarms = (
        check_freshness_and_volume(conn, now)
        + check_stuck_documents(conn, now)
        + check_review_queue(conn, now)
        + check_parser_health(conn, now)
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO monitor_runs (ok, alarms) VALUES (%s, %s)",
            (not alarms, json.dumps([a.as_dict() for a in alarms])),
        )
    conn.commit()
    return alarms
