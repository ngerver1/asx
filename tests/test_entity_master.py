"""Entity-master construction against live Postgres.

The delisting sweep is the survivorship-critical behaviour here (Invariant 4):
the ASX file lists only current companies, so absences must close membership
rather than delete history — and a truncated file must not be believed.
"""

from datetime import date

import pytest

from asx.reference.asic import load_asic_registry
from asx.reference.asx_listed import (
    ImplausibleSnapshotError,
    ListedCompany,
    apply_listing_snapshot,
)
from asx.reference.loads import latest_load, mark_applied, register_load
from asx.reference.verify import acn_coverage, ticker_integrity

ASIC_PIPE = """ACN|Company Name|Type|Class|Status|Date of Registration|Current Name Indicator|ABN
123456789|XYZ MINING LIMITED|APUB|LMSH|REGD|01/07/2015|Y|
234567891|ABC HEALTH LIMITED|APUB|LMSH|REGD|02/02/2016|Y|
345678912|DEAD CO LIMITED|APUB|LMSH|DRGD|03/03/2010|Y|
456789123|TWIN NAME LIMITED|APUB|LMSH|REGD|04/04/2018|Y|
567891234|TWIN NAME LIMITED|APUB|LMSH|REGD|05/05/2019|Y|
"""


@pytest.fixture
def asic_loaded(conn, tmp_path):
    path = tmp_path / "asic.csv"
    path.write_text(ASIC_PIPE)
    load = register_load(conn, path, source="asic_companies", as_at=date(2026, 8, 1))
    n = load_asic_registry(conn, path, load.load_id)
    mark_applied(conn, load.load_id, n)
    conn.commit()
    return load


def _snapshot(conn, companies, as_at, load_id, **kw):
    return apply_listing_snapshot(conn, companies, as_at, load_id,
                                  allow_shrink=True, **kw)


def _listing_load(conn, tmp_path, as_at, tag):
    path = tmp_path / f"asx_{tag}.csv"
    path.write_text(f"generated {tag}\n\nASX code,Company name\nX,Y\n")
    load = register_load(conn, path, source="asx_listed_companies", as_at=as_at)
    conn.commit()
    return load


# --- load bookkeeping ---------------------------------------------------

def test_reference_load_is_idempotent_on_content(conn, tmp_path):
    path = tmp_path / "asic.csv"
    path.write_text(ASIC_PIPE)
    first = register_load(conn, path, source="asic_companies", as_at=date(2026, 8, 1))
    conn.commit()
    second = register_load(conn, path, source="asic_companies", as_at=date(2026, 8, 1))
    conn.commit()
    assert first.load_id == second.load_id
    assert not first.already_loaded and second.already_loaded


def test_reference_file_lands_in_the_raw_zone(conn, tmp_path, asic_loaded):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE doc_id = %s", (asic_loaded.doc_id,))
        doc = cur.fetchone()
    assert doc["sha256"] and doc["possession_source"] == "reference_download"
    assert doc["parse_status"] == "not_applicable"  # reference data is never parsed
    assert latest_load(conn, "asic_companies")["load_id"] == asic_loaded.load_id


def test_asic_registry_deduplicates_within_a_file(conn, asic_loaded):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM asic_registry")
        assert cur.fetchone()["n"] == 5


# --- entity master construction ----------------------------------------

def test_listing_snapshot_creates_entities_with_acns(conn, tmp_path, asic_loaded):
    load = _listing_load(conn, tmp_path, date(2026, 8, 14), "1")
    result = _snapshot(conn, [
        ListedCompany("XYZ", "Xyz Mining Limited"),
        ListedCompany("ABC", "Abc Health Ltd"),
    ], date(2026, 8, 14), load.load_id)
    conn.commit()

    assert result.entities_created == 2
    assert result.acn_resolved == 2 and result.acn_unresolved == 0
    with conn.cursor() as cur:
        cur.execute("""SELECT e.acn, l.ticker FROM entities e
                       JOIN listings l USING (entity_id) ORDER BY l.ticker""")
        rows = cur.fetchall()
    assert [(r["acn"], r["ticker"]) for r in rows] == [
        ("234567891", "ABC"), ("123456789", "XYZ")]


