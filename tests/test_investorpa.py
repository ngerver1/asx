"""investorpa.com as a detection source.

The tests that matter here are the boundary ones. This source is the first
that can answer "which announcements exist?" across the whole exchange, and
that is exactly the capability the access decision spends its length
constraining. So most of what follows asserts what the platform must NOT do
with it.
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from asx.ingest.classifier import classify
from asx.ingest.investorpa import (
    COVERAGE_STARTS,
    DIRECTOR_INTEREST_KEYWORDS,
    InvestorPACredentials,
    InvestorPAClient,
    InvestorPAProtocolError,
    detections_from_text,
    ingest,
)
from asx.parse.registry import parseable_doc_classes

SRC = pathlib.Path(__file__).parent.parent / "src" / "asx"


@pytest.fixture(autouse=True)
def _no_throttle_in_tests(monkeypatch):
    """fetch_guard throttles to one request per host per five seconds, which
    is correct in production and ruinous here: the MCP handshake alone is
    three requests, so a dozen transport tests would spend three minutes
    asleep.

    Zeroed rather than bypassed, so the code path under test is still the real
    one. The interval itself is asserted where it belongs, in
    tests/test_fetch_guard.py — not implicitly, by making every other test
    slow.
    """
    from asx.ingest import fetch_guard

    monkeypatch.setattr(fetch_guard, "MIN_INTERVAL_SECONDS", 0.0)
    fetch_guard._last_request.clear()

# Captured verbatim from the live API on 20 Aug 2026. Kept real rather than
# invented: the whole point of a format fixture is that it is the provider's
# output and not our idea of it.
REAL_RESPONSE = """Found 4 announcements for: 'Director's Interest Notice' | dates: 2026-08-19 to 2026-08-20

• 2026-08-20T19:28:29+10:00 | CPO - Change of Director's Interest Notice x 3 | [PDF](https://investorpa.com/announcement-pdf/20260820/330559.pdf) | [View Details](https://investorpa.com/announcement/330559/)
• 2026-08-20T17:17:24+10:00 | LOT - Change of Director's Interest Notice - G Bittar | [PDF](https://investorpa.com/announcement-pdf/20260820/330505.pdf) | [View Details](https://investorpa.com/announcement/330505/)
• 2026-08-20T14:22:13+10:00 | IVX - Initial Director's Interest Notice - Paul FIeld | [PDF](https://investorpa.com/announcement-pdf/20260820/330383.pdf) | [View Details](https://investorpa.com/announcement/330383/)
• 2026-08-20T14:21:48+10:00 | IVX - Final Director's Interest Notice - Alistair Bennallack | [PDF](https://investorpa.com/announcement-pdf/20260820/330381.pdf) | [View Details](https://investorpa.com/announcement/330381/)

Tip: Use get_announcement_detail tool with any announcement_id from above results to get full transcribed text content."""


def _jsonrpc(text: str) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": text}]},
    }).encode()


class _Response:
    def __init__(self, body: bytes, *, status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """Stands in for urlopen so the guard is exercised without the network."""

    def __init__(self, body: bytes):
        self.body = body
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return _Response(self.body)


class _FakeMCPServer:
    """A stand-in that answers what it was ASKED, not a fixed body.

    The transport tests used to inject an opener returning one canned
    response to every request, which meant they asserted what the fake did
    rather than what a server would — and could not have caught a client that
    skipped the handshake entirely, which is precisely the bug they missed.

    This one implements the MUSTs of the Streamable HTTP transport (revision
    2025-06-18) that the client has to satisfy: it assigns a session at
    initialization, rejects later requests that omit it the way the spec says
    a session-requiring server SHOULD, echoes JSON-RPC ids, and returns 202
    with no body for a notification.
    """

    def __init__(self, tool_text: str, *, session_id="sess-1",
                 protocol_version="2025-06-18", expire_after=None):
        self.tool_text = tool_text
        self.session_id = session_id
        self.protocol_version = protocol_version
        self.expire_after = expire_after      # 404 the Nth tools/call
        self.requests = []
        self.tool_calls = 0

    def __call__(self, request, timeout=None):
        from urllib.error import HTTPError

        body = json.loads(request.data)
        self.requests.append((body, dict(request.headers)))
        method = body.get("method")
        # urllib title-cases header names it is given.
        headers = {k.lower(): v for k, v in request.headers.items()}

        if method == "initialize":
            return _Response(json.dumps({
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"protocolVersion": self.protocol_version,
                           "capabilities": {}, "serverInfo": {"name": "fake"}},
            }).encode(), headers={"Content-Type": "application/json",
                                  "Mcp-Session-Id": self.session_id})

        # Everything after initialization must carry the session and version.
        if headers.get("mcp-session-id") != self.session_id:
            raise HTTPError(request.full_url, 400, "Missing session", {},
                            io.BytesIO(b""))
        if not headers.get("mcp-protocol-version"):
            raise HTTPError(request.full_url, 400, "Missing version", {},
                            io.BytesIO(b""))

        if method == "notifications/initialized":
            return _Response(b"", status=202,
                             headers={"Content-Type": "application/json"})

        self.tool_calls += 1
        if self.expire_after and self.tool_calls == self.expire_after:
            self.session_id = "sess-2"        # server rotated it
            raise HTTPError(request.full_url, 404, "Session expired", {},
                                io.BytesIO(b""))
        return _Response(json.dumps({
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": self.tool_text}]},
        }).encode(), headers={"Content-Type": "application/json"})


# --- the boundaries -------------------------------------------------------

def _string_literals(tree) -> list[str]:
    """Every string CONSTANT in a module — excluding docstrings, which are the
    prose explaining why we do not do these things. A test that reads its own
    documentation as a violation is a test that punishes writing it down."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings]


def _built_strings(tree) -> list[str]:
    """Source of every expression that ASSEMBLES a string: f-strings, `+`
    concatenation of a literal, %-formatting and .format(). Building a URL is
    the act under test; describing one in a comment is not."""
    built = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            built.append(ast.unparse(node))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            built.append(ast.unparse(node))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("format", "join")):
            built.append(ast.unparse(node))
    return built


