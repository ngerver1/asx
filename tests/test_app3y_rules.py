"""Rules-based Appendix 3Y extraction, measured against real lodgements.

Every case below is anchored to a document in fixtures/app3y/documents and to
a figure a human reader verified against the PDF. They are regressions, not
hypotheticals: each one is a defect this parser actually had, found by
measuring against dual-read ground truth rather than by imagining what a form
might contain.

The measured accuracy over the 23 ground-truthed documents is asserted at the
bottom, because criterion 1.1 is a number and a number needs a test.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from asx.parse.app3y_rules import (
    extract_all, parse_date, parse_holdings, parse_money, parse_quantity,
    scan_parcels,
)

DOCS = Path(__file__).parent.parent / "fixtures" / "app3y" / "documents"
GROUND_TRUTH = json.loads((DOCS / "ground_truth.json").read_text())["documents"]


# Several tests below sweep the whole corpus, and a corpus of 195 encrypted
# PDFs is not cheap to parse: read once per session, not once per test. The
# documents are immutable (Invariant 3), so caching them cannot mask a change.
@lru_cache(maxsize=None)
def _text(name: str) -> str:
    import pypdf
    path = DOCS / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(str(path)).pages)


@lru_cache(maxsize=None)
def _forms(name: str) -> tuple:
    return tuple(extract_all(_text(name)))


def _first(name: str):
    forms = _forms(name)
    assert forms, name
    return forms[0]


def _holdings(form):
    after = (form.get("held_at_ceasing") if form.form == "app_3z"
             else form.get("held_after"))
    return parse_holdings(form.get("held_before"), after,
                          interest=form.get("interest_nature"),
                          acquired=form.get("qty_acquired"),
                          disposed=form.get("qty_disposed"))


# --- the ASX's own guidance must not eat the value it guides --------------

def test_a_note_never_swallows_the_consideration():
    """The template prints its guidance BETWEEN the label and the value:

        Value/Consideration  Note: If consideration is non-cash, provide
        details and estimated valuation  $40,000.00

    A pattern ending in an open run walks past the note's last word, through
    the value, and stops at the first full stop it finds — the decimal point.
    "$40,000.00" becomes "00" and the consideration is gone. That cost six of
    the nine measured considerations before it was found.
    """
    assert parse_money(_first("328617.pdf").get("consideration")) == 40000.00
    assert parse_money(_first("328655.pdf").get("consideration")) == 50000
    assert parse_money(_first("329777.pdf").get("consideration")) == 11139


def test_a_note_split_mid_word_is_still_a_note():
    """Brightstar's PDF extracts as "Note: If c onsideration is non -cash".
    A pattern written with whole words misses it, the surviving "Note:"
    truncates the value, and a $52,500 consideration reads as nothing."""
    assert parse_money(_first("328630.pdf").get("consideration")) == 52500


def test_boilerplate_removal_reaches_a_fixpoint():
    """A page footer reads "Appendix 3Y Page 2 01/01/2011". Only once
    "Page 2" is deleted does "Appendix 3Y 01/01/2011" exist to be matched —
    so one pass leaves a stray template date sitting in the date_of_change
    cell, where it reads as a second date and the real one is refused."""
    assert parse_date(_first("327657.pdf").get("date_of_change")) == "2026-08-13"


# --- dates -----------------------------------------------------------------

def test_an_ordinal_date_is_read():
    """Pivotal Metals writes "13th August 2026"."""
    assert parse_date(_first("328653.pdf").get("date_of_change")) == "2026-08-13"


def test_a_date_range_is_refused():
    """"12-14 August 2026" says the change happened across several days.
    Picking one invents a fact (Invariant 2)."""
    assert parse_date("12-14 August 2026") is None
    assert parse_date(_first("6A1339259.pdf").get("date_of_change")) is None


def test_two_dates_in_one_cell_are_refused():
    """Brightstar: "Date of change A. 17 August 2026 B. 13 August 2026" — one
    date per lettered transaction. Returning the first is a substantive
    default for an ambiguous field, which Invariant 8 forbids, and would date
    a conversion to the day of an unrelated lapse."""
    assert parse_date("A. 17 August 2026 B. 13 August 2026") is None
    assert parse_date(_first("328630.pdf").get("date_of_change")) is None


def test_the_same_date_printed_twice_is_still_one_date():
    assert parse_date("14 August 2026 (14 August 2026)") == "2026-08-14"


# --- labels ----------------------------------------------------------------

def test_a_scheme_number_is_not_part_of_the_trust_name():
    """Charter Hall Long WALE REIT is a registered scheme, so its identifier
    label is ARSN. Without that label the name absorbs the numbers — and it
    carries two of them, being a stapled security."""
    form = _first("328987.pdf")
    assert form.get("entity_name") == "Charter Hall Long WALE REIT"
    assert "144 613 641" in form.get("identifier")


def test_a_word_bookmark_artifact_does_not_hide_the_director():
    """Word emits "0BName of Director Rowena Smith". The digits are word
    characters, so a \\b in front of the label never matches and the
    director's name is lost with no error anywhere."""
    assert _first("329701.pdf").get("director_name") == "Rowena Smith"
    assert _first("328049.pdf").get("director_name") == "Luke Cox"


