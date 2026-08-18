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


# --- the Kingston/Nexus collision (found by loading the real register) ----

# ACN 009148529 is KINGSTON RESOURCES, formerly NEXUS MINERALS NL. ACN
# 122074006 is the unrelated NEXUS MINERALS LIMITED listed today. Both names
# normalise to "NEXUS MINERALS", and a deregistered third registration shares
# it too. This is real data, not a contrived case.
# Column order and blank-field placement mirror the real 202608 extract: the
# transition date rides on the FORMER-name rows, and only the current name
# carries the 'Y' indicator.
ASIC_NEXUS = """ACN|Company Name|Type|Class|Status|Date of Registration|Date of Deregistration|Current Name Indicator|ABN|Current Name Start Date
009148529|KINGSTON RESOURCES LIMITED|APUB|LMSH|REGD|06/09/1985||Y||
009148529|NEXUS MINERALS NL|APUB|LMSH|REGD|06/09/1985||||11/10/2012
122074006|NEXUS MINERALS LIMITED|APUB|LMSH|REGD|23/02/2007||Y||
155124324|NEXUS MINERALS PTY LTD|APRV|LMSH|SOFF|01/06/2012|01/06/2019|||
"""


@pytest.fixture
def asic_nexus(conn, tmp_path):
    path = tmp_path / "asic_nexus.csv"
    path.write_text(ASIC_NEXUS)
    load = register_load(conn, path, source="asic_companies", as_at=date(2026, 8, 1))
    n = load_asic_registry(conn, path, load.load_id)
    mark_applied(conn, load.load_id, n)
    conn.commit()
    return load


def test_current_name_outranks_another_companys_former_name(conn, asic_nexus):
    """A live company must resolve to its own ACN even when its name is also
    some other company's former name."""
    from asx.reference.asic import resolve_acn_for_name

    match = resolve_acn_for_name(conn, "NEXUS MINERALS LIMITED")
    assert match.acn == "122074006", match.candidates
    assert match.basis == "current_name"

    # Kingston still resolves to itself.
    assert resolve_acn_for_name(conn, "Kingston Resources Ltd").acn == "009148529"


