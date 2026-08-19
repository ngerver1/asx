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
from functools import lru_cache

def _phrase(text: str) -> str:
    """A regex for `text` that tolerates whitespace inserted anywhere in it.

    These are real extractions of justified PDF text, and the extractor drops
    spaces inside words at will: this corpus contains "c onsideration",
    "Shar e Performance", "R ights", "secu rities" and "t he". A pattern
    written with literal words silently fails on the document that needs it
    most — the ASX guidance note in Brightstar's lodgement went unmatched for
    exactly this reason, and took a $52,500 consideration down with it.

    Whitespace is already collapsed to single spaces before this is used, so
    `\s?` between characters costs nothing to match.
    """
    return "".join(
        r"\s+" if ch == " " else re.escape(ch) + r"\s?" for ch in text
    )


# Field labels in the order the ASX prints them. Order matters: a value is the
# text between its own label and whichever label comes next, so a missing
# label simply widens the previous field rather than corrupting a later one.
#
# Each entry is (key, alternatives). Alternatives exist because issuers use
# the form's older and newer wordings interchangeably.
_LABELS: list[tuple[str, list[str]]] = [
    ("entity_name", ["Name of entity"]),
    # ARSN belongs here even though it is not a company number: Charter Hall
    # Long WALE REIT is a registered scheme, and without the label its scheme
    # numbers are read as part of the trust's name. It also carries TWO
    # ("ARSN 144 613 641; 614 713 138") — a stapled security — so the value is
    # captured verbatim and left for the resolver to refuse, not narrowed here.
    ("identifier", ["ABN", "ACN", "ARBN", "ARSN"]),
    ("director_name", ["Name of Director", "Name of director"]),
    ("date_of_last_notice", ["Date of last notice"]),
    ("date_ceased", ["Date that director ceased to be director"]),
    # 3Z only. A final notice has no before/after structure — it states one
    # holding, as at the date the director ceased. Kept separate from
    # held_after so nothing downstream reads it as the result of a change.
    ("held_at_ceasing", ["Number & class of securities",
                         "Number and class of securities",
                         "No. & class of securities",
                         "No. and class of securities"]),
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
# The ASX prints its own guidance BETWEEN a label and the value it guides:
#
#     Value/Consideration  Note: If consideration is non-cash, provide details
#     and estimated valuation  $40,000.00
#
# Truncating at the note loses the figure; leaving it in captures instructions
# as data. So the guidance is deleted from the document BEFORE any label is
# located, which fixes every field at once.
#
# Two rules govern the patterns below, both learned from this corpus.
#
# 1. ANCHOR ON CLOSING WORDS. Never end a pattern in an open run like
#    "[^.]*\.?" — it walks past the note's last word, through the value, and
#    stops at the first full stop it finds, which is the decimal point.
#    "$40,000.00" becomes "00" and the consideration is gone. That cost six of
#    the nine measured considerations.
# 2. TOLERATE INSERTED WHITESPACE. The wordings are taken from the 60 real
#    lodgements, not the blank template, and PDF extraction splits words at
#    will ("c onsideration", "non -cash", "buy - back"). _phrase() handles it.
# Plain-language template guidance. Written as prose, matched through
# _phrase() so an inserted space cannot make a pattern miss, and each one
# anchored on its OWN CLOSING WORDS.
_BOILERPLATE_PHRASES = [
    "If consideration is non-cash, provide details and estimated valuation",
    "If consideration is non-cash, provide details and an estimated valuation",
    "Provide details of the circumstances giving rise to the relevant interest",
    "Details are only required for a contract in relation to which the "
    "interest has changed",
    "In the case of a trust, this includes interests in the trust made "
    "available by the responsible entity of the trust",
    "Example: on-market trade, off-market trade, exercise of options, issue "
    "of securities under dividend reinvestment plan, participation in buy-back",
    "Information or documents not available now must be given to ASX as soon "
    "as available",
    "Information and documents given to ASX become ASX's property and may be "
    "made public",
    "Part 1 - Change of director's relevant interests in securities",
]

_NOTE_PREFIX = r"(?:\b(?:Note|Example)\s*:\s*)?"

# Structural boilerplate: rule numbers, footers, artifacts. Already regexes.
_BOILERPLATE_PATTERNS = [
    # Two printed variants, "(i)" and "(ii)", ending "should be disclosed in
    # this part." The tail matters: stopping at "should be disclosed" leaves
    # "in this part." behind as a value.
    _NOTE_PREFIX
    + _phrase("In the case of a company, interests which come within paragraph")
    + r"\s*\(\s*i\s?i?\s*\)\s*" + _phrase("of the definition of")
    + r"\s*.{0,3}\s*" + _phrase("notifiable interest of a director")
    + r"\s*.{0,3}\s*" + _phrase("should be disclosed")
    + r"(?:\s*" + _phrase("in this part") + r"\s*\.?)?",
    r"\+?\s*See chapter 19 for defined terms\.?",
    r"Introduced\s+\d[\d/]*\s*(?:Amended\s+\d[\d/]*)?",
    r"Rule\s+3\.19A\.?\d?",
    r"Appendix\s+3[YZ]\s+(?:Change|Final)\s+of\s+\+?\s*Director'?s?\s+Interest\s+Notice",
    r"Appendix\s+3X\s+Initial\s+\+?\s*Director'?s?\s+Interest\s+Notice",
    # Anchored on its final words: an open run stops at the "3." in
    # "listing rule 3.19A.2" and leaves "19A.2 and as agent for the
    # director..." glued to the entity's ABN.
    r"We\s*\(the entity\)\s*give ASX the following information.*?"
    r"Corporations Act\.?",
    r"Page\s*\d+\s*(?:of\s*\d+)?",
    r"\d{1,2}/\d{1,2}/\d{2,4}\s+Appendix\s+3[YZ]",
    r"Appendix\s+3[YZ]\s+\d{1,2}/\d{1,2}/\d{2,4}",
    r"For personal use only",
    # A law firm's document control number sits in the footer of Brightstar's
    # lodgement — "3461-0342-1486, v. 1" — and its groups read as three more
    # parcels in the holdings cell directly above it.
    r"\b\d{4}-\d{4}-\d{4}\b(?:,?\s*v\.\s*\d+)?",
    # Word emits bookmark artifacts glued to the label they precede —
    # "0BName of Director Rowena Smith". The digits are word characters, so
    # the \b in front of the label never matches and the director is lost.
    r"(?<![A-Za-z])\d+B(?=[A-Z][a-z])",
]

# Each note is deleted together with its own "Note:"/"Example:" label, as one
# unit. Deleting only the body leaves the label sitting immediately in front of
# the value — and _NOTE_RE below then truncates the value at it, which loses
# the figure just as surely as capturing the guidance would.
#
# A "Note:" that survives this sweep is one whose body we do NOT recognise, and
# truncating there is then the right answer: unidentified guidance must not be
# captured as data.
_BOILERPLATE = (
    [_NOTE_PREFIX + _phrase(t) + r"\s*\.?" for t in _BOILERPLATE_PHRASES]
    + _BOILERPLATE_PATTERNS
)

# The plain-language notes go through _phrase() so a space inserted inside a
# word cannot make a pattern miss. The structural patterns above (rule numbers,
# page footers, the bookmark artifact) are already written as regexes.
_BOILERPLATE_RE = re.compile("|".join(_BOILERPLATE), re.I)

# What survives the sweep but still must not end a value.
_NOTE_RE = re.compile(r"\b(Note\s*:|Example\s*:)", re.I)

_QTY_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{4,})\b")
_MONEY_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)")
# "13th August 2026" is how Pivotal Metals writes it. The ordinal suffix is
# optional, never required.
_DATE_RES = [
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Z][a-z]{2,8})\s+(\d{4})\b"),
     "dmy_name"),
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
    # Swept repeatedly, because deleting one fragment brings two others
    # together: a page footer reads "Appendix 3Y Page 2 01/01/2011", and only
    # once "Page 2" is gone does "Appendix 3Y 01/01/2011" exist to be matched.
    # Left after one pass, that stray 01/01/2011 lands in the date_of_change
    # cell as a second date and the real one is refused as an enumeration.
    for _ in range(4):
        swept = _BOILERPLATE_RE.sub(" ", text)
        swept = re.sub(r"\s{2,}", " ", swept)
        if swept == text:
            break
        text = swept
    return text.strip()


