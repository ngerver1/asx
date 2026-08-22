"""The ASX no-automation rule is the access decision's central commitment.
These tests are what make it a property of the code rather than a promise."""

import pytest

from asx.ingest.fetch_guard import (
    PROHIBITED_HOSTS,
    ProhibitedSourceError,
    USER_AGENT,
    assert_fetchable,
    fetch,
    is_prohibited,
)


@pytest.mark.parametrize("url", [
    "https://www.asx.com.au/asx/statistics/announcements.do",
    "http://asx.com.au/anything",
    "https://ASX.COM.AU/UPPER/CASE",
    "https://www2.asx.com.au/markets/trade-our-cash-market",
    "https://cdn.asx.com.au/some/announcement.pdf",
    "https://announcements.asx.com.au/asxpdf/20260814/pdf/abc.pdf",
])
def test_asx_urls_are_prohibited(url):
    assert is_prohibited(url)
    with pytest.raises(ProhibitedSourceError):
        assert_fetchable(url)
    with pytest.raises(ProhibitedSourceError):
        fetch(url, opener=lambda *a, **k: pytest.fail("must not reach the network"))


@pytest.mark.parametrize("url", [
    "https://example-mining.com.au/investors/announcement.pdf",
    "https://www.vanguard.com.au/holdings.csv",
])
def test_non_asx_urls_are_not_prohibited(url):
    assert not is_prohibited(url)
    # Not restricted, but still not free: Invariant 11 wants a recorded terms
    # basis for every source, which the caller supplies for IR sites the owner
    # has spot-checked.
    assert_fetchable(url, terms_basis="owner spot-checked this site")


def test_lookalike_domains_are_not_over_blocked():
    # The guard must block the ASX, not every domain containing the letters.
    assert not is_prohibited("https://myasx.com.au/x")
    assert not is_prohibited("https://asx.com.au.evil.example/x")


def test_prohibited_list_covers_the_documented_domains():
    assert "asx.com.au" in PROHIBITED_HOSTS


def test_user_agent_is_honest_and_static():
    # Invariant 11 prohibits rotating or disguising identity to evade limits.
    assert "asx-structural-alpha" in USER_AGENT
    assert "Mozilla" not in USER_AGENT
    assert USER_AGENT == USER_AGENT  # no randomisation


