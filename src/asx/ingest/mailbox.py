"""Mailbox detection source (Tier 0 access decision §1).

Reads a dedicated mailbox the owner controls, containing alert emails from
services whose purpose is sending them — Market Index watchlist alerts,
Listcorp alerts, company IR mailing lists. Each alert becomes a Detection.

The ingester parses this mailbox only. It never follows an asx.com.au link
found in an email: those URLs are recorded on the detection so the owner can
open them personally, and the fetch guard raises if any code tries.

**Market Index is calibrated** against five real alerts saved 2026-08-19
(fixtures/mailbox/, pinned by tests/test_marketindex_gold.py). Its real
format, none of which the pre-calibration guesses handled:

    Subject: ASX:{TICKER} - {Announcement|Sensitive Ann}: {title}
    Body:    Published: DD/MM/YY, HH:MMam (AEST)        two-digit year
             | {Company Name} ({TICKER})
             <.../asx/{code}/announcements/{slug}-{ASX_ID}?utm_...>

Three things that only real email revealed:

- "Sensitive Ann" in the subject is the ASX **price-sensitive** flag, a
  column nothing was populating.
- One announcement per email. The digest worry is answered: no detection is
  being lost to several announcements collapsing into one row.
- **Every link in the HTML part is a Mandrill click-tracker.** Fetching one
  would register a click with the provider's ESP — a side effect on someone
  else's system — so URLs are read from the text/plain part, where they are
  real but hard-wrapped across lines and must be rejoined.

Every other sender is still uncalibrated. Their extractors stay conservative
heuristics that leave anything unreadable as None rather than guessing
(Invariant 8 at field level); tighten them the same way, against fixtures,
and do not loosen the "leave it None" behaviour.
"""

from __future__ import annotations

import email
import email.policy
import gzip
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
    # Market Index: "Published: 18/08/26, 05:37pm (AEST)". Two-digit year,
    # no space before the meridiem. The pre-calibration pattern required a
    # four-digit year and so matched nothing, leaving every lodgement time
    # unknown.
    re.compile(r"(\d{1,2}/\d{1,2}/\d{2})[,\s]+(\d{1,2}:\d{2})\s*([AaPp][Mm])"),
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})[,\s]+(\d{1,2}:\d{2})\s*([AaPp][Mm])?"),
    re.compile(r"(\d{1,2}\s+\w{3,9}\s+\d{4})[,\s]+(\d{1,2}:\d{2})"),
]

# "| AXP Energy Ltd (AXP)" in the alert body.
_COMPANY_RE = re.compile(r"\|\s*([^|()\n]{3,90}?)\s*\(([A-Z0-9]{2,6})\)")
# The ASX announcement number on the end of a Market Index announcement slug.
_ANNOUNCEMENT_ID_RE = re.compile(r"-(\d[A-Z0-9]{6,})(?:$|[?#])")


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
    # A link on the provider's own site that the OWNER should open to reach
    # the announcement. Recorded for the human, never fetched.
    open_url_re: re.Pattern | None = None


