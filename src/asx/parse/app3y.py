"""Appendix 3Y / 3Z parser (SPEC §7).

Appendix 3Y: change of director's interest notice (LR 3.19B — five business
days to lodge; verify current rule text at go-live and update the lag warning
below with a citation). Appendix 3Z: final director's interest, parsed with
the same machinery.

Multiple securities per notice become multiple rows, one per class. Amended
notices are handled downstream by apply_trades' supersession heuristics.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import psycopg

from asx.canonical.director_trades import TradeRow, apply_trades
from asx.ids.resolver import resolve_name
from asx.parse.framework import ValidationResult

# Calendar-day lag beyond which the event_date -> lodgement gap is suspicious:
# LR 3.19B's five business days spans at most ~8 calendar days over a holiday
# weekend. A longer lag is a warning (late lodgements are real), not an error.
MAX_EXPECTED_LAG_DAYS = 8

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

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "company_name", "ticker", "director_name", "date_of_change",
        "interest_nature", "indirect_detail", "is_amendment", "securities",
        "extraction_notes",
    ],
    "properties": {
        "company_name": {"type": ["string", "null"]},
        "ticker": {"type": ["string", "null"]},
        "director_name": {"type": ["string", "null"]},
        "date_of_change": {
            "type": ["string", "null"],
            "description": "Date of change of interest (ISO 8601), NOT the lodgement date",
        },
        "interest_nature": {"enum": ["direct", "indirect", "unknown", None]},
        "indirect_detail": {
            "type": ["string", "null"],
            "description": "Verbatim description of the indirect holding vehicle (trust, super fund, spouse)",
        },
        "is_amendment": {
            "type": ["boolean", "null"],
            "description": "True if the notice states it amends or replaces an earlier notice",
        },
        "securities": {"type": "array", "items": _SECURITY_ITEM},
        "extraction_notes": {"type": ["string", "null"]},
    },
}

TASK_PROMPT = (
    "Extract this Appendix 3Y (change of director's interest) or Appendix 3Z "
    "(final director's interest) notice. One securities entry per security "
    "class mentioned in the form — options and shares in one notice are "
    "separate entries. Copy the nature-of-consideration text verbatim. "
    "date_of_change is the date of the change of interest stated in the form, "
    "never the lodgement date. For an Appendix 3Z, record final holdings in "
    "held_after with quantities null."
)


def _dec(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


class App3YParser:
    name = "app3y"
    version = 1
    doc_classes = {"app_3y", "app_3z"}
    schema = SCHEMA
    task_prompt = TASK_PROMPT

    def locate(self, content: bytes) -> bytes:
        return content  # one-to-three-page standard form: locate is identity

    def validate(self, payload: dict, doc: dict) -> ValidationResult:
        result = ValidationResult()
        if not payload.get("director_name"):
            result.errors.append("director_name missing")
        securities = payload.get("securities") or []
        if not securities:
            result.errors.append("no securities entries extracted")

        is_3z = doc.get("doc_class") == "app_3z"
        change_date = None
        raw_date = payload.get("date_of_change")
        if raw_date:
            try:
                change_date = date.fromisoformat(raw_date)
            except ValueError:
                result.errors.append(f"date_of_change unparseable: {raw_date!r}")
        elif not is_3z:
            result.errors.append("date_of_change missing")

        lodged_at = doc.get("lodged_at")
        if lodged_at is None:
            result.errors.append("document has no lodgement timestamp (knowable_at undefined)")
        elif change_date is not None:
            lodged_date = lodged_at.date()
            if change_date > lodged_date:
                result.errors.append(
                    f"date_of_change {change_date} is after lodgement {lodged_date}"
                )
            elif lodged_date - change_date > timedelta(days=MAX_EXPECTED_LAG_DAYS):
                result.warnings.append(
                    f"lodgement lag {(lodged_date - change_date).days}d exceeds LR 3.19B expectation"
                )

        for i, row in enumerate(securities):
            acquired = _dec(row.get("qty_acquired"))
            disposed = _dec(row.get("qty_disposed"))
            before = _dec(row.get("held_before"))
            after = _dec(row.get("held_after"))
            for label, qty in (("qty_acquired", acquired), ("qty_disposed", disposed)):
                if qty is not None and qty < 0:
                    result.errors.append(f"securities[{i}].{label} negative")
            if not is_3z:
                # Arithmetic self-consistency: held after = held before
                # + acquired - disposed (SPEC §6). Exact, no tolerance —
                # a mismatch is a misread, an amendment, or an issuer error,
                # and all three belong in review.
                if None not in (before, after):
                    expected = before + (acquired or Decimal(0)) - (disposed or Decimal(0))
                    if expected != after:
                        result.errors.append(
                            f"securities[{i}] arithmetic: {before} + {acquired or 0} "
                            f"- {disposed or 0} = {expected} != held_after {after}"
                        )
                else:
                    result.warnings.append(
                        f"securities[{i}] arithmetic unverifiable (held before/after missing)"
                    )
        return result

    def _resolve_entity(self, conn: psycopg.Connection, payload: dict, doc: dict) -> int | None:
        if doc.get("entity_id"):
            return doc["entity_id"]
        # Ticker is a lookup input via the effective-dated listings table,
        # never a join key (Invariant 1).
        ticker = payload.get("ticker") or doc.get("ticker_as_lodged")
        on_date = doc["lodged_at"].date() if doc.get("lodged_at") else None
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

    def apply(self, conn: psycopg.Connection, doc: dict, payload: dict) -> None:
        entity_id = self._resolve_entity(conn, payload, doc)
        if entity_id is None:
            raise RuntimeError("apply called with unresolved issuer; reconcile gate failed")
        event_date_raw = payload.get("date_of_change")
        event_date = (
            date.fromisoformat(event_date_raw) if event_date_raw
            else doc["lodged_at"].date()   # 3Z without a change date: tenure end
        )
        rows = [
            TradeRow(
                entity_id=entity_id,
                person_name_raw=payload["director_name"],
                doc_id=doc["doc_id"],
                event_date=event_date,
                knowable_at=doc["lodged_at"],
                security_class=(s.get("security_class") or "unknown").strip(),
                interest_nature=payload.get("interest_nature"),
                indirect_detail=payload.get("indirect_detail"),
                qty_acquired=_dec(s.get("qty_acquired")),
                qty_disposed=_dec(s.get("qty_disposed")),
                consideration_text=s.get("consideration_text"),
                consideration_aud=_dec(s.get("consideration_aud")),
                held_before=_dec(s.get("held_before")),
                held_after=_dec(s.get("held_after")),
            )
            for s in payload.get("securities") or []
        ]
        apply_trades(conn, doc["doc_id"], rows)