@lru_cache(maxsize=None)
def _label_re(alt: str) -> re.Pattern[str]:
    """A label matcher tolerant of whitespace inserted inside its words.

    The same extraction damage that hits the guidance notes hits the labels:
    "No. and class of securi ties". A label that fails to match does not lose
    one value, it widens the previous field to swallow this one.
    """
    return re.compile(r"(?<![A-Za-z])" + _phrase(alt) + r"(?![A-Za-z])", re.I)


def _find_label(text: str, alternatives: list[str], start: int) -> tuple[int, int] | None:
    best = None
    for alt in alternatives:
        m = _label_re(alt).search(text, start)
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


def split_forms(text: str) -> list[str]:
    """Split a lodgement into one segment per Appendix 3Y/3Z form.

    **A single PDF frequently contains several complete forms.** Measured
    across 58 real lodgements, 6 of them (10%) hold more than one — up to
    four directors in one document, each with their own Part 1, their own
    quantities and their own consideration.

    Reading only the first would drop 15 of the 21 director notices in those
    six files. That is worse than a coverage gap: the cluster-buy signal
    exists to find SEVERAL directors transacting in the same company at the
    same time, and a board that files its notices in one PDF is exactly the
    coordinated event the signal is looking for. Taking only the first
    director would turn the strongest possible signal into the weakest.

    Segments run from one "Name of entity" to the next. A covering letter
    before the first form is discarded with it, which is also what the
    letterhead-ABN problem needs.
    """
    flat = _normalise(text)
    starts = [m.start() for m in re.finditer(
        r"\bName\s*of\s*entity\b", flat, re.I)]
    if not starts:
        return [flat]
    return [flat[a:b] for a, b in zip(starts, starts[1:] + [len(flat)])]


