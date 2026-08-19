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

import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Domains where automated retrieval is permitted ONLY for a specific,
# already-known document. Matched on the registrable domain and all
# subdomains. Not a blocklist and not an allowlist: a gate that demands the
# caller prove the request is targeted.
RESTRICTED_HOSTS = ("asx.com.au",)

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

# What a retrievable document looks like. Deliberately narrow: a PDF. Widen
# only against a verified example, never against a remembered URL shape.
_DOCUMENT_RE = re.compile(r"\.pdf($|[?#])", re.I)

# A bounded run cannot become a crawl. Reset per process; the possession path
# also caps its own batch.
MAX_RESTRICTED_FETCHES_PER_RUN = 50
_restricted_fetches = 0

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


class ProhibitedSourceError(RuntimeError):
    """Raised when automated code attempts a source the access decision
    forbids it from touching."""


class RobotsDisallowedError(RuntimeError):
    """Raised when a site's robots.txt disallows the path."""


@dataclass
class FetchResult:
    url: str
    content: bytes
    content_type: str | None


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
    """True if the URL addresses a document rather than a page about them."""
    return bool(_DOCUMENT_RE.search(url))


def assert_fetchable(url: str, *, targeted_document: bool = False) -> None:
    """Raise unless automated code is permitted to fetch this URL.

    Call this anywhere a URL might be followed, even if no fetch immediately
    follows — it documents the boundary at the point of temptation.

    `targeted_document=True` is an assertion by the caller that this URL came
    from an announcement already known to exist, recorded against a detection.
    It is not a way to unlock the ASX generally: the URL must still address a
    document and must not be a discovery endpoint.
    """
    if is_discovery_url(url):
        raise ProhibitedSourceError(
            f"Refusing to fetch {url}: it looks like a search, listing or "
            f"browse endpoint. The access decision permits retrieving a "
            f"specific known announcement, never discovering announcements. "
            f"No caller flag overrides this."
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


def fetch(url: str, *, opener=None, targeted_document: bool = False) -> FetchResult:
    """Politely fetch a URL. The only sanctioned automated-fetch path.

    `opener` is injectable so tests exercise the guard without network access.
    """
    assert_fetchable(url, targeted_document=targeted_document)
    if not _robots_allows(url):
        raise RobotsDisallowedError(
            f"robots.txt disallows {url} for this user-agent; not fetching"
        )
    _throttle(_host(url))

    request = Request(url, headers={"User-Agent": USER_AGENT})
    do_open = opener or urlopen
    with do_open(request, timeout=TIMEOUT_SECONDS) as response:
        content = response.read()
        content_type = response.headers.get("Content-Type")
    return FetchResult(url, content, content_type)