SENDER_RULES: list[SenderRule] = [
    SenderRule(
        detection_source="market_index_alert",
        from_contains="marketindex",
        # Calibrated against the real emails, not guessed. "Sensitive Ann"
        # is the ASX price-sensitive marker.
        subject_re=re.compile(
            r"^ASX:(?P<ticker>[A-Z0-9]{2,6})\s*[-–]\s*"
            r"(?P<kind>Announcement|Sensitive Ann)\s*:\s*(?P<title>.+)$"),
        own_hosts=("marketindex.com.au", "market-index.com.au"),
        # The provider's announcement page is the owner's route to the
        # document: these alerts carry no PDF and no asx.com.au link at all,
        # so without it the capture worklist has nothing to open.
        open_url_re=re.compile(
            r"^https://www\.marketindex\.com\.au/asx/[a-z0-9]+/announcements/[^?#]+"),
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
    """The message body, preferring text/plain.

    A multipart/alternative message carries the SAME content twice. The
    original concatenated both parts, which doubled every URL and let an HTML
    attribute win a timestamp match over the real one. text/plain is also the
    only place a Market Index alert's URLs appear untracked — see the module
    docstring.
    """
    plain, html = [], []
    for part in ([msg] if not msg.is_multipart() else msg.walk()):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8",
                              errors="replace")
        (plain if ctype == "text/plain" else html).append(text)
    return "\n".join(plain or html)


def _urls_in(text: str) -> list[str]:
    """URLs from a plain-text body.

    Market Index wraps long URLs across lines inside angle brackets, so a
    naive scan truncates them mid-path — losing the ASX announcement id on
    the end. Bracketed URLs are rejoined first; bare ones are then collected.
    """
    urls, seen = [], set()
    for raw in re.findall(r"<(https?://[^>]+)>", text):
        url = re.sub(r"\s+", "", raw)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    stripped = re.sub(r"<https?://[^>]+>", " ", text)
    for url in _URL_RE.findall(stripped):
        url = url.rstrip(".,;)")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


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
        for fmt in ("%d/%m/%y %I:%M %p", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M",
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
        # The provider's own announcement page. It is on a blocked host, so
        # it is never a fetch candidate — but it IS the owner's route to the
        # document, and these alerts contain no other. Tracking parameters
        # are dropped so the stored link is stable and clean.
        if rule.open_url_re:
            m = rule.open_url_re.match(url)
            if m and m.group(0) not in manual:
                manual.append(m.group(0))
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
    price_sensitive = None
    subject_matched = False
    if rule.subject_re:
        m = rule.subject_re.match(subject)
        if m:
            subject_matched = True
            groups = m.groupdict()
            ticker = groups.get("ticker")
            title = (groups.get("title") or "").strip() or None
            # The ASX price-sensitive flag, which Market Index encodes as
            # "Sensitive Ann" rather than "Announcement". Left None for any
            # sender whose rule does not state it — absence of the marker is
            # only evidence of "not sensitive" when the sender is known to
            # emit it (Invariant 8).
            kind = groups.get("kind")
            if kind:
                price_sensitive = kind.strip().lower().startswith("sensitive")
    if title is None:
        title = subject or None
    if ticker is None and rule.subject_re is None:
        # The greedy fallback is ONLY for senders whose format nobody has
        # calibrated. Where the real format IS known, a subject that does not
        # match means "this is not an announcement alert" — Market Index also
        # sends editorial newsletters, and "Evening Wrap: ASX 200 slides on
        # ..." produced ticker "200": a fabricated code attached to something
        # that is not an announcement. Guessing harder is the wrong response
        # to knowing the format and seeing it absent.
        for candidate in _TICKER_RE.findall(subject):
            if candidate.upper() not in _TICKER_STOPWORDS:
                ticker = candidate
                break

    body = _body_text(msg)
    # Deliberately NOT falling back to the email Date header: see docstring.
    lodged_at = _parse_lodged_at(body) or _parse_lodged_at(subject)

    company_name = None
    cm = _COMPANY_RE.search(body)
    if cm and (ticker is None or cm.group(2).upper() == (ticker or "").upper()):
        company_name = cm.group(1).strip()

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
        _urls_in(body), rule, sender_domain)

    # The ASX announcement number, carried on the end of the provider's
    # announcement slug. A far more stable identity than the ESP's
    # Message-ID: it survives a resend and is the same number the ASX itself
    # uses, so a document captured later can be tied back to this detection.
    announcement_id = None
    for url in manual_open_urls:
        am = _ANNOUNCEMENT_ID_RE.search(url)
        if am:
            announcement_id = am.group(1)
            break

    return Detection(
        detection_source=rule.detection_source,
        source_ref=str(msg.get("Message-ID") or "") or subject,
        ticker=ticker.upper() if ticker else None,
        title=title,
        lodged_at=lodged_at,
        detected_at=detected_at or datetime.now(timezone.utc),
        price_sensitive=price_sensitive,
        company_name=company_name,
        announcement_id=announcement_id,
        manual_open_urls=manual_open_urls,
        document_urls=fetch_candidate_urls,
        format_recognised=subject_matched or rule is DEFAULT_RULE,
        raw_sha256=hashlib.sha256(
            msg.as_bytes() if hasattr(msg, "as_bytes") else str(msg).encode()
        ).hexdigest(),
    )


def _message_extension(path: Path) -> tuple[str, bool]:
    """(extension, is_gzipped) for a saved message, tolerant of dotted names."""
    name = path.name.lower()
    gzipped = name.endswith(".gz")
    if gzipped:
        name = name[: -len(".gz")]
    dot = name.rfind(".")
    return (name[dot:] if dot > 0 else ""), gzipped


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
        files = [f for f in sorted(self.path.glob("**/*"))
                 if f.is_file() and f.name != "README.md"]
        matched = 0
        for file in files:
            # NOT Path.suffixes[0]: it splits on every dot, and the committed
            # filenames carry a Mandrill message id full of them, so the
            # "extension" came back as '.20260818073903' and every alert was
            # skipped. Read nothing, said nothing.
            ext, gzipped = _message_extension(file)
            if ext not in (".eml", ".txt", ".msg"):
                continue
            matched += 1
            # Alerts committed to the repo are gzipped: a Market Index email
            # is ~68 KB of mostly styled HTML and compresses about 6x, which
            # is the difference between hundreds of megabytes of git history
            # a year and tens. The bytes are the publisher's, unmodified.
            opener = gzip.open if gzipped else open
            with opener(file, "rb") as fh:
                yield email.message_from_binary_file(fh, policy=email.policy.default)
        if files and not matched:
            # Silence is an alarm (Invariant 7). A directory full of files
            # none of which were recognised reads identically to a quiet day,
            # and that is how a rename convention silently stops a feed.
            raise ValueError(
                f"{self.path} holds {len(files)} file(s) but none look like an "
                f"email (.eml/.txt/.msg, optionally .gz). Refusing to report "
                f"an empty mailbox, which is what a working quiet day looks "
                f"like. Saw: {[f.name for f in files[:3]]}"
            )


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
