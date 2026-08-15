"""Gold-set regression (SPEC §6, Appendix C): a parser change that reduces
gold-set accuracy does not merge. CI runs this on every change.

The synthetic classification gold set pins the rules' behaviour today; the
100-document hand-labelled set arrives with the access decision and will be
picked up from fixtures/app3y/*.labels.json automatically.
"""

import json
from decimal import Decimal
from pathlib import Path

from asx.canonical.director_trades import classify_trade

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load_classification_gold():
    path = FIXTURES / "app3y" / "classification_gold.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_classification_gold_set_present_and_nontrivial():
    cases = _load_classification_gold()
    assert len(cases) >= 25
    labels = {c["label"] for c in cases}
    # The set must exercise the signal class, its confusable neighbours, and
    # the unknown bucket, or precision on onmkt_buy_cash is unmeasured.
    assert {"onmkt_buy_cash", "onmkt_sell", "offmkt_transfer", "unknown"} <= labels


def test_classification_gold_set_exact():
    failures = []
    for case in _load_classification_gold():
        got = classify_trade(
            case["consideration"],
            Decimal(str(case["qty_acquired"])) if case["qty_acquired"] is not None else None,
            Decimal(str(case["qty_disposed"])) if case["qty_disposed"] is not None else None,
        )
        if got != case["label"]:
            failures.append(f"{case['consideration']!r}: got {got}, want {case['label']}")
    assert not failures, "\n".join(failures)


def test_onmkt_buy_cash_precision_is_perfect_on_gold():
    """Phase 1's acceptance hinges on precision of this one class (SPEC §7):
    nothing labelled otherwise may classify as onmkt_buy_cash."""
    for case in _load_classification_gold():
        got = classify_trade(
            case["consideration"],
            Decimal(str(case["qty_acquired"])) if case["qty_acquired"] is not None else None,
            Decimal(str(case["qty_disposed"])) if case["qty_disposed"] is not None else None,
        )
        if got == "onmkt_buy_cash":
            assert case["label"] == "onmkt_buy_cash", (
                f"false positive on the signal class: {case['consideration']!r}"
            )
