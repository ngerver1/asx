"""Shared parsing framework (SPEC §6).

Every parser is a schema + prompts + validators plugged into one pipeline:

    locate -> extract (dual-pass) -> validate -> reconcile -> score -> route

Nothing that fails validation enters canonical tables (Invariant 6). The
pipeline is split into two halves:

- evaluate_doc(): extraction + validation + parsed-zone append. Side effects
  are confined to the append-only parsed zone, so reprocess dry runs are safe.
- route_outcome(): auto-accept (canonical apply) or review queue + document
  status. Only the live pipeline and reprocess --apply call this.

Review resolutions write back through the same validation gate, are persisted
on the review item (a hand-edit that lives only in canonical would be
destroyed by the next reprocess — Invariant 3), and mark their rows with human
provenance.
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

    def apply(self, conn: psycopg.Connection, doc: dict, payload: dict,
              review_status: str = "auto") -> None:
        """Write canonical rows with full provenance (Invariant 12). Must be
        idempotent per doc_id: implementations delete their own prior rows for
        the doc before inserting. review_status records human provenance when
        the write comes from a review resolution."""
        ...

    def retract(self, conn: psycopg.Connection, doc_id: int) -> None:
        """Remove the document's canonical rows (human 'rejected'
        resolution), restoring any state its rows had displaced."""
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
class Evaluation:
    payload: dict
    alt_payload: dict
    disagreements: list[str]
    validation: ValidationResult
    confidence: float
    version_conflict: bool = False  # stored same-version record differs from fresh pass


@dataclass
class ParseOutcome:
    doc_id: int
    status: str            # 'validated' | 'review' | 'skipped'
    confidence: float
    disagreements: list[str]
    errors: list[str]
    warnings: list[str]
    payload: dict | None = None


def _load_doc(conn: psycopg.Connection, doc_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,))
        doc = cur.fetchone()
    if doc is None:
        raise KeyError(f"doc_id {doc_id} not found")
    return doc


def evaluate_doc(
    conn: psycopg.Connection,
    parser: Parser,
    doc: dict,
    extractor: StructuredExtractor,
) -> Evaluation:
    """Extract, validate, reconcile, score, and append to the parsed zone.
    Never touches documents.parse_status, review_items, or canonical tables."""
    content = parser.locate(read_document(conn, doc["doc_id"]))

    # Extract: two independent passes; disagreement routes to review (SPEC §6).
    pass_a = extractor.extract_text_pass(content, parser.schema, parser.task_prompt)
    pass_b = extractor.extract_vision_pass(content, parser.schema, parser.task_prompt)
    disagreements = field_disagreements(pass_a.payload, pass_b.payload)
    if content[:5] == b"%PDF-":
        payload, alt = pass_b.payload, pass_a.payload
    else:
        payload, alt = pass_a.payload, pass_b.payload

    validation = parser.validate(payload, doc)
    validation.errors.extend(parser.reconcile(conn, payload, doc))
    confidence = _score(disagreements, validation)

    version_conflict = _record_parse(conn, doc["doc_id"], parser, pass_a, pass_b,
                                     payload, confidence, disagreements, validation)
    return Evaluation(payload, alt, disagreements, validation, confidence,
                      version_conflict)


def load_stored_evaluation(
    conn: psycopg.Connection, parser: Parser, doc: dict
) -> Evaluation | None:
    """Rebuild an Evaluation from the parsed zone at the parser's current
    version, re-running today's validators over the stored payload. Used by
    reprocess so --apply applies exactly the payload the dry-run recorded."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT payload FROM parsed_records
               WHERE doc_id = %s AND parser_name = %s AND parser_version = %s""",
            (doc["doc_id"], parser.name, parser.version),
        )
        row = cur.fetchone()
    if row is None:
        return None
    stored = row["payload"] or {}
    payload = stored.get("primary") or {}
    text_pass = stored.get("text_pass") or {}
    vision_pass = stored.get("vision_pass") or {}
    disagreements = field_disagreements(text_pass, vision_pass)
    validation = parser.validate(payload, doc)
    validation.errors.extend(parser.reconcile(conn, payload, doc))
    alt = text_pass if payload == vision_pass else vision_pass
    return Evaluation(payload, alt, disagreements, validation,
                      _score(disagreements, validation))


def route_outcome(
    conn: psycopg.Connection,
    parser: Parser,
    doc: dict,
    ev: Evaluation,
) -> ParseOutcome:
    """Accept into canonical or queue for review, and set the document's
    parse status. The only path (besides review resolution) that writes
    canonical."""
    doc_id = doc["doc_id"]
    accept = (
        ev.validation.ok
        and not ev.disagreements
        and not ev.version_conflict
        and ev.confidence >= AUTO_ACCEPT_CONFIDENCE
        and not auto_accept_halted(conn, parser.name)
    )
    if accept:
        parser.apply(conn, doc, ev.payload)
        _set_parse_status(conn, doc_id, "validated")
        status = "validated"
    else:
        reason_bits = list(ev.validation.errors)
        reason_bits += [f"disagree:{d}" for d in ev.disagreements]
        if ev.version_conflict:
            reason_bits.append(
                f"re-extraction at unchanged parser version {parser.version} "
                f"differs from the stored parsed record — bump the parser "
                f"version and reprocess instead of overwriting"
            )
        if not reason_bits:
            reason_bits = [f"confidence {ev.confidence:.2f} below threshold or auto-accept halted"]
        with conn.cursor() as cur:
            # Idempotency: rerunning routing for a document that already has
            # an open item must not stack duplicates in the queue.
            cur.execute(
                """SELECT count(*) AS n FROM review_items
                   WHERE doc_id = %s AND kind = 'extraction'
                     AND resolved_at IS NULL AND payload->>'parser' = %s""",
                (doc_id, parser.name),
            )
            if cur.fetchone()["n"] == 0:
                cur.execute(
                    """INSERT INTO review_items (kind, doc_id, payload, reason)
                       VALUES ('extraction', %s, %s, %s)""",
                    (doc_id,
                     json.dumps({"parser": parser.name, "version": parser.version,
                                 "payload": ev.payload,
                                 "alt_payload": ev.alt_payload,
                                 "disagreements": ev.disagreements,
                                 "warnings": ev.validation.warnings}),
                     "; ".join(reason_bits)[:2000]),
                )
        _set_parse_status(conn, doc_id, "review")
        status = "review"

    conn.commit()
    return ParseOutcome(doc_id, status, ev.confidence, ev.disagreements,
                        ev.validation.errors, ev.validation.warnings, ev.payload)


def run_parser_on_doc(
    conn: psycopg.Connection,
    parser: Parser,
    doc_id: int,
    extractor: StructuredExtractor,
) -> ParseOutcome:
    """The live pipeline: evaluate then route."""
    doc = _load_doc(conn, doc_id)
    if doc["doc_class"] not in parser.doc_classes:
        return ParseOutcome(doc_id, "skipped", 0.0, [],
                            [f"doc_class {doc['doc_class']} not handled"], [])
    ev = evaluate_doc(conn, parser, doc, extractor)
    return route_outcome(conn, parser, doc, ev)


def _record_parse(conn, doc_id, parser, pass_a: ExtractionPass, pass_b: ExtractionPass,
                  payload, confidence, disagreements, validation: ValidationResult) -> bool:
    """Append to the parsed zone. Returns True on a version conflict: a record
    already exists at this (doc, parser, version) with a DIFFERENT payload,
    meaning the prompt/model changed without a version bump. The fresh
    extraction is discarded (the zone is append-only per version) and the
    caller must not apply it."""
    stored_payload = json.dumps({"primary": payload,
                                 "text_pass": pass_a.payload,
                                 "vision_pass": pass_b.payload})
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO parsed_records
                 (doc_id, parser_name, parser_version, model_id, prompt_hash,
                  payload, confidence, validation, passes_agree)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (doc_id, parser_name, parser_version) DO NOTHING
               RETURNING parsed_id""",
            (doc_id, parser.name, parser.version, pass_a.model_id,
             prompt_hash(parser.task_prompt), stored_payload, confidence,
             json.dumps({"errors": validation.errors, "warnings": validation.warnings,
                         "disagreements": disagreements}),
             not disagreements),
        )
        if cur.fetchone() is not None:
            return False
        cur.execute(
            """SELECT payload FROM parsed_records
               WHERE doc_id = %s AND parser_name = %s AND parser_version = %s""",
            (doc_id, parser.name, parser.version),
        )
        existing = cur.fetchone()["payload"] or {}
    return existing.get("primary") != payload


