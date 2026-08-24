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


def record_first_seen(conn: psycopg.Connection, kind: str,
                      keys: list[str], built_at) -> int:
    """Stamp `built_at` on every key not already recorded. Returns how many
    were genuinely new.

    ON CONFLICT DO NOTHING is doing the real work: a key already present keeps
    its original timestamp, so a row that has been on the screen for a month
    does not re-announce itself every time the screen is rebuilt. The count
    returned is therefore the size of the "new" table, and a rebuild against
    unchanged trades correctly returns 0.
    """
    if not keys:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO signal_first_seen
                 (signal_version, kind, natural_key, first_seen_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            [(SIGNAL_VERSION, kind, k, built_at) for k in keys])
        cur.execute(
            """SELECT count(*) AS n FROM signal_first_seen
                WHERE signal_version = %s AND kind = %s
                  AND first_seen_at = %s""",
            (SIGNAL_VERSION, kind, built_at))
        return cur.fetchone()["n"]


def cluster_natural_key(entity_id: int, window_start) -> str:
    """Identity of a cluster across rebuilds: the company, and the buy it
    began with. Deliberately excludes window_end and the member list — both
    grow when another director joins, and a cluster gaining a third director
    is the same cluster, not a new one."""
    return f"{entity_id}:{window_start}"


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
    keys: list[str] = []
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
                    keys.append(cluster_natural_key(entity_id, start))
                    # Advance past this cluster to avoid emitting every
                    # overlapping sub-window as its own row.
                    i += len(members)
                else:
                    i += 1
        cur.execute("SELECT now() AS t")
        record_first_seen(conn, "cluster", keys, cur.fetchone()["t"])
    conn.commit()
    return clusters


# ---------------------------------------------------------------------------
# The display price, beside what the director paid
# ---------------------------------------------------------------------------
#
# What a director paid is traced to a specific lodgement. What the market asks
# today is not — it is a delayed third-party quote (ACCESS_DECISION §3,
# display-quote amendment). Putting them in adjacent columns is useful and
# slightly dangerous, so three rules apply to every screen that does it:
#
#   1. The quote carries its OWN as-at date, per row. Not one date in the
#      header: these are sub-index companies, and a thin explorer's last trade
#      can be days older than a large cap's. A single header date would assert
#      same-day pricing for rows that do not have it.
#   2. A missing quote is a flag, never a blank. `price_unavailable` says we
#      looked and could not price it — a delisted company keeps its row
#      (Invariant 4), it does not quietly vanish or show an empty cell that
#      reads as zero.
#   3. The source is named on the row. An unattributed number next to
#      lodgement-traced figures is the weakest thing on the page.

def _price_columns(quote: dict | None, paid) -> list:
    """The four price cells for one row: price, as-at, move vs paid, source."""
    if not quote or quote["status"] != "ok" or quote["price"] is None:
        return ["", "", "", ""]
    price = float(quote["price"])
    move = ""
    if paid is not None and float(paid) > 0:
        # Against what the director actually paid, not against yesterday's
        # close. The screen exists to ask whether the market has since agreed
        # with them.
        move = round((price / float(paid) - 1) * 100, 1)
    return [price, quote["as_at_date"], move, quote["source_name"]]


PRICE_HEADERS = ["price_aud", "price_as_at", "price_vs_paid_pct", "price_source"]


# What the participating directors hold between them, after buying.
#
# `sum(held_after)` over the cluster's trades is wrong, and wrong in the
# flattering direction. A director may lodge twice inside the 30-day window —
# on the current corpus Michael Addison does, at CBE, reporting 7,000,000 held
# on 4 August and 7,100,000 on 5 August. Those are two states of ONE holding,
# not two holdings: summing them reports 29,923,551 shares against a real
# 22,923,551, overstating the board's stake by 31%.
#
# So the holding is resolved per person first — their most recent notice in
# the window — and only then added up. `held_after`, not `held_before`,
# because the question the column answers is what they hold now that they have
# bought.
#
# Split by security class as well as person, because a director holding both
# ordinary shares and options has two holdings that must not be added into one
# number (Invariant 8: categories never blend). When that happens the row is
# flagged rather than silently totalled — the sum is still shown, because a
# reader who knows it mixes classes can use it, and one who is not told cannot.
HOLDINGS_LATERAL = """
  SELECT sum(h.held_after)                          AS total_held,
         count(*)                                   AS n_holdings,
         count(*) FILTER (WHERE h.held_after IS NULL) AS n_held_missing,
         count(DISTINCT h.person_key)               AS n_holders
    FROM (
      SELECT DISTINCT ON (COALESCE(t2.person_id::text, t2.person_name_raw),
                          t2.security_class)
             COALESCE(t2.person_id::text, t2.person_name_raw) AS person_key,
             t2.security_class, t2.held_after
        FROM director_trades t2
       WHERE t2.trade_id = ANY(s.trade_ids)
       ORDER BY COALESCE(t2.person_id::text, t2.person_name_raw),
                t2.security_class, t2.event_date DESC, t2.knowable_at DESC
    ) h
"""