def test_no_source_file_constructs_an_investorpa_document_url():
    """Their identifiers are sequential at ~400/day, so a URL can always be
    built — and building one would collect the whole exchange. That is the
    bulk crawl docs/SOURCE_INVESTORPA.md says "must never be built". A URL is
    used only when a search result stated it.

    Comments and docstrings are excluded deliberately: this module documents
    the pattern at length precisely so nobody reinvents it, and the prose
    saying "never build this" must not read as building it.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for expression in _built_strings(tree):
            if "announcement-pdf" in expression:
                offenders.append(f"{path.relative_to(SRC)}: {expression}")
    assert not offenders, f"investorpa URL construction found: {offenders}"


def test_nothing_resolves_an_entity_through_their_stock_endpoint():
    """Their stock master is current-state, not effective-dated: today `ALU`
    is Alurion Resources, though every ALU announcement before Aug 2024 is
    Altium's. Resolving through it would attach a delisted company's filings
    to whoever inherited the code — the Invariant 1 failure the listings table
    exists to prevent. Tickers from this source are inputs to
    entity_for_ticker and nothing else.

    Asserted on string literals rather than raw text, for the same reason as
    above: the tool name appears in prose explaining why it is not called.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        if any(literal == "search_stocks" for literal in _string_literals(tree)):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, (
        f"search_stocks is named as a callable tool in {offenders}. It is a "
        f"current-ticker lookup and must never resolve an entity."
    )


def test_a_backfill_cannot_silently_cross_the_coverage_floor():
    """Asking for 2023 returns a short answer that looks complete. A hole in
    the source reported as a hole in the market is the failure Invariant 7
    exists to prevent."""
    client = InvestorPAClient(InvestorPACredentials("cid", "rt"))
    with pytest.raises(ValueError, match="coverage begins"):
        client.director_interest_notices(date_from="2023-01-01",
                                         date_to="2023-02-01")


def test_the_ir_route_does_not_claim_investorpa_documents():
    """fetch_ir_documents stores possession_source='ir_website'. An investorpa
    URL fetched there would be recorded as having come from a company's own
    site, which is false."""
    from asx.ingest.possession import _is_investorpa

    assert _is_investorpa("https://investorpa.com/announcement-pdf/20260820/1.pdf")
    assert not _is_investorpa("https://example.com/investorpa.com/x.pdf")
    assert not _is_investorpa("https://www.marketindex.com.au/a.pdf")


# --- reading their output -------------------------------------------------

def test_the_real_response_format_parses():
    detections = detections_from_text(REAL_RESPONSE).detections
    assert len(detections) == 4
    first = detections[0]
    assert first.ticker == "CPO"
    # Split on the FIRST " - " only: titles routinely contain more.
    assert first.title == "Change of Director's Interest Notice x 3"
    assert first.lodged_at == datetime.fromisoformat("2026-08-20T19:28:29+10:00")
    assert first.document_urls == [
        "https://investorpa.com/announcement-pdf/20260820/330559.pdf"]
    assert all(d.format_recognised for d in detections)


def test_a_title_containing_a_dash_keeps_its_whole_title():
    detections = detections_from_text(REAL_RESPONSE).detections
    lot = next(d for d in detections if d.ticker == "LOT")
    assert lot.title == "Change of Director's Interest Notice - G Bittar"


def test_the_timestamp_is_attributed_to_investorpa_not_market_index():
    """lodged_at_source was hardcoded to 'market_index_alert' for every sender
    until migration 0026. knowable_at is the column every analytic joins
    through, and a provenance that names the wrong observer is worse than no
    provenance at all."""
    for d in detections_from_text(REAL_RESPONSE).detections:
        assert d.lodged_at_source == "investorpa"


def test_a_line_shaped_like_a_result_but_unreadable_is_loud():
    """Silence is the failure mode. A provider who restyles their output would
    otherwise yield half-read detections that look deliberate."""
    broken = "• 2026-08-20T99:99:99+10:00 | XXX - not a parseable line"
    detections = detections_from_text(broken).detections
    assert len(detections) == 1
    assert detections[0].format_recognised is False


def test_prose_around_the_results_is_not_mistaken_for_data():
    page = detections_from_text(
        "Found 0 announcements for: 'x'\n\nTip: Use get_announcement_detail")
    assert page.detections == [] and page.stated == 0 and page.complete


def test_an_initial_directors_interest_notice_is_not_read_as_a_trade():
    """An Appendix 3X states a holding at APPOINTMENT. docs/HANDOVER.md:
    "forcing it into director_trades would fabricate a purchase and corrupt
    the cluster signal". The 3Y title pattern matches the bare phrase
    "Director's Interest Notice", so a 3X fell into it until 0026 — which
    mattered little at 17 captured documents and would matter a great deal
    against a feed that returns every one on the exchange.
    """
    detections = detections_from_text(REAL_RESPONSE).detections
    initial = next(d for d in detections if "Initial" in d.title)
    doc_class, _ = classify(initial.title, None)
    assert doc_class == "app_3x"
    assert doc_class not in parseable_doc_classes()

    # The 3Z beside it must still be read: this is a narrowing, not a blanket.
    final = next(d for d in detections if "Final" in d.title)
    assert classify(final.title, None)[0] == "app_3z"


# --- transport ------------------------------------------------------------

def _client(server):
    client = InvestorPAClient(InvestorPACredentials("cid", "rt"), opener=server)
    client._token = "at"                      # skip the token round-trip
    return client


def test_the_tool_call_goes_through_the_guard_as_a_post():
    server = _FakeMCPServer(REAL_RESPONSE)
    page = _client(server).director_interest_notices(date_from="2026-08-19",
                                                     date_to="2026-08-20")
    assert len(page.detections) == 4

    calls = [(b, h) for b, h in server.requests if b.get("method") == "tools/call"]
    # One search per spelling of the form: the union is what makes the sweep
    # whole-exchange rather than whole-exchange-if-titled-our-way.
    assert [c["params"]["arguments"]["keywords"] for c, _ in calls] == \
        list(DIRECTOR_INTEREST_KEYWORDS)
    for call, headers in calls:
        assert call["params"]["name"] == "search_announcements"
        # Only the forms the platform parses, not the whole feed.
        assert headers["Authorization"] == "Bearer at"
        # Honest identification, never a browser string (Invariant 11).
        assert "asx-structural-alpha" in headers["User-agent"]