def extract_all(text: str) -> list["RulesExtraction"]:
    """Every form in the lodgement, in document order."""
    return [_extract_segment(seg) for seg in split_forms(text)]


def extract(text: str) -> RulesExtraction:
    """Read the FIRST form in a lodgement.

    Kept for callers that want one result. Anything writing canonical rows
    must use extract_all(): a tenth of real lodgements carry more than one
    form, and this returns only the first of them.
    """
    forms = extract_all(text)
    return forms[0] if forms else RulesExtraction()


def _extract_segment(flat: str) -> RulesExtraction:
    """Read one already-isolated form by locating its printed labels."""
    out = RulesExtraction()
    # 3X is the INITIAL interest notice, lodged when a director is appointed.
    # Two of the sixty captured documents are 3Xs. Naming the form is what
    # keeps them out of the 3Y pipeline; leaving them unnamed would file them
    # as unreadable 3Ys, which reads as a parser failure rather than a form
    # this platform does not yet handle.
    out.form = (
        "app_3z" if re.search(r"Appendix\s*3Z|Final Director'?s Interest", flat, re.I)
        else "app_3x" if re.search(r"Appendix\s*3X|Initial Director'?s Interest", flat, re.I)
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

    # Drop a label found INSIDE another label. The 3Z prints "Number & class
    # of securities", where the generic "Class" label matches nine characters
    # in — so the holding is captured as the class and the holding cell comes
    # out empty. The outer label always starts first, so anything starting
    # before the previous label ends is nested in it.
    kept: list[tuple[str, int, int]] = []
    for entry in positions:
        if kept and entry[1] < kept[-1][2]:
            out.unreadable.append(entry[0])
            continue
        kept.append(entry)
    positions = kept

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
    """The single ISO date a field states, or None.

    Two ways a field states no single date, and both must yield nothing:

    - A RANGE. "12-14 August 2026" says the change happened across several
      days. Picking one would invent a fact (Invariant 2).
    - AN ENUMERATION. Brightstar's notice reads "Date of change A. 17 August
      2026 B. 13 August 2026" — one date per lettered transaction. Returning
      the first is a substantive default for an ambiguous field, which
      Invariant 8 forbids; it would also date a conversion to the day of an
      unrelated lapse.

    This is the same defect that once buried a $6.4m sale behind a vesting:
    the form enumerates, the parser reads item one, and the rest disappears.
    """
    if not value:
        return None
    if re.search(r"\d\s*[-–]\s*\d{1,2}\s+[A-Z][a-z]{2,8}", value):
        return None
    found = []
    for iso in _dates_in(value):
        if iso not in found:
            found.append(iso)
    return found[0] if len(found) == 1 else None


def _dates_in(value: str) -> list[str]:
    """Every ISO date the text states, in the order printed."""
    out: list[tuple[int, str]] = []
    for pattern, kind in _DATE_RES:
        for m in pattern.finditer(value):
            if kind == "dmy_name":
                month = _MONTHS.get(m.group(2).lower())
                if not month:
                    continue
                out.append((m.start(),
                            f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"))
                continue
            year = int(m.group(3))
            if kind == "dmy_slash2":
                year += 2000
            out.append((m.start(),
                        f"{year:04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"))
    return [iso for _, iso in sorted(out)]


# --------------------------------------------------------------------------
# Holdings cells
#
# "No. of securities held prior to change" is not a number. It is a LIST, and
# on a third of real lodgements it lists several parcels:
#
#   CurveBeam    Direct 32,501,692 ordinary shares  530,481 Plan Options ...
#                Indirect 10,776,511 ordinary shares through Susmita Singh ...
#   Flagship     Ordinary Shares Direct 9,527,206 Indirect 927,085
#                TOTAL 10,454,291  Convertible Notes Direct 2,000,753 ...
#   Terra        Niv Dagan - Direct - 1,205,155 Fully Paid Ordinary Shares
#                10 Bolivianos Pty Ltd - Indirect - 27,765,832 Fully Paid ...
#
# Taking the first number reads the director's DIRECT parcel out of a notice
# about an INDIRECT one. On Terra that is 1,205,155 in place of 27,765,832 —
# understating the holding by 23x, and understating it in the direction that
# makes an insider look smaller than they are.
#
# So the cell is read as parcels, and a parcel is chosen by evidence printed on
# the form itself:
#
#   1. only one parcel                        -> that one
#   2. only one parcel of the ORDINARY class  -> that one
#   3. the pair whose difference equals the printed acquired/disposed
#   4. the parcel whose Direct/Indirect marker matches the stated interest
#      (or the printed TOTAL, when the interest is stated as both)
#   5. otherwise nothing, and a review item
#
# Step 3 is why the two cells are read as a PAIR rather than one at a time:
# the form's own arithmetic is the strongest evidence available about which
# parcel changed, and it is only visible across before, after and the number
# acquired. Step 5 is Invariant 8 — a holdings figure we cannot attribute is
# reported as unknown, never as the first plausible number on the page.

_MARKER_RE = re.compile(
    r"(?<![A-Za-z])(?:" + _phrase("Indirect") + r"|" + _phrase("Direct")
    + r"|" + _phrase("TOTAL") + r")(?![A-Za-z])", re.I)

# Most issuers write "ordinary"; plenty write only "Shares", "ASM Shares" or
# "Fully paid shares". A bare "share" is therefore ordinary-class evidence —
# but only when nothing in the same parcel names another class, because
# "Share Performance Rights" and "Loan Shares" both contain the word.
_ORDINARY_RE = re.compile(
    _phrase("ordinar") + r"|(?<![A-Za-z])" + _phrase("share") + r"s?(?![A-Za-z])",
    re.I)
_OTHER_CLASS_RE = re.compile("|".join(_phrase(t) for t in (
    "option", "performance right", "share right", "convertible note",
    "warrant", "partly paid", "unpaid", "preference", "loan share",
    "deferred share", "restricted share",
    # NOT "escrow": "8,701,680 fully paid ordinary (escrowed)" is ordinary
    # shares under a restriction, not a different class of security.
)), re.I)

# A four-digit number in this window is a year — an option expiry, a template
# footer, a date of issue — not a share count. Rejecting it costs a genuine
# holding of, say, 2,026 shares written without a separator; that fails to
# "unknown" and a review item, which is the correct direction to fail in.
_YEAR_LO, _YEAR_HI = 1900, 2100


@dataclass(frozen=True)
class Parcel:
    quantity: int
    security: str | None      # "ordinary", "other", or None when unstated
    holder: str | None        # "direct", "indirect", "total", or None
    at: int


def _classify_security(text: str) -> str | None:
    """Which class the words name, or None when they name none unambiguously."""
    ordinary = bool(_ORDINARY_RE.search(text))
    other = bool(_OTHER_CLASS_RE.search(text))
    if ordinary and not other:
        return "ordinary"
    if other and not ordinary:
        return "other"
    return None


def _is_share_count(cell: str, m: re.Match[str]) -> bool:
    if "," in m.group(1):
        return True                      # 1,205,155 — separators say quantity
    if m.start() and cell[m.start() - 1] in ".$,":
        return False                     # the tail of $0.8016, or of 1,409,032
    value = int(m.group(1))
    return not (len(m.group(1)) == 4 and _YEAR_LO <= value <= _YEAR_HI)


def scan_parcels(cell: str | None) -> list[Parcel]:
    """Every holding the cell states, with the class and holder it states."""
    if not cell:
        return []
    spans = [m for m in _QTY_RE.finditer(cell) if _is_share_count(cell, m)]
    if not spans:
        return []

    # Two layouts appear, and a cell commits to one of them. Flagship writes
    # the class as a HEADING over a block of holder lines; CurveBeam, Terra and
    # Brightstar write it AFTER each number. Telling them apart by whether a
    # class is named before the first number keeps Flagship's "Convertible
    # Notes" heading from being read as the class of the ordinary TOTAL that
    # precedes it.
    heading_first = _classify_security(cell[:spans[0].start()]) is not None

    parcels: list[Parcel] = []
    holder: str | None = None
    heading: str | None = None
    prev_end = 0
    for i, m in enumerate(spans):
        before = cell[prev_end:m.start()]
        stop = spans[i + 1].start() if i + 1 < len(spans) else len(cell)
        after = cell[m.end():stop]

        markers = list(_MARKER_RE.finditer(before))
        if markers:
            holder = markers[-1].group(0).lower().replace(" ", "")
        named = _classify_security(before)
        if named:
            heading = named
        elif _ORDINARY_RE.search(before) or _OTHER_CLASS_RE.search(before):
            # A new heading was printed but names no class unambiguously —
            # "Share rights", which is both share-shaped and rights-shaped.
            # Carrying the PREVIOUS heading forward would file a parcel of
            # share rights under fully paid ordinary.
            heading = None

        security = heading if heading_first else _classify_security(after)
        parcels.append(Parcel(int(m.group(1).replace(",", "")), security,
                              holder, m.start()))
        prev_end = m.end()
    return parcels


def _ordinary(parcels: list[Parcel]) -> list[Parcel]:
    """Parcels the form positively calls ordinary.

    A parcel with no class stated counts as ordinary only when the cell names
    no class at all — a bare "1,234,567" in a holdings cell is the holding.
    Once the cell distinguishes classes, an unlabelled number is not one of
    them by default.
    """
    if any(p.security for p in parcels):
        return [p for p in parcels if p.security == "ordinary"]
    return list(parcels)


def _pair(before: list[Parcel], after: list[Parcel]) -> list[tuple[Parcel, Parcel]]:
    """Match before-parcels to after-parcels: by holder where the form marks
    one, otherwise by position."""
    if all(p.holder for p in before + after):
        by_holder = {p.holder: p for p in after}
        pairs = [(b, by_holder[b.holder]) for b in before if b.holder in by_holder]
        if len(pairs) == len(before):
            return pairs
    if len(before) == len(after):
        return list(zip(before, after))
    return []


_BREAKDOWN_RE = re.compile("|".join(_phrase(t) for t in (
    "as follows", "comprising", "made up of", "consisting of", "being:",
)), re.I)


def _stated_total(cell: str | None, parcels: list[Parcel]) -> Parcel | None:
    """The total a cell states before breaking it down, if it states one."""
    if not cell:
        return None
    m = _BREAKDOWN_RE.search(cell)
    if not m:
        return None
    above = [p for p in parcels if p.at < m.start()]
    return above[0] if len(above) == 1 else None


def _interest_kind(interest: str | None) -> str | None:
    """What "Direct or indirect interest" says, as a holder marker."""
    if not interest:
        return None
    text = interest.lower()
    direct = bool(re.search(r"(?<!in)direct", text))
    indirect = "indirect" in text
    if direct and indirect:
        return "total"          # a figure covering both is the printed TOTAL
    if indirect:
        return "indirect"
    if direct:
        return "direct"
    return None


def parse_holdings(
    held_before: str | None,
    held_after: str | None,
    *,
    interest: str | None = None,
    acquired: str | None = None,
    disposed: str | None = None,
) -> tuple[int | None, int | None]:
    """The ordinary-share holding before and after the change.

    Read as a pair because the form's own arithmetic — after minus before
    equals acquired minus disposed — is what identifies which of several
    parcels the notice is actually about.
    """
    b_all, a_all = scan_parcels(held_before), scan_parcels(held_after)
    b, a = _ordinary(b_all), _ordinary(a_all)

    # A cell that states a total and then breaks it down has already answered
    # the question. Pivotal Metals writes "Interest in 300,000,770 Fully paid
    # Ordinary Shares (ASX: PVE) as follows:" and then lists the three
    # holdings that make it up — summing the breakdown, or taking one line of
    # it, both contradict a figure printed on the form.
    b_total, a_total = _stated_total(held_before, b), _stated_total(held_after, a)
    if b_total is not None or a_total is not None:
        return (b_total.quantity if b_total else None,
                a_total.quantity if a_total else None)

    if len(b) <= 1 and len(a) <= 1:
        return (b[0].quantity if b else None, a[0].quantity if a else None)

    pairs = _pair(b, a)

    # 3. Reconcile against the printed movement.
    #
    # Three movements are tried, not one, because a lodgement's acquisition
    # and disposal are frequently in DIFFERENT classes: Brightstar acquires
    # 125,000 ordinary shares and lapses 53,571 performance rights on the same
    # form, so the net of the two describes no class at all. A parcel is
    # chosen only if exactly one pair matches exactly one of them.
    got, lost = parse_quantity(acquired), parse_quantity(disposed)
    moves = set()
    if got is not None or lost is not None:
        moves.add((got or 0) - (lost or 0))
    if got is not None:
        moves.add(got)
    if lost is not None:
        moves.add(-lost)
    if moves and pairs:
        fits = [p for p in pairs
                if (p[1].quantity - p[0].quantity) in moves]
        if len(fits) == 1:
            return fits[0][0].quantity, fits[0][1].quantity

    # 4. Match the holder the notice names — but only where the form marks
    # EVERY ordinary parcel.
    #
    # Aurora Labs writes an unlabelled block of 540,907 ordinary shares and
    # then a block headed "Indirect:" holding 400,000, while declaring the
    # interest "Indirect". Reading the labelled block would answer a question
    # the form never settles: the securities actually acquired sit in the
    # unlabelled block, so the declaration and the layout contradict each
    # other. An unmarked parcel is not the other kind by default; it is
    # unmarked. This lodgement is meant to leave here with nothing and go to
    # review — the human readers who built the ground truth flagged the same
    # contradiction and could not resolve it either.
    want = _interest_kind(interest)
    if want and all(p.holder for p in b) and all(p.holder for p in a):
        if pairs:
            named = [p for p in pairs if p[0].holder == want]
            if len(named) == 1:
                return named[0][0].quantity, named[0][1].quantity
        b_named = [p for p in b if p.holder == want]
        a_named = [p for p in a if p.holder == want]
        if len(b_named) == 1 or len(a_named) == 1:
            return (b_named[0].quantity if len(b_named) == 1 else None,
                    a_named[0].quantity if len(a_named) == 1 else None)

    # 5. Whichever side is unambiguous on its own still answers. One cell
    # listing several parcels is no reason to discard the other cell, which
    # lists one.
    return (b[0].quantity if len(b) == 1 else None,
            a[0].quantity if len(a) == 1 else None)
