"""Where a document's lodgement timestamp comes from.

`knowable_at` is the load-bearing column of this platform (Invariant 2). Every
analytic joins on it, and a signal computed against a timestamp nobody could
have observed is a backtest of a fact nobody could have known. So this module
does one thing: establish when a document became public, and record which
source said so.

Two sources, in preference order.

1. THE ALERT. A MarketIndex alert carries the announcement's published time.
   That is a third party observing the release, and it is the better answer
   wherever it exists — which is to say, from the day the mailbox went live.

2. THE PDF's OWN TIMESTAMP. Announcement PDFs carry a creation time. This is
   when the file was produced, which is NOT by definition when the
   announcement was released. Measured against alerts on documents where both
   exist, it runs about six minutes early and never late:

       2A1690463   created 17:16   alert published 17:22
       329734      created 17:16   alert published 17:22
       329736      created 17:24   alert published 17:30
       6A1339259   created 17:24   alert published 17:30

   Six minutes is immaterial to a daily-resolution signal and material to
   nothing this platform computes. But it is a proxy, so it is labelled one:
   `lodged_at_source = 'pdf_creation'` travels with the row, and any analysis
   that ever needs release-time precision can find and exclude it.

There is no third source. A document with neither yields no timestamp, and a
document with no timestamp yields no canonical rows — the parser's own
validation refuses it. That is the correct direction to fail in: a trade with
an invented knowable_at is worse than a trade that is missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# How long after a document is produced its alert may plausibly arrive. Wide
# enough for an overnight release alerted next morning, narrow enough that a
# LATER, UNRELATED announcement by the same company cannot be mistaken for
# this one — which is exactly what a company-name match alone does. Four such
# false pairings were 162 hours apart; matching without a window would have
# dated four documents to another announcement entirely.
ALERT_WINDOW = timedelta(hours=18)

# Clock skew slack: an alert timestamped fractionally before the PDF it
# describes is the same event, not a different one.
ALERT_SKEW = timedelta(minutes=30)


@dataclass(frozen=True)
class Lodgement:
    at: datetime | None
    source: str | None          # 'market_index_alert' | 'pdf_creation' | None
    corroborated_by: str | None = None   # the other source, when both agree

    @property
    def known(self) -> bool:
        return self.at is not None


def pdf_created_at(content: bytes) -> datetime | None:
    """The PDF's creation timestamp, or None.

    Deliberately NOT falling back to /ModDate. Three captured documents have
    a modification date but no creation date, and every one of those ModDates
    is stamped -04'00' — a US timezone, on Australian announcements. That is
    when some intermediary handled the file, not when the ASX released it.
    A document with no creation date is undated, and an undated document
    produces no canonical rows.
    """
    try:
        import pypdf

        from asx.parse.text import UnreadableDocument
        try:
            metadata = pypdf.PdfReader(__import__("io").BytesIO(content)).metadata
        except Exception as exc:  # a malformed file has no metadata to read
            if isinstance(exc, UnreadableDocument):
                raise
            return None
    except ImportError:
        return None
    raw = str((metadata or {}).get("/CreationDate") or "")
    return _parse_pdf_date(raw)


def _parse_pdf_date(raw: str) -> datetime | None:
    """D:20260819171523+10'00' -> an aware datetime.

    A PDF date with NO offset at all is refused rather than assumed to be
    UTC: a ten-hour error moves a Sydney afternoon lodgement to the previous
    market day, and market_date is what the signal keys on (SPEC §3). But "Z"
    and "Z00'00'" ARE an offset — they say UTC — and seven of the captured
    documents write it that way.
    """
    m = re.match(r"D:(\d{14})(?:Z(?:00'?00'?)?|([+-])(\d{2})'?(\d{2})'?)", raw)
    if not m:
        return None
    stamp, sign, hours, minutes = m.groups()
    if sign is None:                      # "Z" or "Z00'00'": UTC, stated
        offset = timedelta(0)
    else:
        offset = timedelta(hours=int(hours), minutes=int(minutes))
        if sign == "-":
            offset = -offset
    from datetime import timezone
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone(offset))


def resolve(
    *,
    alert_published_at: datetime | None = None,
    pdf_content: bytes | None = None,
) -> Lodgement:
    """When this document became public, and which source said so."""
    created = pdf_created_at(pdf_content) if pdf_content else None

    if alert_published_at is not None:
        corroborated = (
            "pdf_creation"
            if created is not None
            and -ALERT_SKEW <= alert_published_at - created <= ALERT_WINDOW
            else None
        )
        return Lodgement(alert_published_at, "market_index_alert", corroborated)

    if created is not None:
        return Lodgement(created, "pdf_creation")
    return Lodgement(None, None)


def alert_matches_document(
    *,
    alert_entity: str | None,
    document_entity: str | None,
    alert_published_at: datetime | None,
    pdf_content: bytes | None,
) -> bool:
    """Whether an alert describes this document.

    A company name alone is not enough. Companies lodge repeatedly, so name
    matching pairs a document with whichever of that company's announcements
    happens to be in the mailbox — four of the eight name-only matches in the
    captured corpus were a week out. The document's own timestamp is what
    makes the pairing safe.
    """
    if not alert_entity or not document_entity or alert_published_at is None:
        return False
    if _norm(alert_entity) != _norm(document_entity):
        return False
    created = pdf_created_at(pdf_content) if pdf_content else None
    if created is None:
        return False        # nothing to pin the pairing to; refuse it
    return -ALERT_SKEW <= alert_published_at - created <= ALERT_WINDOW


def _norm(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    text = re.sub(r"\b(limited|ltd|nl|pty|the|group|holdings?)\b", " ", text)
    return " ".join(text.split())
