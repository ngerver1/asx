"""The single chokepoint for every automated HTTP fetch in the platform
(Invariant 11, and the Tier 0 access decision's central commitment).

Three rules are enforced here in code, not merely documented:

1. **asx.com.au is TARGETED-RETRIEVAL ONLY** (access decision §1, amended
   20 Aug 2026 on legal advice). A specific announcement document that is
   already known to exist may be retrieved. Nothing may be discovered: no
   listing page, no search, no browse, no index, no link-following, no
   enumeration of identifiers. The distinction is the whole basis of the
   amendment, so it is enforced structurally rather than by intention —
   `assert_fetchable` refuses an ASX URL unless the caller passes
   `targeted_document=True`, and only the possession path that works from a
   recorded announcement URL does so.

   A retrieval is targeted when ALL of these hold:
     * the URL was recorded against a detection we already hold, so the
       document's existence was learned elsewhere, not by asking ASX;
     * the URL addresses a document (a PDF), not a page that lists documents;
     * it carries no search, query or pagination parameters;
     * the run is bounded, so a loop cannot become a crawl.

2. **Discovery endpoints stay blocked outright**, on any host. A URL whose
   path or query says "search", "browse", "list" or similar is refused even
   with `targeted_document=True`, because no amount of caller intent turns a
   search result into a specific known document.

3. **Everything is fetched politely**: robots.txt respected, one request at a
   time per host with a minimum interval, an honest identifying user-agent,
   and no rotation of IPs or user-agents ever. Evasion is prohibited by
   Invariant 11 regardless of whether it would work.

If a source cannot be reached within these rules, the platform stops and says
so rather than working around it.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Domains where automated retrieval is permitted ONLY for a specific,
# already-known document. Matched on the registrable domain and all
# subdomains. Not a blocklist and not an allowlist: a gate that demands the
# caller prove the request is targeted.
# ASX announcement documents are NOT served from asx.com.au. They come from
# the exchange's CDN/API provider, under an ASX-scoped gateway path:
#
#   https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/
#       2924-03123039-2A1690462
#
# Restricting only asx.com.au therefore guarded a door the documents never
# come through — the gate was decorative for the one path it exists to
# control. The restriction follows the DOCUMENTS, not the brand in the
# hostname.
RESTRICTED_HOSTS = ("asx.com.au", "cdn-api.markitdigital.com")

# The ASX document endpoint on that CDN. The host is a shared gateway serving
# many of the provider's clients, so only the ASX-scoped path counts as an
# announcement document; anything else on the host is not one, and is refused
# rather than assumed.
_ASX_CDN_DOCUMENT_RE = re.compile(
    r"^https://cdn-api\.markitdigital\.com/apiman-gateway/ASX/"
    r"asx-research/[\d.]+/file/[\w.-]+", re.I)

# Kept as an alias so nothing silently changes meaning: code that asked
# "is this prohibited?" is really asking "does this host need the targeted
# gate?", and the answer is still yes for the ASX.
PROHIBITED_HOSTS = RESTRICTED_HOSTS

# Path or query fragments that mean DISCOVERY rather than retrieval. Refused
# on every host, with or without targeted_document, because a caller cannot
# assert its way out of the fact that a search result is not a known document.
_DISCOVERY_RE = re.compile(
    r"(/search|/browse|/find|/list|/index|/directory|/results|/query|"
    r"[?&](q|query|search|page|offset|start|from|to|keyword)=)", re.I)

# What a retrievable document looks like. Deliberately narrow, and widened
# only against a VERIFIED example: the ASX endpoint below was added after two
# real announcement URLs were supplied and their shape confirmed, not from a
# remembered pattern.
_DOCUMENT_RE = re.compile(r"\.pdf($|[?#])", re.I)

# A bounded run cannot become a crawl. Reset per process; the possession path
# also caps its own batch.
MAX_RESTRICTED_FETCHES_PER_RUN = 50
_restricted_fetches = 0


@dataclass(frozen=True)
class SourceTerms:
    """The recorded basis on which a host may be fetched at all."""
    basis: str
    targeted_only: bool = False


# Invariant 11 and access decision §6 require a per-source terms basis before
# anything is fetched. That requirement existed only on paper until a
# suggestion to "just use hotcopper.com.au" made the gap obvious: any host not
# named in RESTRICTED_HOSTS was fetchable by the generic path, listing pages
# included, with nobody having read its terms.
#
# A host must now be declared here, or the caller must pass an explicit
# terms_basis, or the fetch is refused. Adding a line to this table is a
# deliberate act that records WHY a source is permitted — which is what
# Invariant 11 asks for and what a later reader will need.
DECLARED_SOURCES: dict[str, SourceTerms] = {
    "cdn-api.markitdigital.com": SourceTerms(
        basis="ASX announcement documents are served from this gateway under "
              "/apiman-gateway/ASX/. Covered by the same access decision §6 "
              "amendment as asx.com.au, because it is the exchange's document "
              "endpoint rather than a third party's copy",
        targeted_only=True,
    ),
    "asx.com.au": SourceTerms(
        basis="access decision §6 amendment, 20 Aug 2026: targeted retrieval "
              "of specific announcement documents, on the owner's legal advice",
        targeted_only=True,
    ),
    # Announcement detection and documents — see asx/ingest/investorpa.py.
    #
    # Basis read at declaration time rather than remembered, per Invariant 11
    # and the working-style rule about verifying against the primary source:
    #   * There is no terms-of-use page. /terms/, /terms-of-use/, /legal/,
    #     /tos/ and /privacy/ all 404 (checked 20 Aug 2026), and the footer
    #     carries a bare "© 2024 investorpa. All rights reserved."
    #   * What exists instead is an affirmative published offer.
    #     investorpa.com/features/ advertises, as a product feature:
    #       "Remote MCP Server — Ask your AI about the ASX. InvestorPA's MCP
    #        Server connects ASX announcements directly to any MCP-compatible
    #        AI harnesses. Works with Claude Desktop & Mobile, ChatGPT Desktop
    #        & Mobile, Claude Code, Codex, LM Studio and more. No local
    #        package installs necessary. Just connect and ask away."
    #   * robots.txt 404s, i.e. no restrictions (RFC 9309 §2.3.1.3).
    #
    # The honest limit of that basis, recorded because it is a judgement and
    # not a quotation: the grant is written for AI harnesses asking questions.
    # Reading it to cover a scheduled ingest is this platform's inference. The
    # proportionality rules in asx/ingest/investorpa.py exist to keep the use
    # recognisably the thing that was offered — Appendix 3Y/3Z only, via the
    # vendor's own search, never by enumerating identifiers.
    "investorpa.com": SourceTerms(
        basis="owner sign-off 20 Aug 2026. No terms page exists; the basis is "
              "the vendor's published /features/ offer of a Remote MCP Server "
              "for MCP-compatible AI harnesses, naming Claude Code. robots.txt "
              "absent (404), so unrestricted per RFC 9309. Re-host, not the "
              "exchange: documents carry possession_source='investorpa' and "
              "lodged_at_source='investorpa', never 'asx'",
    ),
}

# Honest identification. Never randomised, never disguised as a browser.
USER_AGENT = (
    "asx-structural-alpha/0.1 (personal research tool; contact: "
    "nicholas.gerver@gmail.com)"
)

MIN_INTERVAL_SECONDS = 5.0   # per host; deliberately slow
TIMEOUT_SECONDS = 30

_last_request: dict[str, float] = {}
_lock = threading.Lock()
_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


# How many hops an uncredentialed, bodyless GET may take. A redirect chain is
# not a crawl, but it is a way to become one by accident.
MAX_REDIRECTS = 3

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class _NoAutomaticRedirects(HTTPRedirectHandler):
    """urllib must never follow a redirect on this platform's behalf.

    `HTTPRedirectHandler.redirect_request` copies every header except
    content-length and content-type onto the target, with no same-origin
    test — so a 302 replays the Authorization header to whatever host the
    response names. It also issues that request itself, from inside
    OpenerDirector, so `assert_fetchable`, `_robots_allows` and `_throttle`
    are all skipped on the one request that actually leaves the network. The
    chokepoint would be bypassed by the last hop of every fetch that had one.

    And on 301/302/303 it re-issues a POST as a GET with the body dropped,
    which would turn a JSON-RPC call into a bodyless GET whose answer parses
    as "no results" — a silent zero, which is the failure this platform
    treats as an alarm.

    Returning None means no handler will follow it: urllib falls through to
    HTTPDefaultErrorHandler and raises HTTPError carrying the code, the
    Location header and the body. The redirect becomes data that fetch()
    decides on, in the open, rather than an action taken on our behalf.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = None


