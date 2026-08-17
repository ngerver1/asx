"""Mailbox detection source (Tier 0 access decision §1).

Reads a dedicated mailbox the owner controls, containing alert emails from
services whose purpose is sending them — Market Index watchlist alerts,
Listcorp alerts, company IR mailing lists. Each alert becomes a Detection.

The ingester parses this mailbox only. It never follows an asx.com.au link
found in an email: those URLs are recorded on the detection so the owner can
open them personally, and the fetch guard raises if any code tries.

**Per-sender parsing needs calibration against real emails.** The extractors
below are deliberately conservative heuristics: ticker, title, lodgement time
and links, with anything unreadable left None rather than guessed (Invariant
8 at field level). Once real alerts are flowing, tighten `SENDER_RULES` per
sender and add fixtures — do not loosen the "leave it None" behaviour.
"""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Iterable

from asx.ids.market_time import SYDNEY
from asx.ingest.detection import Detection
from asx.ingest.fetch_guard import is_prohibited

# A 3-6 character ASX code, usually presented in caps and often with a label.
_TICKER_RE = re.compile(r"\b(?:ASX[:\s]+)?([A-Z0-9]{3,6})\b")
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
# "14/08/2026 9:30 AM" / "14 Aug 2026 09:30" style stamps in alert bodies.
_DATETIME_RES = [
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})[,\s]+(\d{1,2}:\d{2})\s*([AaPp][Mm])?"),
    re.compile(r"(\d{1,2}\s+\w{3,9}\s+\d{4})[,\s]+(\d{1,2}:\d{2})"),
]


@dataclass
class SenderRule:
    """How to read one alert provider's emails."""
    detection_source: str
    from_contains: str
    # Optional subject pattern with named groups 'ticker' and/or 'title'.
    subject_re: re.Pattern | None = None


SENDER_RULES: list[SenderRule] = [
    SenderRule(
        detection_source="market_index_alert",
        from_contains="marketindex",
        subject_re=re.compile(r"^(?P<ticker>[A-Z0-9]{3,6})\s*[-–:]\s*(?P<title>.+)$"),
    ),
    SenderRule(
        detection_source="listcorp_alert",
        from_contains="listcorp",
        subject_re=re.compile(r"^(?P<ticker>[A-Z0-9]{3,6})\s*[-–:]\s*(?P<title>.+)$"),
    ),
]

DEFAULT_RULE = SenderRule(detection_source="ir_email", from_contains="")


def _body_text(msg: Message) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode(part.get_content_charset() or "utf-8",
                                                errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            parts.append(payload.decode(msg.get_content_charset() or "utf-8",
                                        errors="replace"))
    return "\n".join(parts)


def _parse_lodged_at(text: str) -> datetime | None:
    """Best-effort lodgement timestamp from the alert body. Sydney local time
    (SPEC §3) — alert services quote market time. Returns None rather than
    guessing when nothing parses."""
    for pattern in _DATETIME_RES:
        m = pattern.search(text)
        if not m:
            continue
        date_part, time_part = m.group(1), m.group(2)
        meridiem = m.group(3) if pattern.groups >= 3 and m.lastindex >= 3 else None
        for fmt in ("%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M",
                    "%d %B %Y %H:%M", "%d %b %Y %H:%M"):
            try:
                stamp = f"{date_part} {time_part}" + (f" {meridiem.upper()}" if meridiem else "")
                return datetime.strptime(stamp, fmt).replace(tzinfo=SYDNEY)
            except ValueError:
                continue
    return None


def detection_from_email(msg: Message) -> Detection:
    """Turn one alert email into a Detection. Unreadable fields stay None."""
    sender = (msg.get("From") or "").lower()
    rule = next((r for r in SENDER_RULES if r.from_contains in sender), DEFAULT_RULE)

    subject = (msg.get("Subject") or "").strip()
    ticker = title = None
    if rule.subject_re:
        m = rule.subject_re.match(subject)
        if m:
            groups = m.groupdict()
            ticker = groups.get("ticker")
            title = (groups.get("title") or "").strip() or None
    if title is None:
        title = subject or None
    if ticker is None:
        m = _TICKER_RE.search(subject)
        ticker = m.group(1) if m else None

    body = _body_text(msg)
    lodged_at = _parse_lodged_at(body) or _parse_lodged_at(subject)
    if lodged_at is None and msg.get("Date"):
        # Fall back to the alert's own timestamp. It is an upper bound on the
        # lodgement time, never earlier, so it cannot manufacture look-ahead;
        # capture records the exact stamp when the document itself is read.
        try:
            lodged_at = parsedate_to_datetime(msg["Date"])
        except (TypeError, ValueError):
            lodged_at = None

    urls = _URL_RE.findall(body)
    # ASX links are recorded for the owner to open personally, never followed.
    document_urls = [u for u in urls if not is_prohibited(u)]

    detected_at = None
    if msg.get("Date"):
        try:
            detected_at = parsedate_to_datetime(msg["Date"])
        except (TypeError, ValueError):
            detected_at = None

    return Detection(
        detection_source=rule.detection_source,
        source_ref=msg.get("Message-ID") or subject,
        ticker=ticker.upper() if ticker else None,
        title=title,
        lodged_at=lodged_at,
        detected_at=detected_at or datetime.now(timezone.utc),
        document_urls=document_urls,
    )


class IMAPMailbox:
    """Reads unseen alert emails from a mailbox the owner controls."""

    def __init__(self, host: str, user: str, password: str,
                 folder: str = "INBOX", mark_seen: bool = True):
        self.host, self.user, self.password = host, user, password
        self.folder, self.mark_seen = folder, mark_seen

    def fetch_new(self) -> Iterable[Message]:
        conn = imaplib.IMAP4_SSL(self.host)
        try:
            conn.login(self.user, self.password)
            conn.select(self.folder)
            _typ, data = conn.search(None, "UNSEEN")
            for num in data[0].split():
                fetch_cmd = "(RFC822)" if self.mark_seen else "(BODY.PEEK[])"
                _typ, msg_data = conn.fetch(num, fetch_cmd)
                if msg_data and msg_data[0]:
                    yield email.message_from_bytes(msg_data[0][1])
        finally:
            try:
                conn.close()
            except Exception:
                pass
            conn.logout()
