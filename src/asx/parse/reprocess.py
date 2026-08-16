"""Reprocessing as a first-class operation (SPEC §3).

Re-parses raw documents with the current parser version, writes new
parsed-zone versions (never overwriting old ones), and produces a diff report
of canonical changes for human review before applying. This is the only path
for fixing systematic parse errors — canonical tables are never hand-edited.

Semantics:
- A dry run (default) extracts and appends to the parsed zone only. It never
  flips parse_status, never files review items, never touches canonical.
- --apply routes the RECORDED parsed payload (the one the dry-run diff showed)
  through the normal accept/review gate. Re-extraction happens only when no
  record exists at the current parser version, so changing prompts or models
  requires a version bump to take effect — by design.
- Documents a human explicitly rejected are excluded. Documents whose current
  state came from a human review resolution are never silently overwritten:
  they are skipped with a note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import psycopg

from asx.parse.framework import (
    Parser,
    evaluate_doc,
    load_stored_evaluation,
    route_outcome,
)
from asx.parse.llm import StructuredExtractor, field_disagreements


@dataclass
class ReprocessReport:
    parser: str
    version: int
    applied: bool
    docs: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        changed = [d for d in self.docs if d["changed_fields"]]
        skipped = [d for d in self.docs if d["status"] == "skipped_human_resolved"]
        lines = [
            f"reprocess {self.parser} v{self.version} — "
            f"{len(self.docs)} docs, {len(changed)} with canonical-affecting changes, "
            f"{len(skipped)} skipped (human-resolved), "
            f"{'APPLIED' if self.applied else 'DRY RUN (rerun with --apply after review)'}",
        ]
        for d in changed:
            lines.append(f"  doc {d['doc_id']}: {d['status']}; changed: {', '.join(d['changed_fields'])}")
        for d in skipped:
            lines.append(f"  doc {d['doc_id']}: human-resolved — re-review manually if the "
                         f"new parser version should replace the human correction")
        return "\n".join(lines)


def _human_resolved(conn: psycopg.Connection, doc_id: int, parser_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS n FROM review_items
               WHERE doc_id = %s AND resolution IN ('accepted', 'corrected')
                 AND payload->>'parser' = %s""",
            (doc_id, parser_name),
        )
        return cur.fetchone()["n"] > 0


def reprocess(
    conn: psycopg.Connection,
    parser: Parser,
    extractor: StructuredExtractor,
    *,
    since: date | None = None,
    apply: bool = False,
) -> ReprocessReport:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT doc_id FROM documents
               WHERE doc_class = ANY(%s)
                 AND parse_status NOT IN ('not_applicable', 'rejected')
                 AND (%s::date IS NULL OR lodged_at >= %s)
               ORDER BY doc_id""",
            (list(parser.doc_classes), since, since),
        )
        doc_ids = [r["doc_id"] for r in cur.fetchall()]

    report = ReprocessReport(parser.name, parser.version, apply)
    for doc_id in doc_ids:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,))
            doc = cur.fetchone()
            # Prior extraction at the latest EARLIER version, for the diff.
            cur.execute(
                """SELECT payload FROM parsed_records
                   WHERE doc_id = %s AND parser_name = %s AND parser_version < %s
                   ORDER BY parser_version DESC LIMIT 1""",
                (doc_id, parser.name, parser.version),
            )
            prior = cur.fetchone()
        prior_payload = (prior["payload"] or {}).get("primary") if prior else None

        ev = load_stored_evaluation(conn, parser, doc)
        if ev is None:
            ev = evaluate_doc(conn, parser, doc, extractor)
            conn.commit()  # parsed-zone append is the dry run's only write

        changed = (
            field_disagreements(prior_payload, ev.payload)
            if prior_payload is not None else ["<first parse at this version>"]
        )

        if not apply:
            status = "dry_run"
        elif _human_resolved(conn, doc_id, parser.name):
            status = "skipped_human_resolved"
        else:
            status = route_outcome(conn, parser, doc, ev).status

        report.docs.append({
            "doc_id": doc_id,
            "status": status,
            "changed_fields": changed,
        })
    return report
