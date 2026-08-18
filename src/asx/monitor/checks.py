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

from asx.parse.framework import AUTO_ACCEPT_CONFIDENCE

STUCK_UNPARSED_SLA_HOURS = 24
# How long a parseable announcement may sit detected-but-uncaptured before it
# counts as a dataset hole. Two business days allows for a weekend sweep.
CAPTURE_SLA_HOURS = 96
CAPTURE_RATE_FLOOR = 0.9


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
            # Whitelisted by the feed_slos CHECK constraint, so safe to inline.
            tcol = slo["time_column"]
            params = {"doc_class": slo["doc_class"],
                      "cutoff": now - timedelta(days=slo["window_days"])}
            cur.execute(
                f"""SELECT count(*) AS n, max({tcol}) AS latest
                    FROM documents WHERE {tcol} >= %(cutoff)s {class_filter}""",
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
            # Staleness is measured on lodgement time where we have it: a feed
            # serving a stale backlog keeps fetch/detect times fresh while the
            # market content ages.
            cur.execute(
                f"""SELECT max(coalesce(lodged_at, {tcol})) AS latest
                    FROM documents WHERE {tcol} IS NOT NULL {class_filter}""",
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


def check_capture_gap(conn: psycopg.Connection, now: datetime) -> list[Alarm]:
    """Detected-but-never-captured is the Tier 0 failure mode.

    Under a paid feed, detection and possession coincide. Here they don't:
    an alert can arrive and the document never be captured, which would
    otherwise be an invisible hole in the dataset. This check makes the hole
    loud — it is the single most important completeness metric under the
    current access decision (ACCESS_DECISION §1).
    """
    alarms: list[Alarm] = []
    parseable_cutoff = now - timedelta(hours=CAPTURE_SLA_HOURS)
    with conn.cursor() as cur:
        from asx.parse.registry import parseable_doc_classes

        cur.execute(
            """SELECT count(*) AS n, min(detected_at) AS oldest
               FROM documents
               WHERE parse_status = 'detected' AND doc_class = ANY(%s)
                 AND detected_at < %s""",
            (list(parseable_doc_classes()), parseable_cutoff),
        )
        row = cur.fetchone()
        if row["n"]:
            alarms.append(Alarm(
                "capture_gap",
                f"{row['n']} parseable announcements detected but never captured "
                f"(oldest {row['oldest'].date()}); each is a hole in the dataset "
                f"until opened in the capture browser",
            ))

        # Capture rate over the trailing fortnight: a falling rate means the
        # manual sweep is becoming unsustainable, which is an explicit review
        # trigger in the access decision (§5).
        cur.execute(
            """SELECT count(*) AS detected,
                      count(*) FILTER (WHERE parse_status <> 'detected') AS captured
               FROM documents
               WHERE detected_at >= %s AND doc_class = ANY(%s)""",
            (now - timedelta(days=14), list(parseable_doc_classes())),
        )
        row = cur.fetchone()
        if row["detected"] >= 20:
            rate = row["captured"] / row["detected"]
            if rate < CAPTURE_RATE_FLOOR:
                alarms.append(Alarm(
                    "capture_rate",
                    f"only {rate:.0%} of parseable detections captured over 14d "
                    f"({row['captured']}/{row['detected']}) — below the "
                    f"{CAPTURE_RATE_FLOOR:.0%} floor; the daily sweep may be "
                    f"unsustainable (access decision §5 review trigger)",
                ))
    return alarms


def check_review_queue(conn: psycopg.Connection, now: datetime) -> list[Alarm]:
    """Weekly-drain SLA on the review queue.

    Reference-load identity items are excluded, matching
    `parse.framework.auto_accept_halted`. Roughly 4% of ASX-listed issuers are
    schemes or stapled groups with no ASIC company registration to find, so
    those items never resolve. Counting them here would put the monitor in
    permanent alarm, and a monitor that always alarms is a monitor nobody
    reads — the exact failure Invariant 7 is guarding against. Their *count*
    is watched separately by check_entity_identity_rate.
    """
    alarms = []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS n, min(created_at) AS oldest
               FROM review_items
               WHERE resolved_at IS NULL
                 AND NOT (kind = 'resolution' AND doc_id IS NULL)"""
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


# Share of listed issuers with no resolved ASIC registration. The structural
# floor measured on the 18 Aug 2026 file is 4.0% (73 of 1,834) — listed
# trusts, REITs and stapled groups, which hold an ARSN and are absent from the
# company register. The ceiling below tolerates that floor plus normal drift
# as new schemes list, while still catching a resolver regression: the first
# version of the loader sat at 20%, and the version before the registration-
# type filter at 11%.
UNIDENTIFIED_ISSUER_CEILING = 0.06


def check_entity_identity_rate(conn: psycopg.Connection, now: datetime) -> list[Alarm]:
    """Alarm when the share of listed issuers lacking a registration number
    rises above its structural floor.

    The owner's standing decision is not to chase the unidentified residue
    (docs/ACCEPTANCE.md, criterion 0.2). That decision is only safe while the
    residue stays structural: a *rise* means the resolver broke, not that the
    market changed, and without this check the breakage would be invisible
    because nothing errors — every company still gets an entity.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE e.acn IS NULL AND e.arbn IS NULL
                                         AND e.entity_kind <> 'foreign') AS unidentified
               FROM entities e
               WHERE EXISTS (SELECT 1 FROM listings l
                              WHERE l.entity_id = e.entity_id AND l.valid_to IS NULL)"""
        )
        row = cur.fetchone()
    if not row["total"]:
        return []
    rate = row["unidentified"] / row["total"]
    if rate <= UNIDENTIFIED_ISSUER_CEILING:
        return []
    return [Alarm(
        "entity_identity",
        f"{row['unidentified']} of {row['total']} listed issuers ({rate:.1%}) have "
        f"no ACN or ARBN, above the {UNIDENTIFIED_ISSUER_CEILING:.0%} ceiling. "
        f"The structural floor is ~4% (listed schemes and stapled groups); a "
        f"rise means name resolution regressed. Run `asx coverage` for the list.",
    )]


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
                                       AND (confidence < %(thr)s OR NOT passes_agree)) AS recent_routed,
                      count(*) FILTER (WHERE created_at < %(mid)s AND created_at >= %(start)s) AS prior_total,
                      count(*) FILTER (WHERE created_at < %(mid)s AND created_at >= %(start)s
                                       AND (confidence < %(thr)s OR NOT passes_agree)) AS prior_routed
               FROM parsed_records
               GROUP BY parser_name""",
            {"mid": now - timedelta(days=30), "start": now - timedelta(days=60),
             "thr": AUTO_ACCEPT_CONFIDENCE},
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


def check_classification_base_rates(conn: psycopg.Connection, now: datetime) -> list[Alarm]:
    """SPEC §7: on-market cash buys are a minority of lodgements; if the
    classifier's output distribution drifts from the historical base rate,
    alarm — in either direction (wording drift starves the signal; a rules
    regression floods it)."""
    alarms = []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                 count(*) FILTER (WHERE knowable_at >= %(mid)s) AS recent_total,
                 count(*) FILTER (WHERE knowable_at >= %(mid)s
                                  AND classification = 'onmkt_buy_cash') AS recent_buys,
                 count(*) FILTER (WHERE knowable_at < %(mid)s AND knowable_at >= %(start)s) AS prior_total,
                 count(*) FILTER (WHERE knowable_at < %(mid)s AND knowable_at >= %(start)s
                                  AND classification = 'onmkt_buy_cash') AS prior_buys
               FROM director_trades WHERE NOT superseded""",
            {"mid": now - timedelta(days=30), "start": now - timedelta(days=210)},
        )
        row = cur.fetchone()
    if row["recent_total"] < 30 or row["prior_total"] < 100:
        return []  # not enough volume for a base-rate judgement
    recent_rate = row["recent_buys"] / row["recent_total"]
    prior_rate = row["prior_buys"] / row["prior_total"]
    if recent_rate > max(prior_rate * 2, prior_rate + 0.1):
        alarms.append(Alarm(
            "classification_base_rate",
            f"onmkt_buy_cash share rose {prior_rate:.0%} -> {recent_rate:.0%} — "
            f"check the rules have not started absorbing ambiguous wordings",
        ))
    elif prior_rate > 0.02 and recent_rate < prior_rate / 2:
        alarms.append(Alarm(
            "classification_base_rate",
            f"onmkt_buy_cash share fell {prior_rate:.0%} -> {recent_rate:.0%} — "
            f"probable consideration-wording drift starving the signal",
        ))
    return alarms


def run_monitor(conn: psycopg.Connection, now: datetime | None = None) -> list[Alarm]:
    now = now or datetime.now(timezone.utc)
    alarms = (
        check_freshness_and_volume(conn, now)
        + check_stuck_documents(conn, now)
        + check_capture_gap(conn, now)
        + check_review_queue(conn, now)
        + check_entity_identity_rate(conn, now)
        + check_parser_health(conn, now)
        + check_classification_base_rates(conn, now)
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO monitor_runs (ok, alarms) VALUES (%s, %s)",
            (not alarms, json.dumps([a.as_dict() for a in alarms])),
        )
    conn.commit()
    return alarms
