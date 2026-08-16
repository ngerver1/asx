"""CLI entrypoints. Every command is idempotent so cron reruns are always
safe (SPEC §3)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from asx import db
from asx.parse.registry import PARSERS


def _get_parser(name: str):
    if name not in PARSERS:
        sys.exit(f"unknown parser {name!r}; known: {', '.join(sorted(PARSERS))}")
    return PARSERS[name]()


def cmd_migrate(_args) -> None:
    with db.connect() as conn:
        ran = db.migrate(conn)
    print(f"applied {len(ran)} migration(s)" + (": " + ", ".join(ran) if ran else ""))


def cmd_ingest(args) -> None:
    import os

    from asx.ingest.pipeline import run_ingest
    from asx.ingest.sources import FileDropSource

    # Rules-first, LLM-fallback classification (SPEC §5.3): the fallback is
    # wired whenever credentials exist; --no-llm forces rules-only.
    llm = None
    if not args.no_llm and (os.environ.get("ANTHROPIC_API_KEY")
                            or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        from asx.ingest.classifier import make_llm_classifier

        llm = make_llm_classifier()

    with db.connect() as conn:
        stats = run_ingest(conn, FileDropSource(Path(args.drop_dir)), llm_classifier=llm)
    print(json.dumps(stats))


def cmd_parse(args) -> None:
    from asx.parse.framework import run_parser_on_doc
    from asx.parse.llm import StructuredExtractor

    parser = _get_parser(args.parser)
    extractor = StructuredExtractor()
    with db.connect() as conn:
        if args.doc_id:
            doc_ids = [args.doc_id]
        else:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT doc_id FROM documents
                       WHERE parse_status = 'unparsed' AND doc_class = ANY(%s)
                       ORDER BY doc_id LIMIT %s""",
                    (list(parser.doc_classes), args.limit),
                )
                doc_ids = [r["doc_id"] for r in cur.fetchall()]
        for doc_id in doc_ids:
            outcome = run_parser_on_doc(conn, parser, doc_id, extractor)
            print(f"doc {doc_id}: {outcome.status} (confidence {outcome.confidence:.2f})")


def cmd_reprocess(args) -> None:
    from asx.parse.llm import StructuredExtractor
    from asx.parse.reprocess import reprocess

    parser = _get_parser(args.parser)
    since = date.fromisoformat(args.since) if args.since else None
    with db.connect() as conn:
        report = reprocess(conn, parser, StructuredExtractor(),
                           since=since, apply=args.apply)
    print(report.summary())


def cmd_monitor(_args) -> None:
    from asx.monitor.checks import run_monitor

    with db.connect() as conn:
        alarms = run_monitor(conn)
    if not alarms:
        print("ok: no alarms")
        return
    for a in alarms:
        print(f"ALARM [{a.check}] {a.detail}")
    sys.exit(1)


def cmd_ops_report(_args) -> None:
    from asx.monitor.ops_report import ops_report

    with db.connect() as conn:
        print(ops_report(conn))


def cmd_review(args) -> None:
    from asx.parse.framework import resolve_review_item
    from asx.review import queue

    with db.connect() as conn:
        if args.review_cmd == "list":
            for item in queue.list_open(conn):
                print(f"{item['item_id']:6d}  {item['kind']:12s}  doc {str(item['doc_id']):>6s}  "
                      f"{item['created_at'].date()}  {item['reason'][:80]}")
        elif args.review_cmd == "show":
            print(queue.format_item(queue.show(conn, args.item_id)))
        elif args.review_cmd == "resolve":
            item = queue.show(conn, args.item_id)
            parser_name = (item.get("payload") or {}).get("parser")
            if args.resolution in ("accepted", "corrected") and not parser_name:
                sys.exit("item has no parser recorded; resolve manually in SQL with care")
            parser = _get_parser(parser_name) if parser_name else None
            corrected = json.loads(args.payload) if args.payload else None
            validation = resolve_review_item(
                conn, parser, args.item_id, args.resolution,
                corrected_payload=corrected, note=args.note or "",
            )
            if validation is not None and not validation.ok:
                sys.exit("correction refused by validation gate: "
                         + "; ".join(validation.errors))
            print("resolved")


def cmd_build_signals(_args) -> None:
    from asx.signals.director_signals import build_cluster_buys

    with db.connect() as conn:
        n = build_cluster_buys(conn)
    print(f"built {n} cluster-buy signal rows")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="asx")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)

    p = sub.add_parser("ingest")
    p.add_argument("--drop-dir", required=True)
    p.add_argument("--no-llm", action="store_true",
                   help="skip the LLM classification fallback (rules only)")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("parse")
    p.add_argument("--parser", required=True)
    p.add_argument("--doc-id", type=int)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(fn=cmd_parse)

    p = sub.add_parser("reprocess")
    p.add_argument("--parser", required=True)
    p.add_argument("--since")
    p.add_argument("--apply", action="store_true",
                   help="apply canonical changes; without this, dry-run diff report only")
    p.set_defaults(fn=cmd_reprocess)

    sub.add_parser("monitor").set_defaults(fn=cmd_monitor)
    sub.add_parser("ops-report").set_defaults(fn=cmd_ops_report)

    p = sub.add_parser("review")
    rsub = p.add_subparsers(dest="review_cmd", required=True)
    rsub.add_parser("list")
    ps = rsub.add_parser("show")
    ps.add_argument("item_id", type=int)
    pr = rsub.add_parser("resolve")
    pr.add_argument("item_id", type=int)
    pr.add_argument("resolution", choices=["accepted", "corrected", "rejected"])
    pr.add_argument("--payload", help="corrected payload JSON (for 'corrected')")
    pr.add_argument("--note")
    p.set_defaults(fn=cmd_review)

    sub.add_parser("build-signals").set_defaults(fn=cmd_build_signals)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