def test_unresolvable_company_is_created_and_queued_not_dropped(conn, tmp_path, asic_loaded):
    load = _listing_load(conn, tmp_path, date(2026, 8, 14), "1")
    result = _snapshot(conn, [
        ListedCompany("FOR", "Foreign Incorporated Plc"),
        ListedCompany("TWN", "Twin Name Limited"),   # two ASIC registrations
    ], date(2026, 8, 14), load.load_id)
    conn.commit()

    assert result.acn_unresolved == 2
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM entities WHERE acn IS NULL")
        assert cur.fetchone()["n"] == 2      # created, so the listing is tracked
        cur.execute("""SELECT reason FROM review_items
                       WHERE kind = 'resolution' ORDER BY item_id""")
        reasons = [r["reason"] for r in cur.fetchall()]
    assert len(reasons) == 2
    assert any("multiple ASIC registrations" in r for r in reasons)


def test_reapplying_the_same_snapshot_is_a_no_op(conn, tmp_path, asic_loaded):
    load = _listing_load(conn, tmp_path, date(2026, 8, 14), "1")
    companies = [ListedCompany("XYZ", "Xyz Mining Limited")]
    _snapshot(conn, companies, date(2026, 8, 14), load.load_id)
    conn.commit()
    second = _snapshot(conn, companies, date(2026, 8, 21), load.load_id)
    conn.commit()
    assert second.entities_created == 0 and second.delisted == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM listings WHERE valid_to IS NULL")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM entity_names")
        assert cur.fetchone()["n"] == 1   # unique index prevents name churn


# --- survivorship -------------------------------------------------------

def test_absent_company_is_closed_not_deleted(conn, tmp_path, asic_loaded):
    load = _listing_load(conn, tmp_path, date(2026, 8, 14), "1")
    _snapshot(conn, [
        ListedCompany("XYZ", "Xyz Mining Limited"),
        ListedCompany("ABC", "Abc Health Ltd"),
    ], date(2026, 8, 14), load.load_id)
    conn.commit()

    # ABC is taken over and drops off the next file.
    load2 = _listing_load(conn, tmp_path, date(2026, 9, 14), "2")
    result = _snapshot(conn, [ListedCompany("XYZ", "Xyz Mining Limited")],
                       date(2026, 9, 14), load2.load_id)
    conn.commit()
    assert result.delisted == 1

    with conn.cursor() as cur:
        # Invariant 4: the entity, its name and its history all survive.
        cur.execute("SELECT count(*) AS n FROM entities")
        assert cur.fetchone()["n"] == 2
        cur.execute("""SELECT listed_from, listed_to, delist_reason
                       FROM universe_membership um
                       JOIN listings l USING (entity_id)
                       WHERE l.ticker = 'ABC'""")
        row = cur.fetchone()
        assert row["listed_to"] == date(2026, 9, 13)  # inclusive: last day listed
        assert row["delist_reason"] == "absent_from_listing_file"
        # It remains in a historical universe query as at a date it was listed.
        cur.execute(
            """SELECT count(*) AS n FROM universe_membership
               WHERE listed_from <= %s AND (listed_to IS NULL OR listed_to >= %s)""",
            (date(2026, 8, 20), date(2026, 8, 20)),
        )
        assert cur.fetchone()["n"] == 2


