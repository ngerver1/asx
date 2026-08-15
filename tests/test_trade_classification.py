from decimal import Decimal

from asx.canonical.director_trades import TradeRow, classify_trade, derive_price_per_unit

D = Decimal


def test_onmarket_cash_buy():
    assert classify_trade("On-market purchase, $25,000", D(10000), None) == "onmkt_buy_cash"
    assert classify_trade("On market trade at $0.25 per share", D(100000), None) == "onmkt_buy_cash"


def test_onmarket_sell():
    assert classify_trade("On-market sale", None, D(50000)) == "onmkt_sell"


def test_off_market_never_hits_onmarket_rules():
    assert classify_trade("Off-market transfer for nil consideration", D(1000), None) == "offmkt_transfer"
    assert classify_trade("Off market transfer between related entities", None, D(1000)) == "offmkt_transfer"


def test_exercise_of_options():
    assert classify_trade("Exercise of options at $0.20", D(500000), None) == "exercise"
    assert classify_trade("Conversion of convertible notes", D(100), None) == "exercise"


def test_participations():
    assert classify_trade("Participation in placement", D(200000), None) == "placement_participation"
    assert classify_trade("Share Purchase Plan allocation", D(10000), None) == "spp_participation"
    assert classify_trade("Participation in non-renounceable entitlement offer", D(5000), None) == "rights_participation"
    assert classify_trade("Dividend Reinvestment Plan", D(432), None) == "drp"


def test_vesting_and_incentives():
    assert classify_trade("Vesting of performance rights", D(750000), None) == "vesting_incentive"
    assert classify_trade("Issue under employee share plan", D(1000), None) == "vesting_incentive"


def test_margin_and_buyback():
    assert classify_trade("Forced sale by margin lender", None, D(90000)) == "margin_or_forced"
    assert classify_trade("Shares sold into on-market buy-back", None, D(1000)) == "buyback_into"


def test_ambiguous_is_unknown_never_defaulted():
    # Invariant 8: the on-market cash-buy signal only works because ambiguity
    # never leaks into it.
    assert classify_trade("Nil consideration", D(100000), None) == "unknown"
    assert classify_trade(None, D(1000), None) == "unknown"
    assert classify_trade("", None, D(1000)) == "unknown"
    # On-market both ways in one line: direction ambiguous.
    assert classify_trade("On-market transactions", D(50), D(50)) == "unknown"
    # On-market acquisition with no cash hint at all.
    assert classify_trade("On-market", D(50), None) == "unknown"


def _row(**kw):
    defaults = dict(
        entity_id=1, person_name_raw="A", doc_id=1, event_date=None,
        knowable_at=None, security_class="ORD",
    )
    defaults.update(kw)
    return TradeRow(**defaults)


def test_price_per_unit_only_when_safe():
    assert derive_price_per_unit(
        _row(consideration_aud=D(25000), qty_acquired=D(100000))
    ) == D("0.25")
    # Both sides traded: unsafe.
    assert derive_price_per_unit(
        _row(consideration_aud=D(25000), qty_acquired=D(100), qty_disposed=D(50))
    ) is None
    # No consideration figure: nothing to derive.
    assert derive_price_per_unit(_row(qty_acquired=D(100))) is None
