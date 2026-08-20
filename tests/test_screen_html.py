"""The published screen is generated, not maintained.

It used to be hand-written HTML with the numbers typed into it, which meant
the page could disagree with the tables it reported and nothing would notice.
These tests cover the ways the generator can quietly say something untrue.
"""

import json
import re

from asx.signals import screen_html


def _data(html: str) -> dict:
    return json.loads(re.search(r"const DATA = (\{.*?\});\n", html, re.S).group(1))


def test_every_placeholder_is_substituted(conn):
    html = screen_html.render(conn)
    leftover = set(re.findall(r"__[A-Z_]+__", html))
    assert not leftover, f"unsubstituted placeholders on the page: {leftover}"


def test_a_row_with_no_quote_carries_a_flag_not_a_zero(conn):
    """An absent price must be absent, not 0.00.

    `null` renders as an em dash and sorts last; a zero would sit at the top of
    a "cheapest" sort and read as a company trading at nothing.
    """
    assert screen_html._quote_fields(None, 1.0) == {
        "price": None, "priceAsAt": None, "vsPaid": None}
    assert screen_html._quote_fields(
        {"status": "fetch_error", "price": None, "as_at_date": None}, 1.0
    ) == {"price": None, "priceAsAt": None, "vsPaid": None}


def test_no_move_is_computed_without_a_price_paid(conn):
    from datetime import date
    q = {"status": "ok", "price": 2.0, "as_at_date": date(2026, 8, 20)}
    assert screen_html._quote_fields(q, None)["vsPaid"] is None
    assert screen_html._quote_fields(q, 1.0)["vsPaid"] == 100.0


def test_the_corpus_counts_come_from_the_tables(conn):
    """The lede states how many trades and notices the screens are drawn from.

    Both were wrong on the first generated build — trades excluded superseded
    rows and notices read `asx_doc_types`, which is unpopulated on this corpus,
    giving "parsed from 0 lodged notices" on a page reporting 19 signals. A
    number in prose is as much a claim as a number in a table.
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        for cls in ("app_3y", "app_3y", "app_3z", "other"):
            cur.execute(
                """INSERT INTO documents (source, doc_class, parse_status,
                                          entity_id)
                   VALUES ('test', %s, 'detected', %s)""", (cls, entity_id))
    conn.commit()

    flat = " ".join(screen_html.render(conn).split())
    # 3 of the 4 documents are director notices; all 4 are documents.
    assert "parsed from 3 lodged" in flat
    assert "4 documents held" in flat


def test_the_header_states_a_range_when_quotes_disagree(conn):
    """Thin explorers do not trade every day, so a build usually holds quotes
    struck on different dates. Printing only the newest would let the freshest
    row speak for the stalest, so the header states the span and the stale
    rows are named."""
    from datetime import datetime, timezone

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents (source, parse_status, sha256, fetched_at,
                                      possession_source, document_text,
                                      text_sha256)
               VALUES ('test','parsed',%s,now(),'manual_capture','x',%s)
               RETURNING doc_id""", ("0" * 64, "1" * 64))
        doc_id = cur.fetchone()["doc_id"]
        knowable = datetime(2026, 8, 10, tzinfo=timezone.utc)
        for ticker, as_at, price in [("FRESH", "2026-08-20", "1.00"),
                                     ("STALE", "2026-08-17", "2.00")]:
            cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                        "RETURNING entity_id")
            eid = cur.fetchone()["entity_id"]
            cur.execute(
                """INSERT INTO listings (entity_id, ticker, valid_from)
                   VALUES (%s, %s, '2020-01-01')""", (eid, ticker))
            cur.execute(
                """INSERT INTO director_trades
                     (entity_id, person_name_raw, doc_id, event_date,
                      knowable_at, security_class, qty_acquired,
                      consideration_aud, price_per_unit, held_before,
                      held_after, classification, confidence)
                   VALUES (%s,'A Director',%s,'2026-08-05',%s,'ORD',
                           1000,1000,1.00,1000,2000,'onmkt_buy_cash',1.0)
                   RETURNING trade_id""", (eid, doc_id, knowable))
            trade_id = cur.fetchone()["trade_id"]
            cur.execute(
                """INSERT INTO signal_conviction_buys
                     (signal_version, entity_id, trade_id, person_name_raw,
                      event_date, knowable_at, consideration_aud, qty_acquired,
                      held_before, stake_increase, coverage_flags)
                   VALUES (2,%s,%s,'A Director','2026-08-05',%s,1000,1000,1000,
                           1.0,'{}')""", (eid, trade_id, knowable))
            cur.execute(
                """INSERT INTO price_quotes
                     (entity_id, ticker_used, price, as_at_date, source_name,
                      source_url, status)
                   VALUES (%s,%s,%s,%s,'stockanalysis.com','u','ok')""",
                (eid, ticker, price, as_at))
    conn.commit()

    flat = " ".join(screen_html.render(conn).split())
    assert "Prices as at 17 Aug 2026 &ndash; 20 Aug 2026" in flat, \
        "the header must state the span, not just the newest quote"
    # The older row is named so a reader can see which number is behind.
    assert "1 row(s) carry an older quote (STALE)" in flat


def test_the_page_does_not_call_a_delayed_quote_live(conn):
    flat = " ".join(screen_html.render(conn).lower().split())
    assert "delayed" in flat
    # "latest price" is the label the handover specifically warned against.
    assert "latest price" not in flat


def test_the_page_says_backtesting_is_still_out_of_scope(conn):
    """A price column is the most likely thing to make a reader assume the
    numbers have been tested against what happened next. They have not."""
    # Normalised: the assertion is about what the page says, not where the
    # source happens to wrap.
    flat = " ".join(screen_html.render(conn).lower().split())
    assert "backtesting remains out of scope" in flat
    assert "delisted" in flat


def test_the_cluster_table_shows_what_the_board_holds(conn):
    """The published screen and the CSV must not disagree about the holding.

    They share `HOLDINGS_LATERAL` and `_holding_flags` for exactly that reason:
    two implementations of "resolve per director, then sum" would eventually
    drift, and the drift would be invisible.
    """
    from tests.test_screen_prices import _cluster_fixture

    _cluster_fixture(conn, [
        ("Martin Holland", "2026-07-13", "Shares", 15823551),
        ("Michael Addison", "2026-08-04", "Shares", 7000000),
        ("Michael Addison", "2026-08-05", "Shares", 7100000),
    ])
    html = screen_html.render(conn)
    row = _data(html)["cluster"][0]
    assert row["held"] == 22923551
    assert "Held after" in html
    # The naive sum must not appear anywhere on the page.
    assert "29923551" not in html