def guarded_opener():
    """The only opener this platform uses.

    Shared rather than rebuilt so nothing acquires a redirect-following one by
    reaching for `urlopen`. `build_opener` replaces its default redirect
    handler when passed an instance of a subclass, so this is a substitution
    and not an addition.
    """
    global _opener
    with _lock:
        if _opener is None:
            _opener = build_opener(_NoAutomaticRedirects())
        return _opener


class ProhibitedSourceError(RuntimeError):
    """Raised when automated code attempts a source the access decision
    forbids it from touching."""


class RedirectRefusedError(RuntimeError):
    """Raised when a server tried to send a request somewhere this guard has
    not cleared, or somewhere our credentials must not follow."""


class RobotsDisallowedError(RuntimeError):
    """Raised when a site's robots.txt disallows the path."""


@dataclass
class FetchResult:
    url: str
    content: bytes
    content_type: str | None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """Case-insensitive lookup. RFC 9110 field names are case-insensitive,
        and a server answering 'mcp-session-id' must not read as no session."""
        lowered = name.lower()
        return next((v for k, v in self.headers.items()
                     if k.lower() == lowered), None)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_restricted(url: str) -> bool:
    """True if this host may only be fetched as a targeted known document."""
    host = _host(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in RESTRICTED_HOSTS)