def test_truncated_snapshot_is_refused(conn, tmp_path, asic_loaded):
    load = _listing_load(conn, tmp_path, date(2026, 8, 14), "1")
    _snapshot(conn, [ListedCompany(f"T{i:02d}", f"Company {i} Limited")
                     for i in range(60)], date(2026, 8, 14), load.load_id)
    conn.commit()

    # A download that got cut off would otherwise delist 59 companies as fact.
    # Isolate the proportion guard from the absolute-size guard.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("asx.reference.asx_listed.MIN_PLAUSIBLE_ROWS", 1)
    try:
        load2 = _listing_load(conn, tmp_path, date(2026, 8, 21), "2")
        with pytest.raises(ImplausibleSnapshotError) as e:
            apply_listing_snapshot(conn, [ListedCompany("T00", "Company 0 Limited")],
                                   date(2026, 8, 21), load2.load_id, allow_shrink=False)
        assert "partial download" in str(e.value)
    finally:
        monkeypatch.undo()
    conn.rollback()


def test_short_snapshot_refused_even_with_no_prior_universe(conn, tmp_path, asic_loaded):
    load = _listing_load(conn, tmp_path, date(2026, 8, 14), "1")
    with pytest.raises(ImplausibleSnapshotError):
        apply_listing_snapshot(conn, [ListedCompany("XYZ", "Xyz Mining Limited")],
                               date(2026, 8, 14), load.load_id, allow_shrink=False)
    conn.rollback()


def test_ticker_moving_to_a_new_entity_closes_the_old_listing(conn, tmp_path, asic_loaded):
    load = _listing_load(conn, tmp_path, date(2020, 1, 1), "1")
    _snapshot(conn, [ListedCompany("REC", "Dead Co Limited")],
              date(2020, 1, 1), load.load_id)
    conn.commit()
    # Years later the code is recycled onto an unrelated company — the exact
    # scenario Invariant 1 exists for.
    load2 = _listing_load(conn, tmp_path, date(2026, 1, 1), "2")
    _snapshot(conn, [ListedCompany("REC", "Xyz Mining Limited")],
              date(2026, 1, 1), load2.load_id)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("""SELECT entity_id, valid_from, valid_to FROM listings
                       WHERE ticker = 'REC' ORDER BY valid_from""")
        rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0]["valid_to"] == date(2025, 12, 31)  # inclusive close
    assert rows[1]["valid_to"] is None
    assert rows[0]["entity_id"] != rows[1]["entity_id"]
    # And no overlap remains for the resolver to trip over.
    assert ticker_integrity(conn).meets_criterion
    # The transition is queued for a human: rename or recycled code is not
    # decidable from this file, and is never merged automatically.
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) AS n FROM review_items
                       WHERE reason LIKE 'ticker REC moved%'""")
        assert cur.fetchone()["n"] == 1

    # The transition date itself must resolve to exactly one entity — an
    # exclusive close would make both rows match and silently resolve nothing.
    from asx.ingest.detection import entity_for_ticker
    assert entity_for_ticker(conn, "REC", date(2026, 1, 1)) == rows[1]["entity_id"]
    assert entity_for_ticker(conn, "REC", date(2025, 12, 31)) == rows[0]["entity_id"]


# --- acceptance evidence ------------------------------------------------

def test_coverage_measures_acceptance_criteria(conn, tmp_path, asic_loaded):
    load = _listing_load(conn, tmp_path, date(2026, 8, 14), "1")
    _snapshot(conn, [
        ListedCompany("XYZ", "Xyz Mining Limited"),
        ListedCompany("ABC", "Abc Health Ltd"),
        ListedCompany("FOR", "Foreign Incorporated Plc"),
    ], date(2026, 8, 14), load.load_id)
    conn.commit()

    coverage = acn_coverage(conn)
    assert coverage.total == 3 and coverage.with_acn == 2
    assert coverage.unresolved == 1
    assert not coverage.meets_criterion          # 67% < 99%: honestly failing

    # Marking the unresolved one as foreign-incorporated is the human decision
    # the review item asks for, and it counts toward coverage.
    with conn.cursor() as cur:
        cur.execute("UPDATE entities SET entity_kind = 'foreign' WHERE acn IS NULL")
    conn.commit()
    coverage = acn_coverage(conn)
    assert coverage.flagged_foreign == 1 and coverage.meets_criterion
