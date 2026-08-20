"""Snapshot/restore of the durable state.

The cloud container is reclaimed on inactivity and takes Postgres with it, so
this is the mechanism that decides whether the platform accumulates a dataset
or starts from nothing every session. A restore that silently drops rows, or
that leaves identity sequences behind the restored data, would be discovered
days later as a foreign-key error with no obvious cause.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from asx.reference.asx_listed import ListedCompany, apply_listing_snapshot
from asx.state.snapshot import TABLES, export_state, import_state


@pytest.fixture
def populated(conn, asic_loaded):
    apply_listing_snapshot(
        conn,
        [ListedCompany("XYZ", "Xyz Mining Limited", listing_date=date(2015, 7, 1)),
         ListedCompany("ABC", "Abc Health Limited", listing_date=date(2016, 2, 2))],
        date(2026, 8, 18), asic_loaded.load_id, allow_shrink=True)
    conn.commit()
    return conn


pytest_plugins = ()
from tests.test_entity_master import asic_loaded  # noqa: E402,F401


def _counts(conn):
    out = {}
    with conn.cursor() as cur:
        for t in TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {t}")
            out[t] = cur.fetchone()["n"]
    return out


def test_state_round_trips_exactly(populated, tmp_path):
    conn = populated
    before = _counts(conn)
    export_state(conn, tmp_path)

    import_state(conn, tmp_path)
    conn.commit()
    assert _counts(conn) == before

    with conn.cursor() as cur:
        cur.execute("""SELECT l.ticker, e.acn, n.name
                       FROM listings l JOIN entities e USING (entity_id)
                       JOIN entity_names n ON n.entity_id = e.entity_id
                        AND n.valid_to IS NULL
                       WHERE l.valid_to IS NULL ORDER BY l.ticker""")
        rows = cur.fetchall()
    assert [r["ticker"] for r in rows] == ["ABC", "XYZ"]
    assert all(r["acn"] for r in rows)


def test_restore_advances_identity_sequences(populated, tmp_path):
    """Otherwise the next insert collides with a restored row — days later,
    as an unexplained constraint violation."""
    conn = populated
    export_state(conn, tmp_path)
    import_state(conn, tmp_path)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT max(entity_id) AS m FROM entities")
        highest = cur.fetchone()["m"]
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('other') "
                    "RETURNING entity_id")
        assert cur.fetchone()["entity_id"] > highest


def test_the_asic_register_is_not_snapshotted(populated, tmp_path):
    """1.1 GB of regenerable reference data must not end up in git. A fresh
    container still resolves tickers, because that goes through listings."""
    export_state(populated, tmp_path)
    assert not (tmp_path / "asic_registry.csv").exists()

    with populated.cursor() as cur:
        cur.execute("TRUNCATE asic_registry CASCADE")
    from asx.ingest.detection import entity_for_ticker
    assert entity_for_ticker(populated, "XYZ", date(2026, 8, 18)) is not None


def test_the_committed_snapshot_restores_into_the_current_schema(conn):
    """state/ is the only durable copy of the entity master and the detection
    log, and `asx snapshot --restore` is the documented way to rebuild a
    container. So it has to load into the schema at the *current* migration,
    not the one it happened to be exported from.

    The tests above export and re-import within one process, so their snapshot
    matches the schema by construction and schema drift cannot show up. It
    showed up in the tree instead: migration 0019 added lodged_at_source with
    a CHECK tying it to lodged_at, and every committed row already had a
    timestamp and no source, so restoring the snapshot died on a constraint
    violation. The recovery path was broken for two migrations while the suite
    stayed green.
    """
    snapshot = Path(__file__).resolve().parents[1] / "state"
    counts = import_state(conn, snapshot)
    conn.commit()

    assert counts["entities"] > 0, "the entity master came back empty"
    assert counts["documents"] > 0, "the detection log came back empty"

    with conn.cursor() as cur:
        for table, expected in counts.items():
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            assert cur.fetchone()["n"] == expected, f"{table} lost rows on restore"

        # The invariant migration 0019 exists to enforce: a timestamp always
        # says where it came from, and a source always describes one.
        cur.execute("""SELECT count(*) AS n FROM documents
                       WHERE (lodged_at IS NULL) <> (lodged_at_source IS NULL)""")
        assert cur.fetchone()["n"] == 0
