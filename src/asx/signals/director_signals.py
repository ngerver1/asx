"""Derived director-trade signals (SPEC §7). Definitions are versioned in
code; signal tables live in the derived zone and are rebuilt, never patched.

Signal v2 — cluster buying: >=2 distinct directors with accepted, non-
superseded 'onmkt_buy_cash' trades whose event dates fall within a 30-day
window, in an entity that is NOT a member of the S&P/ASX 300.

The size ceiling changed at v2 (ACCESS_DECISION §3, amendment 4): with no
price vendor, a market-cap ceiling is not computable, so index
non-membership stands in for it. The proxy is derived from an ETF issuer's
published holdings, refreshed weekly, and is *labelled as a proxy* on every
row — it tracks a quarterly-rebalanced index that also screens for liquidity
and free float, so it is not the same thing as a size cut-off.

Entities whose membership cannot be determined (no fresh snapshot) are
flagged 'membership_unknown' and retained rather than dropped or assumed
small — an unknown is reportable output, not a silent exclusion.

Invariant 2: the cluster's knowable_at is the LATEST lodgement among its
members — the cluster does not exist until the last participating notice is
public. Windowing on event_date alone without this would leak look-ahead.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg

from asx.universe.index_membership import DEFAULT_INDEX_CODE, is_index_member

SIGNAL_VERSION = 2
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
                    knowable_at = max(t["knowable_at"] for t in members)
                    # Size ceiling: membership is evaluated as at the date the
                    # cluster became knowable, using only snapshots published
                    # by then (no future rebalance leaks in).
                    member = is_index_member(
                        conn, entity_id, knowable_at.date(), DEFAULT_INDEX_CODE
                    )
                    if member is True:
                        i += len(members)
                        continue  # in the ASX 300 proxy: above the ceiling
                    flags = [f"size_ceiling_proxy:{DEFAULT_INDEX_CODE}"]
                    if member is None:
                        flags.append("membership_unknown")
                    cur.execute(
                        """INSERT INTO signal_cluster_buys
                             (signal_version, entity_id, window_start, window_end,
                              n_directors, total_consideration_aud, knowable_at,
                              trade_ids, coverage_flags)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (SIGNAL_VERSION, entity_id, start,
                         max(t["event_date"] for t in members),
                         len(distinct_people), total, knowable_at,
                         [t["trade_id"] for t in members], flags),
                    )
                    clusters += 1
                    # Advance past this cluster to avoid emitting every
                    # overlapping sub-window as its own row.
                    i += len(members)
                else:
                    i += 1
    conn.commit()
    return clusters


def cluster_buys_csv(conn: psycopg.Connection) -> str:
    """The cluster-buy screen as CSV — the deliverable of Phase 1 (SPEC §15).

    Every row carries its coverage flags, because a screen that states a
    result without stating what it could not check is the prohibited output
    (acceptance 1.6). Two flags matter to a reader:

      size_ceiling_proxy:*  the size cut is index membership standing in for a
                            market-cap ceiling, not a market-cap ceiling.
      membership_unknown    no membership snapshot published by the date this
                            became knowable, so the size cut could not be
                            applied to this row at all. It is NOT a claim that
                            the company is small.

    Ordered by actionable date, newest first: the screen is read forwards.
    """
    import csv
    import io

    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.ticker, n.name AS entity, s.window_start, s.window_end,
                      s.n_directors, s.total_consideration_aud,
                      s.knowable_at, s.coverage_flags, s.signal_version,
                      (SELECT string_agg(DISTINCT t.person_name_raw, '; ')
                         FROM director_trades t
                        WHERE t.trade_id = ANY(s.trade_ids)) AS directors
                 FROM signal_cluster_buys s
                 LEFT JOIN listings l
                        ON l.entity_id = s.entity_id AND l.valid_to IS NULL
                 LEFT JOIN entity_names n
                        ON n.entity_id = s.entity_id AND n.valid_to IS NULL
                ORDER BY s.knowable_at DESC, s.total_consideration_aud DESC""")
        rows = cur.fetchall()

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["ticker", "entity", "directors", "n_directors",
                "first_buy", "last_buy", "actionable_from",
                "total_consideration_aud", "coverage_flags", "signal_version"])
    for r in rows:
        w.writerow([
            r["ticker"] or "", r["entity"] or "", r["directors"] or "",
            r["n_directors"], r["window_start"], r["window_end"],
            # Not the trade date: the screen is only actionable once the
            # lodgement made it public (Invariant 2).
            r["knowable_at"].date(), r["total_consideration_aud"],
            "|".join(r["coverage_flags"] or []), r["signal_version"],
        ])
    return buf.getvalue()
