"""Gold-set regression for the Market Index alert format.

Built from five real alerts saved on 2026-08-19 — the first real emails this
platform has seen. CLAUDE.md requires the fixture set before the parser, so
these expectations were read off the emails by eye and this file was failing
when it was written.

What the real format turned out to be, none of which the original guessed
rules handled:

  Subject:  ASX:{TICKER} - {Announcement|Sensitive Ann}: {title}
  Body:     Published: DD/MM/YY, HH:MMam (AEST)      <- two-digit year
            | {Company Name} ({TICKER})
            <https://www.marketindex.com.au/asx/{code}/announcements/
             {slug}-{ASX_ANNOUNCEMENT_ID}?utm_...>  <- wrapped across lines

Two things matter beyond the field values:

  - "Sensitive Ann" is the ASX price-sensitive flag. It is a real field on
    `documents` that nothing was populating.
  - EVERY link in the HTML part is a Mandrill click-tracker. Fetching one
    would register a click with the provider's ESP, so the URLs are taken
    from the plain-text part instead, where they are real but hard-wrapped.
"""

from __future__ import annotations

import email
import email.policy
import json
from pathlib import Path

import pytest

from asx.ingest.mailbox import detection_from_email

FIXTURES = Path(__file__).parent.parent / "fixtures" / "mailbox"
GOLD = json.loads((FIXTURES / "gold.json").read_text())["cases"]


def _load(name: str):
    with open(FIXTURES / name, "rb") as fh:
        return email.message_from_binary_file(fh, policy=email.policy.default)


@pytest.mark.parametrize("case", GOLD, ids=[c["file"][13:-4] for c in GOLD])
def test_marketindex_alert_fields(case):
    d = detection_from_email(_load(case["file"]))
    assert d.detection_source == "market_index_alert"
    assert d.ticker == case["ticker"]
    assert d.title == case["title"]
    assert d.price_sensitive is case["price_sensitive"]
    assert d.lodged_at is not None, "the alert states a Published time"
    assert d.lodged_at.isoformat() == case["lodged_at_utc"].replace("+00:00", "+00:00") \
        or d.lodged_at.astimezone().isoformat() is not None
    from datetime import timezone
    assert d.lodged_at.astimezone(timezone.utc).isoformat() == case["lodged_at_utc"]
    assert d.company_name == case["company_name"]
    assert d.announcement_id == case["announcement_id"]


@pytest.mark.parametrize("case", GOLD, ids=[c["file"][13:-4] for c in GOLD])
def test_the_owner_gets_a_working_link_and_the_fetcher_gets_nothing(case):
    """Market Index alerts carry no PDF and no asx.com.au link: the only route
    to the document is their announcement page. That page is the owner's
    capture link, and nothing in these emails is machine-fetchable."""
    d = detection_from_email(_load(case["file"]))
    assert case["announcement_url"] in d.manual_open_urls
    assert d.document_urls == []
    # Never a tracking redirect: fetching one registers a click with the ESP.
    assert not any("mandrillapp.com" in u for u in
                   d.manual_open_urls + d.document_urls)
    assert not any(h in u for u in d.manual_open_urls + d.document_urls
                   for h in ("hotjar", "facebook", "linkedin", "twitter",
                             "/alerts"))


def test_the_format_is_recognised_so_nothing_is_flagged_as_unreadable():
    for case in GOLD:
        d = detection_from_email(_load(case["file"]))
        assert d.format_recognised, case["file"]


def test_one_announcement_per_email_not_a_digest():
    """The open question before real emails existed. Answered: Market Index
    sends one alert per announcement, so no detection is being lost to a
    digest collapsing into a single row."""
    subjects = {str(_load(c["file"])["Subject"]) for c in GOLD}
    assert len(subjects) == len(GOLD)
    for s in subjects:
        assert s.count("ASX:") == 1


def test_an_officers_interest_notice_is_not_quietly_filed_as_other():
    """Alcoa (AAI), a foreign issuer listed via CDIs, lodges "Change of
    Officer's Interest Notice" — an officer, not a director, so the Appendix
    3Y rule correctly does not match. But 'other' means no parser ever looks
    at it and the detection is terminal, so an unexamined 'other' here is a
    substantive default on an ambiguous case (Invariant 8)."""
    from asx.ingest.classifier import classify

    assert classify("Change of Officer's Interest Notice") == ("other", "ambiguous")
    # A genuine non-disclosure title is still an ordinary 'other'.
    assert classify("Wapiti drilling intersects phosphate") == ("other", "default")


def test_ambiguous_classification_reaches_a_human(conn):
    from asx.ingest.detection import record_detection

    doc_id, _ = record_detection(conn, detection_from_email(
        _load("marketindex_aai_change_of_officers_interest_notice.eml")))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT reason FROM review_items WHERE kind='detection' AND doc_id=%s",
            (doc_id,))
        reasons = [r["reason"] for r in cur.fetchall()]
    assert any("no parser will ever look at it" in r for r in reasons)


def test_the_asx_announcement_number_is_the_identity(conn):
    """Two providers reporting the same lodgement, or one provider resending,
    must produce ONE detection. Only the ASX's own number can do that — an
    ESP Message-ID is meaningless across sources."""
    from asx.ingest.detection import record_detection

    msg = _load("marketindex_axp_final_directors_interest_notice.eml")
    first_id, is_new = record_detection(conn, detection_from_email(msg))
    assert is_new

    resent = detection_from_email(msg)
    resent.source_ref = "<a-completely-different-message-id@elsewhere>"
    second_id, is_new = record_detection(conn, resent)
    assert not is_new and second_id == first_id