def _set_parse_status(conn: psycopg.Connection, doc_id: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE documents SET parse_status = %s WHERE doc_id = %s",
                    (status, doc_id))


def resolve_review_item(
    conn: psycopg.Connection,
    parser: Parser | None,   # None only for non-extraction items being rejected
    item_id: int,
    resolution: str,
    corrected_payload: dict | None = None,
    note: str = "",
) -> ValidationResult | None:
    """Resolve a review item. 'accepted'/'corrected' resolutions re-run the
    same validation gate as automated extraction; a correction that still
    fails validation is refused rather than written to canonical. The applied
    payload is persisted on the item and the rows carry human provenance."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review_items WHERE item_id = %s", (item_id,))
        item = cur.fetchone()
    if item is None:
        raise KeyError(f"review item {item_id} not found")
    if item["resolved_at"] is not None:
        raise ValueError(f"review item {item_id} already resolved")

    validation: ValidationResult | None = None
    applied_payload: dict | None = None
    if resolution in ("accepted", "corrected"):
        stored = item["payload"] or {}
        payload = corrected_payload if corrected_payload is not None else stored.get("payload")
        doc = _load_doc(conn, item["doc_id"])
        validation = parser.validate(payload, doc)
        validation.errors.extend(parser.reconcile(conn, payload, doc))
        if not validation.ok:
            return validation  # refused: fix the correction, not the gate
        review_status = "human_corrected" if corrected_payload is not None else "human_accepted"
        parser.apply(conn, doc, payload, review_status=review_status)
        _set_parse_status(conn, item["doc_id"], "validated")
        applied_payload = payload
    elif resolution == "rejected":
        # Only extraction items speak for the document itself; rejecting a
        # resolution or reconciliation item closes that item without touching
        # the document's status or its canonical rows.
        if item["doc_id"] is not None and item["kind"] == "extraction":
            # A rejected document's rows must not keep feeding signals; the
            # retraction also reactivates any notice this doc had superseded.
            if parser is not None:
                parser.retract(conn, item["doc_id"])
            _set_parse_status(conn, item["doc_id"], "rejected")
    else:
        raise ValueError(f"unknown resolution {resolution!r}")

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE review_items
               SET resolved_at = now(), resolution = %s, resolver_note = %s,
                   payload = coalesce(payload, '{}'::jsonb)
                             || jsonb_build_object('applied_payload', %s::jsonb)
               WHERE item_id = %s""",
            (resolution, note,
             json.dumps(applied_payload) if applied_payload is not None else None,
             item_id),
        )
    conn.commit()
    return validation
