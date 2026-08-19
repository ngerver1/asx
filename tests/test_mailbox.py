import email
from datetime import timezone

from asx.ingest.mailbox import detection_from_email

# Shaped like a real Market Index alert (see fixtures/mailbox/ for the
# genuine articles). The ASX and company-site links are NOT what Market Index
# actually sends — real alerts carry neither — but they exercise the URL
# partition, which the real ones cannot.
RAW_ALERT = """From: Market Index Alerts <no-reply@marketindex.com.au>
To: owner@example.com
Subject: ASX:XYZ - Announcement: Change in Director's Interest Notice
Message-ID: <alert-1@marketindex.com.au>
Date: Fri, 14 Aug 2026 09:35:00 +1000
Content-Type: text/plain

| XYZ Limited (XYZ)   Published: 14/08/26, 09:30am (AEST)

View on ASX: <https://www.asx.com.au/asxpdf/20260814/pdf/xyz.pdf>
Company site: <https://xyzlimited.com.au/investors/3y-aug26.pdf>
"""


def _msg(raw=RAW_ALERT):
    return email.message_from_string(raw)


def test_extracts_ticker_title_and_market_time():
    d = detection_from_email(_msg())
    assert d.detection_source == "market_index_alert"
    assert d.ticker == "XYZ"
    assert d.title == "Change in Director's Interest Notice"
    assert d.price_sensitive is False       # "Announcement", not "Sensitive Ann"
    assert d.company_name == "XYZ Limited"
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
    other = detection_from_email(
        _msg(RAW_ALERT.replace("alert-1@", "alert-2@").replace("XYZ", "ABC")))
    assert other.key() != a.key()


def test_detection_key_does_not_move_when_the_parser_improves():
    """The rules in SENDER_RULES are uncalibrated guesses that WILL change
    once real Market Index emails exist. If the key depended on what they
    extract, that calibration would re-insert every alert already ingested."""
    a = detection_from_email(_msg())
    b = detection_from_email(_msg())
    b.ticker, b.title = "DIFFERENT", "a completely different reading"
    assert a.key() == b.key()


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


# --- credential-free ingestion from saved emails -------------------------

def test_eml_directory_reads_saved_alerts(tmp_path):
    """Saved .eml files must ingest identically to IMAP: it is how fixtures
    get built, and how an alert already opened in the mail client (and so no
    longer UNSEEN) is recovered rather than silently lost."""
    from asx.ingest.mailbox import EmlDirectory

    (tmp_path / "sub").mkdir()
    (tmp_path / "a.eml").write_text(RAW_ALERT)
    (tmp_path / "sub" / "b.eml").write_text(RAW_ALERT.replace("XYZ", "ABC"))
    (tmp_path / "notes.pdf").write_bytes(b"%PDF-1.4 not an email")

    msgs = list(EmlDirectory(tmp_path).fetch_new())
    assert len(msgs) == 2
    tickers = {detection_from_email(m).ticker for m in msgs}
    assert tickers == {"XYZ", "ABC"}


def test_eml_directory_says_so_when_the_path_is_wrong(tmp_path):
    """A typo'd path must not look like an empty inbox — silence is an
    alarm (Invariant 7), and "0 new detections" is exactly what a working
    quiet day looks like."""
    import pytest

    from asx.ingest.mailbox import EmlDirectory

    with pytest.raises(FileNotFoundError):
        list(EmlDirectory(tmp_path / "nope").fetch_new())


# --- defects found by reviewing the path before it saw real email ---------

def test_rfc2047_encoded_subject_is_decoded_before_parsing():
    """Real alert subjects arrive encoded when they contain an apostrophe or
    a dash. Read raw, the sender rule cannot match and the ticker fallback
    then guesses from the encoded blob."""
    raw = ("From: alerts@marketindex.com.au\n"
           "Subject: =?utf-8?q?ASX:BHP_-_Announcement:_Change_of_Director=27s_"
           "Interest_Notice?=\n"
           "Message-ID: <enc@marketindex.com.au>\n"
           "Date: Mon, 18 Aug 2026 09:35:00 +1000\n\nbody\n")
    d = detection_from_email(email.message_from_string(raw, policy=__import__(
        "email.policy", fromlist=["default"]).default))
    assert d.ticker == "BHP"
    assert d.title == "Change of Director's Interest Notice"


def test_a_plausible_word_is_never_promoted_to_a_ticker():
    """"ASX" is itself a listed code (ASX Limited), so guessing it from a
    subject attached the announcement confidently to the wrong entity."""
    raw = ("From: alerts@marketindex.com.au\n"
           "Subject: New ASX announcement for BHP Group Ltd\n"
           "Message-ID: <guess@marketindex.com.au>\n\nbody\n")
    d = detection_from_email(email.message_from_string(raw))
    assert d.ticker != "ASX"
    assert d.ticker in (None, "BHP")


