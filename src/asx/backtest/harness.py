"""Backtest harness — deliberately inert (ACCESS_DECISION §3, amendment 5).

Invariant 10 requires point-in-time, after-cost, after-tax performance
figures, and Invariant 4 requires that delisted companies be present. Neither
is satisfiable without a survivorship-complete price source, and no price
vendor is subscribed under the Tier 0 access decision.

So this module refuses to run rather than producing a number that would look
like a result. A gross-return backtest over surviving companies only is not a
weaker answer than the real thing — it is a systematically flattering one,
and the failure mode Invariant 10 exists to prevent is precisely believing it.

When a vendor is configured (see the review triggers in ACCESS_DECISION §5),
implement PriceSource, register it here, and the guard opens.
"""

from __future__ import annotations

import os

from asx.ingest.sources import PriceSource

DECISION_REF = "docs/ACCESS_DECISION.md §3 (price vendor: none, deferred)"


class BacktestUnavailableError(RuntimeError):
    """Raised on any attempt to backtest without a survivorship-complete
    price source."""


_registered_source: PriceSource | None = None


def register_price_source(source: PriceSource) -> None:
    """Register a survivorship-complete EOD price source.

    The caller asserts the source includes delisted securities. A source that
    silently drops them violates Invariant 4 at the root, and no code here can
    detect that on its behalf — which is why the access decision requires a
    paid vendor rather than a free endpoint.
    """
    global _registered_source
    _registered_source = source


def price_source_available() -> bool:
    return _registered_source is not None or bool(os.environ.get("ASX_PRICE_SOURCE"))


def require_price_source() -> PriceSource:
    if _registered_source is None:
        raise BacktestUnavailableError(
            "Backtesting is out of scope under the current access decision: no "
            "survivorship-complete price source is configured, so Invariant 10 "
            "(point-in-time, after-cost, after-tax) and Invariant 4 (delisted "
            "companies present) cannot be satisfied. A gross-return backtest "
            "over surviving companies only would flatter every result. "
            f"See {DECISION_REF}. Register a vendor with "
            "register_price_source() to enable."
        )
    return _registered_source


def run_event_study(*_args, **_kwargs):
    """Cumulative abnormal returns around knowable_at (SPEC §12). Gated."""
    require_price_source()
    raise NotImplementedError(
        "Event-study implementation is deferred until a price vendor is "
        "configured; the guard above is the operative behaviour today."
    )


def run_portfolio_simulation(*_args, **_kwargs):
    """Portfolio simulation with costs and the pluggable tax regime. Gated."""
    require_price_source()
    raise NotImplementedError(
        "Portfolio simulation is deferred until a price vendor is configured; "
        "the guard above is the operative behaviour today."
    )
