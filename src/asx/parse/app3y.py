"""Appendix 3Y / 3Z parser (SPEC §7).

Appendix 3Y: change of director's interest notice (LR 3.19B — five business
days to lodge; verify current rule text at go-live and update the lag warning
below with a citation). Appendix 3Z: final director's interest, parsed with
the same machinery.

Multiple securities per notice become multiple rows, one per class. Amended
notices are handled downstream by apply_trades' supersession heuristics.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import psycopg

from asx.canonical.director_trades import TradeRow, apply_trades, retract_trades
from asx.ids.market_time import market_date
from asx.ids.resolver import resolve_name
from asx.parse.framework import ARITHMETIC_UNVERIFIABLE, ValidationResult

# Calendar-day lag beyond which the event_date -> lodgement gap is suspicious:
# LR 3.19B's five business days spans at most ~8 calendar days over a holiday
# weekend. A longer lag is a warning (late lodgements are real), not an error.
MAX_EXPECTED_LAG_DAYS = 8

# Re-exported: the framework owns this string, because it is the framework
# that treats an unchecked notice as an uncorroborated reading.

_SECURITY_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "security_class", "qty_acquired", "qty_disposed", "consideration_text",
        "consideration_aud", "held_before", "held_after",
    ],
    "properties": {
        "security_class": {
            "type": ["string", "null"],
            "description": "Class of securities as printed, e.g. 'Ordinary shares', 'Unlisted options'",
        },
        "qty_acquired": {"type": ["number", "null"]},
        "qty_disposed": {"type": ["number", "null"]},
        "consideration_text": {
            "type": ["string", "null"],
            "description": "Nature of consideration verbatim, e.g. 'On-market purchase $10,000'",
        },
        "consideration_aud": {
            "type": ["number", "null"],
            "description": "Total AUD consideration if the form prints a number; null otherwise",
        },
        "held_before": {"type": ["number", "null"]},
        "held_after": {"type": ["number", "null"]},
    },
}

_NOTICE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["director_name", "date_of_change", "interest_nature",
                 "indirect_detail", "securities"],
    "properties": {
        "director_name": {"type": ["string", "null"]},
        "date_of_change": {
            "type": ["string", "null"],
            "description": "Date of change of interest (ISO 8601), NOT the lodgement date",
        },
        # A form may state several dates in the one box. What it stated is
        # kept, and date_basis says whether date_of_change was printed or
        # estimated between them (migration 0022).
        "date_basis": {
            "enum": ["stated", "range_midpoint", "enumeration_midpoint", None]},
        "dates_stated": {
            "type": ["array", "null"], "items": {"type": "string"}},
        # 'both' is a real category, not a blend: 58 of the 209 forms in the
        # captured corpus state the interest as direct AND indirect, for a
        # director holding some shares personally and some through a trust.
        "interest_nature": {"enum": ["direct", "indirect", "both", "unknown", None]},
        "indirect_detail": {
            "type": ["string", "null"],
            "description": "Verbatim description of the indirect holding vehicle (trust, super fund, spouse)",
        },
        "securities": {"type": "array", "items": _SECURITY_ITEM},
    },
}

# One PDF, several directors.
#
# A tenth of real lodgements carry more than one complete Appendix 3Y — up to
# four directors, each with their own Part 1, their own date of change and
# their own quantities. A schema with one director_name at the top can only
# record the first, which would drop 15 of the 21 director notices in the six
# multi-director files captured so far.
#
# It would also drop them in the worst possible place. The cluster-buy screen
# exists to find SEVERAL directors transacting in the same company at the same
# time, and a board filing its notices in one PDF is exactly that event — so
# reading one director would turn the strongest available signal into the
# weakest. director_trades already keys the person per row; only this schema
# was singular.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["company_name", "ticker", "is_amendment", "notices",
                 "extraction_notes"],
    "properties": {
        "company_name": {"type": ["string", "null"]},
        "ticker": {"type": ["string", "null"]},
        "is_amendment": {
            "type": ["boolean", "null"],
            "description": "True if the notice states it amends or replaces an earlier notice",
        },
        "notices": {"type": "array", "items": _NOTICE},
        "extraction_notes": {"type": ["string", "null"]},
    },
}


TASK_PROMPT = (
    "Extract this Appendix 3Y (change of director's interest) or Appendix 3Z "
    "(final director's interest) lodgement. ONE NOTICES ENTRY PER DIRECTOR: a "
    "single PDF often contains several complete forms, one per director, and "
    "every one of them must be returned. One securities entry per security "
    "class mentioned in the form — options and shares in one notice are "
    "separate entries. Copy the nature-of-consideration text verbatim. "
    "date_of_change is the date of the change of interest stated in the form, "
    "never the lodgement date. For an Appendix 3Z, record final holdings in "
    "held_after with quantities null."
)


def _interest_nature(printed: str | None) -> str | None:
    """What the "Direct or indirect interest" cell says.

    'both' is a category the form genuinely states — 58 of the 209 captured
    forms say direct AND indirect — and collapsing it into 'unknown' would
    discard a stated fact on a quarter of the corpus. A cell naming neither
    is 'unknown', never a substantive default (Invariant 8).
    """
    if not printed:
        return None
    text = printed.lower()
    indirect = "indirect" in text
    direct = bool(re.search(r"(?<!in)direct", text))
    if direct and indirect:
        return "both"
    if indirect:
        return "indirect"
    if direct:
        return "direct"
    return "unknown"


def _dec(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


class App3YParser:
    name = "app3y"
    # v2: one lodgement holds several directors' notices, not one.
    # v3: a "Date of change" box may state several dates. They are kept, and a
    #     date close enough to estimate between is estimated and labelled
    #     (migration 0022); month abbreviations and dotted dates now read.
    # v4: holdings are read for the class that CHANGED, not only for ordinary
    #     shares. A third of the corpus reports an options or performance-
    #     rights change beside an unchanged ordinary parcel; those notices had
    #     their holdings nulled, so nothing could check them and they sat in
    #     review permanently. The bump matters operationally as well as for
    #     the record: reprocess re-extracts only where no record exists at the
    #     current version, so without it the fix would never reach a database
    #     that already holds v3 readings.
    # v5: a holdings cell reading only "Nil" is the issuer stating a holding of
    #     nothing, and is read as zero. Only when the cell is nothing else —
    #     "Direct Nil Indirect 5,901,982" states two parcels, one empty.
    version = 5
    doc_classes = {"app_3y", "app_3z"}
    schema = SCHEMA
    task_prompt = TASK_PROMPT

    def read_rules(self, content: bytes) -> dict:
        """Read the lodgement with the rules reader — no model, no API key.

        Every value here is located on the page. Nothing is inferred, summed,
        or defaulted: a cell the reader cannot attribute comes back as null
        and the notice goes to review, which is the correct direction to fail
        in (Invariant 8).
        """
        from asx.parse import app3y_rules as rules
        from asx.parse.text import document_text

        forms = rules.extract_all(document_text(content))
        notices, notes = [], []
        for i, form in enumerate(forms):
            if form.scrambled:
                # The PDF emitted its table cells out of order, so no value can
                # be trusted to belong to the label it was found under — not
                # even the ones that look right. Recorded, never extracted.
                notes.append(f"form {i}: cells extracted out of order, not read")
                continue
            notices.append(self._notice_from_rules(form))

        company = next((f.get("entity_name") for f in forms if f.get("entity_name")), None)
        unreadable = sorted({k for f in forms for k in f.unreadable})
        if unreadable:
            notes.append("labels not found: " + ", ".join(unreadable))
        return {
            "company_name": company,
            # A 3Y prints the issuer's name and ABN, not its ticker. The
            # document's own ticker_as_lodged is the lookup input downstream.
            "ticker": None,
            "is_amendment": None,
            "notices": notices,
            "extraction_notes": "; ".join(notes) or None,
        }

    @staticmethod
    def _notice_from_rules(form) -> dict:
        from asx.parse import app3y_rules as rules

        # A single stated date is used as printed. Several close together are
        # reduced to their midpoint and labelled an estimate, so the notice
        # yields a row instead of nothing; several far apart still yield no
        # date, because there is no honest point between them.
        change_date, date_basis, stated_dates = rules.resolve_change_date(
            form.get("date_of_change"))

        held_before, held_after = rules.parse_holdings(
            form.get("held_before"),
            form.get("held_at_ceasing") if form.form == "app_3z" else form.get("held_after"),
            interest=form.get("interest_nature"),
            acquired=form.get("qty_acquired"),
            disposed=form.get("qty_disposed"),
        )
        # A form listing several classes enumerates every cell in step, so
        # the ordinary-class figures are the ones under the ordinary-class
        # marker. Where they cannot be paired, this returns nothing rather
        # than the first number in the cell.
        security_class, (acquired, disposed, consideration) = rules.select_by_class(
            form.get("security_class"),
            form.get("qty_acquired"),
            form.get("qty_disposed"),
            form.get("consideration"),
        )
        # The holdings read above are the ORDINARY parcel — that is the only
        # class the parcel reader attributes. So they belong beside the
        # quantities only when the form says the change was in that class.
        #
        # A third of the corpus reports a change in options or performance
        # rights while stating the ordinary holding unchanged. Pairing the two
        # produces arithmetic like "1,412,912 - 6,000,000 = -4,587,088 vs
        # 1,412,912" and files it as a failed reconciliation, which blames the
        # reader or the issuer for a subtraction nobody performed: the options
        # went, the shares did not. Where the changed class is not ordinary,
        # the holdings are left null and the notice goes to review as
        # unverified — which is what it is, not what it was being called.
        changed_ordinary = rules.security_is_ordinary(security_class)

        # Read the holding in the class that actually MOVED, where the form's
        # own arithmetic confirms it. This is what rescues the third of the
        # corpus that reports an options or performance-rights change beside
        # an unchanged ordinary parcel: those notices had their holdings
        # nulled and could never be verified against anything, so they sat in
        # review permanently. Nothing uncorroborated comes back from here — it
        # returns None rather than a figure the printed movement does not
        # support, and None lands exactly where the notice already was.
        changed = rules.holdings_for_changed_class(
            form.get("held_before"),
            form.get("held_at_ceasing") if form.form == "app_3z" else form.get("held_after"),
            security_class=security_class,
            acquired=form.get("qty_acquired"),
            disposed=form.get("qty_disposed"),
            interest=form.get("interest_nature"),
        )
        # Two separate questions, and conflating them reads the wrong quantity.
        # `changed_ordinary` decides WHICH CLASS's numbers to pull out of the
        # acquired/disposed cells, and stays exactly what the form says.
        # `holdings_known` decides only whether the before/after pair may be
        # reported — true when the class-aware read succeeded, whatever class
        # that was.
        holdings_known = changed_ordinary
        if changed is not None:
            _, held_before, held_after = changed
            holdings_known = True
        return {
            "director_name": form.get("director_name"),
            "date_of_change": change_date,
            "date_basis": date_basis,
            "dates_stated": stated_dates or None,
            "interest_nature": _interest_nature(form.get("interest_nature")),
            "indirect_detail": form.get("indirect_detail"),
            "securities": [{
                "security_class": security_class,
                "qty_acquired": rules.quantity_of_class(
                    acquired, ordinary=changed_ordinary),
                "qty_disposed": rules.quantity_of_class(
                    disposed, ordinary=changed_ordinary),
                "consideration_text": form.get("nature_of_change") or consideration,
                "consideration_aud": rules.parse_money(consideration),
                "held_before": held_before if holdings_known else None,
                "held_after": held_after if holdings_known else None,
            }],
        }

    def locate(self, content: bytes) -> bytes:
        return content  # one-to-three-page standard form: locate is identity

    def validate(self, payload: dict, doc: dict) -> ValidationResult:
        result = ValidationResult()
        notices = payload.get("notices") or []
        if not notices:
            result.errors.append("no director notices extracted")
        for n, notice in enumerate(notices):
            self._validate_notice(result, notice, doc, f"notices[{n}]")
        return result

    def _validate_notice(self, result: ValidationResult, payload: dict,
                         doc: dict, where: str) -> None:
        if not payload.get("director_name"):
            result.errors.append(f"{where}.director_name missing")
        securities = payload.get("securities") or []
        if not securities:
            result.errors.append(f"{where}: no securities entries extracted")

        is_3z = doc.get("doc_class") == "app_3z"
        change_date = None
        raw_date = payload.get("date_of_change")
        if raw_date:
            try:
                change_date = date.fromisoformat(raw_date)
            except ValueError:
                result.errors.append(f"{where}.date_of_change unparseable: {raw_date!r}")
        elif not is_3z:
            result.errors.append(f"{where}.date_of_change missing")

        lodged_at = doc.get("lodged_at")
        if lodged_at is None:
            result.errors.append("document has no lodgement timestamp (knowable_at undefined)")
        elif change_date is not None:
            # Sydney market date, not UTC: a pre-open Sydney lodgement is the
            # previous UTC day, and comparing against the UTC date would
            # reject every same-day disclosure (SPEC §3).
            lodged_date = market_date(lodged_at)
            if change_date > lodged_date:
                result.errors.append(
                    f"{where}.date_of_change {change_date} is after lodgement {lodged_date}"
                )
            elif lodged_date - change_date > timedelta(days=MAX_EXPECTED_LAG_DAYS):
                result.warnings.append(
                    f"{where} lodgement lag {(lodged_date - change_date).days}d "
                    f"exceeds LR 3.19B expectation"
                )

        for i, row in enumerate(securities):
            acquired = _dec(row.get("qty_acquired"))
            disposed = _dec(row.get("qty_disposed"))
            before = _dec(row.get("held_before"))
            after = _dec(row.get("held_after"))
            for label, qty in (("qty_acquired", acquired), ("qty_disposed", disposed)):
                if qty is not None and qty < 0:
                    result.errors.append(f"{where}.securities[{i}].{label} negative")
            if not is_3z:
                # Arithmetic self-consistency: held after = held before
                # + acquired - disposed (SPEC §6). Exact, no tolerance —
                # a mismatch is a misread, an amendment, or an issuer error,
                # and all three belong in review.
                #
                # For a rules extraction this is not merely a check, it is the
                # ONLY corroboration there is: the LLM path scores confidence
                # by two independent readings agreeing, and a single reading
                # has no such witness. What stands in its place is stronger —
                # not two guesses agreeing, but a sum printed on the document
                # itself. See ARITHMETIC_UNVERIFIABLE below, which the
                # framework treats as an uncorroborated reading.
                if None not in (before, after):
                    expected = before + (acquired or Decimal(0)) - (disposed or Decimal(0))
                    if expected != after:
                        result.errors.append(
                            f"{where}.securities[{i}] arithmetic: {before} + {acquired or 0} "
                            f"- {disposed or 0} = {expected} != held_after {after}"
                        )
                else:
                    result.warnings.append(
                        f"{ARITHMETIC_UNVERIFIABLE} {where}.securities[{i}] "
                        f"(held before/after missing)"
                    )

    def _resolve_entity(self, conn: psycopg.Connection, payload: dict, doc: dict) -> int | None:
        if doc.get("entity_id"):
            return doc["entity_id"]
        # Ticker is a lookup input via the effective-dated listings table,
        # never a join key (Invariant 1).
        ticker = payload.get("ticker") or doc.get("ticker_as_lodged")
        on_date = market_date(doc["lodged_at"]) if doc.get("lodged_at") else None
        if ticker and on_date:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT entity_id FROM listings
                       WHERE exchange = 'ASX' AND ticker = %s
                         AND valid_from <= %s AND (valid_to IS NULL OR valid_to >= %s)""",
                    (ticker.upper(), on_date, on_date),
                )
                rows = cur.fetchall()
            if len(rows) == 1:
                return rows[0]["entity_id"]
        if payload.get("company_name"):
            resolution = resolve_name(conn, payload["company_name"],
                                      source_doc_id=doc["doc_id"])
            if resolution.entity_id is not None:
                return resolution.entity_id
        return None

    def reconcile(self, conn: psycopg.Connection, payload: dict, doc: dict) -> list[str]:
        if self._resolve_entity(conn, payload, doc) is None:
            return ["issuer could not be resolved to an entity_id"]
        return []

    def apply(self, conn: psycopg.Connection, doc: dict, payload: dict,
              review_status: str = "auto") -> None:
        entity_id = self._resolve_entity(conn, payload, doc)
        if entity_id is None:
            raise RuntimeError("apply called with unresolved issuer; reconcile gate failed")
        rows: list[TradeRow] = []
        for notice in payload.get("notices") or []:
            event_date_raw = notice.get("date_of_change")
            event_date = (
                date.fromisoformat(event_date_raw) if event_date_raw
                else market_date(doc["lodged_at"])   # 3Z without a change date: tenure end
            )
            rows.extend(
                TradeRow(
                    entity_id=entity_id,
                    person_name_raw=notice["director_name"],
                    doc_id=doc["doc_id"],
                    event_date=event_date,
                    event_date_basis=notice.get("date_basis") or "stated",
                    event_dates_stated=[date.fromisoformat(d)
                                        for d in (notice.get("dates_stated") or [])] or None,
                    knowable_at=doc["lodged_at"],
                    security_class=(s.get("security_class") or "unknown").strip(),
                    interest_nature=notice.get("interest_nature"),
                    indirect_detail=notice.get("indirect_detail"),
                    qty_acquired=_dec(s.get("qty_acquired")),
                    qty_disposed=_dec(s.get("qty_disposed")),
                    consideration_text=s.get("consideration_text"),
                    consideration_aud=_dec(s.get("consideration_aud")),
                    held_before=_dec(s.get("held_before")),
                    held_after=_dec(s.get("held_after")),
                )
                for s in notice.get("securities") or []
            )
        apply_trades(conn, doc["doc_id"], rows, review_status=review_status)

    def retract(self, conn: psycopg.Connection, doc_id: int) -> None:
        retract_trades(conn, doc_id)
