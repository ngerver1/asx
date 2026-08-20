"""The rules reader wired into the parser: payloads, not just readings.

Until this path existed the platform had a reader that read 209 real forms
and nothing that used it — director_trades held zero rows. These tests pin
the join: what the reader sees becomes what the canonical tables record, and
what it cannot attribute becomes a review item instead of a number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from asx.parse import app3y_rules as rules
from asx.parse.app3y import App3YParser
from asx.parse.rules_extractor import RulesExtractor

DOCS = Path(__file__).parent.parent / "fixtures" / "app3y" / "documents"


def _payload(name: str) -> dict:
    path = DOCS / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    return App3YParser().read_rules(path.read_bytes())


def _only(payload: dict) -> dict:
    assert len(payload["notices"]) == 1
    return payload["notices"][0]["securities"][0]


# --- one lodgement, several directors ---------------------------------------

def test_every_director_in_a_lodgement_becomes_a_notice():
    """The schema used to carry one director_name, so a four-director
    lodgement could only ever record the first. That drops the signal exactly
    where it is strongest: several directors transacting in the same company
    on the same day is what the cluster-buy screen exists to find."""
    payload = _payload("327725.pdf")
    assert [n["director_name"] for n in payload["notices"]] == [
        "Patrick Burke", "Oliver Kiddie", "Justin Werner", "Cameron Peacock"]
    assert payload["company_name"] == "FMR Resources Ltd"


def test_a_multi_director_lodgement_yields_a_row_per_director(monkeypatch):
    """apply() must write one canonical row per director, not per document."""
    from asx.canonical import director_trades as ct

    written = []
    monkeypatch.setattr(App3YParser, "_resolve_entity", lambda *a, **k: 7)
    monkeypatch.setattr(ct, "apply_trades",
                        lambda conn, doc_id, rows, **kw: written.extend(rows))
    monkeypatch.setattr("asx.parse.app3y.apply_trades",
                        lambda conn, doc_id, rows, **kw: written.extend(rows))

    from datetime import datetime, timezone
    doc = {"doc_id": 1, "doc_class": "app_3y", "entity_id": 7,
           "lodged_at": datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)}
    App3YParser().apply(None, doc, _payload("327725.pdf"))
    assert [r.person_name_raw for r in written] == [
        "Patrick Burke", "Oliver Kiddie", "Justin Werner", "Cameron Peacock"]
    assert {r.entity_id for r in written} == {7}


# --- quantities are attributed, never assumed -------------------------------

def test_an_enumerated_cell_is_paired_by_its_class_marker():
    """FMR lists three classes and three quantities in step. Taking the first
    number and pairing it with the whole class string records a quantity of no
    particular security — right here, wrong on the next form that lists its
    performance rights first."""
    security = _only({"notices": [_payload("327725.pdf")["notices"][0]]})
    assert security["security_class"] == "Fully Paid Ordinary Shares"
    assert security["qty_acquired"] == 150000
    assert security["consideration_aud"] == 51000.0


def test_a_single_marker_cell_still_names_its_class():
    """Brightstar's disposal cell reads only "B. 53,571" — one marker, because
    only one of its two classes was disposed of. Read as an unenumerated
    number it subtracts performance rights from a holding of ordinary
    shares."""
    cls, (acq, dis) = rules.select_by_class(
        "A. Fully-paid Ordinary Shares B. Share Performance Rights expiring 2027",
        "A. 125,000", "B. 53,571")
    assert cls == "Fully-paid Ordinary Shares"
    assert rules.parse_quantity(acq) == 125000
    assert dis is None


def test_a_quantity_that_names_another_class_is_not_taken():
    """Pantoro writes the class beside the number instead of enumerating:
    "5,025 fully paid ordinary shares" acquired, "5,025 share rights"
    disposed."""
    assert rules.quantity_of_class("5,025 fully paid ordinary shares",
                                   ordinary=True) == 5025
    assert rules.quantity_of_class("5,025 share rights", ordinary=True) is None
    assert rules.quantity_of_class(
        "1,224,478 – performance rights expiring 29 May 2026", ordinary=True) is None
    # No class words: the notice's own class applies.
    assert rules.quantity_of_class("20,415", ordinary=True) == 20415


def test_holdings_are_not_paired_with_a_change_in_another_class():
    """A third of the corpus reports a change in options while stating the
    ordinary holding unchanged. Pairing them produces "1,412,912 - 6,000,000
    = -4,587,088" and files it as a failed reconciliation — blaming the reader
    for a subtraction nobody performed."""
    security = _only(_payload("326106.pdf"))
    assert not rules.security_is_ordinary(security["security_class"])
    assert security["held_before"] is None and security["held_after"] is None
    assert security["qty_disposed"] == 6000000


def test_a_scrambled_form_contributes_no_notice_at_all():
    payload = _payload("329297.pdf")
    assert payload["notices"] == []
    assert "out of order" in payload["extraction_notes"]


# --- one reading is not two readings agreeing -------------------------------

def test_the_rules_extractor_refuses_to_be_asked_twice():
    """Running a deterministic function twice and comparing the results is not
    a second opinion. Scoring that as agreement would manufacture confidence
    out of nothing, which is the failure the dual-pass design exists to
    prevent."""
    extractor = RulesExtractor(App3YParser())
    assert extractor.single_pass
    with pytest.raises(NotImplementedError):
        extractor.extract_vision_pass(b"%PDF-", {}, "")


def test_an_unverifiable_notice_is_reported_as_uncorroborated():
    """With no second reading, the witness is the form's own arithmetic. A
    notice with no before/after figures has been checked against nothing and
    must route to review exactly as a two-pass disagreement would."""
    from datetime import datetime, timezone

    from asx.parse.framework import _score, _uncorroborated

    doc = {"doc_id": 1, "doc_class": "app_3y",
           "lodged_at": datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)}
    notice = {"director_name": "A Director", "date_of_change": "2026-08-19",
              "interest_nature": "direct", "indirect_detail": None,
              "securities": [{"security_class": "Ordinary shares",
                              "qty_acquired": 1000, "qty_disposed": None,
                              "consideration_text": None, "consideration_aud": None,
                              "held_before": None, "held_after": None}]}
    payload = {"company_name": "X", "ticker": None, "is_amendment": None,
               "notices": [notice], "extraction_notes": None}

    validation = App3YParser().validate(payload, doc)
    assert validation.ok                       # not an error: nothing is wrong
    reasons = _uncorroborated(validation)
    assert reasons, "an unchecked notice must not pass as corroborated"
    assert _score(reasons, validation) <= 0.5  # routes to review

    # The same notice with holdings that reconcile is corroborated.
    notice["securities"][0].update(held_before=9000, held_after=10000)
    checked = App3YParser().validate(payload, doc)
    assert checked.ok and not _uncorroborated(checked)
    assert _score([], checked) == 1.0


# --- the whole corpus -------------------------------------------------------

def test_the_corpus_yields_more_notices_than_documents():
    """A tenth of lodgements carry several directors, so notices must exceed
    documents. If this ever equals the document count, the multi-form read has
    regressed to first-director-only."""
    parser = App3YParser()
    documents = sorted(DOCS.glob("*.pdf"))
    if len(documents) < 50:
        pytest.skip("corpus not present")
    notices = sum(len(parser.read_rules(d.read_bytes())["notices"])
                  for d in documents)
    assert notices > len(documents) * 1.1, (
        f"{notices} notices from {len(documents)} documents")


def test_most_reconciling_notices_reconcile_exactly():
    """The gate that replaces two-pass agreement. Measured over the whole
    corpus: notices whose arithmetic can be checked should overwhelmingly
    pass it, or the reader is misattributing quantities and the gate is
    absorbing the damage silently."""
    parser = App3YParser()
    documents = sorted(DOCS.glob("*.pdf"))
    if len(documents) < 50:
        pytest.skip("corpus not present")
    checked = passed = 0
    for path in documents:
        for notice in parser.read_rules(path.read_bytes())["notices"]:
            s = notice["securities"][0]
            if s["held_before"] is None or s["held_after"] is None:
                continue
            checked += 1
            passed += (s["held_before"] + (s["qty_acquired"] or 0)
                       - (s["qty_disposed"] or 0)) == s["held_after"]
    assert checked >= 50
    assert passed / checked >= 0.90, f"{passed}/{checked} reconcile"
