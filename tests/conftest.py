import os
import re

import psycopg
import pytest
from psycopg.rows import dict_row

from asx import db as adb

BASE_URL = os.environ.get("DATABASE_URL")
TEST_DB = "asx_test"


def _pg_available() -> bool:
    if not BASE_URL:
        return False
    try:
        with psycopg.connect(BASE_URL, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def test_db_url():
    if not _pg_available():
        pytest.skip("postgres unavailable (set DATABASE_URL to run integration tests)")
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    url = re.sub(r"/[^/?]+(\?|$)", f"/{TEST_DB}\\1", BASE_URL, count=1)
    with psycopg.connect(url, row_factory=dict_row) as conn:
        adb.migrate(conn)
        # Idempotency: a second run applies nothing.
        assert adb.migrate(conn) == []
    return url


_ALL_TABLES = [
    "asic_registry", "reference_loads", "index_membership", "manual_share_counts",
    # Both signal tables and the display quotes keyed off them: a signal row
    # surviving into the next test is a false pass waiting to happen.
    #
    # signal_first_seen belongs here for the same reason and was missed when
    # it was added: it is NOT emptied by build-signals (that is the point of
    # it — an existing row keeps its original arrival date), so without an
    # explicit truncate it is the one table that accumulates across the whole
    # session. Two arrival-tracking tests passed alone and failed in the full
    # suite, which is how it surfaced.
    "signal_cluster_buys", "signal_conviction_buys", "signal_first_seen",
    "price_quotes",
    "float_series", "monitor_runs", "hypothesis_log",
    "share_reconciliations", "director_trades", "substantial_holdings",
    "holder_snapshots", "escrow_parcels", "share_events", "share_anchors",
    "security_classes", "parsed_records", "review_items", "entity_aliases",
    "persons", "universe_membership", "listings", "entity_names",
    "documents", "entities",
]


@pytest.fixture
def conn(test_db_url, tmp_path, monkeypatch):
    monkeypatch.setenv("ASX_RAW_ROOT", str(tmp_path / "raw"))
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    with psycopg.connect(test_db_url, row_factory=dict_row) as c:
        yield c
        c.rollback()
        with c.cursor() as cur:
            # feed_slos and schema_migrations are seed/state tables and are
            # deliberately not truncated.
            cur.execute(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE")
        c.commit()