# Retained name: callers asking "is this prohibited?" want the targeted gate.
is_prohibited = is_restricted


def is_discovery_url(url: str) -> bool:
    """True if the URL looks like searching or browsing rather than fetching a
    named document."""
    return bool(_DISCOVERY_RE.search(url))


def is_document_url(url: str) -> bool:
    """True if the URL addresses a document rather than a page about them.

    Two accepted shapes: a plain PDF, and the ASX research file endpoint,
    which serves a PDF without ever saying so in the path.
    """
    return bool(_DOCUMENT_RE.search(url) or _ASX_CDN_DOCUMENT_RE.match(url))


def normalise_document_url(url: str) -> str:
    """Strip the junk a browser address bar leaves on an ASX document URL.

    The exchange's own UI appends `&v=undefined` — a JavaScript artifact with
    no `?` before it, so it is not even a query string. It is removed so the
    same document cannot be recorded under two different URLs.
    """
    return re.sub(r"&v=undefined\b", "", url.strip())


def _declared_for(host: str) -> "SourceTerms | None":
    for domain, terms in DECLARED_SOURCES.items():
        if host == domain or host.endswith("." + domain):
            return terms
    return None


def assert_fetchable(url: str, *, targeted_document: bool = False,
                     terms_basis: str | None = None) -> None:
    """Raise unless automated code is permitted to fetch this URL.

    Call this anywhere a URL might be followed, even if no fetch immediately
    follows — it documents the boundary at the point of temptation.

    `targeted_document=True` is an assertion by the caller that this URL came
    from an announcement already known to exist, recorded against a detection.
    It is not a way to unlock the ASX generally: the URL must still address a
    document and must not be a discovery endpoint.

    `terms_basis` is for hosts not in DECLARED_SOURCES — company IR sites,
    which the owner spot-checks individually as they enter the watchlist. The
    caller states the basis; passing one is a claim that somebody read the
    terms, so it belongs at a path where that is true.
    """
    if is_discovery_url(url):
        raise ProhibitedSourceError(
            f"Refusing to fetch {url}: it looks like a search, listing or "
            f"browse endpoint. The access decision permits retrieving a "
            f"specific known announcement, never discovering announcements. "
            f"No caller flag overrides this."
        )
    host = _host(url)
    declared = _declared_for(host)
    if declared is None and not terms_basis:
        raise ProhibitedSourceError(
            f"Refusing to fetch {url}: no terms basis is recorded for "
            f"{host!r}. Invariant 11 and access decision §6 require knowing "
            f"why a source may be fetched BEFORE fetching it. Add it to "
            f"DECLARED_SOURCES with the basis, or pass terms_basis=... if the "
            f"caller carries a standing per-site sign-off. A source being "
            f"useful is not a basis."
        )
    if not is_restricted(url):
        return
    if not targeted_document:
        raise ProhibitedSourceError(
            f"Refusing to fetch {url}: {_host(url)} is targeted-retrieval only "
            f"(access decision §1, amended 20 Aug 2026). Automated code may "
            f"retrieve a specific announcement document recorded against a "
            f"detection it already holds; it may not follow links, browse, or "
            f"fetch on a hunch. Use possession.fetch_asx_documents(), which "
            f"works from documents.asx_document_url."
        )
    if not is_document_url(url):
        raise ProhibitedSourceError(
            f"Refusing to fetch {url}: targeted retrieval is for a document "
            f"(a PDF). This URL addresses a page, and fetching pages from the "
            f"ASX is how targeted retrieval turns into scraping. If the real "
            f"document URL is known, record that instead."
        )
    global _restricted_fetches
    if _restricted_fetches >= MAX_RESTRICTED_FETCHES_PER_RUN:
        raise ProhibitedSourceError(
            f"Refusing to fetch {url}: this run has already retrieved "
            f"{_restricted_fetches} documents from restricted hosts, the "
            f"per-run ceiling. A bounded run cannot become a crawl by "
            f"accident. Re-run deliberately to continue."
        )
    _restricted_fetches += 1