def test_alert_arrival_time_never_becomes_the_lodgement_time():
    """lodged_at feeds knowable_at (Invariant 2). The email Date header is
    when the ALERT was sent — a different fact about a different event."""
    raw = ("From: alerts@marketindex.com.au\n"
           "Subject: ASX:BHP - Announcement: Trading Halt\n"
           "Message-ID: <nolodge@marketindex.com.au>\n"
           "Date: Mon, 18 Aug 2026 09:35:00 +1000\n\n"
           "no lodgement timestamp anywhere in this body\n")
    d = detection_from_email(email.message_from_string(raw))
    assert d.lodged_at is None          # unknown is recorded as unknown
    assert d.detected_at is not None    # but we know when we found out


def test_a_naive_date_header_is_pinned_to_utc_not_left_ambiguous():
    raw = ("From: a@b\nSubject: ASX:BHP - Announcement: Trading Halt\n"
           "Message-ID: <n@n>\n"
           "Date: Mon, 18 Aug 2026 09:35:00 -0000\n\nbody\n")
    d = detection_from_email(email.message_from_string(raw))
    assert d.detected_at.tzinfo is not None


def test_unrecognised_alert_format_is_flagged_not_quietly_accepted():
    """A digest listing several announcements would otherwise collapse into
    one confident-looking detection and the rest vanish."""
    raw = ("From: alerts@marketindex.com.au\n"
           "Subject: Your watchlist: 4 new announcements - 18 Aug\n"
           "Message-ID: <digest@marketindex.com.au>\n\n"
           "BHP Change of Director's Interest Notice 10:12 AM\n"
           "PLS Appendix 3Y 2:15 PM\n")
    d = detection_from_email(email.message_from_string(raw))
    assert d.format_recognised is False


def test_provider_and_list_management_links_are_never_fetch_candidates():
    """Fetching the first non-ASX link would store an HTML page as the
    announcement — and could unsubscribe us from our only detection source."""
    raw = ("From: alerts@marketindex.com.au\n"
           "Subject: ASX:XYZ - Announcement: Appendix 3Y\n"
           "Message-ID: <urls@marketindex.com.au>\n\n"
           "View: https://www.asx.com.au/asxpdf/20260818/pdf/xyz.pdf\n"
           "Track: https://mandrillapp.com/track/click/31137204/x?p=abc\n"
           "Unsubscribe: https://mail.example.com/unsubscribe?u=9\n"
           "Watchlist: https://www.marketindex.com.au/alerts\n"
           "Document: https://xyzlimited.com.au/investors/3y-aug26.pdf\n")
    d = detection_from_email(email.message_from_string(raw))
    assert d.document_urls == ["https://xyzlimited.com.au/investors/3y-aug26.pdf"]
    # The ASX link is KEPT for the owner, not destroyed.
    assert d.manual_open_urls == [
        "https://www.asx.com.au/asxpdf/20260818/pdf/xyz.pdf"]


def test_gzipped_alerts_read_identically(tmp_path):
    """Alerts land in the repo gzipped — a Market Index email is ~68 KB of
    styled HTML and compresses about 8x. The parse must not care."""
    import gzip as _gzip

    from asx.ingest.mailbox import EmlDirectory

    (tmp_path / "a.eml").write_text(RAW_ALERT)
    with _gzip.open(tmp_path / "b.eml.gz", "wb") as fh:
        fh.write(RAW_ALERT.replace("XYZ", "ABC").encode())

    got = {detection_from_email(m).ticker for m in EmlDirectory(tmp_path).fetch_new()}
    assert got == {"XYZ", "ABC"}


def test_dotted_filenames_are_still_recognised_as_email(tmp_path):
    """The committed filename carries a Mandrill message id full of dots, so
    Path.suffixes[0] returned '.20260818073903' and every alert was skipped —
    reading nothing and reporting an empty mailbox."""
    import gzip as _gzip

    from asx.ingest.mailbox import EmlDirectory

    name = "20260818T073903Z-31137204.20260818073903.6a840c17.02633952.eml.gz"
    (tmp_path / "2026" / "08").mkdir(parents=True)
    with _gzip.open(tmp_path / "2026" / "08" / name, "wb") as fh:
        fh.write(RAW_ALERT.encode())
    assert len(list(EmlDirectory(tmp_path).fetch_new())) == 1


def test_a_directory_of_unrecognised_files_is_an_alarm_not_an_empty_inbox(tmp_path):
    """Zero detections is what a working quiet day looks like, so it must
    never also be what a broken naming convention looks like (Invariant 7)."""
    import pytest

    from asx.ingest.mailbox import EmlDirectory

    (tmp_path / "alert-1.email").write_text(RAW_ALERT)
    (tmp_path / "alert-2.email").write_text(RAW_ALERT)
    with pytest.raises(ValueError, match="none look like an email"):
        list(EmlDirectory(tmp_path).fetch_new())

    # A genuinely empty directory is a genuinely quiet day.
    (tmp_path / "empty").mkdir()
    assert list(EmlDirectory(tmp_path / "empty").fetch_new()) == []


def test_the_readme_in_the_alerts_directory_is_not_mistaken_for_mail(tmp_path):
    from asx.ingest.mailbox import EmlDirectory

    (tmp_path / "README.md").write_text("# alerts\n")
    (tmp_path / "a.eml").write_text(RAW_ALERT)
    assert len(list(EmlDirectory(tmp_path).fetch_new())) == 1
