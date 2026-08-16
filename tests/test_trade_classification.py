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


def test_price_reference_wording_is_not_an_on_market_trade():
    # "based on market value" is a PRICE REFERENCE on off-market related-party
    # transfers, not an on-market execution. Coercing it into the signal class
    # is exactly the substantive default Invariant 8 prohibits.
    assert classify_trade(
        "Transfer to Smith Family Trust at a price based on market value of $0.50 per share",
        D(100000), None,
    ) == "unknown"
    assert classify_trade(
        "Disposal at a price based on market value to a related entity", None, D(80000)
    ) == "unknown"


def test_nil_consideration_blocks_the_cash_buy_class():
    # A nil-consideration shuffle between the director's own vehicles is not a
    # trade at all (SPEC §7).
    assert classify_trade(
        "On-market transfer of 500,000 shares, nil consideration", D(500000), None
    ) == "unknown"


def test_word_anchored_rules_do_not_capture_substrings():
    # 'investment' / 'divestment' / 'replacement' must not match the vesting
    # and placement rules.
    assert classify_trade(
        "On-market purchase by the director's investment company - $18,000", D(60000), None
    ) == "onmkt_buy_cash"
    assert classify_trade("Divestment of shares on market - $75,000", None, D(250000)) == "onmkt_sell"
    assert classify_trade("Issue of replacement options following expiry", D(250000), None) == "unknown"
    assert classify_trade("Reinvestment of dividends under the plan", D(512), None) == "drp"


def test_consideration_amount_counts_as_cash_evidence():
    # The 3Y nature box often reads just "On market purchase" with the dollar
    # figure in the separate value-of-consideration box.
    assert classify_trade("On market purchase", D(1000), None, D(2500)) == "onmkt_buy_cash"
    # Without any cash evidence at all it stays unknown.
    assert classify_trade("On market purchase", D(1000), None) == "unknown"


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