def reset_restricted_budget() -> None:
    """Start a fresh per-run budget. Called by the possession entrypoint."""
    global _restricted_fetches
    _restricted_fetches = 0


def _robots_allows(url: str) -> bool:
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    rp = _robots_cache.get(root)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{root}/robots.txt")
        try:
            rp.read()
        except Exception:
            # Unreachable robots.txt: treat as disallowed rather than assume
            # permission. Stopping is the prescribed response to an access
            # question we cannot answer.
            return False
        _robots_cache[root] = rp
    return rp.can_fetch(USER_AGENT, url)


def _throttle(host: str) -> None:
    with _lock:
        last = _last_request.get(host)
        now = time.monotonic()
        if last is not None:
            wait = MIN_INTERVAL_SECONDS - (now - last)
            if wait > 0:
                time.sleep(wait)
        _last_request[host] = time.monotonic()


def fetch(url: str, *, opener=None, targeted_document: bool = False,
          terms_basis: str | None = None,
          post_json: dict | None = None,
          bearer_token: str | None = None,
          allow_status: frozenset = frozenset(),
          extra_headers: dict[str, str] | None = None) -> FetchResult:
    """Politely fetch a URL. The only sanctioned automated-fetch path.

    `opener` is injectable so tests exercise the guard without network access.

    `post_json` sends a JSON body instead of a plain GET. It exists for one
    source: an MCP endpoint speaks JSON-RPC over POST, and routing it through
    here rather than around here is the point — the chokepoint is only a
    chokepoint if every outbound request passes it, including the ones whose
    shape is inconvenient. All the same gates apply.

    A limit worth stating where someone will read it: this guard reads URLs,
    and a JSON-RPC method name lives in the body. So `is_discovery_url` cannot
    see that an MCP call is a search. That is tolerable only because the
    discovery prohibition was never a blanket rule — it is specific to the
    exchange, whose terms do not offer a search API. A vendor whose published
    product IS a search endpoint is offering exactly that use, and calling it
    is the sanctioned thing rather than the evasion. Where that reasoning does
    not hold, the host does not belong in DECLARED_SOURCES.

    `bearer_token` attaches a credential, and changes the redirect rule: a
    credentialed request follows no redirect at all. See RedirectRefusedError
    and _NoAutomaticRedirects — urllib would otherwise replay the header to
    whatever host a 302 names, on a request that never re-enters this module.
    An uncredentialed, bodyless GET may follow up to MAX_REDIRECTS hops, and
    each hop re-enters every gate above rather than slipping past them.

    `allow_status` names statuses the CALLER will read itself; everything else
    outside 2xx still raises, so a failed fetch is never handed back as a thin
    successful one.
    """
    credentialed = bool(bearer_token)
    if credentialed and not url.lower().startswith("https://"):
        raise ProhibitedSourceError(
            f"Refusing to send a bearer token to {url}: not https. A "
            f"credential on a plaintext hop is disclosed to every device on "
            f"the path, and no terms basis makes that acceptable."
        )

    for hop in range(MAX_REDIRECTS + 1):
        assert_fetchable(url, targeted_document=targeted_document,
                         terms_basis=terms_basis)
        if not _robots_allows(url):
            raise RobotsDisallowedError(
                f"robots.txt disallows {url} for this user-agent; not fetching"
            )
        _throttle(_host(url))

        headers = {"User-Agent": USER_AGENT}
        if extra_headers:
            # Protocol headers a caller genuinely needs — an MCP client must
            # echo a session id and state a protocol version. Deliberately
            # NOT a general header pass-through: User-Agent and Authorization
            # are the guard's own, and a caller able to set User-Agent could
            # disguise this platform as something else, which Invariant 11
            # prohibits outright ("never randomised, never disguised as a
            # browser"). Refused rather than ignored, so the attempt is loud.
            for name in extra_headers:
                if name.lower() in ("user-agent", "authorization"):
                    raise ValueError(
                        f"{name!r} is the guard's to set, not a caller's. "
                        f"Honest identification is Invariant 11."
                    )
            headers.update(extra_headers)
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        body = None
        if post_json is not None:
            body = json.dumps(post_json).encode("utf-8")
            headers["Content-Type"] = "application/json"
            # Streamable-HTTP MCP servers may answer either way.
            headers["Accept"] = "application/json, text/event-stream"
        request = Request(url, data=body, headers=headers)
        do_open = opener or guarded_opener().open
        try:
            with do_open(request, timeout=TIMEOUT_SECONDS) as response:
                return FetchResult(
                    url, response.read(),
                    response.headers.get("Content-Type"),
                    status=getattr(response, "status", 200) or 200,
                    headers={k: v for k, v in response.headers.items()},
                )
        except HTTPError as exc:
            if exc.code in allow_status:
                # A status the CALLER declared it will interpret itself. Never
                # a blanket pass: anything not named still raises, so a failed
                # fetch is never returned as a thin successful one.
                return FetchResult(url, exc.read(),
                                   exc.headers.get("Content-Type"),
                                   status=exc.code,
                                   headers={k: v for k, v in exc.headers.items()})
            if exc.code not in _REDIRECT_CODES:
                raise
            target = exc.headers.get("Location")
            if not target:
                raise RedirectRefusedError(
                    f"{url} answered {exc.code} with no Location header"
                ) from exc
            target = urljoin(url, target)

            # A request carrying credentials or a body follows NOTHING, not
            # even to the same origin. Three reasons, in order of weight:
            # a credential must go only where we decided to send it; urllib
            # cannot replay a POST body across 301/302/303 anyway, so
            # "following" would mean silently re-POSTing on the endpoint's
            # say-so; and these URLs are module constants, so a redirect on
            # one means the vendor moved it or something is intercepting —
            # both facts a human must see rather than route around.
            if credentialed or post_json is not None:
                raise RedirectRefusedError(
                    f"{url} redirected to {target} ({exc.code}), and this "
                    f"request carries "
                    f"{'credentials' if credentialed else 'a body'}. Refusing "
                    f"to follow: a redirect is the endpoint telling us it "
                    f"moved, which is a decision for a human, not a hop to "
                    f"take automatically."
                ) from exc

            if hop >= MAX_REDIRECTS:
                raise RedirectRefusedError(
                    f"more than {MAX_REDIRECTS} redirects starting at {url}"
                ) from exc
            # An uncredentialed, bodyless GET may follow — but through the
            # front door. The loop re-enters assert_fetchable, robots and the
            # throttle on the NEW url, so a hop onto an undeclared host is
            # refused for want of a terms basis, and a restricted-host
            # document that redirects to a login page now fails
            # is_document_url instead of being stored as announcement bytes.
            url = target

    raise RedirectRefusedError(f"redirect loop starting at {url}")