def test_a_former_name_never_merges_two_listed_companies(conn, asic_nexus):
    """The original bug: NXM merged onto Kingston's entity, giving one entity
    two unrelated open codes and overwriting Kingston's legal name."""
    _snapshot(conn, [ListedCompany("KSN", "KINGSTON RESOURCES LIMITED")],
              date(2026, 8, 17), asic_nexus.load_id)
    _snapshot(conn, [ListedCompany("KSN", "KINGSTON RESOURCES LIMITED"),
                     ListedCompany("NXM", "NEXUS MINERALS LIMITED")],
              date(2026, 8, 18), asic_nexus.load_id)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.ticker, e.entity_id, e.acn FROM listings l
               JOIN entities e USING (entity_id)
               WHERE l.valid_to IS NULL ORDER BY l.ticker"""
        )
        rows = {r["ticker"]: r for r in cur.fetchall()}
    assert rows["KSN"]["entity_id"] != rows["NXM"]["entity_id"]
    assert rows["KSN"]["acn"] == "009148529"
    assert rows["NXM"]["acn"] == "122074006"

    # Kingston's legal name survived.
    with conn.cursor() as cur:
        cur.execute(
            """SELECT name FROM entity_names
               WHERE entity_id = %s AND name_kind = 'legal' AND valid_to IS NULL""",
            (rows["KSN"]["entity_id"],),
        )
        assert cur.fetchone()["name"] == "KINGSTON RESOURCES LIMITED"
    assert not ticker_integrity(conn).collisions


def test_second_code_under_a_different_name_is_refused_not_merged(
    conn, asic_loaded, monkeypatch,
):
    """Defence in depth: if resolution ever hands back a shared entity anyway
    — a future resolver change, a hand-edited row — one entity must still
    never end up holding two open codes under different names.

    Resolution is stubbed to always return the first entity, which is the
    worst case the guard exists to survive.
    """
    from asx.reference import asx_listed as mod

    _snapshot(conn, [ListedCompany("XYZ", "Xyz Mining Limited")],
              date(2026, 8, 17), asic_loaded.load_id)
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM entities WHERE acn = '123456789'")
        xyz = cur.fetchone()["entity_id"]

    monkeypatch.setattr(mod, "_resolve_or_create_entity",
                        lambda *a, **k: (xyz, False, True))
    _snapshot(conn, [ListedCompany("XYZ", "Xyz Mining Limited"),
                     ListedCompany("UNR", "Unrelated Co Limited")],
              date(2026, 8, 18), asic_loaded.load_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticker, entity_id FROM listings WHERE valid_to IS NULL ORDER BY ticker"
        )
        rows = {r["ticker"]: r["entity_id"] for r in cur.fetchall()}
        cur.execute(
            "SELECT count(*) AS n FROM review_items WHERE reason LIKE '%%Refused to merge%%'"
        )
        assert cur.fetchone()["n"] == 1
    assert rows["XYZ"] != rows["UNR"]


def test_dual_class_codes_still_share_one_entity(conn, asic_loaded):
    """The guard must not break genuine dual-class listings, where the
    publisher prints the same company name against both codes."""
    _snapshot(conn, [ListedCompany("XYZ", "Xyz Mining Limited", market_cap_aud=5e7),
                     ListedCompany("XYZN", "Xyz Mining Limited", market_cap_aud=5e7)],
              date(2026, 8, 18), asic_loaded.load_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticker, entity_id FROM listings WHERE valid_to IS NULL ORDER BY ticker"
        )
        rows = {r["ticker"]: r["entity_id"] for r in cur.fetchall()}
        # ...and each code keeps its own snapshot. Keyed per entity, the second
        # code overwrote the first and four real codes read as "market cap
        # unknown" on a size screen.
        cur.execute(
            "SELECT ticker, market_cap_aud FROM listing_snapshots ORDER BY ticker"
        )
        snaps = {r["ticker"]: r["market_cap_aud"] for r in cur.fetchall()}
    assert rows["XYZ"] == rows["XYZN"]
    assert set(snaps) == {"XYZ", "XYZN"}
    assert all(v == 5e7 for v in snaps.values())


# --- registration type discriminates issuers from their subsidiaries ------

# The real pattern behind ~150 unresolved companies: a listed public company
# and a proprietary company of the same normalised name.
ASIC_TYPES = """ACN|Company Name|Type|Class|Status|Date of Registration|Date of Deregistration|Current Name Indicator|ABN|Current Name Start Date
051588348|CSL LIMITED|APUB|LMSH|REGD|01/04/1991||Y||
602039485|CSL HOLDINGS PTY LTD|APTY|LMSH|REGD|02/02/2015||Y||
000123456|OVERSEAS MINING PLC|FNOS|NONE|REGD|03/03/2003||Y||
099092618|SETTLE SAFE PTY LTD|APTY|LMSH|REGD|04/04/2001||Y||
099092618|GOODMAN GROUP|APTY|LMSH|REGD|04/04/2001||||01/01/2010
"""


@pytest.fixture
def asic_types(conn, tmp_path):
    path = tmp_path / "asic_types.csv"
    path.write_text(ASIC_TYPES)
    load = register_load(conn, path, source="asic_companies", as_at=date(2026, 8, 1))
    n = load_asic_registry(conn, path, load.load_id)
    mark_applied(conn, load.load_id, n)
    conn.commit()
    return load


def test_a_proprietary_namesake_does_not_make_a_listed_company_ambiguous(
    conn, asic_types,
):
    from asx.reference.asic import resolve_acn_for_name

    # Without the issuer filter the two CSLs are simply ambiguous.
    assert resolve_acn_for_name(conn, "CSL Limited").acn is None
    # With it, the public company is the only candidate that can list.
    match = resolve_acn_for_name(conn, "CSL Limited", listed_issuer=True)
    assert match.acn == "051588348"
    assert not match.is_foreign


def test_former_name_of_a_proprietary_shell_is_not_a_listed_issuer(
    conn, asic_types,
):
    """GOODMAN GROUP is a former name of a proprietary company. Resolving a
    listed issuer onto it attached the wrong ACN in the real load."""
    from asx.reference.asic import resolve_acn_for_name

    assert resolve_acn_for_name(conn, "Goodman Group").acn == "099092618"
    assert resolve_acn_for_name(conn, "Goodman Group", listed_issuer=True).acn is None


def test_registered_foreign_company_gets_an_arbn_and_a_foreign_flag(
    conn, asic_types,
):
    _snapshot(conn, [ListedCompany("OVM", "Overseas Mining Plc")],
              date(2026, 8, 18), asic_types.load_id)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.acn, e.arbn, e.entity_kind FROM entities e
               JOIN listings l USING (entity_id) WHERE l.ticker = 'OVM'"""
        )
        row = cur.fetchone()
    assert row["acn"] is None
    assert row["arbn"] == "000123456"
    assert row["entity_kind"] == "foreign"
    # Criterion 0.2 counts it as covered via the explicit flag, not as an ACN.
    cov = acn_coverage(conn)
    assert cov.with_acn == 0 and cov.flagged_foreign == 1 and cov.unresolved == 0


