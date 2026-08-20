"""Display quotes: what a share trades at now, to sit beside what a director
paid for it.

**This is not a price vendor and must never become one.** ACCESS_DECISION §3
records that there is no price feed and that backtesting is out of scope
because of it, and that is still true. The two things are different:

    A backtest price source has to be survivorship-complete (Invariant 4). It
    must price the companies that later delisted, or every study run over it
    is measuring only the survivors and flattering itself twice over.

    A display quote answers "what is this worth today?" for a name that is
    listed today, on a screen a human reads and acts on. A delisted company
    has no such number, and reporting that it has none is a correct answer
    rather than a gap.

`sources.PriceSource` is the protocol for the first kind, and its docstring
says a source that silently drops delisted names must not implement it. This
module deliberately does not implement it, is not registered as one, and the
backtest harness cannot reach `price_quotes`. A test holds that line, because
wiring a display source into a backtest is a one-line mistake with a silent,
unfalsifiable result.

**Terms basis** (Invariant 11, owner sign-off 20 Aug 2026): recorded in
`fetch_guard.DECLARED_SOURCES`. robots.txt permits `/quote/`; the Terms of
Use carry no automated-access prohibition and permit unmodified, attributed
snippets. Every fetch still goes through `fetch_guard.fetch`, so the polite
rate, honest user-agent and robots check apply here exactly as they do to the
exchange.

**The source says its ASX prices are delayed**, so quotes are stored with the
source's own timestamp and shown as "price as at <date>", never "latest".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.error import HTTPError

from asx.ingest import fetch_guard

SOURCE_NAME = "stockanalysis.com"

# One company, one page, addressed by ticker. Note what this is not: there is
# no search, no listing, no ticker-discovery endpoint. We ask only about codes
# we already hold on a screen, which is the same shape of restraint the ASX
# amendment demands — retrieval of a known thing, never discovery.
QUOTE_URL = "https://stockanalysis.com/quote/asx/{ticker}/"

# A bounded run cannot become a crawl by accident (the same reasoning as
# fetch_guard.MAX_RESTRICTED_FETCHES_PER_RUN). The screens carry ~22 tickers;
# this leaves room to grow without leaving room to sweep the market.
MAX_QUOTES_PER_RUN = 100


class QuoteUnavailable(Exception):
    """The source has no usable quote for this code. Carries the reason so it
    can be recorded on the row rather than dropped."""

    def __init__(self, status: str, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: float
    currency: str | None
    as_at: datetime | None
    as_at_date: date
    as_at_label: str | None
    previous_close: float | None
    market_status: str | None
    delayed: bool
    symbol_returned: str | None
    exchange_returned: str | None
    source_url: str
    source_name: str = SOURCE_NAME


# The page embeds its quote as a JavaScript object literal, not JSON: keys are
# unquoted and numbers may be written `-.76`. Rather than repair it into JSON
# and hope, each field is read individually and a missing one is fatal — a
# quote assembled from a page whose shape has changed is exactly the kind of
# confident-and-wrong number this platform is built to refuse.
_QUOTE_OBJ = re.compile(r"\bquote:\{")
_NUM = r"(-?(?:\d+\.?\d*|\.\d+))"


def _quote_object(page: str) -> str:
    """The text of the `quote:{...}` literal, by balanced-brace scan."""
    m = _QUOTE_OBJ.search(page)
    if not m:
        raise QuoteUnavailable(
            "unparsed", "no quote object on the page (source layout changed?)")
    i = m.end() - 1
    depth = 0
    for j in range(i, min(len(page), i + 20000)):
        if page[j] == "{":
            depth += 1
        elif page[j] == "}":
            depth -= 1
            if depth == 0:
                return page[i:j + 1]
    raise QuoteUnavailable("unparsed", "quote object is not brace-balanced")


def _field(obj: str, key: str, pattern: str) -> str | None:
    m = re.search(rf"\b{key}:{pattern}", obj)
    return m.group(1) if m else None


def parse_quote_page(page: str, ticker: str, url: str) -> Quote:
    """Read one quote out of a fetched page.

    Raises QuoteUnavailable rather than returning a partial quote. Every
    caller records the failure against the row, so a company whose price we
    could not read keeps its place on the screen with the reason attached.
    """
    obj = _quote_object(page)

    # Which security did the source actually answer about? A price site will
    # happily redirect a code it does not recognise, and a price for the wrong
    # company is worse than no price. Checked before anything is believed.
    symbol = _field(page, "symbol", r'"([^"]+)"')
    exchange = _field(page, "exchange_code", r'"([^"]+)"')
    if symbol and symbol.upper() != ticker.upper():
        raise QuoteUnavailable(
            "not_found",
            f"asked for {ticker}, page is for {symbol} — refusing to record "
            f"another company's price")
    if exchange and exchange.upper() != "ASX":
        raise QuoteUnavailable(
            "not_found", f"asked for an ASX code, page is on {exchange}")

    raw_price = _field(obj, "p", _NUM)
    if raw_price is None:
        raise QuoteUnavailable("unparsed", "no price field in the quote object")
    price = float(raw_price)
    if price <= 0:
        raise QuoteUnavailable("unparsed", f"non-positive price {price}")

    # The source's own trade date. Without it there is no honest way to label
    # the column, so its absence disqualifies the quote — an undated number
    # beside dated lodgements is the weakest thing on the page.
    trade_date = _field(obj, "td", r'"(\d{4}-\d{2}-\d{2})"')
    if not trade_date:
        raise QuoteUnavailable("unparsed", "no trade date on the quote")

    epoch_ms = _field(obj, "ts", r"(\d+)")
    as_at = (datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
             if epoch_ms else None)
    prev = _field(obj, "cl", _NUM)
    currency = None
    curr = re.search(r"\bcurr:\{[^}]*\}", page)
    if curr:
        currency = _field(curr.group(0), "price", r'"([A-Z]{3})"')

    return Quote(
        ticker=ticker.upper(),
        price=price,
        currency=currency,
        as_at=as_at,
        as_at_date=date.fromisoformat(trade_date),
        # Verbatim, so a timezone error in our own parsing can be caught
        # against what the source actually said.
        as_at_label=_field(obj, "u", r'"([^"]*)"'),
        previous_close=float(prev) if prev is not None else None,
        market_status=_field(obj, "ms", r'"([^"]*)"'),
        # Always true for this source, which labels its ASX quotes "Delayed
        # Price" and states in its terms that data "may be delayed by 15
        # minutes or more". Recorded per row rather than assumed by the
        # reader, and never softened: overstating freshness is the costly
        # direction, and there is no page marker that would justify it.
        delayed=True,
        symbol_returned=symbol,
        exchange_returned=exchange,
        source_url=url,
    )


class StockAnalysisQuotes:
    """Display-only quote lookup.

    Deliberately NOT an implementation of `sources.PriceSource`: it has no
    `eod_bars` and no `shares_outstanding`, and it cannot price a delisted
    security. Those absences are the point, not an unfinished job.
    """

    source_name = SOURCE_NAME

    def __init__(self, opener=None):
        self._opener = opener
        self._fetched = 0

    def url_for(self, ticker: str) -> str:
        return QUOTE_URL.format(ticker=ticker.upper())

    def quote(self, ticker: str) -> Quote:
        if self._fetched >= MAX_QUOTES_PER_RUN:
            raise QuoteUnavailable(
                "fetch_error",
                f"per-run ceiling of {MAX_QUOTES_PER_RUN} quotes reached")
        url = self.url_for(ticker)
        self._fetched += 1
        try:
            # Through the guard, never around it: robots, rate and user-agent
            # are enforced there for every source alike.
            result = fetch_guard.fetch(url, opener=self._opener)
        except fetch_guard.ProhibitedSourceError:
            # A refusal by the guard is a decision about what we are allowed to
            # do, not a transport failure. It stops the run rather than being
            # filed as a blank cell, because filing it would turn "we are not
            # permitted to fetch this" into "this company has no price".
            raise
        except fetch_guard.RobotsDisallowedError as exc:
            # Robots is per-path, so this is recorded against the row rather
            # than stopping the run — another code on the same host may well be
            # allowed. Recorded verbatim: when the guard was reading robots.txt
            # under the wrong user-agent, this detail is what made the cause
            # visible instead of a silent empty column.
            raise QuoteUnavailable("fetch_error", f"RobotsDisallowedError: {exc}") from exc
        except HTTPError as exc:
            # The status code, not a substring of the message: a 404 means the
            # source has no such code (delisted, or never listed), which is a
            # different fact from the network failing.
            status = "not_found" if exc.code == 404 else "fetch_error"
            raise QuoteUnavailable(status, f"HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:  # transport, timeout, decode
            raise QuoteUnavailable(
                "fetch_error", f"{type(exc).__name__}: {exc}") from exc
        page = result.content.decode("utf-8", errors="replace")
        return parse_quote_page(page, ticker, url)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def screen_entities(conn) -> list[dict]:
    """Every entity currently on a director screen, with the code to ask about.

    LEFT JOIN, deliberately. An entity whose listing has closed has no open
    ticker, and dropping it here would quietly remove a delisted company from
    the screen — the exact failure Invariant 4 exists to prevent. It comes
    back with `ticker = None` and is recorded as unquotable, keeping its row.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.entity_id, l.ticker, n.name AS entity
                 FROM (SELECT entity_id FROM signal_cluster_buys
                       UNION
                       SELECT entity_id FROM signal_conviction_buys) s
                 LEFT JOIN listings l
                        ON l.entity_id = s.entity_id AND l.valid_to IS NULL
                       AND l.exchange = 'ASX'
                 LEFT JOIN entity_names n
                        ON n.entity_id = s.entity_id AND n.valid_to IS NULL
                ORDER BY l.ticker NULLS LAST""")
        return cur.fetchall()


def _record(conn, entity_id: int, ticker: str, *, quote: Quote | None,
            status: str, detail: str | None, url: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO price_quotes
                 (entity_id, ticker_used, symbol_returned, exchange_returned,
                  price, currency, previous_close, as_at, as_at_date,
                  as_at_label, delayed, market_status, source_name, source_url,
                  status, status_detail)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (entity_id, ticker,
             quote.symbol_returned if quote else None,
             quote.exchange_returned if quote else None,
             quote.price if quote else None,
             quote.currency if quote else None,
             quote.previous_close if quote else None,
             quote.as_at if quote else None,
             quote.as_at_date if quote else None,
             quote.as_at_label if quote else None,
             quote.delayed if quote else True,
             quote.market_status if quote else None,
             SOURCE_NAME, url, status, detail))
    conn.commit()


def refresh_screen_quotes(conn, source: "StockAnalysisQuotes | None" = None,
                          progress=None) -> dict:
    """Fetch a display quote for every entity on the screens; record each
    outcome, success or not.

    Returns a tally. Nothing is skipped silently: a company we could not price
    gets a row saying why, because a blank cell with no reason is
    indistinguishable from a company nobody looked at.
    """
    source = source or StockAnalysisQuotes()
    tally = {"ok": 0, "not_found": 0, "unparsed": 0, "fetch_error": 0,
             "no_listing": 0}

    for row in screen_entities(conn):
        entity_id, ticker = row["entity_id"], row["ticker"]
        if not ticker:
            # No open ASX listing: delisted, or never resolved to one. Recorded
            # against the entity so the screen can say so.
            _record(conn, entity_id, "", quote=None, status="not_found",
                    detail="no open ASX listing for this entity — delisted, or "
                           "the ticker alias is unknown; a quote cannot be "
                           "addressed without one",
                    url="")
            tally["no_listing"] += 1
            if progress:
                progress(None, "no_listing")
            continue
        try:
            quote = source.quote(ticker)
        except QuoteUnavailable as exc:
            _record(conn, entity_id, ticker, quote=None, status=exc.status,
                    detail=exc.detail, url=source.url_for(ticker))
            tally[exc.status] = tally.get(exc.status, 0) + 1
            if progress:
                progress(ticker, exc.status)
            continue
        _record(conn, entity_id, ticker, quote=quote, status="ok",
                detail=None, url=quote.source_url)
        tally["ok"] += 1
        if progress:
            progress(ticker, "ok")
    return tally


# The newest quote per entity. Written as a plain query rather than a view so
# a reader of the screen code can see exactly which row won and why.
LATEST_QUOTE_SQL = """
SELECT DISTINCT ON (entity_id)
       entity_id, price, currency, as_at_date, as_at_label, delayed,
       source_name, source_url, retrieved_at, status, status_detail
  FROM price_quotes
 ORDER BY entity_id, retrieved_at DESC
"""


def latest_quotes(conn) -> dict[int, dict]:
    with conn.cursor() as cur:
        cur.execute(LATEST_QUOTE_SQL)
        return {r["entity_id"]: r for r in cur.fetchall()}
