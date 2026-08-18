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
import email.policy
import hashlib
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

from asx.ids.market_time import SYDNEY
from asx.ingest.detection import Detection
from asx.ingest.fetch_guard import is_prohibited

# A 3-6 character ASX code, usually presented in caps and often with a label.
_TICKER_RE = re.compile(r"\b(?:ASX[:\s]+)?([A-Z0-9]{3,6})\b")

# Words that look exactly like an ASX code in an all-caps subject line and are
# not one. "ASX" is the dangerous one: ASX Limited's own code IS "ASX", so
# "New ASX announcement for BHP Group Ltd" resolved to a real, wrong entity
# and read as a confident answer rather than a failure. A guessed ticker is
# worse than none — it silently attaches a director's trade to the wrong
# company (Invariant 1).
_TICKER_STOPWORDS = frozenset({
    "ASX", "NEW", "ALERT", "ALERTS", "PDF", "HTML", "FYI", "RE", "FW", "FWD",
    "THE", "AND", "FOR", "YOUR", "LTD", "PTY", "NL", "PLC", "INC", "LIMITED",
    "AGM", "EGM", "CEO", "CFO", "USD", "AUD", "NZD", "GST", "ABN", "ACN",
    "MARKET", "INDEX", "WATCH", "DAILY", "WEEKLY", "REPORT", "UPDATE",
})
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
# "14/08/2026 9:30 AM" / "14 Aug 2026 09:30" style stamps in alert bodies.
_DATETIME_RES = [
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})[,\s]+(\d{1,2}:\d{2})\s*([AaPp][Mm])?"),
    re.compile(r"(\d{1,2}\s+\w{3,9}\s+\d{4})[,\s]+(\d{1,2}:\d{2})"),
]


# Path fragments that mark a link as list-management rather than a document.
# Fetching one of these would store an HTML confirmation page as the
# announcement — and could unsubscribe the owner from the platform's only
# detection source.
_NON_DOCUMENT_URL_RE = re.compile(
    r"(unsubscribe|optout|opt-out|preferences|manage|profile|login|signin|"
    r"/ss/c/|/click|/track|/redirect|/r/|webversion|viewonline)", re.I)


@dataclass
class SenderRule:
    """How to read one alert provider's emails."""
    detection_source: str
    from_contains: str
    # Optional subject pattern with named groups 'ticker' and/or 'title'.
    subject_re: re.Pattern | None = None
    # The provider's own domains. An alert aggregator does not host the
    # company's PDF, so links to these are never fetch candidates — they are
    # tracking redirects, "view announcement" pages and list management.
    own_hosts: tuple[str, ...] = ()