def test_the_handshake_happens_before_any_tool_is_called():
    """The transport requires a session to begin with initialization. The
    client used to send tools/call cold, which a server assigning a session
    would answer 400 — and the old fake, which replied identically to
    anything, could never have shown it."""
    server = _FakeMCPServer(REAL_RESPONSE)
    _client(server).director_interest_notices(date_from="2026-08-19",
                                              date_to="2026-08-20")
    methods = [body.get("method") for body, _ in server.requests]
    assert methods == ["initialize", "notifications/initialized"] + \
        ["tools/call"] * len(DIRECTOR_INTEREST_KEYWORDS)
    # The handshake happens ONCE for the whole sweep, not per keyword.
    assert methods.count("initialize") == 1


def test_the_session_id_and_protocol_version_travel_on_every_later_request():
    """Both are MUSTs of the 2025-06-18 transport, and the fake enforces them
    the way a session-requiring server does — by answering 400 without them."""
    server = _FakeMCPServer(REAL_RESPONSE)
    _client(server).director_interest_notices(date_from="2026-08-19",
                                              date_to="2026-08-20")
    for body, headers in server.requests[1:]:          # all but initialize
        assert headers["Mcp-session-id"] == "sess-1", body.get("method")
        assert headers["Mcp-protocol-version"] == "2025-06-18"
    # ...and initialize itself carries no session, because none exists yet.
    assert "Mcp-session-id" not in server.requests[0][1]


def test_the_negotiated_protocol_version_is_used_not_ours():
    """The spec says the header SHOULD carry the version agreed at
    initialization, which need not be the one we proposed."""
    server = _FakeMCPServer(REAL_RESPONSE, protocol_version="2025-03-26")
    client = _client(server)
    client.director_interest_notices(date_from="2026-08-19", date_to="2026-08-20")
    assert client._protocol_version == "2025-03-26"
    assert server.requests[-1][1]["Mcp-protocol-version"] == "2025-03-26"


def test_an_expired_session_is_re_established_once():
    """"When a client receives HTTP 404 in response to a request containing an
    Mcp-Session-Id, it MUST start a new session by sending a new
    InitializeRequest without a session id attached." Once, not forever: a 404
    on a fresh session means something other than expiry."""
    server = _FakeMCPServer(REAL_RESPONSE, expire_after=1)
    page = _client(server).director_interest_notices(date_from="2026-08-19",
                                                     date_to="2026-08-20")
    assert len(page.detections) == 4, "the retry recovered the call"
    methods = [body.get("method") for body, _ in server.requests]
    assert methods.count("initialize") == 2
    assert server.requests[-1][1]["Mcp-session-id"] == "sess-2"


class _SSEServer(_FakeMCPServer):
    """Answers tool calls as an event stream. "the server MUST either return
    Content-Type: text/event-stream ... or application/json ... The client
    MUST support both these cases."""

    def __call__(self, request, timeout=None):
        response = super().__call__(request, timeout)
        body = json.loads(request.data)
        if body.get("method") != "tools/call":
            return response
        framed = (f"event: message\ndata: {response.read().decode()}\n\n"
                  f"event: message\ndata: "
                  f'{json.dumps({"jsonrpc": "2.0", "method": "notifications/message"})}'
                  f"\n\n").encode()
        return _Response(framed, headers={"Content-Type": "text/event-stream"})


def test_an_sse_framed_answer_is_read_too():
    """And a trailing notification after the result does not displace it —
    taking the last frame reported a good answer as carrying no content."""
    server = _SSEServer(REAL_RESPONSE)
    assert len(_client(server).director_interest_notices(
        date_from="2026-08-19", date_to="2026-08-20").detections) == 4


def test_an_error_answer_raises_rather_than_returning_nothing():
    """Zero detections and a failed call must not look the same: zero
    lodgements is a pipeline alarm, and so is a broken one."""
    opener = _Opener(json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "error": {"code": -32000, "message": "nope"}}).encode())
    client = InvestorPAClient(InvestorPACredentials("cid", "rt"), opener=opener)
    client._token = "at"
    with pytest.raises(InvestorPAProtocolError):
        client.director_interest_notices(date_from="2026-08-19",
                                         date_to="2026-08-20")


def test_credentials_say_what_to_do_when_absent(monkeypatch):
    monkeypatch.delenv("ASX_INVESTORPA_CLIENT_ID", raising=False)
    monkeypatch.delenv("ASX_INVESTORPA_REFRESH_TOKEN", raising=False)
    with pytest.raises(Exception, match="investorpa_consent"):
        InvestorPACredentials.from_env()


# --- end to end, against a database --------------------------------------

class _FakeClient:
    def __init__(self, detections, *, stated=None, truncated=False):
        from asx.ingest.investorpa import SearchPage

        self._page = SearchPage(
            detections=detections,
            stated=len(detections) if stated is None else stated,
            recognised=len(detections),
            truncated=truncated,
        )

    def director_interest_notices(self, **_kwargs):
        return self._page


