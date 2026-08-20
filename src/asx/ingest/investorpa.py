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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

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
# fields have to be recovered from a line. That is fragile in the specific way
# CLAUDE.md cares about: a provider who restyles their output would otherwise
# start yielding half-read detections that look deliberate. So a line that does
# not match is not skipped — it sets format_recognised=False and reaches the
# review queue through the same path an unreadable alert email does.
#
#   • 2026-08-20T17:17:24+10:00 | LOT - Change of Director's Interest Notice - G Bittar | [PDF](https://…) | [View Details](https://…)
#
# Split on the FIRST " - " only: titles routinely contain more of them.
_RESULT_LINE = re.compile(
    r"^\s*[•*\-]\s*"
    r"(?P<when>\d{4}-\d{2}-\d{2}T[\d:]{8}[+\-]\d{2}:\d{2})\s*\|\s*"
    r"(?P<ticker>[A-Z0-9]{2,6})\s*-\s*"
    r"(?P<title>.*?)\s*\|\s*"
    r"\[PDF\]\((?P<pdf>[^)\s]+)\)"
    r"(?:\s*\|\s*\[View Details\]\((?P<page>[^)\s]+)\))?"
    r"\s*$"
)

# A line that begins like a result but does not parse. Used to tell "the
# provider changed their format" apart from "this line was never a result"
# (headers, tips, blank lines), because only the first is an alarm.
_LOOKS_LIKE_RESULT = re.compile(r"^\s*[•*]\s*\d{4}-\d{2}-\d{2}T")


def _parse_result_line(line: str) -> Detection | None:
    """One search-result line -> a Detection, or None if it is not a result."""
    match = _RESULT_LINE.match(line)
    if match is None:
        if not _LOOKS_LIKE_RESULT.match(line):
            return None
        # Shaped like a result and unreadable: record it so it is visible.
        return Detection(
            detection_source="investorpa",
            source_ref=line.strip()[:200],
            title=line.strip()[:200],
            format_recognised=False,
        )

    when = datetime.fromisoformat(match["when"])
    pdf = match["pdf"]
    return Detection(
        detection_source="investorpa",
        # Their announcement id, taken from the URL they gave us rather than
        # constructed. It is their publication sequence, NOT the ASX
        # announcement number, so it is deliberately not written to
        # documents.asx_announcement_id — that column means the exchange's
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


def detections_from_text(text: str) -> list[Detection]:
    """Every detection in one tool response."""
    return [d for d in (_parse_result_line(line) for line in text.splitlines())
            if d is not None]


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
        return _text_from_jsonrpc(result.content)

    # -- the one query this module makes -----------------------------------
    def director_interest_notices(self, *, date_from: str, date_to: str,
                                  limit: int = MAX_RESULTS_PER_CALL,
                                  ) -> list[Detection]:
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
        return detections_from_text(text)


def _text_from_jsonrpc(raw: bytes) -> str:
    """Pull the text content out of a JSON-RPC (or SSE-framed) tool result.

    Streamable-HTTP MCP servers may answer with application/json or with an
    event stream carrying the same object, so both are handled rather than
    assumed.
    """
    body = raw.decode("utf-8", errors="replace").strip()
    if not body:
        raise InvestorPAProtocolError("empty response from the MCP endpoint")

    payload = None
    if body.startswith("{"):
        payload = json.loads(body)
    else:
        # SSE framing: one or more "data: {...}" lines.
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                candidate = line[len("data:"):].strip()
                if candidate.startswith("{"):
                    payload = json.loads(candidate)
        if payload is None:
            raise InvestorPAProtocolError(
                f"could not find a JSON-RPC object in the response: {body[:200]!r}")

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
    is the document URL the vendor stated — stable across re-reads, so running
    it twice costs nothing and a wider window is always safe.
    """
    from asx.ingest.detection import record_detection

    client = client or InvestorPAClient()
    now = today or datetime.now(timezone.utc)
    date_to = now.date().isoformat()
    date_from = (now.date() - timedelta(days=since_days)).isoformat()

    stats = {"found": 0, "new": 0, "duplicate": 0, "unreadable": 0}
    for detection in client.director_interest_notices(date_from=date_from,
                                                      date_to=date_to):
        stats["found"] += 1
        if not detection.format_recognised:
            stats["unreadable"] += 1
        _, is_new = record_detection(conn, detection, llm_classifier=llm_classifier)
        stats["new" if is_new else "duplicate"] += 1
    conn.commit()
    return stats
