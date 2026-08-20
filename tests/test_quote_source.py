"""Display quotes are allowed to be approximate about the market. They are not
allowed to be approximate about WHOSE price it is, WHEN it was struck, or
where it came from — and they are never allowed to become a backtest input.

These tests hold those four lines.
"""

import inspect
from datetime import date
from pathlib import Path

import pytest

from asx.ingest import fetch_guard, quote_source
from asx.ingest.quote_source import (
    QuoteUnavailable,
    StockAnalysisQuotes,
    parse_quote_page,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "quotes" / \
    "stockanalysis_asx_tne.html"
URL = "https://stockanalysis.com/quote/asx/TNE/"


@pytest.fixture(scope="module")
def page() -> str:
    return FIXTURE.read_text(encoding="utf-8", errors="replace")


# --- reading the page ------------------------------------------------------

def test_parses_the_real_page(page):
    q = parse_quote_page(page, "TNE", URL)
    assert q.price == 32.46
    assert q.currency == "AUD"
    assert q.as_at_date == date(2026, 8, 20)
    assert q.previous_close == 32.71
    assert q.symbol_returned == "TNE"
    assert q.exchange_returned == "ASX"
    assert q.source_url == URL


def test_keeps_the_sources_own_wording_for_the_timestamp(page):
    # Kept verbatim so a timezone bug in our own parsing is catchable against
    # what the source actually said.
    q = parse_quote_page(page, "TNE", URL)
    assert q.as_at_label == "Aug 20, 2026, 4:10 PM AEST"
    assert q.as_at.hour == 6  # 16:10 AEST is 06:10 UTC


def test_quote_is_always_marked_delayed(page):
    # The source labels ASX prices "Delayed Price" and its terms say data may
    # be 15+ minutes old. Nothing may present it as live.
    assert parse_quote_page(page, "TNE", URL).delayed is True


# --- refusing to be confidently wrong --------------------------------------

def test_refuses_a_page_about_a_different_company(page):
    # A price site will redirect an unrecognised code. Another company's price
    # in this row is worse than an empty cell, so it is refused outright.
    with pytest.raises(QuoteUnavailable) as exc:
        parse_quote_page(page, "XYZ", URL)
    assert exc.value.status == "not_found"
    assert "TNE" in exc.value.detail


def test_refuses_a_quote_with_no_trade_date(page):
    # An undated number beside figures traced to specific lodgements is the
    # weakest thing on the page, so it is not recorded at all.
    broken = page.replace('td:"2026-08-20"', 'xx:"2026-08-20"')
    with pytest.raises(QuoteUnavailable) as exc:
        parse_quote_page(broken, "TNE", URL)
    assert exc.value.status == "unparsed"


def test_refuses_when_the_page_shape_is_gone(page):
    broken = page.replace("quote:{", "notaquote:{")
    with pytest.raises(QuoteUnavailable) as exc:
        parse_quote_page(broken, "TNE", URL)
    assert exc.value.status == "unparsed"


def test_refuses_a_non_positive_price(page):
    broken = page.replace("p:32.46,", "p:0,")
    with pytest.raises(QuoteUnavailable):
        parse_quote_page(broken, "TNE", URL)


# --- the line that must not be crossed -------------------------------------

def test_is_not_a_price_source():
    """A display quote must never satisfy the backtest price protocol.

    `sources.PriceSource` requires survivorship-complete EOD bars (Invariant
    4). This source cannot price a delisted security at all, so it must not
    carry the protocol's methods — a backtest silently fed on surviving
    companies only would flatter every result, and nothing downstream could
    detect it.
    """
    assert not hasattr(StockAnalysisQuotes, "eod_bars")
    assert not hasattr(StockAnalysisQuotes, "shares_outstanding")


def test_the_backtest_harness_never_learns_about_quotes():
    harness = Path(inspect.getfile(
        __import__("asx.backtest.harness", fromlist=["x"]))).read_text()
    assert "price_quotes" not in harness
    assert "quote_source" not in harness


def test_backtesting_is_still_unavailable():
    # Adding a display quote must not accidentally open the gate that
    # ACCESS_DECISION §3 closes.
    from asx.backtest.harness import BacktestUnavailableError, require_price_source
    with pytest.raises(BacktestUnavailableError):
        require_price_source()


# --- the terms basis -------------------------------------------------------

def test_source_is_declared_with_a_recorded_basis():
    terms = fetch_guard.DECLARED_SOURCES["stockanalysis.com"]
    assert "sign-off" in terms.basis
    assert "robots.txt" in terms.basis
    # Declared for display only; it must not be marked as a document host.
    assert terms.targeted_only is False


def test_quote_urls_pass_the_guard_but_discovery_never_does():
    fetch_guard.assert_fetchable("https://stockanalysis.com/quote/asx/TNE/")
    # The screens address companies they already hold. Searching the site for
    # companies is discovery, and no declaration unlocks it.
    for url in ["https://stockanalysis.com/search/?q=lithium",
                "https://stockanalysis.com/stocks/screener/",
                "https://stockanalysis.com/list/biggest-companies/"]:
        with pytest.raises(fetch_guard.ProhibitedSourceError):
            fetch_guard.assert_fetchable(url)


def test_screening_ore_is_not_screening_the_market():
    """The discovery guard must read paths, not keywords.

    This universe is mining explorers, where "screening" is ore processing. A
    real announcement about screening results is a document, and refusing it
    as if it were a stock screener would block the platform's actual subject
    matter.
    """
    assert fetch_guard.is_discovery_url("https://x.com/stocks/screener/")
    assert not fetch_guard.is_discovery_url(
        "https://miner.com.au/asx/ore-screening-results.pdf")
    assert not fetch_guard.is_discovery_url(
        "https://miner.com.au/announcements/screened-assay.pdf")


def test_a_quote_fetch_goes_through_the_guard(monkeypatch):
    """The source must not hold its own opener bypassing fetch_guard."""
    seen = {}

    def fake_fetch(url, **kwargs):
        seen["url"] = url
        raise RuntimeError("stop here")

    monkeypatch.setattr(quote_source.fetch_guard, "fetch", fake_fetch)
    with pytest.raises(QuoteUnavailable):
        StockAnalysisQuotes().quote("TNE")
    assert seen["url"] == URL


def test_run_is_bounded():
    # A bounded run cannot become a crawl by accident.
    source = StockAnalysisQuotes()
    source._fetched = quote_source.MAX_QUOTES_PER_RUN
    with pytest.raises(QuoteUnavailable) as exc:
        source.quote("TNE")
    assert exc.value.status == "fetch_error"


def test_a_404_is_not_found_not_a_network_error(monkeypatch):
    """A code the source does not carry (delisted, or never listed) is a fact
    about the security, not about the network. The two are recorded
    differently because they mean different things on the screen."""
    from urllib.error import HTTPError

    def gone(url, **kwargs):
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(quote_source.fetch_guard, "fetch", gone)
    with pytest.raises(QuoteUnavailable) as exc:
        StockAnalysisQuotes().quote("GONE")
    assert exc.value.status == "not_found"


def test_a_server_error_is_a_fetch_error(monkeypatch):
    from urllib.error import HTTPError

    def broken(url, **kwargs):
        raise HTTPError(url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(quote_source.fetch_guard, "fetch", broken)
    with pytest.raises(QuoteUnavailable) as exc:
        StockAnalysisQuotes().quote("TNE")
    assert exc.value.status == "fetch_error"


def test_a_guard_refusal_stops_the_run_rather_than_blanking_a_cell(monkeypatch):
    """Filing "we are not permitted to fetch this" as an empty price would
    turn a governance decision into a claim about the company."""
    def refuse(url, **kwargs):
        raise fetch_guard.ProhibitedSourceError("not declared")

    monkeypatch.setattr(quote_source.fetch_guard, "fetch", refuse)
    with pytest.raises(fetch_guard.ProhibitedSourceError):
        StockAnalysisQuotes().quote("TNE")


def test_a_robots_refusal_is_recorded_against_the_row(monkeypatch):
    """Robots is per-path, so another code on the same host may be allowed.
    The reason is kept verbatim — when the guard was reading robots.txt under
    the wrong user-agent, this detail is what made the cause visible."""
    def disallow(url, **kwargs):
        raise fetch_guard.RobotsDisallowedError(f"robots.txt disallows {url}")

    monkeypatch.setattr(quote_source.fetch_guard, "fetch", disallow)
    with pytest.raises(QuoteUnavailable) as exc:
        StockAnalysisQuotes().quote("TNE")
    assert exc.value.status == "fetch_error"
    assert "robots.txt" in exc.value.detail