def test_detections_reach_the_database_with_honest_provenance(conn):
    detections = detections_from_text(REAL_RESPONSE).detections
    stats = ingest(conn, client=_FakeClient(detections),
                   today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert stats["found"] == 4
    assert stats["new"] == 4

    with conn.cursor() as cur:
        cur.execute("""SELECT detection_source, lodged_at_source, doc_class,
                              parse_status, ticker_as_lodged, entity_id,
                              fetch_candidate_urls, asx_announcement_id
                       FROM documents ORDER BY lodged_at DESC""")
        rows = cur.fetchall()

    assert [r["detection_source"] for r in rows] == ["investorpa"] * 4
    assert [r["lodged_at_source"] for r in rows] == ["investorpa"] * 4
    # The document URL arrives with the detection — the point of the source.
    assert all(r["fetch_candidate_urls"] for r in rows)
    # Their id is a publication counter, not the exchange's announcement
    # number. Writing it to this column would make two feeds' rows collide or
    # diverge at random.
    assert all(r["asx_announcement_id"] is None for r in rows)
    # The ticker is recorded verbatim for audit; entity_id stays NULL because
    # no listings row exists in this fixture. Unresolved, never guessed.
    assert {r["ticker_as_lodged"] for r in rows} == {"CPO", "LOT", "IVX"}
    assert all(r["entity_id"] is None for r in rows)

    classes = {r["doc_class"] for r in rows}
    assert classes == {"app_3y", "app_3x", "app_3z"}
    by_class = {r["doc_class"]: r["parse_status"] for r in rows}
    assert by_class["app_3y"] == "detected"
    assert by_class["app_3z"] == "detected"
    assert by_class["app_3x"] == "not_applicable"   # recorded, never parsed


def test_re_reading_the_same_window_records_nothing_twice(conn):
    detections = detections_from_text(REAL_RESPONSE).detections
    client = _FakeClient(detections)
    first = ingest(conn, client=client,
                   today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    second = ingest(conn, client=client,
                    today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert first["new"] == 4
    assert second["new"] == 0
    assert second["duplicate"] == 4


# --- cross-feed coverage --------------------------------------------------

def _entity_with_ticker(conn, ticker: str) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO listings (entity_id, exchange, ticker,
                                     security_class, valid_from, source)
               VALUES (%s, 'ASX', %s, 'ORD', DATE '2020-01-01', 'manual')""",
            (entity_id, ticker))
    conn.commit()
    return entity_id


def _coverage(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id, detection_source, coverage "
                    "FROM detection_feed_coverage ORDER BY doc_id")
        return {r["doc_id"]: r for r in cur.fetchall()}


def test_one_lodgement_seen_by_both_feeds_is_two_rows_that_agree(conn):
    """Both feeds report the same ASX release instant, but at different
    precisions - Market Index to the minute, investorpa to the second - so a
    partner is found within a tolerance rather than by bucketing. Bucketing to
    the minute split a lodgement whose two observations straddled a boundary,
    inflating the one number this view exists to produce."""
    from asx.ingest.detection import Detection, record_detection

    _entity_with_ticker(conn, "CPO")
    lodged = datetime.fromisoformat("2026-08-20T19:28:29+10:00")
    record_detection(conn, Detection(
        detection_source="market_index_alert", source_ref="<mi@x>",
        ticker="CPO", title="Change of Director's Interest Notice",
        lodged_at=lodged.replace(second=0), announcement_id="2A1690462"))
    record_detection(conn, Detection(
        detection_source="investorpa",
        source_ref="https://investorpa.com/announcement-pdf/20260820/330559.pdf",
        ticker="CPO", title="Change of Director's Interest Notice x 3",
        lodged_at=lodged, lodged_at_source="investorpa",
        document_urls=["https://investorpa.com/announcement-pdf/20260820/330559.pdf"]))
    conn.commit()

    rows = _coverage(conn)
    assert len(rows) == 2, "one lodgement seen twice is two documents, not one"
    assert {r["coverage"] for r in rows.values()} == {"both"}


def test_two_directors_filing_in_the_same_second_are_not_called_duplicates(conn):
    """The failure that mattered most. Grouping by (entity, minute) merged two
    DIFFERENT directors' notices into one row flagged as a duplicate - and a
    company whose directors file together is exactly the batch-lodgement
    pattern the cluster-buy screen exists to find. The view cried wolf on the
    platform's best signal."""
    from asx.ingest.detection import Detection, record_detection

    _entity_with_ticker(conn, "TVL")
    lodged = datetime.fromisoformat("2026-08-21T11:59:01+10:00")
    for i, person in enumerate(("Poswell", "Jefferies")):
        record_detection(conn, Detection(
            detection_source="investorpa",
            source_ref=f"https://investorpa.com/announcement-pdf/20260821/33{i}.pdf",
            ticker="TVL",
            title=f"Change of Director's Interest Notice - {person}",
            lodged_at=lodged + timedelta(seconds=i),
            lodged_at_source="investorpa"))
    conn.commit()

    rows = _coverage(conn)
    assert len(rows) == 2, "two directors filing together are two announcements"
    # Neither is 'both': the partner search excludes the same feed, so a
    # sibling notice from the SAME source can never be mistaken for the other
    # feed having seen it.
    assert {r["coverage"] for r in rows.values()} == {"investorpa_only"}


def test_a_watchlist_gap_shows_as_investorpa_only(conn):
    """The reason the second feed exists: a company nobody was watching."""
    from asx.ingest.detection import Detection, record_detection

    _entity_with_ticker(conn, "JDO")
    record_detection(conn, Detection(
        detection_source="investorpa",
        source_ref="https://investorpa.com/announcement-pdf/20260820/330416.pdf",
        ticker="JDO", title="Change of Director's Interest Notice",
        lodged_at=datetime.fromisoformat("2026-08-20T15:25:18+10:00"),
        lodged_at_source="investorpa"))
    conn.commit()
    assert {r["coverage"] for r in _coverage(conn).values()} == {"investorpa_only"}


def test_an_unresolved_ticker_is_a_bucket_not_an_omission(conn):
    """The old view filtered entity_id IS NOT NULL, dropping exactly the rows
    most likely to BE a coverage gap and reporting perfect agreement over what
    was left. Against a database whose listings are not loaded it returned
    nothing at all and looked like a clean bill of health."""
    from asx.ingest.detection import Detection, record_detection

    # No listings row, so the ticker resolves to nothing.
    record_detection(conn, Detection(
        detection_source="investorpa",
        source_ref="https://investorpa.com/announcement-pdf/20260820/999.pdf",
        ticker="ZZZ", title="Change of Director's Interest Notice",
        lodged_at=datetime.fromisoformat("2026-08-20T15:25:18+10:00"),
        lodged_at_source="investorpa"))
    conn.commit()

    rows = _coverage(conn)
    assert len(rows) == 1, "an unattributable announcement is still a fact"
    assert next(iter(rows.values()))["coverage"] == "unresolved_entity"


# --- possession -----------------------------------------------------------
#
# fetch_ir_documents, which this route is modelled on, was a silent no-op for
# its entire life: it read URLs out of source_ref, which holds a Message-ID,
# so it found nothing and reported an all-zero stats dict that was
# indistinguishable from "nothing to do". fetch_candidate_urls is still empty
# on all 1,124 rows because of it. These tests exist so the sibling route
# cannot repeat that.

def _detected_investorpa_doc(conn, *, ticker="JDO", url=None,
                             title="Change of Director's Interest Notice"):
    """A detection in the state possession finds it: known, held by nobody."""
    from asx.ingest.detection import Detection, record_detection

    url = url or "https://investorpa.com/announcement-pdf/20260820/330416.pdf"
    doc_id, _ = record_detection(conn, Detection(
        detection_source="investorpa", source_ref=url, ticker=ticker,
        title=title,
        lodged_at=datetime.fromisoformat("2026-08-20T15:25:18+10:00"),
        lodged_at_source="investorpa", document_urls=[url]))
    conn.commit()
    return doc_id


def test_a_stated_pdf_url_is_fetched_and_attributed_to_investorpa(conn,
                                                                  monkeypatch,
                                                                  tmp_path):
    from types import SimpleNamespace

    from asx.ingest import possession

    doc_id = _detected_investorpa_doc(conn)
    monkeypatch.setattr(possession, "raw_zone_root", lambda: tmp_path)
    fetched = []

    def _fetch(url, **kwargs):
        fetched.append((url, kwargs))
        return SimpleNamespace(content=b"%PDF-1.7\nfake",
                               content_type="application/pdf", url=url)

    monkeypatch.setattr(possession, "fetch", _fetch)
    stats = possession.fetch_investorpa_documents(conn)

    assert stats == {"attempted": 1, "captured": 1, "robots_blocked": 0,
                     "failed": 0, "not_a_document": 0, "no_candidates": 0}
    # No terms_basis asserted at the call site: investorpa.com is in
    # DECLARED_SOURCES, so the basis is recorded centrally. A caller passing
    # one here would be claiming a per-site sign-off nobody gave.
    assert fetched[0][1].get("terms_basis") is None
    assert fetched[0][1].get("targeted_document") is not True

    with conn.cursor() as cur:
        cur.execute("""SELECT possession_source, parse_status, sha256,
                              storage_path, fetched_at
                       FROM documents WHERE doc_id = %s""", (doc_id,))
        row = cur.fetchone()
    # Never 'ir_website': that would say a company published it on its own site.
    assert row["possession_source"] == "investorpa"
    assert row["parse_status"] == "unparsed"       # now parseable
    assert row["sha256"] and row["storage_path"] and row["fetched_at"]


def test_a_login_wall_is_refused_and_the_capture_gap_stays_open(conn,
                                                                monkeypatch,
                                                                tmp_path):
    """200 with HTML is what a login wall returns. Storing it would poison the
    raw zone AND flip the row out of 'detected', clearing the alarm that says
    the document is still missing."""
    from types import SimpleNamespace

    from asx.ingest import possession

    doc_id = _detected_investorpa_doc(conn)
    monkeypatch.setattr(possession, "raw_zone_root", lambda: tmp_path)
    monkeypatch.setattr(possession, "fetch", lambda url, **kw: SimpleNamespace(
        content=b"<html><body>Please sign in</body></html>",
        content_type="text/html", url=url))

    stats = possession.fetch_investorpa_documents(conn)
    assert stats["not_a_document"] == 1 and stats["captured"] == 0

    with conn.cursor() as cur:
        cur.execute("SELECT parse_status, sha256, possession_source "
                    "FROM documents WHERE doc_id = %s", (doc_id,))
        row = cur.fetchone()
    assert row["parse_status"] == "detected"      # still an open capture gap
    assert row["sha256"] is None
    assert row["possession_source"] is None


def test_the_two_fetch_routes_do_not_take_each_other_s_documents(conn,
                                                                 monkeypatch,
                                                                 tmp_path):
    """Provenance is the point. An investorpa PDF fetched by the IR route
    would be recorded as having come from the company's own website."""
    from types import SimpleNamespace

    from asx.ingest import possession

    ipa = _detected_investorpa_doc(conn)
    monkeypatch.setattr(possession, "raw_zone_root", lambda: tmp_path)
    monkeypatch.setattr(possession, "fetch", lambda url, **kw: SimpleNamespace(
        content=b"%PDF-1.7\nfake", content_type="application/pdf", url=url))

    # The IR route sees it and declines to touch it.
    ir_stats = possession.fetch_ir_documents(conn)
    assert ir_stats["attempted"] == 0 and ir_stats["captured"] == 0

    with conn.cursor() as cur:
        cur.execute("SELECT parse_status FROM documents WHERE doc_id = %s", (ipa,))
        assert cur.fetchone()["parse_status"] == "detected"

    # And the investorpa route does.
    assert possession.fetch_investorpa_documents(conn)["captured"] == 1


def test_a_dead_route_cannot_report_itself_as_a_quiet_one(conn, monkeypatch):
    """no_candidates and attempted are counted separately on purpose: a route
    that never runs and a route that runs and finds nothing look identical in
    an all-zero stats dict, and the first is the bug that went unnoticed on
    the IR route for the life of the project."""
    from asx.ingest import possession
    from asx.ingest.detection import Detection, record_detection

    # Detected, but no document URL was ever recorded against it.
    record_detection(conn, Detection(
        detection_source="market_index_alert", source_ref="<mi@x>",
        ticker="JDO", title="Change of Director's Interest Notice",
        lodged_at=datetime.fromisoformat("2026-08-20T15:25:18+10:00"),
        announcement_id="2A1690999"))
    conn.commit()

    def _never_called(url, **kwargs):        # noqa: ARG001
        raise AssertionError("nothing should be fetched with no candidates")

    monkeypatch.setattr(possession, "fetch", _never_called)
    stats = possession.fetch_investorpa_documents(conn)
    assert stats["no_candidates"] == 1
    assert stats["attempted"] == 0 and stats["captured"] == 0


# ==========================================================================
# Regression tests for the 20 Aug 2026 review. Each one FAILS against the
# code as first shipped, and each pins a bug that reported success while
# doing nothing — the failure mode CLAUDE.md names most sharply and that this
# module reproduced six times in one sitting.
# ==========================================================================

# The vendor's header states how many results it RETURNED, not how many exist:
# the same 19-hour window answers "Found 20" at limit=100 and "Found 5" at
# limit=5 (checked against the live API, 21 Aug 2026). So it is worthless for
# detecting truncation, and exact for detecting a line we failed to read.

def test_a_restyled_line_is_recognised_and_flagged_not_dropped():
    """A dash bullet whose link is no longer wrapped in [PDF](...) markdown.
    The strict pattern misses it; the shape pattern must still recognise it,
    so it becomes an unreadable detection with a review item rather than
    evaporating. Both patterns are built from one _BULLET, so widening one
    cannot leave the other behind again.

    Deliberately NOT a raise. An earlier version of this test demanded one,
    which would have thrown away every readable detection on the same page —
    turning one styling change into a lost day, against the rule that a single
    bad row must not destroy a batch."""
    drifted = ("Found 2 announcements for: 'x'\n\n"
               "- 2026-08-20T17:17:24+10:00 | LOT - Change of Director's "
               "Interest Notice | PDF: https://investorpa.com/a.pdf\n"
               "- 2026-08-20T17:15:44+10:00 | LOT - Change of Director's "
               "Interest Notice | PDF: https://investorpa.com/b.pdf\n")
    page = detections_from_text(drifted)
    assert page.recognised == 2 and page.stated == 2
    assert page.missing == 0                       # nothing vanished
    assert [d.format_recognised for d in page.detections] == [False, False]


def test_a_line_that_vanishes_entirely_is_counted_against_the_stated_total():
    """The case the two patterns are blind to by construction: a line neither
    parses NOR recognises. Only the vendor's own count can see it, which is
    why the count exists — it is independent of how they style anything."""
    body = ("Found 3 announcements for: 'x'\n\n"
            "• 2026-08-20T17:17:24+10:00 | LOT - Title | [PDF](https://investorpa.com/a.pdf)\n"
            "~~~ 2026-08-20 LOT something in a shape nothing recognises ~~~\n"
            "~~~ 2026-08-20 LOT another one ~~~\n")
    page = detections_from_text(body)
    assert page.recognised == 1
    assert page.stated == 3
    assert page.missing == 2, "two lines vanished and nothing else can see it"
    assert not page.complete
    # The readable one survives: a gap is reported, not widened.
    assert len(page.detections) == 1 and page.detections[0].ticker == "LOT"


def test_an_impossible_timestamp_becomes_one_unreadable_line():
    """Shape-valid but impossible times reach datetime.fromisoformat. One bad
    line must not abandon the batch with a traceback."""
    body = ("Found 2 announcements for: 'x'\n\n"
            "• 2026-08-20T25:61:61+10:00 | LOT - Title | [PDF](https://investorpa.com/a.pdf)\n"
            "• 2026-08-20T17:15:44+10:00 | LOT - Good | [PDF](https://investorpa.com/b.pdf)\n")
    page = detections_from_text(body)
    assert len(page.detections) == 2
    assert [d.format_recognised for d in page.detections] == [False, True]
    assert page.detections[1].ticker == "LOT"       # the good line still survives


def test_a_full_page_is_reported_as_possibly_truncated():
    """'Found N' is capped by the limit, so a full page cannot be told apart
    from a page that happens to be exactly N long. Silence here would make
    dropped announcements indistinguishable from announcements never lodged."""
    from asx.ingest.investorpa import page_looks_truncated

    assert page_looks_truncated(returned=500, limit=500) is True
    assert page_looks_truncated(returned=499, limit=500) is False


def test_the_window_is_the_sydney_trading_day_not_the_utc_one(conn, monkeypatch):
    """market_time.py: 'Any code that needs the calendar date of a lodgement
    must go through market_date() — taking .date() of a UTC timestamp shifts
    pre-open lodgements to the previous day.' The 09:00 UTC cron is safe by
    luck; workflow_dispatch at 23:00 Sydney is not."""
    from asx.ingest import investorpa

    asked = {}

    class _Recorder:
        def director_interest_notices(self, *, date_from, date_to, **_kw):
            from asx.ingest.investorpa import SearchPage

            asked["from"], asked["to"] = date_from, date_to
            return SearchPage(detections=[], stated=0, recognised=0)

    # 2026-08-21T14:00Z is 2026-08-22 00:00 in Sydney — a new trading day.
    investorpa.ingest(conn, client=_Recorder(), since_days=1,
                      today=datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc))
    assert asked["to"] == "2026-08-22", (
        f"asked for a window ending {asked['to']}, missing everything lodged "
        f"on the current Sydney trading day")


def test_an_investorpa_alert_keeps_its_document_link_for_the_owner():
    """partition_urls' docstring promises manual_open links are "KEPT, not
    dropped", and the sender rule's comment says own_hosts merely "keeps the
    alert's links out of the automatic fetch set". Both are false for this
    sender: the own_hosts branch continues without appending, so the
    announcement PDF URL — the only route to the document, and the whole
    reason this sender has a rule — is discarded from both lists."""
    import email as _email

    from asx.ingest.mailbox import detection_from_email

    raw = ("From: alerts@investorpa.com\n"
           "Subject: ASX:CYL - Change of Director's Interest Notice\n"
           "Message-ID: <ipa-2@investorpa.com>\n"
           "Date: Wed, 19 Aug 2026 09:35:00 +1000\n\n"
           "https://investorpa.com/announcement-pdf/20260819/293079.pdf\n")
    d = detection_from_email(_email.message_from_string(raw))
    assert d.document_urls or d.manual_open_urls, (
        "the announcement PDF URL was dropped from BOTH lists; the detection "
        "now has no route to the document at all")


def test_skipping_a_document_on_the_ir_route_leaves_a_trace(conn, monkeypatch):
    """possession.py's own comment: "a route that never runs and a route that
    runs and finds nothing look identical in an all-zero stats dict, and the
    first is a dead route reported as a quiet one." The investorpa skip
    continues without incrementing anything, so it is invisible."""
    from asx.ingest import possession

    _detected_investorpa_doc(conn)

    def _never_called(url, **kwargs):        # noqa: ARG001
        raise AssertionError("the IR route must not fetch an investorpa URL")

    monkeypatch.setattr(possession, "fetch", _never_called)
    stats = possession.fetch_ir_documents(conn)
    assert sum(stats.values()) > 0, (
        f"the skipped document left no trace anywhere in {stats}")


def test_the_answer_is_picked_by_request_id_not_by_being_last():
    """The framing helper in isolation. A server may emit the tool result and
    then a log notification or a ping; taking the last frame left us reading
    the notification, which has neither 'result' nor 'error', so a perfectly
    good answer was reported as carrying no text content. The end-to-end path
    is covered by _SSEServer, which appends a trailing notification of its
    own."""
    from asx.ingest.investorpa import _tool_text

    result_frame = json.dumps({
        "jsonrpc": "2.0", "id": 7,
        "result": {"content": [{"type": "text", "text": REAL_RESPONSE}]}})
    trailing = json.dumps({
        "jsonrpc": "2.0", "method": "notifications/message",
        "params": {"level": "info", "data": "search complete"}})
    body = (f"event: message\ndata: {result_frame}\n\n"
            f"event: message\ndata: {trailing}\n\n").encode()

    assert len(detections_from_text(_tool_text(body, 7)).detections) == 4


def test_a_frame_that_answers_a_different_request_is_not_accepted():
    """Silently accepting someone else's answer is worse than failing."""
    from asx.ingest.investorpa import InvestorPAProtocolError, _tool_text

    other = json.dumps({"jsonrpc": "2.0", "id": 99,
                        "result": {"content": [{"type": "text", "text": "x"}]}})
    with pytest.raises(InvestorPAProtocolError, match="no frame answered"):
        _tool_text(f"data: {other}\n\n".encode(), 1)


def test_a_possibly_truncated_page_is_reported_in_the_run_stats(conn):
    """A page exactly as long as the limit cannot be told from a truncated one,
    so ingest reports it rather than treating it as complete. The stats dict is
    what cmd_detect prints and what the workflow reads, so the fact has to
    reach it — a truncation nobody can see is announcements gone missing while
    the run reports success."""
    detections = detections_from_text(REAL_RESPONSE).detections
    stats = ingest(conn, client=_FakeClient(detections, truncated=True),
                   today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert stats["truncated"] is True
    assert stats["found"] == 4          # and the page's contents still landed


def test_lines_the_vendor_counted_but_we_never_saw_reach_the_run_stats(conn):
    """page.missing is the only signal that a line vanished. It must survive
    into the stats rather than being computed and dropped."""
    detections = detections_from_text(REAL_RESPONSE).detections
    stats = ingest(conn, client=_FakeClient(detections, stated=9),
                   today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert stats["missing"] == 5
    assert stats["found"] == 4, "the readable detections still landed"


def test_no_sender_rule_can_silently_lose_a_document_link():
    """The standing accounting invariant, and the correction to the test that
    let this bug ship.

    `test_investorpa_links_are_recorded_not_auto_fetched` asserted
    `document_urls == []` — which a function that drops EVERY link satisfies
    just as well as one that correctly withholds a fetch candidate. So the
    announcement PDF URL disappeared from both lists and the suite stayed
    green.

    The property that actually matters is conservation: a link that looks like
    a document, on a host nothing is prohibited from touching, must end up
    somewhere a human or a fetcher can reach it. Asserted across every
    configured sender rather than for one of them, because the next
    uncalibrated aggregator will have the same shape and nobody will think to
    write this test again.
    """
    from asx.ingest.mailbox import SENDER_RULES, partition_urls

    for rule in SENDER_RULES:
        host = rule.own_hosts[0] if rule.own_hosts else "example-ir.com.au"
        document = f"https://{host}/announcements/3y-aug26.pdf"
        manual, candidates = partition_urls([document], rule)
        assert document in manual or document in candidates, (
            f"{rule.detection_source} drops {document} from both lists: the "
            f"detection would be recorded with no route to the document at "
            f"all, which is indistinguishable from an announcement that has "
            f"no document"
        )


def test_a_calibrated_sender_still_drops_its_tracking_furniture():
    """The counterpart. Recording own-host links for an uncalibrated sender
    must not become 'record everything': Market Index has been read against
    real emails, so its non-announcement own-host links are known to be
    tracking redirects and list management, and putting those on the owner's
    worklist would bury the documents among them."""
    from asx.ingest.mailbox import SENDER_RULES, partition_urls

    market_index = next(r for r in SENDER_RULES
                        if r.detection_source == "market_index_alert")
    noise = "https://www.marketindex.com.au/manage/unsubscribe?u=123"
    manual, candidates = partition_urls([noise], market_index)
    assert noise not in manual and noise not in candidates


def test_the_pairing_tolerance_has_an_actual_boundary(conn):
    """±90 seconds is 59s of precision difference between the two feeds plus
    margin. It is a hypothesis, not a measurement — no lodgement has yet been
    seen by both feeds — so the boundary is pinned here rather than left to a
    comment, and anyone recalibrating it against the first real overlap will
    find this test and have to change it deliberately."""
    from asx.ingest.detection import Detection, record_detection

    def _pair(ticker, gap_seconds):
        _entity_with_ticker(conn, ticker)
        base = datetime.fromisoformat("2026-08-20T11:59:00+10:00")
        record_detection(conn, Detection(
            detection_source="market_index_alert", source_ref=f"<mi-{ticker}>",
            ticker=ticker, title="Change of Director's Interest Notice",
            lodged_at=base, announcement_id=f"2A{ticker}"))
        record_detection(conn, Detection(
            detection_source="investorpa",
            source_ref=f"https://investorpa.com/announcement-pdf/x/{ticker}.pdf",
            ticker=ticker, title="Change of Director's Interest Notice",
            lodged_at=base + timedelta(seconds=gap_seconds),
            lodged_at_source="investorpa"))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT coverage FROM detection_feed_coverage v "
                        "JOIN documents d ON d.doc_id = v.doc_id "
                        "WHERE d.ticker_as_lodged = %s", (ticker,))
            return {r["coverage"] for r in cur.fetchall()}

    assert _pair("AAA", 89) == {"both"}, "inside the tolerance must pair"
    assert _pair("BBB", 91) == {"market_index_only", "investorpa_only"}, (
        "outside the tolerance must NOT pair — silently widening it would "
        "start merging genuinely different lodgements")


# --- the whole exchange, and the keyword union -----------------------------
#
# The sweep searched ONE keyword until 26 Aug 2026. Measured against a single
# day of the exchange it returned 33 announcements where three keywords
# returned 46, and the 13 it missed were not exotic: they were titled
# "Appendix 3Y - <name>" or bare "Appendix 3Z".

def _page(*lines, stated=None):
    head = f"Found {stated if stated is not None else len(lines)} announcements\n"
    return head + "\n".join(lines)


def _line(when, ticker, title, pdf):
    return (f"• {when} | {ticker} - {title} | "
            f"[PDF](https://investorpa.com/announcement-pdf/{pdf}.pdf) | "
            f"[View Details](https://investorpa.com/announcement/{pdf}/)")


def test_the_sweep_asks_for_every_spelling_of_the_form():
    """Not a style check. A title-only filter means the keyword list IS the
    coverage, so a form the list does not name is invisible at detection and
    no later step can recover it."""
    from asx.ingest.investorpa import DIRECTOR_INTEREST_KEYWORDS

    assert not isinstance(DIRECTOR_INTEREST_KEYWORDS, str), \
        "a single keyword misses every notice titled 'Appendix 3Y - <name>'"
    lowered = [k.lower() for k in DIRECTOR_INTEREST_KEYWORDS]
    assert any("3y" in k for k in lowered), "nothing would find a bare Appendix 3Y"
    assert any("3z" in k for k in lowered), "nothing would find a bare Appendix 3Z"
    assert any("director" in k for k in lowered), \
        "nothing would find 'Change of Director's Interest Notice'"


def test_the_same_notice_found_by_two_keywords_is_one_detection():
    """AOV's 24 Aug notice is titled "Appendix 3Y - Change of Director's
    Interest Notice" and answers to both searches. Counting it twice would
    inflate the coverage numbers the whole exercise exists to produce."""
    from asx.ingest.investorpa import detections_from_text, merge_pages

    both = _line("2026-08-24T16:40:01+10:00", "AOV",
                 "Appendix 3Y - Change of Director's Interest Notice", "20260824/331797")
    only_3y = _line("2026-08-24T19:03:47+10:00", "SUN",
                    "Appendix 3Y - Steve Johnston", "20260824/331896")
    merged = merge_pages([detections_from_text(_page(both)),
                          detections_from_text(_page(both, only_3y))])

    keys = [d.key() for d in merged.detections]
    assert len(keys) == len(set(keys)) == 2, keys
    assert {d.ticker for d in merged.detections} == {"AOV", "SUN"}


def test_merging_keeps_the_audit_counts_comparable():
    """stated and recognised answer "did we read every line the vendor sent",
    which is a question about the RESPONSES. Deduplicating them would let a
    line vanish from one page and be hidden by an overlap with another."""
    from asx.ingest.investorpa import detections_from_text, merge_pages

    good = detections_from_text(_page(
        _line("2026-08-24T19:03:47+10:00", "SUN", "Appendix 3Y", "20260824/331896")))
    # The vendor says three, one line is readable, one is recognisable but
    # unparseable, one never arrived at all.
    lossy = detections_from_text(_page(
        _line("2026-08-24T16:40:01+10:00", "AOV", "Appendix 3Y", "20260824/331797"),
        "• 2026-08-24T09:00:00+10:00 | mangled beyond recognition",
        stated=3))

    merged = merge_pages([good, lossy])
    assert merged.stated == 4, "a vendor count was dropped by the union"
    assert merged.recognised == 3
    assert merged.missing == 1, "the line that never arrived stopped being visible"
    assert not merged.complete


def test_one_truncated_page_truncates_the_union():
    """A window under-reported by any one search is under-reported, however
    complete the other searches look."""
    from asx.ingest.investorpa import SearchPage, merge_pages

    full = SearchPage(detections=[], stated=1, recognised=1, truncated=True)
    fine = SearchPage(detections=[], stated=1, recognised=1, truncated=False)
    assert merge_pages([fine, full]).truncated
    assert merge_pages([fine, fine]).truncated is False


def test_pasted_results_take_the_same_path_as_the_live_client():
    """The session bridge must not be a second parser. If it drifts from
    detections_from_text, a session and the cron disagree about the same
    window and nothing says which is right."""
    from asx.ingest.investorpa import PastedSearchClient

    text = _page(_line("2026-08-24T18:59:37+10:00", "SLM",
                       "Change of Director's Interest Notice CG CE KW",
                       "20260824/331895"))
    page = PastedSearchClient([text]).director_interest_notices(
        date_from="2026-08-24", date_to="2026-08-24")

    assert [d.ticker for d in page.detections] == ["SLM"]
    assert page.detections[0].document_urls == [
        "https://investorpa.com/announcement-pdf/20260824/331895.pdf"]
    assert page.complete


def test_pasted_results_refuse_a_window_before_the_coverage_floor():
    from asx.ingest.investorpa import COVERAGE_STARTS, PastedSearchClient

    with pytest.raises(ValueError, match=COVERAGE_STARTS):
        PastedSearchClient(["Found 0 announcements"]).director_interest_notices(
            date_from="2024-01-01", date_to="2024-01-02")


def test_an_empty_paste_is_refused_rather_than_read_as_a_quiet_day():
    """Zero lodgements is a pipeline alarm until a human says otherwise
    (CLAUDE.md). A run given no files at all must not produce that alarm's
    evidence."""
    from asx.ingest.investorpa import PastedSearchClient

    with pytest.raises(ValueError, match="at least one file"):
        PastedSearchClient([]).director_interest_notices(
            date_from="2026-08-24", date_to="2026-08-24")
