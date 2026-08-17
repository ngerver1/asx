"""The single chokepoint for every automated HTTP fetch in the platform
(Invariant 11, and the Tier 0 access decision's central commitment).

Two rules are enforced here in code, not merely documented:

1. **No automated device ever accesses asx.com.au.** Not the ingester, not a
   link-follower, not a retry, not a "just this once" backfill script. The
   owner opens ASX announcements personally in a browser; a local watcher
   files what they opened. Any automated attempt raises ProhibitedSourceError
   — including links extracted from alert emails, which is exactly the path
   most likely to reach for asx.com.au by accident.

2. **Everything else is fetched politely**: robots.txt respected, one request
   at a time per host with a minimum interval, an honest identifying
   user-agent, and no rotation of IPs or user-agents ever. Evasion is
   prohibited by Invariant 11 regardless of whether it would work.

If a source cannot be reached within these rules, the platform stops and says
so rather than working around it.
"""

from __future__ import annotations

import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Domains no automated process may ever contact. Matched on the registrable
# domain and all subdomains.
PROHIBITED_HOSTS = ("asx.com.au", "www2.asx.com.au")

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


def is_prohibited(url: str) -> bool:
    """True if this URL may never be fetched by automated code."""
    host = _host(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in PROHIBITED_HOSTS)


def assert_fetchable(url: str) -> None:
    """Raise unless automated code is permitted to fetch this URL. Call this
    anywhere a URL might be followed, even if no fetch immediately follows —
    it documents the boundary at the point of temptation."""
    if is_prohibited(url):
        raise ProhibitedSourceError(
            f"Refusing to fetch {url}: the ASX website is accessed by the owner "
            f"personally, never by automated code (access decision §1/§6). "
            f"Open it in the capture browser profile; the watcher will file it."
        )


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


def fetch(url: str, *, opener=None) -> FetchResult:
    """Politely fetch a URL. The only sanctioned automated-fetch path.

    `opener` is injectable so tests exercise the guard without network access.
    """
    assert_fetchable(url)
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