def _holding_flags(row: dict) -> list[str]:
    """Coverage flags for the total-held figure."""
    flags = []
    if row["n_held_missing"]:
        # Some participant's notice states no closing holding, so the total is
        # a floor rather than a total.
        flags.append("held_partial")
    if row["n_holdings"] > row["n_holders"]:
        # At least one director holds more than one security class, so the sum
        # spans classes. Reported, not hidden, and not silently split.
        flags.append("held_mixed_classes")
    return flags


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

    avg_price_aud is what the directors actually paid, weighted by size:
    total consideration over total shares, NOT the mean of the per-trade
    prices. A director buying 2.5m shares and one buying 5,000 did not pay the
    same average, and averaging their prices equally would say they did. It is
    the number to compare against the current price, so it belongs on the
    screen rather than one join away.

    Where some trades in a cluster state no quantity or no consideration, the
    average is computed from those that do and the row is flagged
    partial_price_coverage — a price drawn from part of a cluster is usable
    only if the reader knows that is what it is.

    counter_evidence is what the insiders of the same company were doing in
    the 90 days before the cluster formed, restricted to what was knowable
    when the cluster was (see counter_evidence()). Empty means nothing
    contradicting was knowable by then — not that nothing has happened since.

    Ordered by actionable date, newest first: the screen is read forwards.
    """
    import csv
    import io

    from asx.ingest.quote_source import latest_quotes

    quotes = latest_quotes(conn)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.ticker, n.name AS entity, s.entity_id,
                      s.window_start, s.window_end,
                      s.n_directors, s.total_consideration_aud,
                      s.knowable_at, s.coverage_flags, s.signal_version,
                      p.shares, p.priced_consideration,
                      p.n_trades, p.n_priced, p.lodged_sources,
                      hold.total_held, hold.n_holdings, hold.n_held_missing,
                      hold.n_holders,
                      (SELECT string_agg(DISTINCT t.person_name_raw, '; ')
                         FROM director_trades t
                        WHERE t.trade_id = ANY(s.trade_ids)) AS directors
                 FROM signal_cluster_buys s
                 CROSS JOIN LATERAL (""" + HOLDINGS_LATERAL + """) AS hold
                 CROSS JOIN LATERAL (
                   SELECT count(*) AS n_trades,
                          count(*) FILTER (WHERE t.qty_acquired IS NOT NULL
                                             AND t.consideration_aud IS NOT NULL)
                            AS n_priced,
                          array_agg(DISTINCT dd.lodged_at_source) AS lodged_sources,
                          sum(t.qty_acquired) FILTER (
                            WHERE t.qty_acquired IS NOT NULL
                              AND t.consideration_aud IS NOT NULL) AS shares,
                          sum(t.consideration_aud) FILTER (
                            WHERE t.qty_acquired IS NOT NULL
                              AND t.consideration_aud IS NOT NULL)
                            AS priced_consideration
                     FROM director_trades t
                     JOIN documents dd ON dd.doc_id = t.doc_id
                    WHERE t.trade_id = ANY(s.trade_ids)
                 ) AS p
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
                "total_consideration_aud", "total_shares", "avg_price_aud",
                *PRICE_HEADERS,
                "total_held", "held_value_aud",
                "counter_evidence", "coverage_flags", "signal_version"])
    for r in rows:
        shares, spend = r["shares"], r["priced_consideration"]
        avg_price = round(spend / shares, 4) if shares else None
        flags = list(r["coverage_flags"] or [])
        if r["n_priced"] < r["n_trades"]:
            flags.append("partial_price_coverage")
        quote = quotes.get(r["entity_id"])
        price_cells = _price_columns(quote, avg_price)
        if price_cells[0] == "":
            # Looked and could not price it. Said out loud, because a blank
            # cell is indistinguishable from a company nobody checked.
            flags.append("price_unavailable")
        flags.extend(_holding_flags(r))
        held = r["total_held"]
        # What the whole holding is worth at the current quote — the buy sized
        # against the position it went into. Only computable with both, so it
        # is blank rather than approximated when either is missing.
        held_value = ""
        if held is not None and price_cells[0] != "":
            held_value = round(float(held) * float(price_cells[0]), 2)
        # The cluster is knowable only once its LAST notice is public, so any
        # soft timestamp among its members can push the whole row early.
        if any(src in SOFT_LODGEMENT_SOURCES
               for src in (r["lodged_sources"] or [])):
            flags.append(ACTIONABLE_EARLY_FLAG)
        # Windowed on the cluster's FIRST buy: the question is what insiders
        # were doing in the run-up to the cluster forming, and dating it from
        # the last member would let a long cluster shorten its own lookback.
        against = counter_evidence(conn, r["entity_id"], r["window_start"],
                                   r["knowable_at"])
        if against:
            flags.append("counter_evidence")
        w.writerow([
            r["ticker"] or "", r["entity"] or "", r["directors"] or "",
            r["n_directors"], r["window_start"], r["window_end"],
            # Not the trade date: the screen is only actionable once the
            # lodgement made it public (Invariant 2).
            r["knowable_at"].date(), r["total_consideration_aud"],
            shares if shares is not None else "",
            avg_price if avg_price is not None else "",
            *price_cells,
            held if held is not None else "", held_value,
            against, "|".join(flags), r["signal_version"],
        ])
    return buf.getvalue()


# --- counter-evidence ---------------------------------------------------
#
# Both screens answer "a director bought"; neither answered "what else were
# the insiders of this company doing at the time". They are not the same
# question, and the second one can reverse the first: ALQ reaches the
# conviction screen on a $57,178 buy taken 14 days after a fellow director
# sold $1,285,097 on-market. A screen that shows the buy and not the sell is
# not neutral about the omission — it reads as an endorsement.
#
# So contradicting activity already sitting in canonical data is reported on
# the row, not left one join away. Two kinds, kept distinct because they mean
# different things:
#
#   onmkt_sell  an insider sold. Evidence against, on the same footing as the
#               buy that raised the row.
#   unknown     activity the classifier refused to call (Invariant 8). NOT
#               evidence against — evidence that the row is incomplete. AGC
#               carries $979,637 of it beside a $15,237 buy, which is the
#               reader's cue that the screen is showing the small parcel and
#               not the story.
#
# Invariant 2 governs the window as strictly here as anywhere: only trades
# already knowable when the signal became knowable are counted. Counting a
# later sell would make today's screen unreproducible tomorrow and would put
# look-ahead into the one column whose job is to argue with the row.
# The consequence is stated rather than hidden: a sell lodged AFTER the
# signal is not here, and is not claimed to be absent.
COUNTER_EVIDENCE_LOOKBACK_DAYS = 90

# Correlated to the signal row by :entity_id/:event_date/:knowable_at, which
# both screens supply from their own anchor columns.
_COUNTER_EVIDENCE_SQL = """
  SELECT ce.classification, count(*) AS n, sum(ce.consideration_aud) AS aud
    FROM director_trades ce
   WHERE ce.entity_id = %(ce_entity)s
     AND NOT ce.superseded
     AND ce.classification IN ('onmkt_sell', 'unknown')
     AND ce.knowable_at <= %(ce_knowable)s
     AND ce.event_date >= %(ce_event)s::date - %(ce_lookback)s
   GROUP BY ce.classification