def test_guard_refuses_before_consulting_robots():
    # An ASX URL must be refused outright, not merely subjected to robots
    # rules that might one day permit it.
    calls = []

    def spy_opener(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must not open")

    with pytest.raises(ProhibitedSourceError):
        fetch("https://www.asx.com.au/x.pdf", opener=spy_opener)
    assert calls == []


# --- redirects ------------------------------------------------------------
#
# The guard gained a bearer_token parameter, and with it the whole class of
# bug that urllib's redirect handling creates: it copies Authorization onto
# the target of a 302 with no same-origin test, and issues that request from
# inside OpenerDirector, so the terms gate, the robots check and the throttle
# are skipped on the one request that actually leaves.

from urllib.error import HTTPError                                   # noqa: E402

from asx.ingest.fetch_guard import (                                 # noqa: E402
    MAX_REDIRECTS,
    RedirectRefusedError,
    _NoAutomaticRedirects,
    guarded_opener,
)

_DECLARED = "https://investorpa.com/announcement-pdf/20260820/1.pdf"


def _redirect(to, code=302, url=_DECLARED):
    return HTTPError(url, code, "Found", {"Location": to}, None)


class _Hops:
    """An opener that answers each call from a scripted list."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    def __call__(self, request, timeout=None):
        self.seen.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Ok:
    status = 200

    def __init__(self, body=b"%PDF-1.7", headers=None):
        self._body = body
        self.headers = headers or {"Content-Type": "application/pdf"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_urllib_is_never_allowed_to_follow_a_redirect_itself():
    """The handler returns None so urllib raises HTTPError instead of quietly
    re-issuing the request. If this ever returns a Request again, every other
    protection in this module is bypassed on the final hop."""
    handler = _NoAutomaticRedirects()
    assert handler.redirect_request(
        None, None, 302, "Found", {"Location": "https://elsewhere.example/"},
        "https://elsewhere.example/") is None
    # And the shared opener is built from it, so nothing gets a following one.
    assert any(isinstance(h, _NoAutomaticRedirects)
               for h in guarded_opener().handlers)


def test_a_credentialed_request_follows_no_redirect_even_same_origin():
    """A credential goes only where we decided to send it. Same-origin is not
    an exception: urllib cannot replay a POST body across 302 anyway, so
    'following' would mean re-sending on the endpoint's say-so."""
    hops = _Hops(_redirect("https://investorpa.com/somewhere-else.pdf"))
    with pytest.raises(RedirectRefusedError, match="carries credentials"):
        fetch(_DECLARED, opener=hops, bearer_token="secret")
    assert len(hops.seen) == 1, "it must not have made a second request"


def test_a_redirected_credential_never_reaches_the_target():
    """The point of the whole fix, asserted on the wire rather than inferred."""
    hops = _Hops(_redirect("https://attacker.example/collect"))
    with pytest.raises(RedirectRefusedError):
        fetch(_DECLARED, opener=hops, bearer_token="secret")
    sent_to = [r.full_url for r in hops.seen]
    assert sent_to == [_DECLARED]
    assert not any("attacker.example" in u for u in sent_to)


def test_a_post_follows_no_redirect_because_the_body_would_be_dropped():
    """urllib re-issues a POST as a bodyless GET on 301/302/303. The answer to
    a JSON-RPC search sent as a bodyless GET parses as 'no results' — a silent
    zero, which this platform treats as an alarm, not a result."""
    hops = _Hops(_redirect("https://investorpa.com/mcp2/"))
    with pytest.raises(RedirectRefusedError, match="carries a body"):
        fetch("https://investorpa.com/mcp/", opener=hops,
              post_json={"jsonrpc": "2.0"})


def test_an_uncredentialed_hop_re_enters_the_guard():
    """A plain GET may follow — but the new URL must pass the terms gate. A
    redirect onto an undeclared host is refused for want of a basis, which is
    strictly tighter than before, when urllib followed it unexamined."""
    hops = _Hops(_redirect("https://undeclared.example/thing.pdf"))
    with pytest.raises(ProhibitedSourceError, match="no terms basis"):
        fetch(_DECLARED, opener=hops)


def test_an_uncredentialed_hop_to_a_declared_host_is_followed():
    hops = _Hops(_redirect("https://investorpa.com/announcement-pdf/x/2.pdf"),
                 _Ok())
    result = fetch(_DECLARED, opener=hops)
    assert result.content == b"%PDF-1.7"
    assert result.url.endswith("2.pdf")
    assert len(hops.seen) == 2


def test_a_redirect_chain_is_bounded():
    hops = _Hops(*[_redirect(f"https://investorpa.com/a{i}.pdf")
                   for i in range(MAX_REDIRECTS + 2)])
    with pytest.raises(RedirectRefusedError, match="more than"):
        fetch(_DECLARED, opener=hops)


def test_a_bearer_token_is_refused_over_plaintext():
    with pytest.raises(ProhibitedSourceError, match="not https"):
        fetch("http://investorpa.com/mcp/", bearer_token="secret")


def test_only_a_status_the_caller_declared_comes_back_as_a_result():
    """A caller that means to read a 400 body says so. Everything else still
    raises: a failed fetch handed back as a thin success is how a broken run
    reports green."""
    hops = _Hops(HTTPError(_DECLARED, 400, "Bad", {"Content-Type": "application/json"},
                           __import__("io").BytesIO(b'{"error":"nope"}')))
    result = fetch(_DECLARED, opener=hops, allow_status=frozenset({400}))
    assert result.status == 400 and b"nope" in result.content

    hops = _Hops(HTTPError(_DECLARED, 500, "Boom", {}, __import__("io").BytesIO(b"")))
    with pytest.raises(HTTPError):
        fetch(_DECLARED, opener=hops, allow_status=frozenset({400}))