def test_the_suffix_convention_is_undone(conn, asic_types):
    """The ASX prints a leading "The" as a trailing "(THE)"."""
    from asx.reference.asx_listed import listed_name_variants

    assert listed_name_variants("ENVIRONMENTAL GROUP LIMITED (THE)") == [
        "ENVIRONMENTAL GROUP LIMITED (THE)", "THE ENVIRONMENTAL GROUP LIMITED",
    ]
    assert listed_name_variants("CSL Limited") == ["CSL Limited"]


def test_unresolved_listed_companies_do_not_halt_parser_auto_accept(
    conn, asic_loaded,
):
    """A backlog of "which ACN is this?" questions must not stop the parsing
    pipeline. Listed trusts hold an ARSN and can never be answered from the
    company register, so a halting rule would stall Phase 1 permanently."""
    from datetime import timedelta

    from asx.parse.framework import auto_accept_halted

    _snapshot(conn, [ListedCompany("TRU", "Some Property Trust")],
              date(2026, 8, 18), asic_loaded.load_id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM review_items WHERE kind='resolution'")
        assert cur.fetchone()["n"] == 1
        # Age it well past the review SLA.
        cur.execute("UPDATE review_items SET created_at = now() - interval '60 days'")
    assert not auto_accept_halted(conn, "app3y")

    # A stale item raised by a parse still halts.
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents (source, doc_class, sha256, storage_path,
                                      fetched_at, possession_source, parse_status)
               VALUES ('t', 'app3y', 'deadbeef', '/tmp/x', now(),
                       'manual_capture', 'parsed')
               RETURNING doc_id"""
        )
        doc_id = cur.fetchone()["doc_id"]
        cur.execute(
            """INSERT INTO review_items (kind, doc_id, payload, reason, created_at)
               VALUES ('extraction', %s, '{}'::jsonb, 'disagreement',
                       now() - interval '60 days')""",
            (doc_id,),
        )
    assert auto_accept_halted(conn, "app3y")


def test_identity_rate_alarms_only_on_a_regression(conn, asic_loaded):
    """The unidentified residue is accepted at its structural floor and
    alarms when it rises — the resolver breaking is otherwise silent."""
    from datetime import datetime, timezone

    from asx.monitor.checks import check_entity_identity_rate

    now = datetime.now(timezone.utc)
    # 1 of 20 unidentified (5%) sits under the ceiling.
    companies = [ListedCompany(f"R{i:02d}", f"Resolvable {i} Limited")
                 for i in range(19)] + [ListedCompany("TRU", "Some Property Trust")]
    with conn.cursor() as cur:
        for i in range(19):
            cur.execute(
                """INSERT INTO asic_registry (acn, name, name_norm, is_current_name,
                       status, company_type, load_id)
                   VALUES (%s, %s, %s, true, 'REGD', 'APUB', %s)""",
                (f"9{i:08d}", f"RESOLVABLE {i} LIMITED", f"RESOLVABLE {i}",
                 asic_loaded.load_id),
            )
    _snapshot(conn, companies, date(2026, 8, 18), asic_loaded.load_id)
    assert check_entity_identity_rate(conn, now) == []

    # A resolver regression that strands a quarter of the universe must not
    # pass silently just because every company still got an entity.
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE entities SET acn = NULL, arbn = NULL
               WHERE entity_id IN (SELECT entity_id FROM entities
                                   WHERE acn IS NOT NULL LIMIT 5)"""
        )
    alarms = check_entity_identity_rate(conn, now)
    assert len(alarms) == 1 and alarms[0].check == "entity_identity"