def test_a_label_nested_inside_another_label_is_dropped():
    """The 3Z prints "Number & class of securities", where the generic
    "Class" label matches nine characters in. Both then claim the cell, and
    the holding comes out as the class while the holding reads empty."""
    form = _first("2A1690462.pdf")
    assert form.form == "app_3z"
    assert _holdings(form)[1] == 8991870


def test_an_initial_notice_is_named_not_mistaken_for_a_change_notice():
    """Two of the sixty captured documents are Appendix 3X. Naming the form
    keeps them out of the 3Y pipeline; leaving them unnamed files them as
    unreadable 3Ys, which reads as a parser failure rather than a form this
    platform does not yet handle."""
    assert _first("327850.pdf").form == "app_3x"
    assert _first("328003.pdf").form == "app_3x"


# --- holdings cells are lists ----------------------------------------------

def test_the_indirect_parcel_is_read_when_the_notice_is_about_it():
    """Terra Critical Minerals holds 1,205,155 shares directly and 27,765,832
    through 10 Bolivianos Pty Ltd, and the notice concerns the indirect
    parcel. First-number-wins understates the holding by 23x — in the
    direction that makes an insider look smaller than they are."""
    assert _holdings(_first("329745.pdf")) == (27765832, 27765833)


def test_a_direct_parcel_that_did_not_move_is_not_the_answer():
    """CurveBeam's director holds 32,501,692 directly, unchanged; the 20,415
    shares were issued to his wife's indirect holding of 10,776,511."""
    assert _holdings(_first("329688.pdf")) == (10776511, 10796926)


def test_the_printed_total_answers_a_notice_covering_both_interests():
    """Flagship prints Direct, Indirect and TOTAL under each class heading,
    and the interest is stated as "Direct & Indirect" — so the figure covering
    both is the one the form already prints."""
    assert _holdings(_first("329737.pdf")) == (10454291, 10654291)


def test_a_stated_total_beats_its_own_breakdown():
    """"Interest in 300,000,770 Fully paid Ordinary Shares (ASX: PVE) as
    follows:" and then the three holdings that make it up. Taking one line of
    the breakdown contradicts a figure printed on the same form."""
    assert _holdings(_first("328653.pdf")) == (300000770, 300000770)


def test_the_arithmetic_on_the_form_picks_the_parcel():
    """Brightstar lists three unmarked ordinary parcels. Only one moves by the
    125,000 shares the form says were acquired — and the 53,571 disposed are
    performance rights, a different class, so the NET of the two describes no
    class at all and must not be the only movement tried."""
    assert _holdings(extract_all(_text("328630.pdf"))[0]) == (2842857, 2967857)


def test_a_self_contradicting_form_yields_nothing():
    """Aurora Labs declares the interest "Indirect", then prints an unlabelled
    block of 540,907 ordinary shares above a block headed "Indirect:" holding
    400,000 — and puts the securities actually acquired in the unlabelled one.
    The form never reconciles this. An unmarked parcel is not the other kind
    by default; it is unmarked. This lodgement is meant to leave with nothing
    and go to review (Invariant 8) — the human readers who built the ground
    truth flagged the same contradiction and could not resolve it either.
    """
    assert _holdings(_first("328627.pdf")) == (None, None)


def test_one_ambiguous_cell_does_not_discard_the_other():
    """A before-cell listing one ordinary parcel still answers when the
    after-cell lists several."""
    before, _ = parse_holdings(
        "2,000,000 Fully Paid Ordinary Shares",
        "1,000,000 Fully Paid Ordinary Shares 1,000,000 Fully Paid Ordinary "
        "Shares held by a second trust")
    assert before == 2000000


