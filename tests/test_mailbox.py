import email
from datetime import timezone

from asx.ingest.mailbox import detection_from_email

RAW_ALERT = """From: alerts@marketindex.com.au
To: owner@example.com
Subject: XYZ - Change in Director's Interest Notice
Message-ID: <alert-1@marketindex.com.au>
Date: Fri, 14 Aug 2026 09:35:00 +1000
Content-Type: text/plain

XYZ Limited lodged an announcement at 14/08/2026 9:30 AM.

View on ASX: https://www.asx.com.au/asxpdf/20260814/pdf/xyz.pdf
Company site: https://xyzlimited.com.au/investors/3y-aug26.pdf
"""


def _msg(raw=RAW_ALERT):
    return email.message_from_string(raw)


def test_extracts_ticker_title_and_market_time():
    d = detection_from_email(_msg())
    assert d.detection_source == "market_index_alert"
    assert d.ticker == "XYZ"
    assert d.title == "Change in Director's Interest Notice"
    # 09:30 Sydney on 14 Aug 2026 == 23:30 UTC on the 13th.
    assert d.lodged_at.astimezone(timezone.utc).isoformat() == "2026-08-13T23:30:00+00:00"


def test_asx_links_are_never_offered_for_fetching():
    # The single most likely accidental route to asx.com.au is following a
    # link out of an alert email. Detection strips them.
    d = detection_from_email(_msg())
    assert all("asx.com.au" not in u for u in d.document_urls)
    assert d.document_urls == ["https://xyzlimited.com.au/investors/3y-aug26.pdf"]


def test_detection_key_is_stable_and_distinct():
    a = detection_from_email(_msg())
    b = detection_from_email(_msg())
    assert a.key() == b.key()  # re-reading the mailbox is idempotent
    other = detection_from_email(_msg(RAW_ALERT.replace("XYZ", "ABC")))
    assert other.key() != a.key()


def test_unreadable_fields_stay_none_rather_than_guessed():
    raw = """From: someone@example.com
To: owner@example.com
Subject:\x20
Message-ID: <bare@example.com>

no content
"""
    d = detection_from_email(email.message_from_string(raw))
    assert d.ticker is None
    assert d.detection_source == "ir_email"
