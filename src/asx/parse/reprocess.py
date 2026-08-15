"""Reprocessing as a first-class operation (SPEC §3).

Re-parses raw documents with the current parser version, writes new
parsed-zone versions (never overwriting old ones), and produces a diff report
of canonical changes for human review before applying. This is the only path
for fixing systematic parse errors — canonical tables are never hand-edited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import psycopg

from asx.parse.framework import Parser, run_parser_on_doc
from asx.parse.llm import StructuredExtractor, field_disagreements


@dataclass
class ReprocessReport:
    parser: str
    version: int
    applied: bool
    docs: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        changed = [d for d in self.docs if d["changed_fields"]]
        lines = [
            f"reprocess {self.parser} v{self.version} — "
            f"{len(self.docs)} docs, {len(changed)} with canonical-affecting changes, "
            f"{'APPLIED' if self.applied else 'DRY RUN (rerun with --apply after review)'}",
        ]
        for d in changed:
            lines.append(f"  doc {d['doc_id']}: {d['new_status']}; changed: {', '.join(d['changed_fields'])}")
        return "\n".join(lines)


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
                 AND parse_status <> 'not_applicable'
                 AND (%s::date IS NULL OR lodged_at >= %s)
               ORDER BY doc_id""",
            (list(parser.doc_classes), since, since),
        )
        doc_ids = [r["doc_id"] for r in cur.fetchall()]

    report = ReprocessReport(parser.name, parser.version, apply)
    for doc_id in doc_ids:
        # Prior extraction, latest version of this parser, for the diff.
        with conn.cursor() as cur:
            cur.execute(
                """SELECT payload FROM parsed_records
                   WHERE doc_id = %s AND parser_name = %s
                   ORDER BY parser_version DESC LIMIT 1""",
                (doc_id, parser.name),
            )
            prior = cur.fetchone()
        prior_payload = (prior["payload"] or {}).get("primary") if prior else None

        outcome = run_parser_on_doc(
            conn, parser, doc_id, extractor, apply_canonical=apply
        )

        with conn.cursor() as cur:
            cur.execute(
                """SELECT payload FROM parsed_records
                   WHERE doc_id = %s AND parser_name = %s AND parser_version = %s""",
                (doc_id, parser.name, parser.version),
            )
            new = cur.fetchone()
        new_payload = (new["payload"] or {}).get("primary") if new else None

        changed = []
        if prior_payload is not None and new_payload is not None:
            changed = field_disagreements(prior_payload, new_payload)
        elif prior_payload is None:
            changed = ["<first parse>"]
        report.docs.append({
            "doc_id": doc_id,
            "new_status": outcome.status,
            "changed_fields": changed,
        })
    return report