def test_an_option_expiry_is_not_a_share_count():
    """"530,481 Plan Options, with an exercise price of $0.8016 and expiry
    date of 16 August 2029" contains two numbers that look like quantities to
    a regex: the price's tail and the year."""
    quantities = [p.quantity for p in scan_parcels(
        "530,481 Plan Options, with an exercise price of $0.8016 and expiry "
        "date of 16 August 2029")]
    assert quantities == [530481]


def test_escrowed_ordinary_shares_are_ordinary_shares():
    """"8,701,680 fully paid ordinary (escrowed)" is a restriction on a
    holding, not a different class of security."""
    assert _holdings(_first("328515.pdf"))[0] == 8701680


def test_share_rights_are_not_shares():
    """"Share rights" is both share-shaped and rights-shaped. A cell heading
    that names no class unambiguously must clear the heading in force, not
    leave the previous one to claim the parcel."""
    parcels = {p.quantity: p.security for p in scan_parcels(
        "Fully paid ordinary shares 311,526 ordinary shares (indirectly held) "
        "Share rights 417,908 share rights granted on 30 October 2025")}
    assert parcels[311526] == "ordinary"
    assert parcels[417908] != "ordinary"


def test_a_holder_the_form_never_marks_is_not_matched():
    """Selection by stated interest requires the form to mark EVERY ordinary
    parcel. A cell with one marked and one unmarked parcel has not said which
    is which."""
    assert parse_holdings(
        "540,907 fully paid ordinary shares Indirect: 400,000 fully paid "
        "ordinary shares",
        "540,907 fully paid ordinary shares Indirect: 400,000 fully paid "
        "ordinary shares",
        interest="Indirect") == (None, None)


# --- quantities and money --------------------------------------------------

def test_a_quantity_keeps_all_of_its_digits():
    """Stripping spaces before matching turns "2,885,833 Fully paid" into
    "2,885,833Fully", where the pattern backtracks to the longest match
    ending at a word boundary — "2,885", a thousandth of the real holding."""
    assert parse_quantity("2,885,833 Fully paid ordinary shares") == 2885833


def test_a_per_share_price_is_not_a_consideration():
    """Treating $1.215 as the transaction value understates it by five orders
    of magnitude."""
    assert parse_money("$1.215 per share") is None
    assert parse_money("$183,750 ($0.49 per share)") == 183750


def test_an_empty_field_is_not_zero():
    """'No number printed' and 'zero securities' are different facts."""
    assert parse_quantity("") is None
    assert parse_quantity("Nil") is None


# --- the measured criterion ------------------------------------------------

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


def _expected_date(printed: str) -> str | None:
    """Ground truth stores the printed text. A field naming ONE date must
    yield that date; a range or a per-holder enumeration names no single date
    and must yield nothing."""
    import re
    days = re.findall(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Z][a-z]{2,8})\s+(\d{4})",
                      printed)
    if re.search(r"\d\s*[-–]\s*\d{1,2}\s+[A-Z][a-z]", printed) or len(set(days)) != 1:
        return None
    day, month, year = days[0]
    return f"{int(year):04d}-{_MONTHS[month.lower()]:02d}-{int(day):02d}"


def test_field_accuracy_against_dual_read_ground_truth():
    """Criterion 1.1: >=98% field accuracy on real documents.

    Measured over every scalar the two readers agreed on, across the 23
    ground-truthed lodgements. A refusal counts as a miss here — reporting
    nothing is safer than reporting a wrong number, but it is not accuracy,
    and this number must not be allowed to improve by refusing more.
    """
    def same_name(a, b):
        return " ".join((a or "").lower().replace(",", " ").split()) == \
               " ".join((b or "").lower().replace(",", " ").split())

    right = total = 0
    wrong: list[str] = []
    for doc in GROUND_TRUTH:
        if not (DOCS / doc["file"]).exists():
            pytest.skip(f"{doc['file']} not present")
        form = _first(doc["file"])
        qty_before, qty_after = _holdings(form)
        got = {
            "entity_name": form.get("entity_name"),
            "form": form.form,
            "director_name": form.get("director_name"),
            "qty_before": qty_before,
            "qty_after": qty_after,
            "date_of_change": parse_date(form.get("date_of_change")),
            "consideration_aud": parse_money(form.get("consideration")),
        }
        for field, value in got.items():
            expected = doc.get(field)
            if expected in (None, "", "na"):
                continue
            if field in ("entity_name", "director_name"):
                ok = same_name(value, expected)
            elif field in ("qty_before", "qty_after"):
                ok = value is not None and int(value) == int(expected)
            elif field == "consideration_aud":
                ok = value is not None and abs(float(value) - float(expected)) < 0.01
            elif field == "date_of_change":
                expected = _expected_date(expected)
                ok = value == expected
            else:
                ok = value == expected
            total += 1
            right += ok
            if not ok:
                wrong.append(f"{doc['file']} {field}: want {expected!r} got {value!r}")

    assert total >= 130, f"only {total} fields measured"
    accuracy = right / total
    assert accuracy >= 0.98, (
        f"{right}/{total} = {accuracy:.1%}\n" + "\n".join(wrong))


