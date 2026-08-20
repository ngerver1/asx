"""What did the director hold, in the class that actually changed?

The reader's corroboration is the arithmetic the issuer printed —
held after = held before + acquired - disposed. A holdings figure that
satisfies it has been checked against the company's own numbers; one that
cannot be checked is reported as unknown and goes to review.

Two failures this pins down, both measured on the corpus rather than imagined:

  * A third of lodgements report a change in options or performance rights and
    state the ordinary holding unchanged beside it. Reading the ordinary
    parcel and pairing it with an options movement produces arithmetic like
    "1,412,912 - 6,000,000 = -4,587,088", so the reader nulled the holdings
    entirely — leaving the notice permanently unverifiable. The holding to
    read is the one in the class that moved.

  * A director's interest routinely spans several parcels of ONE class —
    direct and indirect, or two family trusts. Their relevant interest is the
    sum. Summing parcels of the SAME class is not the category-blending
    Invariant 8 prohibits; that is about security classes, and this is about
    holder vehicles.

Both are accepted ONLY where the printed movement confirms them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asx.parse import app3y_rules as rules

GOLD = [json.loads(line) for line in
        (Path(__file__).parent.parent / "fixtures" / "app3y" /
         "holdings_gold.jsonl").read_text().splitlines() if line.strip()]

POSITIVE = [c for c in GOLD if c["expect"]]
NEGATIVE = [c for c in GOLD if not c["expect"]]


def _read(case: dict):
    """Exactly what the notice reader does, in the same order.

    The class cell is reduced by `select_by_class` FIRST — a form enumerating
    "(i) Ordinary Shares (ii) Unlisted Options" against "(i) 500,000 (ii)
    250,000" resolves to one class and one quantity before holdings are read.
    Passing the raw cell instead tests a pipeline that does not exist, and
    reports a parser bug where there is only a harness bug.
    """
    security_class, (acquired, disposed, _) = rules.select_by_class(
        case["security_class"], case["qty_acquired"],
        case["qty_disposed"], None)
    return rules.holdings_for_changed_class(
        case["held_before"], case["held_after"],
        security_class=security_class,
        acquired=acquired, disposed=disposed,
        interest=case["interest_nature"],
    )


def test_the_gold_set_is_present_and_has_both_kinds():
    assert len(POSITIVE) >= 8, "too few worked cases to be a regression test"
    assert NEGATIVE, "a gold set with no must-stay-unknown cases tests only optimism"


@pytest.mark.parametrize("case", POSITIVE, ids=[str(c["doc_id"]) for c in POSITIVE])
def test_the_changed_class_holding_is_read(case):
    got = _read(case)
    assert got is not None, f"doc {case['doc_id']}: {case['note']}"
    klass, before, after = got
    assert klass == case["expect"]["klass"], case["note"]
    assert before == case["expect"]["before"], case["note"]
    assert after == case["expect"]["after"], case["note"]


@pytest.mark.parametrize("case", POSITIVE, ids=[str(c["doc_id"]) for c in POSITIVE])
def test_every_answer_satisfies_the_printed_arithmetic(case):
    """The property that makes this safe.

    Nothing here is accepted on the reader's say-so. Whatever it returns must
    reconcile against the movement the issuer printed, or it must return
    nothing — so a wrong reading cannot become a validated row.
    """
    klass, before, after = _read(case)
    ordinary = klass == "ordinary"
    _, (acquired, disposed, _c) = rules.select_by_class(
        case["security_class"], case["qty_acquired"], case["qty_disposed"], None)
    got = rules.quantity_of_class(acquired, ordinary=ordinary) or 0
    lost = rules.quantity_of_class(disposed, ordinary=ordinary) or 0
    assert after - before == got - lost, (
        f"doc {case['doc_id']}: {before} + {got} - {lost} != {after}")


@pytest.mark.parametrize("case", NEGATIVE, ids=[str(c["doc_id"]) for c in NEGATIVE])
def test_an_unattributable_holding_stays_unknown(case):
    """Invariant 8. A number the form does not settle is not the first
    plausible figure on the page."""
    assert _read(case) is None, (
        f"doc {case['doc_id']} produced a holding it cannot justify: {case['note']}")


def test_a_notice_with_no_printed_movement_is_not_guessed():
    """With nothing to reconcile against there is no corroboration, so there
    is no answer — however few parcels the cells happen to contain."""
    assert rules.holdings_for_changed_class(
        "Direct 1,000,000 Ordinary Shares", "Direct 1,000,000 Ordinary Shares",
        security_class="Ordinary Shares", acquired=None, disposed=None) is None


def test_parcels_of_different_classes_are_never_summed():
    """Invariant 8 proper: shares and options are not one holding.

    The sum rule applies WITHIN a class. A cell listing 1,000,000 ordinary and
    500,000 options against an acquisition of 1,500,000 must not answer
    1,500,000 by adding the two.
    """
    assert rules.holdings_for_changed_class(
        "1,000,000 Ordinary Shares 500,000 Unlisted Options",
        "2,500,000 Ordinary Shares 500,000 Unlisted Options",
        security_class="Ordinary Shares",
        acquired="1,500,000", disposed=None) == ("ordinary", 1000000, 2500000)


def test_reading_an_options_holding_does_not_switch_the_quantity_class():
    """A notice whose change is in options must still report the OPTIONS
    quantity, not the ordinary one.

    Two questions live close together in the notice reader: which class's
    numbers to take out of the acquired/disposed cells, and whether the
    before/after pair may be reported at all. Answering the second by
    overwriting the first reads 78,960 ordinary shares off a form that
    acquired 250,000 performance rights — a wrong number that reconciles
    against nothing and would have shipped silently.
    """
    from asx.parse.app3y import App3YParser

    text = (
        "Appendix 3Y\nChange of Director's Interest Notice\n"
        "Name of entity: Test Co Ltd\nABN: 12 345 678 901\n"
        "Name of Director A Director\n"
        "Date of change 12 August 2026\n"
        "Direct or indirect interest Direct\n"
        "No. of securities held prior to change "
        "1,000,000 Ordinary Shares 250,000 Performance Rights\n"
        "Class Performance Rights\n"
        "Number acquired 500,000\n"
        "Number disposed Nil\n"
        "Value/Consideration Nil\n"
        "No. of securities held after change "
        "1,000,000 Ordinary Shares 750,000 Performance Rights\n"
        "Nature of change Issue of performance rights\n"
    )
    payload = App3YParser().read_rules(text.encode())
    sec = payload["notices"][0]["securities"][0]
    assert sec["qty_acquired"] == 500000, "took the ordinary quantity, not the rights"
    assert sec["held_before"] == 250000
    assert sec["held_after"] == 750000


def test_a_nil_prior_holding_is_zero_not_missing():
    """'Nil' is the issuer stating a holding, not failing to state one.

    Reading it as zero is reading the word on the page. Treating it as absent
    left notices unverifiable that the form settles completely: nothing held,
    500,000 acquired, 500,000 held after — arithmetic that reconciles exactly.
    """
    assert rules.holdings_for_changed_class(
        "Nil", "500,000 Fully paid ordinary shares",
        security_class="Ordinary Shares",
        acquired="500,000", disposed=None) == ("ordinary", 0, 500000)


def test_nil_beside_a_number_is_not_the_whole_cell():
    """'Direct Nil Indirect 5,901,982' states TWO parcels, one of them empty.

    Only a cell that is nothing but 'Nil' says the whole holding is zero.
    Reading the word wherever it appears would zero out a director's real
    indirect holding because their direct parcel happens to be empty.
    """
    got = rules.holdings_for_changed_class(
        "Direct Nil Indirect 1,000,000 Ordinary Shares",
        "Direct Nil Indirect 1,500,000 Ordinary Shares",
        security_class="Ordinary Shares",
        acquired="500,000", disposed=None)
    assert got == ("ordinary", 1000000, 1500000)