"""

_COUNTER_EVIDENCE_LABEL = {"onmkt_sell": "onmkt_sell", "unknown": "unclassified"}


def counter_evidence(conn: psycopg.Connection, entity_id: int, event_date,
                     knowable_at) -> str:
    """Contradicting insider activity knowable when this signal became
    knowable, as `kind:count:aud` parts joined by '|' — the same pipe-
    delimited shape the screens already use for coverage flags.

    Consideration is summed only over the trades that state one; a count
    larger than the amount implies is the reader's signal that some notice in
    there disclosed no value, which is commoner on disposals than on buys.
    """
    with conn.cursor() as cur:
        cur.execute(_COUNTER_EVIDENCE_SQL, {
            "ce_entity": entity_id,
            "ce_knowable": knowable_at,
            "ce_event": event_date,
            "ce_lookback": timedelta(days=COUNTER_EVIDENCE_LOOKBACK_DAYS),
        })
        rows = cur.fetchall()
    parts = []
    for r in sorted(rows, key=lambda r: r["classification"]):
        label = _COUNTER_EVIDENCE_LABEL[r["classification"]]
        aud = r["aud"]
        parts.append(f"{label}:{r['n']}:{int(aud)}" if aud is not None
                     else f"{label}:{r['n']}:unstated")
    return "|".join(parts)


# --- soft actionable dates ----------------------------------------------
#
# actionable_from is knowable_at's date, and knowable_at is documents.lodged_at.
# For 971 of the 1,109 dated documents held (88%) that timestamp came from the
# PDF's creation time, because the alert that detected the announcement did not
# carry a lodgement time. Creation is when the company MADE the notice, not
# when ASX released it, and the two differ by the overnight gap:
#
#   SPZ doc 557/558   created 19 Aug 2026 20:20 AEST
#                     released by ASX 20 Aug 2026 08:00 AEST   (11h40m later)
#
# So the screen said those rows were actionable on the 19th when nobody could
# have acted before the 20th. Checked against five BSA notices the same skew
# runs from 2 minutes to nearly 3 days, always in the same direction, and lands
# on the wrong calendar day whenever the notice was prepared after the close or
# over a weekend — which is most of them.
#
# That is look-ahead bias in the column whose entire job is to prevent it, and
# it currently applies to every signal row on both screens. It cannot be fixed
# here: the true release time is not in the data, and the source that has it is
# not a declared source (Invariant 11, docs/SOURCE_INVESTORPA.md). What CAN be
# done is stop the screen asserting a date it cannot support, so the flag says
# the date may be early rather than the column quietly pretending otherwise.
#
# Fixing it properly means capturing a release timestamp at detection. Until
# then, read a flagged actionable_from as "this date, or the next trading
# morning".
ACTIONABLE_EARLY_FLAG = "actionable_from_may_be_early"
SOFT_LODGEMENT_SOURCES = ("pdf_creation",)


# Conviction sizing (SPEC §7). The bar is the top quartile of the stake
# increases actually observed on the corpus — 19 of 73 on-market buys clear
# 25% — rather than a number chosen for looking round. Revisit it against the
# distribution when the corpus grows; that is what versioning is for.
CONVICTION_MIN_STAKE_INCREASE = 0.25

# Below this, a large percentage is arithmetic rather than conviction: a 27%
# increase costing $2,460 says the holding was tiny, not that anyone changed
# their mind. Such rows are FLAGGED, never dropped — the reader can see them
# and decide, which a silent exclusion does not allow.
CONVICTION_SMALL_SPEND_AUD = 20000


def build_conviction_buys(conn: psycopg.Connection) -> int:
    """Rebuild the conviction-sizing signal table for SIGNAL_VERSION."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM signal_conviction_buys WHERE signal_version = %s",
                    (SIGNAL_VERSION,))
        cur.execute(
            """SELECT trade_id, entity_id, person_name_raw, event_date,
                      knowable_at, consideration_aud, qty_acquired, held_before
                 FROM director_trades
                WHERE classification = 'onmkt_buy_cash'
                  AND NOT superseded
                  AND review_status IN ('auto','human_accepted','human_corrected')
                  AND held_before > 0
                  AND qty_acquired > 0
                ORDER BY knowable_at, trade_id""")
        candidates = cur.fetchall()

    written = 0
    keys: list[str] = []
    with conn.cursor() as cur:
        for t in candidates:
            increase = t["qty_acquired"] / t["held_before"]
            if increase < CONVICTION_MIN_STAKE_INCREASE:
                continue
            # Same size ceiling as the cluster signal, evaluated as at the day
            # this became knowable so no later rebalance leaks backwards.
            member = is_index_member(
                conn, t["entity_id"], t["knowable_at"].date(), DEFAULT_INDEX_CODE)
            if member is True:
                continue
            flags = [f"size_ceiling_proxy:{DEFAULT_INDEX_CODE}"]
            if member is None:
                flags.append("membership_unknown")
            if (t["consideration_aud"] or 0) < CONVICTION_SMALL_SPEND_AUD:
                flags.append("small_absolute_spend")
            if t["consideration_aud"] is None:
                flags.append("consideration_not_stated")
            cur.execute(
                """INSERT INTO signal_conviction_buys
                     (signal_version, entity_id, trade_id, person_name_raw,
                      event_date, knowable_at, consideration_aud, qty_acquired,
                      held_before, stake_increase, coverage_flags)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (SIGNAL_VERSION, t["entity_id"], t["trade_id"],
                 t["person_name_raw"], t["event_date"], t["knowable_at"],
                 t["consideration_aud"], t["qty_acquired"], t["held_before"],
                 increase, flags))
            keys.append(str(t["trade_id"]))
            written += 1
        cur.execute("SELECT now() AS t")
        record_first_seen(conn, "conviction", keys, cur.fetchone()["t"])
    conn.commit()
    return written


def conviction_buys_csv(conn: psycopg.Connection) -> str:
    """The conviction-sizing screen as CSV, biggest stake increase first.

    price_paid_aud is the canonical per-unit price from the trade, not a
    recomputation: director_trades derives it only where it is safely
    computable, and rederiving here would quietly disagree with canonical on
    the rows where it refused.

    counter_evidence carries the sells and unclassified activity by insiders
    of the same company in the 90 days before the trade, knowable by the time
    the trade was (see counter_evidence()). It is the column that stops a
    large stake_increase_pct being read on its own: the biggest increases here
    belong to the smallest prior holdings, and a director who quadruples 8,490
    shares while a colleague sells $1.28m has not told you what the screen
    ordering implies.
    """
    import csv
    import io

    from asx.ingest.quote_source import latest_quotes

    quotes = latest_quotes(conn)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.ticker, n.name AS entity, s.entity_id, s.person_name_raw,
                      s.event_date, s.knowable_at, s.consideration_aud,
                      s.qty_acquired, s.held_before, s.stake_increase,
                      t.price_per_unit, s.coverage_flags, s.signal_version,
                      d.lodged_at_source
                 FROM signal_conviction_buys s
                 JOIN director_trades t ON t.trade_id = s.trade_id
                 JOIN documents d ON d.doc_id = t.doc_id
                 LEFT JOIN listings l
                        ON l.entity_id = s.entity_id AND l.valid_to IS NULL
                 LEFT JOIN entity_names n
                        ON n.entity_id = s.entity_id AND n.valid_to IS NULL
                ORDER BY s.stake_increase DESC""")
        rows = cur.fetchall()

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["ticker", "entity", "director", "event_date", "actionable_from",
                "consideration_aud", "price_paid_aud", "qty_acquired",
                "held_before", "stake_increase_pct",
                *PRICE_HEADERS,
                "counter_evidence", "coverage_flags", "signal_version"])
    for r in rows:
        # 4dp: enough for a sub-cent explorer, and the raw quotient is a
        # 28-digit repeating decimal that reads as false precision.
        paid = (round(float(r["price_per_unit"]), 4)
                if r["price_per_unit"] is not None else None)
        flags = list(r["coverage_flags"] or [])
        price_cells = _price_columns(quotes.get(r["entity_id"]), paid)
        if price_cells[0] == "":
            flags.append("price_unavailable")
        if r["lodged_at_source"] in SOFT_LODGEMENT_SOURCES:
            flags.append(ACTIONABLE_EARLY_FLAG)
        against = counter_evidence(conn, r["entity_id"], r["event_date"],
                                   r["knowable_at"])
        if against:
            flags.append("counter_evidence")
        w.writerow([
            r["ticker"] or "", r["entity"] or "", r["person_name_raw"],
            r["event_date"], r["knowable_at"].date(), r["consideration_aud"],
            paid if paid is not None else "",
            r["qty_acquired"], r["held_before"],
            round(float(r["stake_increase"]) * 100, 1),
            *price_cells,
            against, "|".join(flags), r["signal_version"],
        ])
    return buf.getvalue()