# --- the environment must be able to read the corpus -----------------------

def test_the_encrypted_fixtures_are_actually_readable():
    """55 of the 60 real ASX announcement PDFs are AES-encrypted, with an
    empty user password. pypdf opens those silently when a crypto backend is
    installed and raises DependencyError when one is not — so an install
    missing `pypdf[crypto]` reads 5 documents in 60.

    This test exists because that is exactly what happened: the dependency
    was present locally by accident and absent in CI, and the whole
    real-document suite failed there while passing here.
    """
    import pypdf
    from asx.parse.text import document_text

    encrypted = [f for f in sorted(DOCS.glob("*.pdf"))
                 if pypdf.PdfReader(str(f)).is_encrypted]
    if not encrypted:
        pytest.skip("no encrypted fixtures present")
    assert len(encrypted) > 20, "the corpus should be mostly encrypted"
    assert "Appendix" in document_text(encrypted[0].read_bytes())


def test_a_missing_crypto_backend_is_never_read_as_an_empty_document(monkeypatch):
    """A scanned page and a missing library both yield no text, and they
    demand opposite responses. A scanned page is a document problem: route it
    to review and carry on. A missing backend affects EVERY encrypted
    document at once, and carrying on builds a complete-looking dataset out
    of almost nothing (Invariant 7)."""
    import pypdf
    from pypdf.errors import DependencyError

    from asx.ingest.possession import read_document_facts
    from asx.parse.text import UnreadableDocument, pdf_to_text

    def refuse(*_args, **_kwargs):
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")

    monkeypatch.setattr(pypdf, "PdfReader", refuse)

    with pytest.raises(UnreadableDocument):
        pdf_to_text(b"%PDF-1.7 encrypted")
    with pytest.raises(UnreadableDocument):
        read_document_facts(b"%PDF-1.7 encrypted")


def test_a_genuinely_unreadable_pdf_still_degrades_quietly():
    """The loud path is only for the environment. A malformed file is a
    document problem and still yields empty facts for review."""
    from asx.ingest.possession import read_document_facts

    facts = read_document_facts(b"%PDF-1.7 this is not a pdf")
    assert facts.doc_class is None and facts.text == ""


# --- a form whose cells came out in the wrong order -------------------------

def test_a_scrambled_extraction_yields_nothing_at_all():
    """Klevo Rewards' PDF extracts with the table's cells out of order — all
    the labels bunched together, then all the values:

        Name of Director  Date of last notice  Andrew Shi  08 July 2026

    Reading "the text between a label and the next label" then yields nothing
    for most cells. Four core cells came out blank; two others returned
    numbers that happened to be correct, because the value landed beside the
    right label by chance.

    That coincidence is the danger. A form read out of order can bind a real
    number to the wrong label and look entirely ordinary doing it, and nothing
    distinguishes the cells that bound correctly from the ones that did not.
    So the whole form is refused rather than allowed to contribute the cells
    that look fine, and it goes to review intact.
    """
    form = _first("329297.pdf")
    assert form.scrambled
    assert form.get("director_name") is None
    assert form.get("entity_name") is None
    # ...including the two cells that DID produce plausible numbers.
    assert form.get("qty_acquired") is None
    assert _holdings(form) == (None, None)
    # The raw reading survives for the review item to show a human.
    assert "3,515,000" in form.fields["qty_acquired"]


def test_an_ordinary_form_is_never_called_scrambled():
    """The guard must not fire on forms that simply leave a cell empty."""
    clean = [f for name in ("329745.pdf", "329737.pdf", "328630.pdf",
                            "6A1339259.pdf", "2A1690462.pdf")
             for f in _forms(name)]
    assert clean and not any(f.scrambled for f in clean)


