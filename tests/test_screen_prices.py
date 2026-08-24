"""The price column is the one number on the screen that is NOT traced to a
lodgement. These tests keep it honest about that.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from asx.ids.normalize import name_norm
from asx.signals.director_signals import _price_columns


def _quote(price="1.50", as_at=date(2026, 8, 20), status="ok"):
    return {"status": status, "price": price, "as_at_date": as_at,
            "source_name": "stockanalysis.com"}


def test_move_is_measured_against_what_the_director_paid():
    # Not against yesterday's close: the screen exists to ask whether the
    # market has since agreed with the director.
    cells = _price_columns(_quote("1.50"), paid=1.00)
    assert cells[0] == 1.50
    assert cells[2] == 50.0
    assert cells[3] == "stockanalysis.com"


def test_every_priced_row_names_its_source_and_its_date():
    cells = _price_columns(_quote("1.50", date(2026, 8, 17)), paid=1.00)
    assert cells[1] == date(2026, 8, 17)
    assert cells[3] == "stockanalysis.com"


@pytest.mark.parametrize("quote", [
    None,                                    # never looked up
    _quote(status="not_found"),              # delisted / no such code
    _quote(price=None, status="unparsed"),   # page shape changed
])
def test_an_unpriceable_row_yields_blanks_not_a_wrong_number(quote):
    assert _price_columns(quote, paid=1.00) == ["", "", "", ""]


def test_no_move_is_claimed_without_a_price_paid():
    # Several trades state no computable per-unit price. A move against an
    # unknown base is not a small inaccuracy, it is a fabricated one.
    assert _price_columns(_quote("1.50"), paid=None)[2] == ""
    assert _price_columns(_quote("1.50"), paid=0)[2] == ""


# --- against the database --------------------------------------------------

def test_unpriceable_rows_keep_their_place_on_the_screen(conn):
    """Invariant 4 in its display form: a company we cannot price stays on the
    screen, flagged, rather than dropping out of it."""
    from asx.signals.director_signals import conviction_buys_csv
    import csv, io

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        name = "Delisted Explorer Ltd"
        cur.execute(
            """INSERT INTO entity_names
                 (entity_id, name, name_norm, name_kind, valid_from)
               VALUES (%s, %s, %s, 'legal', '2020-01-01')""",
            (entity_id, name, name_norm(name)))
        cur.execute(
            """INSERT INTO documents
                 (source, source_ref, parse_status, sha256, fetched_at,
                  possession_source, document_text, text_sha256)
               VALUES ('test','ref-1','parsed', %s, now(), 'manual_capture',
                       'a notice', %s)
               RETURNING doc_id""", ("0" * 64, "1" * 64))
        doc_id = cur.fetchone()["doc_id"]
        knowable = datetime(2026, 8, 10, tzinfo=timezone.utc)
        cur.execute(
            """INSERT INTO director_trades
                 (entity_id, person_name_raw, doc_id, event_date, knowable_at,
                  security_class, qty_acquired, consideration_aud,
                  price_per_unit, held_before, held_after, classification,
                  confidence)
               VALUES (%s,'A Director',%s,'2026-08-05',%s,'ORD',
                       1000, 100, 0.10, 1000, 2000,'onmkt_buy_cash', 1.0)
               RETURNING trade_id""",
            (entity_id, doc_id, knowable))
        trade_id = cur.fetchone()["trade_id"]
        cur.execute(
            """INSERT INTO signal_conviction_buys
                 (signal_version, entity_id, trade_id, person_name_raw,
                  event_date, knowable_at, consideration_aud, qty_acquired,
                  held_before, stake_increase, coverage_flags)
               VALUES (2,%s,%s,'A Director','2026-08-05',%s,100,1000,1000,1.0,
                       '{}')""",
            (entity_id, trade_id, knowable))
        # We looked, and there was no open listing to address a quote to.
        cur.execute(
            """INSERT INTO price_quotes
                 (entity_id, ticker_used, source_name, source_url, status,
                  status_detail)
               VALUES (%s,'','stockanalysis.com','','not_found',
                       'no open ASX listing for this entity')""",
            (entity_id,))
    conn.commit()

    rows = list(csv.DictReader(io.StringIO(conviction_buys_csv(conn))))
    assert len(rows) == 1, "a company we cannot price must not vanish"
    row = rows[0]
    assert row["entity"] == "Delisted Explorer Ltd"
    assert row["price_aud"] == ""
    assert "price_unavailable" in row["coverage_flags"]


def test_the_newest_quote_wins(conn):
    """Quotes are an append-only log; the screen reads the latest row."""
    from asx.ingest.quote_source import latest_quotes

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        old = datetime.now(timezone.utc) - timedelta(days=1)
        cur.execute(
            """INSERT INTO price_quotes
                 (entity_id, ticker_used, price, as_at_date, source_name,
                  source_url, status, retrieved_at)
               VALUES (%s,'ABC',1.00,'2026-08-19','stockanalysis.com','u','ok',%s),
                      (%s,'ABC',2.00,'2026-08-20','stockanalysis.com','u','ok',now())""",
            (entity_id, old, entity_id))
    conn.commit()

    assert float(latest_quotes(conn)[entity_id]["price"]) == 2.00


def test_a_failed_refetch_does_not_resurrect_an_older_price(conn):
    """If today's fetch failed, the screen must say so rather than quietly
    showing last week's number as if it were current."""
    from asx.ingest.quote_source import latest_quotes

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        old = datetime.now(timezone.utc) - timedelta(days=7)
        cur.execute(
            """INSERT INTO price_quotes
                 (entity_id, ticker_used, price, as_at_date, source_name,
                  source_url, status, retrieved_at)
               VALUES (%s,'ABC',1.00,'2026-08-13','stockanalysis.com','u','ok',%s)""",
            (entity_id, old))
        cur.execute(
            """INSERT INTO price_quotes
                 (entity_id, ticker_used, source_name, source_url, status,
                  status_detail)
               VALUES (%s,'ABC','stockanalysis.com','u','fetch_error','timeout')""",
            (entity_id,))
    conn.commit()

    latest = latest_quotes(conn)[entity_id]
    assert latest["status"] == "fetch_error"
    assert _price_columns(latest, paid=1.00) == ["", "", "", ""]


