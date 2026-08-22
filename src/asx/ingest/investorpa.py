"""investorpa.com — announcement detection across the whole exchange.

**Why this exists.** Detection was watchlist-bounded. Market Index alerts are
driven by a subscription capped at ~200 codes against a ~1,475-code universe,
and docs/ACCEPTANCE.md states the consequence plainly: "a watchlist-scoped feed
can confirm a thesis about a company already on the list but can never surface
one, which is the opposite of what the cluster-buy screen is for." This source
searches every announcement on the exchange, so the cluster screen can find a
company nobody was already watching.

It also carries the document URL, which Market Index never did. Detection and
possession arrive together, and the manual capture sweep stops being the only
route to bytes.

**What it is not.** A re-host, not the exchange. Documents obtained here are
recorded `possession_source='investorpa'` and timestamps `lodged_at_source=
'investorpa'`, never `'asx'` — that value stays reserved for the exchange's own
feed. Their transcribed text is deliberately NOT used as the document: the
gold set calibrates App3YParser against pypdf output, so this module takes the
PDF and the platform reads it itself. Their text is a second reading available
for corroboration, not a substitute for the platform's own.

**Terms basis.** investorpa.com/features/ advertises the Remote MCP Server as a
product feature for "MCP-compatible AI harnesses", naming Claude Code. Recorded
in full in fetch_guard.DECLARED_SOURCES, along with the honest limit of it:
that grant is written for harnesses asking questions, and reading it to cover a
scheduled ingest is this platform's inference, not the vendor's words.

Everything below is shaped by that inference having to stay defensible:

  * **Appendix 3Y/3Z only.** The exchange publishes ~400 announcements a day;
    this asks for the tens that the platform parses, by title.
  * **Their search, never our enumeration.** Announcement identifiers are
    sequential at ~400/day, so enumerating `announcement-pdf/{date}/{id}.pdf`
    would collect the entire exchange. docs/SOURCE_INVESTORPA.md named that as
    a crawl that "must never be built", and it still must not be. A document
    URL is used only when a search result stated it. A test asserts no source
    file builds one.
  * **The guard, not around it.** Every request goes through fetch_guard.fetch,
    which throttles to one per five seconds and identifies us honestly.

**Auth.** The MCP endpoint is OAuth-protected (`mcp:read`, a read-only scope
the server defines and enforces — this module cannot write to the account even
if it tried). The server is a public client supporting Dynamic Client
Registration and PKCE, so consent is a one-off browser step producing a refresh
token; see `asx.ingest.investorpa_consent`. The refresh token lives in the
environment and never in the repo, exactly as the Gmail grant does.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from asx.ids.market_time import market_date
from asx.ingest.detection import Detection
from asx.ingest.fetch_guard import fetch

MCP_URL = "https://investorpa.com/mcp/"
TOKEN_URL = "https://investorpa.com/oauth/token/"
AUTHORIZE_URL = "https://investorpa.com/oauth/authorize/"
REGISTER_URL = "https://investorpa.com/oauth/register/"
READ_SCOPE = "mcp:read"

# The forms this platform parses. Searched by title because that is the only
# filter the API offers, and kept to the two the registry actually handles —
# asking for more would be collecting documents nothing reads.
DIRECTOR_INTEREST_KEYWORDS = "Director's Interest Notice"

# Coverage floor, stated by the API's own tool descriptions. Anything earlier
# is not absent from the exchange, only from this vendor, and a backfill that
# crosses it silently would report a hole in the market that is a hole in the
# source (Invariant 7).
COVERAGE_STARTS = "2024-06-15"

MAX_RESULTS_PER_CALL = 500


class InvestorPAAuthError(RuntimeError):
    """Raised when the stored grant cannot produce an access token."""


class InvestorPAProtocolError(RuntimeError):
    """Raised when the endpoint answers in a shape this client cannot read."""


@dataclass
class InvestorPACredentials:
    """A public-client OAuth grant. There is no client secret: the server's
    metadata declares `token_endpoint_auth_methods_supported: ["none"]`, so
    the refresh token is the whole credential and is treated as one."""
    client_id: str
    refresh_token: str

    @classmethod
    def from_env(cls) -> "InvestorPACredentials":
        missing = [v for v in ("ASX_INVESTORPA_CLIENT_ID",
                               "ASX_INVESTORPA_REFRESH_TOKEN")
                   if not os.environ.get(v)]
        if missing:
            raise InvestorPAAuthError(
                f"missing {', '.join(missing)}. Run "
                f"`python -m asx.ingest.investorpa_consent` once to obtain "
                f"them, then set them as environment variables on the cloud "
                f"environment (not in the repo, not in chat) — see "
                f"docs/SOURCE_INVESTORPA.md."
            )
        return cls(os.environ["ASX_INVESTORPA_CLIENT_ID"],
                   os.environ["ASX_INVESTORPA_REFRESH_TOKEN"])


# --------------------------------------------------------------------------
# Parsing the API's answers
# --------------------------------------------------------------------------
# The tools return prose for a human to read, not structured records, so the
# fields have to be recovered from a line. Three mechanisms keep that from
# failing silently, in increasing order of how little they trust the format.
#
# 1. _RESULT_LINE parses a line we understand.
# 2. _LOOKS_LIKE_RESULT recognises a line we were MEANT to understand and did
#    not, so it becomes an unreadable Detection and reaches the review queue
#    through the same path an unreadable alert email does.
# 3. The vendor states how many results it returned. Comparing that against
#    the lines we recognised catches a line that matched NEITHER pattern -
#    the case the first two are blind to by construction.
#
# The third exists because the first two drifted apart: the bullet class was
# widened in one and not the other, and a restyled line stopped being parsed
# AND stopped being recognised, so the run reported success having read
# nothing. The count is independent of how the vendor styles anything, which
# is the property the other two cannot have.
#
# What the count is NOT: a total. Checked against the live API on 21 Aug 2026,
# the same 19-hour window answers "Found 20" at limit=100 and "Found 5" at
# limit=5, so N is the number RETURNED, capped by the limit. Truncation is
# invisible in it and needs page_looks_truncated() instead.

# One source of truth for the bullet, used by both patterns so they cannot
# drift again. The bullet is OPTIONAL: a provider who drops it entirely must
# still be recognised rather than read as prose.
_BULLET = r"[\u2022*\-\u2013\u2014]"
_ISO_INSTANT = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{2}:\d{2}"

#   \u2022 2026-08-20T17:17:24+10:00 | LOT - Change of Director's Interest Notice - G Bittar | [PDF](https://...) | [View Details](https://...)
#
# Split on the FIRST " - " only: titles routinely contain more.
_RESULT_LINE = re.compile(
    rf"^\s*{_BULLET}?\s*"
    rf"(?P<when>{_ISO_INSTANT})\s*\|\s*"
    r"(?P<ticker>[A-Z0-9]{2,6})\s*-\s*"
    r"(?P<title>.*?)\s*\|\s*"
    r"\[PDF\]\((?P<pdf>[^)\s]+)\)"
    r"(?:\s*\|\s*\[View Details\]\((?P<page>[^)\s]+)\))?"
    r"\s*$"
)

# Result-SHAPED: a timestamp where a result's timestamp goes. Deliberately
# weaker than _RESULT_LINE and built from the same bullet, so the set it
# recognises is always a superset of the set the other parses.
_LOOKS_LIKE_RESULT = re.compile(rf"^\s*{_BULLET}?\s*{_ISO_INSTANT}")

# "Found 20 announcements for: 'x' | dates: ..."
_STATED_COUNT = re.compile(r"^\s*Found\s+(\d+)\s+announcement", re.I | re.M)


@dataclass
class SearchPage:
    """One search response: what we read, and what the vendor said it sent.

    Carries the discrepancy rather than raising on it. A count mismatch means
    a line vanished, which is serious - but the readable detections on the
    same page are still facts, and throwing them away would turn one styling
    change into a lost day (Invariant 7: make the gap visible, do not widen
    it).
    """
    detections: list[Detection]
    stated: int | None
    recognised: int
    # Set by the caller that knows the limit it asked for. A full page and a
    # page that is exactly `limit` long look identical from the response.
    truncated: bool = False

    @property
    def missing(self) -> int:
        """Lines the vendor counted that we did not even recognise."""
        if self.stated is None:
            return 0
        return max(0, self.stated - self.recognised)

    @property
    def complete(self) -> bool:
        return self.stated is not None and self.missing == 0


def page_looks_truncated(*, returned: int, limit: int) -> bool:
    """True when a page is exactly as long as we allowed it to be.

    The vendor's stated count is capped by the limit, so a full page and a
    page that happens to be exactly `limit` long are indistinguishable from
    the response alone. Treating a full page as complete is how announcements
    go missing while the run reports success, so a full page is always
    reported as possibly truncated and the caller narrows its window.
    """
    return returned >= limit


def _unreadable(line: str) -> Detection:
    """A line we were meant to read and could not.

    Recorded rather than skipped: it reaches the review queue through the same
    path an unreadable alert email does, so a provider who restyles their
    output produces a visible pile of review items instead of a quiet run.
    """
    return Detection(
        detection_source="investorpa",
        source_ref=line.strip()[:200],
        title=line.strip()[:200],
        format_recognised=False,
    )


def _parse_result_line(line: str) -> Detection | None:
    """One search-result line -> a Detection, or None if it is not a result."""
    match = _RESULT_LINE.match(line)
    if match is None:
        return _unreadable(line) if _LOOKS_LIKE_RESULT.match(line) else None

    try:
        when = datetime.fromisoformat(match["when"])
    except ValueError:
        # Shape-valid but impossible - '25:61:61' matches the pattern and is
        # not a time. One bad line costs one line; letting this escape
        # abandoned every detection in the batch.
        return _unreadable(line)

    pdf = match["pdf"]
    return Detection(
        detection_source="investorpa",
        # Their announcement id, taken from the URL they gave us rather than
        # constructed. It is their publication sequence, NOT the ASX
        # announcement number, so it is deliberately not written to
        # documents.asx_announcement_id - that column means the exchange's
        # identifier, and putting a vendor's counter in it would make two
        # feeds' rows look like the same lodgement or different ones at random.
        source_ref=pdf,
        ticker=match["ticker"],
        title=match["title"],
        lodged_at=when,
        # The vendor observed the publication and timed it to the second.
        # Better than pdf_creation, which runs ~6 minutes early (migration
        # 0019), and not 'asx', which a re-host has not earned.
        lodged_at_source="investorpa",
        # The document URL arrives WITH the detection. This is the whole
        # reason the source is worth having.
        document_urls=[pdf],
        manual_open_urls=[match["page"]] if match["page"] else [],
    )


def detections_from_text(text: str) -> SearchPage:
    """Read one tool response into a page of detections plus its own audit."""
    detections, recognised = [], 0
    for line in text.splitlines():
        if _LOOKS_LIKE_RESULT.match(line):
            recognised += 1
        detection = _parse_result_line(line)
        if detection is not None:
            detections.append(detection)
    stated = _STATED_COUNT.search(text)
    return SearchPage(detections=detections,
                      stated=int(stated.group(1)) if stated else None,
                      recognised=recognised)


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------
class InvestorPAClient:
    """Speaks MCP JSON-RPC to investorpa.com through the fetch guard."""

    def __init__(self, credentials: InvestorPACredentials | None = None, *,
                 opener=None, token_opener=None):
        self.credentials = credentials or InvestorPACredentials.from_env()
        self._opener = opener
        # Separate because the token endpoint is form-encoded and not a
        # guarded document fetch; tests inject both.
        self._token_opener = token_opener
        self._token: str | None = None
        self._request_id = 0

    # -- auth --------------------------------------------------------------
    def access_token(self) -> str:
        if self._token:
            return self._token
        fields = {
            "grant_type": "refresh_token",
            "refresh_token": self.credentials.refresh_token,
            "client_id": self.credentials.client_id,
        }
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(
            TOKEN_URL, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            opener = self._token_opener or urllib.request
            with opener.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode())
        except Exception as exc:
            raise InvestorPAAuthError(
                f"could not refresh the investorpa grant: {exc}. If this is "
                f"'invalid_grant', the refresh token has been revoked or has "
                f"expired — re-run `python -m asx.ingest.investorpa_consent`."
            ) from exc
        token = payload.get("access_token")
        if not token:
            raise InvestorPAAuthError(
                f"token endpoint returned no access_token: {payload!r}")
        self._token = token
        return token

    # -- transport ---------------------------------------------------------
    def call_tool(self, name: str, arguments: dict) -> str:
        """Invoke one MCP tool and return its text content."""
        self._request_id += 1
        envelope = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        result = fetch(MCP_URL, opener=self._opener, post_json=envelope,
                       bearer_token=self.access_token())
        return _tool_text(result.content, self._request_id)

    # -- the one query this module makes -----------------------------------
    def director_interest_notices(self, *, date_from: str, date_to: str,
                                  limit: int = MAX_RESULTS_PER_CALL,
                                  ) -> SearchPage:
        """Appendix 3Y/3Z lodged in a date range, across the whole exchange.

        `search_stocks` is deliberately never called, here or anywhere. Their
        stock master is current-state, not effective-dated: `ALU` resolves to
        Alurion Resources today, though every ALU announcement before August
        2024 is Altium's. Resolving a ticker through it would attach a
        delisted company's filings to whoever inherited the code — the exact
        Invariant 1 failure the listings table exists to prevent. Tickers from
        this source are lookup inputs to entity_for_ticker and nothing else.
        """
        if date_from < COVERAGE_STARTS:
            raise ValueError(
                f"investorpa coverage begins {COVERAGE_STARTS}; asking for "
                f"{date_from} would return a short answer that looks complete. "
                f"Documents before that date come from the manual route."
            )
        text = self.call_tool("search_announcements", {
            "keywords": DIRECTOR_INTEREST_KEYWORDS,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
        })
        page = detections_from_text(text)
        # Truncation is judged on the vendor's OWN count of what it returned,
        # not on what we managed to recognise. If a line vanished, recognised
        # is short of the page's real length, and using it would hide a full
        # page — two silent failures compounding into one invisible gap.
        page.truncated = page_looks_truncated(
            returned=page.stated if page.stated is not None else page.recognised,
            limit=limit)
        return page


def _jsonrpc_messages(raw: bytes) -> list[dict]:
    """Every JSON-RPC object in a response, whether framed as JSON or as SSE.

    Streamable-HTTP MCP servers may answer either way, and an event stream may
    carry more than the answer: a log notification or a ping can follow the
    result. So all frames are collected and the caller picks by id.
    """
    body = raw.decode("utf-8", errors="replace").strip()
    if not body:
        raise InvestorPAProtocolError("empty response from the MCP endpoint")
    if body.startswith("{"):
        return [json.loads(body)]

    messages = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload.startswith("{"):
            messages.append(json.loads(payload))
    if not messages:
        raise InvestorPAProtocolError(
            f"no JSON-RPC object in the response: {body[:200]!r}")
    return messages


def _response_for(messages: list[dict], request_id: int) -> dict:
    """The frame answering OUR request.

    Taking the last frame instead was a real bug: a server that emits the tool
    result followed by any notification left us reading the notification,
    which has neither 'result' nor 'error', so a perfectly good answer was
    reported as carrying no content.
    """
    for message in messages:
        if message.get("id") == request_id:
            return message
    ids = [m.get("id") for m in messages]
    raise InvestorPAProtocolError(
        f"no frame answered request {request_id}; got ids {ids}")


def _tool_text(raw: bytes, request_id: int) -> str:
    """The text content of a tools/call result."""
    payload = _response_for(_jsonrpc_messages(raw), request_id)
    if "error" in payload:
        raise InvestorPAProtocolError(f"MCP error: {payload['error']!r}")
    result = payload.get("result") or {}
    if result.get("isError"):
        raise InvestorPAProtocolError(f"tool reported an error: {result!r}")
    parts = [block.get("text", "") for block in result.get("content", [])
             if block.get("type") == "text"]
    if not parts:
        raise InvestorPAProtocolError(
            f"tool result carried no text content: {result!r}")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
def ingest(conn, *, since_days: int = 3, client: InvestorPAClient | None = None,
           llm_classifier=None, today: datetime | None = None) -> dict:
    """Record every Appendix 3Y/3Z lodged in the window as a detection.

    Idempotent through record_detection's detection_key, which for this source
    is the document URL the vendor stated - stable across re-reads, so running
    it twice costs nothing and a wider window is always safe.
    """
    from asx.ingest.detection import record_detection

    client = client or InvestorPAClient()
    now = today or datetime.now(timezone.utc)
    # The Sydney trading date, not the UTC one. market_time.py: "Any code that
    # needs the calendar date of a lodgement must go through market_date() -
    # taking .date() of a UTC timestamp shifts pre-open lodgements to the
    # previous day." The scheduled 09:00 UTC run was safe by luck; the manual
    # workflow_dispatch button was not, and would have asked for a window
    # ending yesterday while the current Sydney session was still lodging.
    end = market_date(now)
    date_to = end.isoformat()
    date_from = (end - timedelta(days=since_days)).isoformat()

    stats = {"found": 0, "new": 0, "duplicate": 0, "unreadable": 0,
             "missing": 0, "truncated": False, "failed": 0,
             "window": [date_from, date_to]}

    page = client.director_interest_notices(date_from=date_from, date_to=date_to)
    stats["truncated"] = page.truncated
    # Lines the vendor counted and we did not even recognise. Not raised: the
    # readable detections on the same page are still facts, and discarding
    # them would turn one styling change into a lost day. Reported instead,
    # and the monitor decides.
    stats["missing"] = page.missing

    for detection in page.detections:
        stats["found"] += 1
        if not detection.format_recognised:
            stats["unreadable"] += 1
        # Committed per detection, exactly as cmd_detect's mailbox loop does
        # and for the reason stated there: "One malformed email must not roll
        # back the alerts already read in this run - under Tier 0 a dropped
        # alert is a permanent dataset hole." Committing once at the end made
        # a single bad row cost the whole window.
        try:
            _doc_id, is_new = record_detection(conn, detection,
                                               llm_classifier=llm_classifier)
            conn.commit()
            stats["new" if is_new else "duplicate"] += 1
        except Exception as exc:      # noqa: BLE001 - deliberate: keep going
            conn.rollback()
            stats["failed"] += 1
            print(f"could not record {detection.source_ref!r}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return stats