# --- point-in-time universe export ---------------------------------------

def test_universe_export_is_point_in_time(conn, asic_loaded):
    """The export must answer "what was in scope on D", not "what is in scope
    now, back-projected" — the survivorship trap Invariant 4 exists for."""
    import csv as _csv
    import io as _io

    from asx.universe.export import universe_csv

    _snapshot(conn, [ListedCompany("XYZ", "Xyz Mining Limited", listing_date=date(2015, 7, 1)),
                     ListedCompany("ABC", "Abc Health Limited", listing_date=date(2016, 2, 2))],
              date(2026, 8, 17), asic_loaded.load_id)
    # ABC delists: absent from the next snapshot.
    _snapshot(conn, [ListedCompany("XYZ", "Xyz Mining Limited", listing_date=date(2015, 7, 1))],
              date(2026, 8, 18), asic_loaded.load_id)

    def tickers(as_at):
        return {r["ticker"] for r in _csv.DictReader(_io.StringIO(universe_csv(conn, as_at)))}

    assert tickers(date(2026, 8, 17)) == {"XYZ", "ABC"}   # ABC was in scope
    assert tickers(date(2026, 8, 18)) == {"XYZ"}          # and is not now
    assert tickers(date(2015, 12, 31)) == {"XYZ"}         # before ABC listed


def test_universe_export_dates_a_single_former_name_but_never_guesses(
    conn, asic_nexus,
):
    """ASIC dates only the transition to a company's CURRENT name. One former
    name is therefore unambiguous at a past date; several are not, and the
    real register gives Kingston six sharing one range. A guess would read as
    fact (Invariant 8), so the column goes empty instead."""
    import csv as _csv
    import io as _io

    from asx.universe.export import universe_csv

    _snapshot(conn, [ListedCompany("KSN", "KINGSTON RESOURCES LIMITED",
                                   listing_date=date(1987, 3, 5))],
              date(2026, 8, 18), asic_nexus.load_id)

    def name_in_1990():
        rows = {r["ticker"]: r for r in
                _csv.DictReader(_io.StringIO(universe_csv(conn, date(1990, 1, 1))))}
        return rows["KSN"]["company_name"], rows["KSN"]["current_company_name"]

    # One former name in force: known.
    assert name_in_1990() == ("NEXUS MINERALS NL", "KINGSTON RESOURCES LIMITED")

    # A second former name over the same range: no longer knowable.
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO entity_names (entity_id, name, name_norm, name_kind,
                                         valid_from, valid_to, source_load_id)
               SELECT entity_id, 'DRY CREEK MINING NL', 'DRY CREEK MINING',
                      'former', valid_from, valid_to, source_load_id
                 FROM entity_names WHERE name_kind = 'former' LIMIT 1"""
        )
    assert name_in_1990() == ("", "KINGSTON RESOURCES LIMITED")