# --- what the cluster holds between them ------------------------------------

def _cluster_fixture(conn, lodgements):
    """Build a one-entity cluster from (person, event_date, class, held_after)."""
    from asx.ids.normalize import name_norm

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO entity_names (entity_id, name, name_norm, name_kind,
                                         valid_from)
               VALUES (%s,'Cluster Co Ltd',%s,'legal','2020-01-01')""",
            (entity_id, name_norm("Cluster Co Ltd")))
        cur.execute(
            """INSERT INTO documents (source, parse_status, sha256, fetched_at,
                                      possession_source, document_text,
                                      text_sha256)
               VALUES ('test','parsed',%s,now(),'manual_capture','x',%s)
               RETURNING doc_id""", ("0" * 64, "1" * 64))
        doc_id = cur.fetchone()["doc_id"]

        trade_ids, people = [], {}
        for person, event, sec_class, held_after in lodgements:
            if person not in people:
                cur.execute(
                    "INSERT INTO persons (name_norm, display_name) "
                    "VALUES (%s, %s) RETURNING person_id",
                    (name_norm(person), person))
                people[person] = cur.fetchone()["person_id"]
            knowable = datetime.fromisoformat(f"{event}T00:00:00+00:00")
            cur.execute(
                """INSERT INTO director_trades
                     (entity_id, person_name_raw, person_id, doc_id, event_date,
                      knowable_at, security_class, qty_acquired,
                      consideration_aud, price_per_unit, held_before,
                      held_after, classification, confidence)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,100,100,1.00,%s,%s,
                           'onmkt_buy_cash',1.0)
                   RETURNING trade_id""",
                (entity_id, person, people[person], doc_id, event, knowable,
                 sec_class, (held_after - 100) if held_after else None,
                 held_after))
            trade_ids.append(cur.fetchone()["trade_id"])

        dates = sorted(l[1] for l in lodgements)
        cur.execute(
            """INSERT INTO signal_cluster_buys
                 (signal_version, entity_id, trade_ids, n_directors,
                  window_start, window_end, total_consideration_aud,
                  knowable_at, coverage_flags)
               VALUES (2,%s,%s,%s,%s,%s,%s,%s,'{}')""",
            (entity_id, trade_ids, len(people), dates[0], dates[-1],
             100 * len(trade_ids),
             datetime.fromisoformat(f"{dates[-1]}T00:00:00+00:00")))
    conn.commit()
    return entity_id


def _cluster_row(conn):
    import csv, io
    from asx.signals.director_signals import cluster_buys_csv
    return list(csv.DictReader(io.StringIO(cluster_buys_csv(conn))))[0]


def test_a_director_who_lodged_twice_is_counted_once(conn):
    """The reason this is not `sum(held_after)`.

    A director may lodge twice inside the 30-day window, and the two notices
    report two states of ONE holding. Summing the rows double-counts them, in
    the flattering direction. Taken from the real CBE cluster: Addison reports
    7,000,000 held on 4 Aug and 7,100,000 on 5 Aug.
    """
    _cluster_fixture(conn, [
        ("Martin Holland", "2026-07-13", "Shares", 15823551),
        ("Michael Addison", "2026-08-04", "Shares", 7000000),
        ("Michael Addison", "2026-08-05", "Shares", 7100000),
    ])
    row = _cluster_row(conn)
    # Holland's holding plus Addison's LATEST, not both of Addison's.
    assert row["total_held"] == "22923551"
    assert "29923551" != row["total_held"], "the naive sum double-counts"


def test_two_directors_in_differently_labelled_classes_still_add_up(conn):
    """Real forms label the same class inconsistently — the M2R cluster has
    '(a) Ordinary Shares' and '(c) Ordinary Shares', which are section markers
    from the form, not two kinds of share. Different PEOPLE always add."""
    _cluster_fixture(conn, [
        ("Allan Kelly", "2026-07-06", "(c) Ordinary Shares", 45636258),
        ("Terry Gadenne", "2026-07-02", "(a) Ordinary Shares", 15000000),
    ])
    row = _cluster_row(conn)
    assert row["total_held"] == "60636258"
    assert "held_mixed_classes" not in row["coverage_flags"]


def test_one_director_holding_two_classes_is_flagged(conn):
    """Options and shares are not the same holding (Invariant 8). The total is
    still shown — a reader who is told can use it — but it says so."""
    _cluster_fixture(conn, [
        ("A Director", "2026-07-06", "Ordinary Shares", 1000),
        ("A Director", "2026-07-06", "Unlisted Options", 500),
        ("B Director", "2026-07-07", "Ordinary Shares", 2000),
    ])
    row = _cluster_row(conn)
    assert row["total_held"] == "3500"
    assert "held_mixed_classes" in row["coverage_flags"]


def test_a_missing_closing_holding_makes_the_total_a_floor(conn):
    _cluster_fixture(conn, [
        ("A Director", "2026-07-06", "Ordinary Shares", 1000),
        ("B Director", "2026-07-07", "Ordinary Shares", None),
    ])
    row = _cluster_row(conn)
    assert "held_partial" in row["coverage_flags"]


def test_held_value_needs_both_a_holding_and_a_quote(conn):
    """No quote means no valuation — never a valuation at zero."""
    entity_id = _cluster_fixture(conn, [
        ("A Director", "2026-07-06", "Ordinary Shares", 1000),
        ("B Director", "2026-07-07", "Ordinary Shares", 2000),
    ])
    assert _cluster_row(conn)["held_value_aud"] == ""

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO price_quotes
                 (entity_id, ticker_used, price, as_at_date, source_name,
                  source_url, status)
               VALUES (%s,'ABC',2.50,'2026-08-20','stockanalysis.com','u','ok')""",
            (entity_id,))
    conn.commit()
    assert _cluster_row(conn)["held_value_aud"] == "7500.0"
