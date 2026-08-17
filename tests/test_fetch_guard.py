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
    assert_fetchable(url)  # does not raise


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