SENDER_RULES: list[SenderRule] = [
    SenderRule(
        detection_source="market_index_alert",
        from_contains="marketindex",
        subject_re=re.compile(r"^(?P<ticker>[A-Z0-9]{3,6})\s*[-–:]\s*(?P<title>.+)$"),
        own_hosts=("marketindex.com.au", "market-index.com.au"),
    ),
    SenderRule(
        detection_source="listcorp_alert",
        from_contains="listcorp",
        subject_re=re.compile(r"^(?P<ticker>[A-Z0-9]{3,6})\s*[-–:]\s*(?P<title>.+)$"),
        own_hosts=("listcorp.com",),
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


def _url_host(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url, re.I)
    return (m.group(1) if m else "").lower()


def partition_urls(urls: list[str], rule: SenderRule,
                   sender_domain: str = "") -> tuple[list[str], list[str]]:
    """Split an alert's links into (manual_open, fetch_candidates).

    `manual_open` is every link on a host no automated device may touch. They
    are KEPT, not dropped: the access decision promises they are recorded so
    the owner can open them personally, and `asx worklist` prints them.

    `fetch_candidates` is an ALLOWLIST, not "everything else". An alert body
    also carries the provider's tracking redirects, "manage your watchlist"
    and unsubscribe links; fetching the first of those would store an HTML
    confirmation page as the announcement bytes and could unsubscribe the
    owner from the platform's only detection source. A candidate must be
    https, off the provider's own hosts and the sender's domain, free of
    list-management path markers, and look like a document.

    An empty candidate list is the correct result for an alert-aggregator
    email — the aggregator does not host the company's PDF. Route 1 of the
    access decision (fetch from the company's own website) is fed by IR
    mailing lists, not by these.
    """
    manual, candidates = [], []
    blocked_hosts = {h.lower() for h in rule.own_hosts}
    if sender_domain:
        blocked_hosts.add(sender_domain.lower())
    for url in urls:
        if is_prohibited(url):
            manual.append(url)
            continue
        host = _url_host(url)
        if not url.lower().startswith("https://"):
            continue
        if any(host == b or host.endswith("." + b) for b in blocked_hosts):
            continue
        if _NON_DOCUMENT_URL_RE.search(url):
            continue
        if not re.search(r"\.pdf($|[?#])", url, re.I):
            continue      # ambiguous -> left out entirely (Invariant 8)
        candidates.append(url)
    return manual, candidates


def detection_from_email(msg: Message) -> Detection:
    """Turn one alert email into a Detection. Unreadable fields stay None.

    Two rules this function will not bend:

    - **It never invents a ticker.** A plausible-looking all-caps token is not
      a code; "New ASX announcement for BHP Group Ltd" used to yield "ASX",
      which is a real listed code (ASX Limited) and so attached the detection
      confidently to the wrong entity.
    - **It never puts the alert's arrival time in `lodged_at`.** `lodged_at`
      is the ASX release timestamp and feeds knowable_at (Invariant 2). The
      email Date header is when the *alert* was sent, which is a different
      fact about a different event. When the body carries no lodgement time,
      `lodged_at` stays None and `detected_at` carries the arrival — an
      unknown release time is recorded as unknown.
    """
    sender = (str(msg.get("From") or "")).lower()
    rule = next((r for r in SENDER_RULES if r.from_contains in sender), DEFAULT_RULE)
    sender_domain = _url_host("http://" + sender.split("@")[-1].strip("<> ")) \
        if "@" in sender else ""

    subject = str(msg.get("Subject") or "").strip()
    ticker = title = None
    subject_matched = False
    if rule.subject_re:
        m = rule.subject_re.match(subject)
        if m:
            subject_matched = True
            groups = m.groupdict()
            ticker = groups.get("ticker")
            title = (groups.get("title") or "").strip() or None
    if title is None:
        title = subject or None
    if ticker is None:
        for candidate in _TICKER_RE.findall(subject):
            if candidate.upper() not in _TICKER_STOPWORDS:
                ticker = candidate
                break

    body = _body_text(msg)
    # Deliberately NOT falling back to the email Date header: see docstring.
    lodged_at = _parse_lodged_at(body) or _parse_lodged_at(subject)

    detected_at = None
    if msg.get("Date"):
        try:
            detected_at = parsedate_to_datetime(str(msg["Date"]))
        except (TypeError, ValueError):
            detected_at = None
    if detected_at is not None and detected_at.tzinfo is None:
        # RFC 5322 "-0000" parses naive. A naive timestamp inserted into a
        # timestamptz column is silently reinterpreted in the server's zone,
        # so it is pinned to UTC here rather than left to chance.
        detected_at = detected_at.replace(tzinfo=timezone.utc)

    manual_open_urls, fetch_candidate_urls = partition_urls(
        _URL_RE.findall(body), rule, sender_domain)

    return Detection(
        detection_source=rule.detection_source,
        source_ref=str(msg.get("Message-ID") or "") or subject,
        ticker=ticker.upper() if ticker else None,
        title=title,
        lodged_at=lodged_at,
        detected_at=detected_at or datetime.now(timezone.utc),
        manual_open_urls=manual_open_urls,
        document_urls=fetch_candidate_urls,
        format_recognised=subject_matched or rule is DEFAULT_RULE,
        raw_sha256=hashlib.sha256(
            msg.as_bytes() if hasattr(msg, "as_bytes") else str(msg).encode()
        ).hexdigest(),
    )


class EmlDirectory:
    """Reads alert emails saved as .eml files in a directory.

    Three jobs, all of which the IMAP path cannot do:

    - **Calibration.** CLAUDE.md requires the gold fixture set before the
      parser. Real alerts have to be readable off disk to build one.
    - **Credential-free operation.** Exporting a few emails and pointing the
      ingester at them proves the whole detection path works before any
      password is handed to anything.
    - **Recovery.** An alert already opened in the mail client is no longer
      UNSEEN and IMAP will never hand it over; saving it to disk is how it
      gets ingested rather than silently lost.

    Nothing is deleted or modified: recording a detection is idempotent on
    detection_key, so re-reading the directory is always safe.
    """

    def __init__(self, path: Path, recursive: bool = True):
        self.path = Path(path)

    def fetch_new(self) -> Iterable[Message]:
        if not self.path.exists():
            raise FileNotFoundError(f"no such alert directory: {self.path}")
        for file in sorted(self.path.glob("**/*")):
            if not file.is_file() or file.suffix.lower() not in (".eml", ".txt", ".msg"):
                continue
            with open(file, "rb") as fh:
                yield email.message_from_binary_file(fh, policy=email.policy.default)


class IMAPMailbox:
    """Reads alert emails from a mailbox the owner controls.

    **Never destructive.** Messages are fetched with BODY.PEEK, so the \\Seen
    flag is not set by reading, and nothing is deleted or moved. The previous
    version fetched RFC822 (which sets \\Seen) inside a loop that committed
    once at the end: a crash or a malformed email part-way through left the
    earlier messages flagged as read and unrecorded, and an UNSEEN search
    would never return them again. Lost silently and permanently.

    **UNSEEN is not the default search** for the same reason in reverse: an
    alert the owner reads on their phone at 09:31 is no longer unseen, and
    IMAP would never hand it over. Searching by date and relying on
    detection_key for idempotency means reading your own mail cannot punch a
    hole in the dataset. `--unseen-only` is available for a mailbox nobody
    ever opens.
    """

    def __init__(self, host: str, user: str, password: str,
                 folder: str = "INBOX", mark_seen: bool = False,
                 since_days: int = 7, unseen_only: bool = False):
        self.host, self.user, self.password = host, user, password
        self.folder, self.mark_seen = folder, mark_seen
        self.since_days, self.unseen_only = since_days, unseen_only

    def _criteria(self) -> tuple[str, ...]:
        if self.unseen_only:
            return ("UNSEEN",)
        since = (datetime.now(timezone.utc) - timedelta(days=self.since_days))
        return ("SINCE", since.strftime("%d-%b-%Y"))

    def fetch_new(self) -> Iterable[Message]:
        conn = imaplib.IMAP4_SSL(self.host)
        try:
            conn.login(self.user, self.password)
            # readonly: the ingester has no business changing the mailbox.
            conn.select(self.folder, readonly=not self.mark_seen)
            typ, data = conn.search(None, *self._criteria())
            if typ != "OK":
                raise RuntimeError(
                    f"IMAP search failed on folder {self.folder!r}: {typ}. "
                    f"A wrong folder name reads as an empty inbox, which is "
                    f"indistinguishable from a quiet day."
                )
            for num in data[0].split():
                # PEEK: reading must not change what a later run can see.
                typ, msg_data = conn.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                yield email.message_from_bytes(msg_data[0][1],
                                               policy=email.policy.default)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass
