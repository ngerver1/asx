"""Reprocessing is the recovery path. It must survive contact with the screens.

`make reprocess` is the ONLY sanctioned way to fix a systematic parse error —
canonical tables are never hand-edited — and it works by replacing the rows a
document produced. Anything that blocks that replacement disables the recovery
path, and this file exists because something did.
"""

from datetime import datetime, timezone

from asx.ids.normalize import name_norm


def _trade_with_signal(conn):
    """One document -> one trade -> one conviction signal referencing it."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO documents (source, source_ref, parse_status, sha256,
                                      fetched_at, possession_source,
                                      document_text, text_sha256, doc_class)
               VALUES ('test','r1','validated',%s,now(),'manual_capture','x',%s,
                       'app_3y')
               RETURNING doc_id""", ("0" * 64, "1" * 64))
        doc_id = cur.fetchone()["doc_id"]
        cur.execute("INSERT INTO persons (name_norm, display_name) "
                    "VALUES (%s,'A Director') RETURNING person_id",
                    (name_norm("A Director"),))
        person_id = cur.fetchone()["person_id"]
        knowable = datetime(2026, 8, 10, tzinfo=timezone.utc)
        cur.execute(
            """INSERT INTO director_trades
                 (entity_id, person_name_raw, person_id, doc_id, event_date,
                  knowable_at, security_class, qty_acquired, consideration_aud,
                  price_per_unit, held_before, held_after, classification,
                  confidence)
               VALUES (%s,'A Director',%s,%s,'2026-08-05',%s,'ORD',1000,1000,
                       1.00,1000,2000,'onmkt_buy_cash',1.0)
               RETURNING trade_id""",
            (entity_id, person_id, doc_id, knowable))
        trade_id = cur.fetchone()["trade_id"]
        cur.execute(
            """INSERT INTO signal_conviction_buys
                 (signal_version, entity_id, trade_id, person_name_raw,
                  event_date, knowable_at, consideration_aud, qty_acquired,
                  held_before, stake_increase, coverage_flags)
               VALUES (2,%s,%s,'A Director','2026-08-05',%s,1000,1000,1000,1.0,
                       '{}')""",
            (entity_id, trade_id, knowable))
    conn.commit()
    return doc_id, trade_id


def test_a_reprocess_can_replace_a_trade_a_signal_points_at(conn):
    """The bug this pins down.

    `apply_trades` replaces a document's rows with DELETE-then-INSERT. The
    conviction signal's foreign key was RESTRICT, so the first reprocess after
    any signal build died with a ForeignKeyViolation at the fourth document —
    after three had already been rewritten. Building a screen from the parser
    disabled the ability to fix the parser.
    """
    from asx.canonical.director_trades import apply_trades

    doc_id, _ = _trade_with_signal(conn)
    # The corrected reading for the same document: this must not raise.
    apply_trades(conn, doc_id, [], review_status="auto")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM director_trades WHERE doc_id = %s",
                    (doc_id,))
        assert cur.fetchone()["n"] == 0


def test_a_signal_never_outlives_the_trade_it_was_computed_from(conn):
    """CASCADE, not orphaning. A conviction row pointing at a trade_id that no
    longer exists is the outcome the foreign key is there to prevent — the
    screens are rebuilt by `asx build-signals`, never left dangling."""
    from asx.canonical.director_trades import apply_trades

    doc_id, trade_id = _trade_with_signal(conn)
    apply_trades(conn, doc_id, [], review_status="auto")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM signal_conviction_buys "
                    "WHERE trade_id = %s", (trade_id,))
        assert cur.fetchone()["n"] == 0, "a signal survived its trade"
