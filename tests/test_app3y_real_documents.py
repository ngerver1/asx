"""Classification regression against the first four REAL Appendix 3Y/3Z forms.

Field extraction needs the LLM and is measured separately (criterion 1.1).
What is pinned here is the part that decides the product: whether a line of a
real form becomes a buy, a sell, or an admission that we do not know.

Every case below is text copied verbatim out of a lodged document.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from asx.canonical.director_trades import classify_trade

GOLD = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "app3y" / "documents" / "gold.json")
    .read_text())["cases"]


def test_the_gold_documents_are_present():
    """The labels are worthless without the documents they describe."""
    docs = Path(__file__).parent.parent / "fixtures" / "app3y" / "documents"
    for case in GOLD:
        assert (docs / case["file"]).exists(), case["file"]


def test_a_sale_buried_behind_a_vesting_is_never_called_a_vesting():
    """Catalyst Metals (CYL, 6A1339259). 'Number disposed' enumerates a
    rights conversion AND a 1,000,000-share on-market sale for $6,410,050.
    Classifying by first match returns 'vesting_incentive' and buries the
    sale — the exact opposite of the signal this platform looks for."""
    both = "1.  Conversion of vested Performance Rights\n2. On market trades"
    assert classify_trade(both, Decimal("534188"), Decimal("1106838"),
                          Decimal("6410050")) == "unknown"

    # Split, each half classifies correctly.
    assert classify_trade("1. Conversion of vested Performance Rights",
                          Decimal("534188"), None, Decimal("0")) == "vesting_incentive"
    assert classify_trade("2. On market trades",
                          None, Decimal("1000000"), Decimal("6410050")) == "onmkt_sell"


def test_a_two_directional_line_is_never_given_one_direction():
    """Adrad (AHL, 2A1690463): 'On market disposal & on market acquisition',
    13,107 acquired and 13,107 disposed. Net zero, four real trades."""
    assert classify_trade("On market disposal & on market acquisition",
                          Decimal("13107"), Decimal("13107"),
                          Decimal("15925")) == "unknown"


def test_the_individual_adrad_transactions_classify():
    assert classify_trade("On-market acquisition, $1.215 per share",
                          Decimal("13107"), None, Decimal("15925")) == "onmkt_buy_cash"
    assert classify_trade("on-market sale, $1.2104 per share",
                          None, Decimal("5877"), Decimal("7113.52")) == "onmkt_sell"


@pytest.mark.parametrize("text", [
    "On market purchase at $1.215 per share",     # a price, not an enumeration
    "Acquired 1,000,000 shares on market for $2,500,000",
    "On-market purchase under the share purchase plan",
])
def test_numbers_in_the_text_are_not_mistaken_for_an_enumeration(text):
    """The enumeration guard must fire on list markers, never on a price or a
    share count — otherwise it would turn ordinary buys into 'unknown' and
    quietly empty the signal."""
    assert classify_trade(text, Decimal("1000"), None, Decimal("2500")) != "unknown"


def test_every_gold_transaction_classifies_as_labelled():
    """Walk the hand-labelled per-transaction classifications."""
    checked = 0
    for case in GOLD:
        for txn in case.get("transactions", []):
            expected = txn.get("classification")
            if not expected or "note" not in txn and "price_aud" not in txn:
                continue
            note = txn.get("note", "")
            text = note or ("on-market acquisition" if txn.get("acquired")
                            else "on-market disposal")
            got = classify_trade(
                text,
                Decimal(str(txn["acquired"])) if txn.get("acquired") else None,
                Decimal(str(txn["disposed"])) if txn.get("disposed") else None,
                Decimal(str(txn.get("consideration_aud",
                                    (txn.get("price_aud", 0) or 0)
                                    * (txn.get("acquired") or txn.get("disposed") or 0)))),
            )
            assert got == expected, f"{case['ticker']} {txn['seq']}: {text!r} -> {got}"
            checked += 1
    assert checked >= 6


# --- one PDF, several directors ------------------------------------------

def _text(name):
    import pypdf
    path = (Path(__file__).parent.parent / "fixtures" / "app3y" / "documents" / name)
    if not path.exists():
        pytest.skip(f"{name} not present")
    return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(str(path)).pages)


def test_a_lodgement_with_several_directors_yields_several_forms():
    """Ten percent of real lodgements carry more than one complete Appendix
    3Y — up to four directors in one PDF. Reading only the first drops the
    rest, and a board filing together is precisely the coordinated event the
    cluster-buy signal exists to find: taking one director would turn the
    strongest available signal into the weakest."""
    from asx.parse.app3y_rules import extract_all

    forms = extract_all(_text("327725.pdf"))
    assert len(forms) == 4
    directors = [f.get("director_name") for f in forms]
    assert directors == ["Patrick Burke", "Oliver Kiddie", "Justin Werner",
                         "Cameron Peacock"]
    assert all(f.get("entity_name") and "FMR" in f.get("entity_name")
               for f in forms)


def test_two_director_lodgement_splits_cleanly():
    from asx.parse.app3y_rules import extract_all

    forms = extract_all(_text("328630.pdf"))
    assert [f.get("director_name") for f in forms] == ["Alex Rovira", "Andrew Rich"]


def test_a_single_form_still_yields_exactly_one():
    from asx.parse.app3y_rules import extract_all

    forms = extract_all(_text("6A1339259.pdf"))
    assert len(forms) == 1
    assert forms[0].get("director_name") == "James Champion de Crespigny"
