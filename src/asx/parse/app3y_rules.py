"""Rules-based Appendix 3Y/3Z extraction — no model, no API key.

The platform's design is rules-first with an LLM fallback (SPEC §5.3). This
is the rules half, built because the fallback may never be configured, and
because it turns out to carry most of the weight on its own: an Appendix 3Y is
a *form*. Across 39 real lodgements every field label appears in 20-23 of the
23 measured, because the ASX prescribes the layout.

So extraction here is not language understanding, it is reading a form: locate
each printed label, take the text between it and the next label. What the
model was for is the residue — a scanned page, a rewritten layout, an issuer
who invented their own headings — and that residue must go to review rather
than to a guess (Invariant 8). Nothing here returns a value it is not reading
off the page.

The cost of losing the model is therefore not accuracy, it is coverage: forms
this cannot read become review items instead of extractions. That is the
correct direction to fail in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Field labels in the order the ASX prints them. Order matters: a value is the
# text between its own label and whichever label comes next, so a missing
# label simply widens the previous field rather than corrupting a later one.
#
# Each entry is (key, alternatives). Alternatives exist because issuers use
# the form's older and newer wordings interchangeably.
_LABELS: list[tuple[str, list[str]]] = [
    ("entity_name", ["Name of entity"]),
    ("identifier", ["ABN", "ACN", "ARBN"]),
    ("director_name", ["Name of Director", "Name of director"]),
    ("date_of_last_notice", ["Date of last notice"]),
    ("date_ceased", ["Date that director ceased to be director"]),
    ("interest_nature", ["Direct or indirect interest"]),
    ("indirect_detail", ["Nature of indirect interest", "Name of holder & nature of interest"]),
    ("date_of_change", ["Date of change"]),
    ("held_before", ["No. of securities held prior to change",
                     "Number of securities held prior to change"]),
    ("security_class", ["Class"]),
    ("qty_acquired", ["Number acquired"]),
    ("qty_disposed", ["Number disposed"]),
    ("consideration", ["Value/Consideration", "Value / Consideration"]),
    ("held_after", ["No. of securities held after change",
                    "Number of securities held after change"]),
    ("nature_of_change", ["Nature of change"]),
    ("part2", ["Part 2"]),          # terminator, not a field
]

# The ASX template prints its own guidance between a label and its value —
# "Value/Consideration  Note: If consideration is non-cash, provide details
# and estimated valuation  $6,410,050". Truncating at the note loses the
# value; leaving it in captures instructions as data. Since this text is
# boilerplate with fixed wording, it is deleted from the document BEFORE any
# label is located, which removes it from every field at once.
_BOILERPLATE = [
    r"Note\s*:\s*If consideration is non\s*-?\s*cash[^.]*\.?",
    r"Note\s*:\s*Provide details of the circumstances giving rise to the "
    r"relevant interest\.?",
    r"Note\s*:\s*Details are only required for a contract[^.]*\.?",
    r"Note\s*:\s*In the case of a company[^.]*\.?",
    r"Example\s*:\s*on\s*-?\s*market trade[^.]*\.?",
    r"In the case of a (company|trust),?[^.]*\.?",
    r"\+?\s*See chapter 19 for defined terms\.?",
    r"Information or documents not available now[^.]*\.\s*Information and "
    r"documents given to ASX[^.]*\.",
    r"Introduced\s+\d[\d/]*\s*(Amended\s+\d[\d/]*)?",
    r"Rule\s+3\.19A\.?\d?",
    r"Appendix\s+3[YZ]\s+(Change|Final) of ...?Director'?s? Interest Notice",
    # Anchored on its final words: "[^.]*\." stops at the "3." in
    # "listing rule 3.19A.2" and leaves "19A.2 and as agent for the
    # director..." glued to the entity's ABN.
    r"We\s*\(the entity\)\s*give ASX the following information.*?"
    r"Corporations Act\.?",
    r"Part\s*1\s*[-–]?\s*Change of director'?s relevant interests in securities",
    r"Page\s*\d+\s*(of\s*\d+)?",
    r"\d{2}/\d{2}/\d{4}\s+Appendix\s+3[YZ]",
]
_BOILERPLATE_RE = re.compile("|".join(_BOILERPLATE), re.I)

# What survives the sweep but still must not end a value.
_NOTE_RE = re.compile(r"\b(Note\s*:|Example\s*:)", re.I)

_QTY_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{4,})\b")
_MONEY_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)")
_DATE_RES = [
    (re.compile(r"\b(\d{1,2})\s+([A-Z][a-z]{2,8})\s+(\d{4})\b"), "dmy_name"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "dmy_slash"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b"), "dmy_slash2"),
]
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


@dataclass
class RulesExtraction:
    fields: dict[str, str] = field(default_factory=dict)
    form: str | None = None
    unreadable: list[str] = field(default_factory=list)

    def get(self, key: str) -> str | None:
        v = (self.fields.get(key) or "").strip()
        return v or None


def _normalise(text: str) -> str:
    """Flatten the document and delete the ASX template's own guidance.

    Two things happen here, and the order matters. Several issuers' forms
    extract with a tab between every word, so whitespace is collapsed first —
    any pattern written with literal spaces fails otherwise. Then the
    template's boilerplate is removed, BEFORE any label is located, because
    the ASX prints its instructions between a label and its value:

        Value/Consideration  Note: If consideration is non-cash, provide
        details and estimated valuation  $6,410,050

    Truncating at the note loses the figure; keeping it captures instructions
    as data. Deleting it once, up front, fixes every field at the same time.
    """
    text = text.replace("\xa0", " ").replace("\u2019", "'")
    text = re.sub(r"[\s\u200b]+", " ", text)
    text = _BOILERPLATE_RE.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _find_label(text: str, alternatives: list[str], start: int) -> tuple[int, int] | None:
    best = None
    for alt in alternatives:
        pattern = re.compile(r"\b" + r"\s*".join(map(re.escape, alt.split())) + r"\b", re.I)
        m = pattern.search(text, start)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end())
    return best


def _clean_value(raw: str) -> str:
    """Strip the form's own guidance out of a captured value."""
    m = _NOTE_RE.search(raw)
    if m:
        raw = raw[:m.start()]
    raw = re.sub(r"^[\s:.\-–]+", "", raw)
    return re.sub(r"\s+", " ", raw).strip(" :.-––")


