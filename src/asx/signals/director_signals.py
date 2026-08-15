"""Derived director-trade signals (SPEC §7). Definitions are versioned in
code; signal tables live in the derived zone and are rebuilt, never patched.

Signal v1 — cluster buying: >=2 distinct directors with accepted, non-
superseded 'onmkt_buy_cash' trades whose event dates fall within a 30-day
window. The market-cap ceiling from SPEC §7 is applied downstream once the
price vendor is wired in; until then screens over this table must state that
coverage gap.

Invariant 2: the cluster's knowable_at is the LATEST lodgement among its
members — the cluster does not exist until the last participating notice is
public. Windowing on event_date alone without this would leak look-ahead.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg

SIGNAL_VERSION = 1
WINDOW_DAYS = 30
MIN_DIRECTORS = 2


def build_cluster_buys(conn: psycopg.Connection) -> int:
    """Rebuild the cluster-buy signal table for SIGNAL_VERSION. Returns the
    number of clusters written."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM signal_cluster_buys WHERE signal_version = %s",
            (SIGNAL_VERSION,),
        )
        cur.execute(
            """SELECT trade_id, entity_id, person_id, event_date, knowable_at,
                      consideration_aud
               FROM director_trades
               WHERE classification = 'onmkt_buy_cash'
                 AND NOT superseded
                 AND review_status IN ('auto', 'human_accepted', 'human_corrected')
               ORDER BY entity_id, event_date""",
        )
        trades = cur.fetchall()

    clusters = 0
    by_entity: dict[int, list[dict]] = {}
    for t in trades:
        by_entity.setdefault(t["entity_id"], []).append(t)

    window = timedelta(days=WINDOW_DAYS)
    with conn.cursor() as cur:
        for entity_id, entity_trades in by_entity.items():
            i = 0
            n = len(entity_trades)
            while i < n:
                start = entity_trades[i]["event_date"]
                members = [t for t in entity_trades[i:]
                           if t["event_date"] - start <= window]
                distinct_people = {t["person_id"] for t in members if t["person_id"]}
                if len(distinct_people) >= MIN_DIRECTORS:
                    total = sum((t["consideration_aud"] or 0) for t in members)
                    cur.execute(
                        """INSERT INTO signal_cluster_buys
                             (signal_version, entity_id, window_start, window_end,
                              n_directors, total_consideration_aud, knowable_at,
                              trade_ids)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (SIGNAL_VERSION, entity_id, start,
                         max(t["event_date"] for t in members),
                         len(distinct_people), total,
                         max(t["knowable_at"] for t in members),
                         [t["trade_id"] for t in members]),
                    )
                    clusters += 1
                    # Advance past this cluster to avoid emitting every
                    # overlapping sub-window as its own row.
                    i += len(members)
                else:
                    i += 1
    conn.commit()
    return clusters