def test_the_captured_corpus_is_overwhelmingly_readable():
    """A whole-corpus canary. If a future change starts refusing forms
    wholesale — or starts trusting scrambled ones — this moves."""
    forms = [f for path in sorted(DOCS.glob("*.pdf"))
             for f in _forms(path.name)]
    threes = [f for f in forms if f.form == "app_3y"]
    assert len(threes) >= 100, f"only {len(threes)} Appendix 3Y forms found"
    scrambled = [f for f in threes if f.scrambled]
    assert len(scrambled) <= len(threes) // 20, (
        f"{len(scrambled)} of {len(threes)} forms read as scrambled")
    named = [f for f in threes if f.get("entity_name") and f.get("director_name")]
    assert len(named) >= len(threes) - len(scrambled), (
        f"only {len(named)} of {len(threes)} forms yield entity and director")


def test_the_form_names_itself_in_a_line_the_sweep_deletes():
    """"Appendix 3Y Change of Director's Interest Notice" is both the
    document's identity and a running header that lands in the middle of
    cells, so it is deleted before any label is located. Most lodgements
    repeat it in a page footer and survive losing one copy; three of 195 print
    it exactly once, and were left with no form type at all — filed as
    unreadable rather than as the ordinary 3Ys they are.

    The form type is therefore read from the segment BEFORE the sweep.
    """
    for name in ("326268.pdf", "326319.pdf", "327093.pdf"):
        assert _first(name).form == "app_3y", name


def test_every_captured_document_has_a_form_type():
    """Corpus canary. A document whose form cannot be named goes nowhere: no
    parser claims it and no gap report counts it."""
    unnamed = [path.name for path in sorted(DOCS.glob("*.pdf"))
               for f in _forms(path.name) if f.form is None]
    assert not unnamed, f"form type not recognised: {unnamed}"


# --- a change stated across several days ---------------------------------

def test_a_single_stated_date_is_used_as_printed():
    from asx.parse.app3y_rules import resolve_change_date

    assert resolve_change_date("11 August 2026") == (
        "2026-08-11", "stated", ["2026-08-11"])


def test_formats_the_reader_used_to_refuse():
    """Neither was a principle — just a gap that left the notice unusable.
    '06 Aug 2026' failed on the month abbreviation, '4.8.2026' on the
    separator."""
    from asx.parse.app3y_rules import resolve_change_date

    assert resolve_change_date("06 Aug 2026")[0] == "2026-08-06"
    assert resolve_change_date("4.8.2026")[0] == "2026-08-04"


def test_a_range_shares_its_month_across_both_endpoints():
    """'12-14 August 2026' states two dates using one month. Read literally
    only the 14th is a date, which turns a three-day change into a one-day
    one."""
    from asx.parse.app3y_rules import resolve_change_date

    day, basis, stated = resolve_change_date("12-14 August 2026")
    assert stated == ["2026-08-12", "2026-08-14"]
    assert day == "2026-08-13" and basis == "range_midpoint"


def test_an_enumeration_is_estimated_but_labelled_differently_from_a_range():
    """Both are estimates; they are not equally good. The midpoint of a period
    lies inside it, whereas the midpoint of '17 August and 13 August' is a day
    on which nothing happened — so a screen that cannot carry an invented day
    can exclude one and keep the other."""
    from asx.parse.app3y_rules import resolve_change_date

    day, basis, stated = resolve_change_date("A. 17 August 2026 B. 13 August 2026")
    assert day == "2026-08-15" and basis == "enumeration_midpoint"
    assert stated == ["2026-08-13", "2026-08-17"], "the real dates must survive"


def test_dates_too_far_apart_are_still_refused():
    """The midpoint of 12 March and 14 August is 28 May: five months from
    either real date, and it would put a fabricated event_date into the
    cluster-buy window. LR 3.19B's five business days is the yardstick."""
    from asx.parse.app3y_rules import resolve_change_date

    day, _basis, stated = resolve_change_date("12 March 2026 14 August 2026")
    assert day is None, "a five-month midpoint was invented"
    assert stated == ["2026-03-12", "2026-08-14"], "but what it said is kept"


def test_a_field_stating_no_date_yields_none():
    from asx.parse.app3y_rules import resolve_change_date

    assert resolve_change_date("N/A")[0] is None
    assert resolve_change_date(None)[0] is None