def extract(text: str) -> RulesExtraction:
    """Read an Appendix 3Y/3Z by locating its printed labels."""
    flat = _normalise(text)
    out = RulesExtraction()
    out.form = ("app_3z" if re.search(r"Appendix\s*3Z|Final Director'?s Interest", flat, re.I)
                else "app_3y" if re.search(r"Appendix\s*3Y|Change of Director'?s Interest", flat, re.I)
                else None)

    # Locate every label first, then slice between consecutive hits. Slicing
    # against the NEXT FOUND label rather than the next expected one means a
    # form that omits a field does not silently absorb the following field's
    # value.
    # Part 1 is where the change is described; Part 2 is contracts and Part 3
    # the closed-period question. A Part 1 value must never run into them, so
    # they bound the search rather than being fields.
    # The form may not start at the top of the file. Adrad's lodgement opens
    # with two pages of covering announcement on company letterhead, and that
    # letterhead prints the company's ABN — so the first "ABN" in the document
    # is not the form's, and every field anchored before it is wrong.
    # "Name of entity" is the form's first printed field, so that is where
    # reading begins.
    begin = 0
    m = _find_label(flat, ["Name of entity"], 0)
    if m:
        begin = m[0]

    limit = len(flat)
    for terminator in (r"\bPart\s*2\b", r"\bPart\s*3\b"):
        m2 = re.search(terminator, flat[begin:], re.I)
        if m2:
            limit = min(limit, begin + m2.start())

    # Every label's FIRST occurrence, then slice between them in document
    # order. Searching sequentially from a moving cursor instead assumes the
    # ASX's printed order, and an issuer who reorders two cells — or a page
    # header that repeats a word — silently shifts every field after it.
    positions: list[tuple[str, int, int]] = []
    for key, alts in _LABELS:
        if key == "part2":
            continue
        hit = _find_label(flat[:limit], alts, begin)
        if hit is None:
            out.unreadable.append(key)
            continue
        positions.append((key, hit[0], hit[1]))
    positions.sort(key=lambda p: p[1])

    for i, (key, _start, end) in enumerate(positions):
        stop = positions[i + 1][1] if i + 1 < len(positions) else limit
        out.fields[key] = _clean_value(flat[end:min(stop, limit)])
    return out


def parse_quantity(value: str | None) -> int | None:
    """First plain quantity in a field, or None. Never a guess: a field with
    no number returns nothing rather than zero, because 'no number printed'
    and 'zero securities' are different facts."""
    if not value:
        return None
    # Deliberately NOT stripping spaces first. "2,885,833 Fully paid" with
    # spaces removed becomes "2,885,833Fully", where there is no word boundary
    # after the digits, so the pattern backtracks to the longest match that
    # ends at one — "2,885" — and silently reports a holding a thousand times
    # too small.
    m = _QTY_RE.search(value)
    if m:
        return int(m.group(1).replace(",", ""))
    m2 = re.search(r"(?<![\d,.])(\d{1,3})(?![\d,.])", value)
    return int(m2.group(1)) if m2 else None


def parse_money(value: str | None) -> float | None:
    """Total consideration if the form prints one. A per-share price is NOT a
    total, so anything qualified by 'per share' is refused — treating $1.215
    as a transaction value would understate it by five orders of magnitude."""
    if not value:
        return None
    for m in _MONEY_RE.finditer(value):
        tail = value[m.end():m.end() + 24].lower()
        if re.match(r"\s*(per|/)\s*(share|security|unit)", tail):
            continue
        return float(m.group(1).replace(",", ""))
    return None


def parse_date(value: str | None) -> str | None:
    """ISO date from a field, or None. A range ('12-14 August 2026') yields
    nothing: the form is stating that the change happened across several days
    and picking one would invent a fact (Invariant 2)."""
    if not value:
        return None
    if re.search(r"\d\s*[-–]\s*\d{1,2}\s+[A-Z][a-z]{2,8}", value):
        return None
    for pattern, kind in _DATE_RES:
        m = pattern.search(value)
        if not m:
            continue
        if kind == "dmy_name":
            month = _MONTHS.get(m.group(2).lower())
            if not month:
                continue
            return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"
        year = int(m.group(3))
        if kind == "dmy_slash2":
            year += 2000
        return f"{year:04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None
