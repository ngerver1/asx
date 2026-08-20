"""Where knowable_at comes from, and what it must never come from.

knowable_at is the column every analytic joins on (Invariant 2). A signal
computed against a timestamp nobody could have observed is a backtest of a
fact nobody could have known, so each of these pins one way that could
happen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pytest

from asx.ingest.lodgement import (
    _parse_pdf_date, alert_matches_document, pdf_created_at, resolve,
)

AEST = timezone(timedelta(hours=10))
DOCS = Path(__file__).parent.parent / "fixtures" / "app3y" / "documents"


@lru_cache(maxsize=None)
def _doc(name: str) -> bytes:
    path = DOCS / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    return path.read_bytes()


# --- reading the PDF's own timestamp ---------------------------------------

def test_a_pdf_timestamp_is_read_with_its_offset():
    assert _parse_pdf_date("D:20260819171523+10'00'") == datetime(
        2026, 8, 19, 17, 15, 23, tzinfo=AEST)
    assert _parse_pdf_date("D:20260819164635+09'30'") == datetime(
        2026, 8, 19, 16, 46, 35, tzinfo=timezone(timedelta(hours=9, minutes=30)))


def test_utc_is_an_offset_and_is_accepted():
    """Seven captured documents write their timestamp as "Z" or "Z00'00'".
    That is not a missing offset, it is a stated one."""
    for raw in ("D:20260813123704Z", "D:20260819060805Z00'00'"):
        got = _parse_pdf_date(raw)
        assert got is not None and got.utcoffset() == timedelta(0), raw


def test_a_timestamp_with_no_offset_at_all_is_refused():
    """Assuming UTC where none is stated would move a Sydney afternoon
    lodgement to the previous market day, and market_date is what the signal
    keys on (SPEC §3)."""
    assert _parse_pdf_date("D:20260819171523") is None
    assert _parse_pdf_date("") is None
    assert _parse_pdf_date("19 August 2026") is None


def test_real_documents_carry_a_timestamp():
    assert pdf_created_at(_doc("6A1339259.pdf")) is not None
    assert pdf_created_at(b"%PDF-1.7 not really a pdf") is None


# --- choosing a source ------------------------------------------------------

def test_the_alert_wins_where_there_is_one():
    published = datetime(2026, 8, 19, 17, 22, tzinfo=AEST)
    got = resolve(alert_published_at=published, pdf_content=_doc("6A1339259.pdf"))
    assert got.at == published
    assert got.source == "market_index_alert"
    assert got.corroborated_by == "pdf_creation"


def test_the_pdf_timestamp_is_used_when_there_is_no_alert():
    got = resolve(pdf_content=_doc("6A1339259.pdf"))
    assert got.known
    assert got.source == "pdf_creation"
    assert got.corroborated_by is None


def test_no_source_means_no_timestamp_and_no_pretending():
    """A document with neither yields nothing. The parser's validation then
    refuses it, and no canonical row is written — a trade with an invented
    knowable_at is worse than a trade that is missing."""
    got = resolve(pdf_content=b"not a pdf at all")
    assert not got.known
    assert got.source is None


def test_a_timestamp_always_says_where_it_came_from():
    for lodgement in (resolve(pdf_content=_doc("6A1339259.pdf")),
                      resolve(alert_published_at=datetime(2026, 8, 19, 17, 22, tzinfo=AEST)),
                      resolve(pdf_content=b"nope")):
        assert (lodgement.at is None) == (lodgement.source is None)


# --- pairing an alert to a document ----------------------------------------

def test_a_company_name_alone_does_not_pair_an_alert_to_a_document():
    """Companies lodge repeatedly. Name matching alone pairs a document with
    whichever of that company's announcements happens to be in the mailbox —
    four of the eight name-only matches in the captured corpus were a week
    out, which would have dated four documents to another announcement."""
    content = _doc("6A1339259.pdf")
    created = pdf_created_at(content)
    assert alert_matches_document(
        alert_entity="Catalyst Metals Limited",
        document_entity="Catalyst Metals Ltd",
        alert_published_at=created + timedelta(minutes=6),
        pdf_content=content)
    # Same company, an announcement a week later: not this document.
    assert not alert_matches_document(
        alert_entity="Catalyst Metals Limited",
        document_entity="Catalyst Metals Ltd",
        alert_published_at=created + timedelta(days=7),
        pdf_content=content)


def test_an_unpinnable_pairing_is_refused():
    """With no timestamp in the document there is nothing to pin the pairing
    to, so the alert is not accepted as describing it."""
    assert not alert_matches_document(
        alert_entity="Catalyst Metals Limited",
        document_entity="Catalyst Metals Ltd",
        alert_published_at=datetime(2026, 8, 19, 17, 22, tzinfo=AEST),
        pdf_content=b"not a pdf")


def test_different_companies_never_pair():
    content = _doc("6A1339259.pdf")
    assert not alert_matches_document(
        alert_entity="Terra Critical Minerals Limited",
        document_entity="Catalyst Metals Ltd",
        alert_published_at=pdf_created_at(content) + timedelta(minutes=6),
        pdf_content=content)


# --- the corpus -------------------------------------------------------------

def test_almost_every_captured_document_can_be_dated():
    """Without a timestamp a document produces no rows at all, so this is a
    direct measure of how much of the corpus can enter the dataset."""
    documents = sorted(DOCS.glob("*.pdf"))
    if len(documents) < 50:
        pytest.skip("corpus not present")
    undated = [d.name for d in documents
               if not resolve(pdf_content=d.read_bytes()).known]
    # Three documents carry no creation date at all. Their /ModDate is stamped
    # in a US timezone — an intermediary's handling time, not the ASX's
    # release — so they stay undated and produce no rows.
    assert len(undated) <= 5, f"{len(undated)} undatable: {undated}"
