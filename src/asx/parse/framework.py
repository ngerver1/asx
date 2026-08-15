"""Shared parsing framework (SPEC §6).

Every parser is a schema + prompts + validators plugged into one pipeline:

    locate -> extract (dual-pass) -> validate -> reconcile -> score -> route

Nothing that fails validation enters canonical tables (Invariant 6); the
route stage either auto-accepts (and applies canonical writes) or queues for
human review. Review resolutions write back through the same validation gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

import psycopg

from asx.parse.llm import (
    ExtractionPass,
    StructuredExtractor,
    field_disagreements,
    prompt_hash,
)
from asx.raw.store import read_document

AUTO_ACCEPT_CONFIDENCE = 0.9
# Queue older than two weeks halts the affected parser's auto-accept path:
# better to stop than to let the threshold quietly become the only gate.
REVIEW_SLA = timedelta(days=14)


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class Parser(Protocol):
    name: str
    version: int
    doc_classes: set[str]
    schema: dict
    task_prompt: str

    def locate(self, content: bytes) -> bytes:
        """Return the slice of the document the extractor should see. Identity
        for one-page standard forms; the hard half of the problem for annual
        reports (SPEC §9)."""
        ...

    def validate(self, payload: dict, doc: dict) -> ValidationResult: ...

    def reconcile(self, conn: psycopg.Connection, payload: dict, doc: dict) -> list[str]:
        """Cross-checks against independent sources; returns error strings."""
        ...

    def apply(self, conn: psycopg.Connection, doc: dict, payload: dict) -> None:
        """Write canonical rows with full provenance (Invariant 12). Must be
        idempotent per doc_id: implementations delete their own prior rows for
        the doc before inserting."""
        ...


def auto_accept_halted(conn: psycopg.Connection, parser_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS n FROM review_items
               WHERE resolved_at IS NULL
                 AND created_at < %s
                 AND (payload->>'parser' = %s OR payload->>'parser' IS NULL)""",
            (datetime.now(timezone.utc) - REVIEW_SLA, parser_name),
        )
        return cur.fetchone()["n"] > 0


def _score(disagreements: list[str], validation: ValidationResult) -> float:
    if validation.errors:
        return 0.0
    confidence = 1.0
    if disagreements:
        confidence = min(confidence, 0.5)
    confidence -= 0.1 * len(validation.warnings)
    return max(confidence, 0.0)


@dataclass
class ParseOutcome:
    doc_id: int
    status: str            # 'validated' | 'review' | 'skipped'
    confidence: float
    disagreements: list[str]
    errors: list[str]
    warnings: list[str]


def run_parser_on_doc(
    conn: psycopg.Connection,
    parser: Parser,
    doc_id: int,
    extractor: StructuredExtractor,
    *,
    apply_canonical: bool = True,
) -> ParseOutcome:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,))
        doc = cur.fetchone()
    if doc is None:
        raise KeyError(f"doc_id {doc_id} not found")
    if doc["doc_class"] not in parser.doc_classes:
        return ParseOutcome(doc_id, "skipped", 0.0, [], [f"doc_class {doc['doc_class']} not handled"], [])

    content = parser.locate(read_document(conn, doc_id))

    # Extract: two independent passes; disagreement routes to review (SPEC §6).
    pass_a = extractor.extract_text_pass(content, parser.schema, parser.task_prompt)
    pass_b = extractor.extract_vision_pass(content, parser.schema, parser.task_prompt)
    disagreements = field_disagreements(pass_a.payload, pass_b.payload)
    payload = pass_b.payload if content[:5] == b"%PDF-" else pass_a.payload

    validation = parser.validate(payload, doc)
    recon_errors = parser.reconcile(conn, payload, doc)
    validation.errors.extend(recon_errors)
    confidence = _score(disagreements, validation)

    _record_parse(conn, doc_id, parser, pass_a, pass_b, payload, confidence,
                  disagreements, validation)

    accept = (
        validation.ok
        and not disagreements
        and confidence >= AUTO_ACCEPT_CONFIDENCE
        and not auto_accept_halted(conn, parser.name)
    )
    if accept:
        if apply_canonical:
            parser.apply(conn, doc, payload)
        _set_parse_status(conn, doc_id, "validated")
        status = "validated"
    else:
        reason_bits = validation.errors + [f"disagree:{d}" for d in disagreements]
        if not reason_bits:
            reason_bits = [f"confidence {confidence:.2f} below threshold or auto-accept halted"]
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO review_items (kind, doc_id, payload, reason)
                   VALUES ('extraction', %s, %s, %s)""",
                (doc_id,
                 json.dumps({"parser": parser.name, "version": parser.version,
                             "payload": payload,
                             "alt_payload": pass_a.payload if payload is pass_b.payload else pass_b.payload,
                             "disagreements": disagreements,
                             "warnings": validation.warnings}),
                 "; ".join(reason_bits)[:2000]),
            )
        _set_parse_status(conn, doc_id, "review")
        status = "review"

    conn.commit()
    return ParseOutcome(doc_id, status, confidence, disagreements,
                        validation.errors, validation.warnings)


def _record_parse(conn, doc_id, parser, pass_a: ExtractionPass, pass_b: ExtractionPass,
                  payload, confidence, disagreements, validation: ValidationResult):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO parsed_records
                 (doc_id, parser_name, parser_version, model_id, prompt_hash,
                  payload, confidence, validation, passes_agree)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (doc_id, parser_name, parser_version) DO NOTHING""",
            (doc_id, parser.name, parser.version, pass_a.model_id,
             prompt_hash(parser.task_prompt),
             json.dumps({"primary": payload,
                         "text_pass": pass_a.payload,
                         "vision_pass": pass_b.payload}),
             confidence,
             json.dumps({"errors": validation.errors, "warnings": validation.warnings,
                         "disagreements": disagreements}),
             not disagreements),
        )


def _set_parse_status(conn: psycopg.Connection, doc_id: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE documents SET parse_status = %s WHERE doc_id = %s",
                    (status, doc_id))


def resolve_review_item(
    conn: psycopg.Connection,
    parser: Parser,
    item_id: int,
    resolution: str,
    corrected_payload: dict | None = None,
    note: str = "",
) -> ValidationResult | None:
    """Resolve a review item. 'accepted'/'corrected' resolutions re-run the
    same validation gate as automated extraction; a correction that still
    fails validation is refused rather than written to canonical."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review_items WHERE item_id = %s", (item_id,))
        item = cur.fetchone()
    if item is None:
        raise KeyError(f"review item {item_id} not found")
    if item["resolved_at"] is not None:
        raise ValueError(f"review item {item_id} already resolved")

    validation: ValidationResult | None = None
    if resolution in ("accepted", "corrected"):
        stored = item["payload"] or {}
        payload = corrected_payload if corrected_payload is not None else stored.get("payload")
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM documents WHERE doc_id = %s", (item["doc_id"],))
            doc = cur.fetchone()
        validation = parser.validate(payload, doc)
        validation.errors.extend(parser.reconcile(conn, payload, doc))
        if not validation.ok:
            return validation  # refused: fix the correction, not the gate
        parser.apply(conn, doc, payload)
        _set_parse_status(conn, item["doc_id"], "validated")
    elif resolution == "rejected":
        if item["doc_id"] is not None:
            _set_parse_status(conn, item["doc_id"], "rejected")
    else:
        raise ValueError(f"unknown resolution {resolution!r}")

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE review_items
               SET resolved_at = now(), resolution = %s, resolver_note = %s
               WHERE item_id = %s""",
            (resolution, note, item_id),
        )
    conn.commit()
    return validation
