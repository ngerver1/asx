"""CLI entrypoints. Every command is idempotent so cron reruns are always
safe (SPEC §3)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
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


def cmd_detect(args) -> None:
    """Read the alert mailbox and record detections (Tier 0 §1)."""
    import os

    from asx.ingest.detection import record_detection
    from asx.ingest.mailbox import IMAPMailbox, detection_from_email

    mailbox = IMAPMailbox(
        host=os.environ["ASX_IMAP_HOST"],
        user=os.environ["ASX_IMAP_USER"],
        password=os.environ["ASX_IMAP_PASSWORD"],
        folder=os.environ.get("ASX_IMAP_FOLDER", "INBOX"),
        mark_seen=not args.peek,
    )
    new = seen = 0
    with db.connect() as conn:
        for msg in mailbox.fetch_new():
            detection = detection_from_email(msg)
            _doc_id, is_new = record_detection(conn, detection)
            new += int(is_new)
            seen += int(not is_new)
        conn.commit()
    print(json.dumps({"new_detections": new, "already_known": seen}))


def cmd_capture(args) -> None:
    """File documents the owner captured personally, and optionally fetch
    those available from company IR sites."""
    from asx.ingest.possession import fetch_ir_documents, file_captured_documents

    with db.connect() as conn:
        stats = file_captured_documents(
            conn, Path(args.capture_dir),
            archive_dir=Path(args.archive_dir) if args.archive_dir else None,
        )
        if args.ir:
            stats["ir"] = fetch_ir_documents(conn)
    print(json.dumps(stats, default=str))


def cmd_worklist(args) -> None:
    """Print announcements detected but not yet captured — what to open."""
    from asx.ingest.detection import open_detections
    from asx.parse.registry import parseable_doc_classes

    classes = parseable_doc_classes() if args.parseable_only else None
    with db.connect() as conn:
        rows = open_detections(conn, doc_classes=classes, limit=args.limit)
    if not rows:
        print("nothing awaiting capture")
        return
    for r in rows:
        lodged = r["lodged_at"].strftime("%Y-%m-%d %H:%M") if r["lodged_at"] else "?"
        print(f"{r['doc_id']:6d}  {(r['ticker_as_lodged'] or '?'):6s}  "
              f"{(r['doc_class'] or '?'):16s}  {lodged}  {(r['title'] or '')[:60]}")


def cmd_load_index(args) -> None:
    """Load an ETF holdings file as the ASX 300 membership proxy."""
    from datetime import date as _date

    from asx.universe.index_membership import load_membership

    content = Path(args.file).read_bytes()
    as_of = _date.fromisoformat(args.as_of)
    with db.connect() as conn:
        result = load_membership(
            conn, content, source_url=args.source_url, as_of=as_of,
            knowable_at=datetime.combine(as_of, datetime.min.time()).replace(
                tzinfo=timezone.utc),
            source_note=args.note,
        )
    print(f"{result.index_code} @ {result.as_of}: {result.resolved}/{result.total} "
          f"tickers resolved to entities")
    if result.unresolved:
        print(f"unresolved (recorded, not joined on code): "
              f"{', '.join(result.unresolved[:20])}"
              + (" ..." if len(result.unresolved) > 20 else ""))


def cmd_spot_check(args) -> None:
    """Weekly ten-ticker manual completeness spot-check (ACCEPTANCE amendment).

    Prints what the platform believes it holds for a random sample of covered
    entities over the window, so the owner can compare against the ASX site
    and record any misses. This is the standing guard against the Tier 0
    failure mode: announcements that were never detected at all.
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT e.entity_id,
                      (SELECT ticker FROM listings l WHERE l.entity_id = e.entity_id
                        AND l.valid_to IS NULL ORDER BY l.valid_from DESC LIMIT 1) AS ticker,
                      (SELECT name FROM entity_names n WHERE n.entity_id = e.entity_id
                        AND n.valid_to IS NULL ORDER BY n.valid_from DESC LIMIT 1) AS name
               FROM entities e
               WHERE EXISTS (SELECT 1 FROM listings l WHERE l.entity_id = e.entity_id)
               ORDER BY random() LIMIT %s""",
            (args.n,),
        )
        sample = cur.fetchall()
        print(f"Manual completeness spot-check — compare each against the ASX "
              f"announcements page for the last {args.days} days:\n")
        for row in sample:
            cur.execute(
                """SELECT doc_class, parse_status, lodged_at, title
                   FROM documents
                   WHERE entity_id = %s AND lodged_at >= now() - make_interval(days => %s)
                   ORDER BY lodged_at DESC""",
                (row["entity_id"], args.days),
            )
            docs = cur.fetchall()
            print(f"{row['ticker'] or '?'} — {row['name'] or '?'} "
                  f"(entity {row['entity_id']}): {len(docs)} known")
            for d in docs:
                stamp = d["lodged_at"].strftime("%Y-%m-%d") if d["lodged_at"] else "?"
                print(f"    {stamp}  {d['parse_status']:12s} {(d['title'] or '')[:60]}")
            print()
        print("Record any announcement present on the ASX site but missing above "
              "as a completeness miss in docs/ACCEPTANCE.md.")


def cmd_load_reference(args) -> None:
    """Load a reference dataset (ASIC registry, ABN extract, ASX listings).

    Files are supplied by the owner (downloaded from data.gov.au / the ASX
    site under their published terms) — nothing here fetches them.
    """
    from datetime import date as _date

    from asx.reference import abn as abn_mod
    from asx.reference import asic as asic_mod
    from asx.reference import asx_listed
    from asx.reference.loads import mark_applied, register_load

    as_at = _date.fromisoformat(args.as_at)
    path = Path(args.file)
    parts = [Path(f) for f in args.files] if args.files else [path]
    with db.connect() as conn:
        # Every part enters the raw zone, not just the first: a multi-part
        # extract must stay regenerable from raw.
        load = register_load(conn, path, source=args.source, as_at=as_at,
                             source_ref=args.source_url, notes=args.note,
                             parts=parts)
        conn.commit()
        if load.already_loaded and load.applied and not args.force:
            print(f"identical file already applied as load {load.load_id} "
                  f"({load.as_at}); nothing to do")
            return

        if args.source == "asic_companies":
            n = asic_mod.load_asic_registry_parts(conn, parts, load.load_id)
            mark_applied(conn, load.load_id, n)
            conn.commit()
            print(f"asic_registry: {n} rows loaded (load {load.load_id})")
        elif args.source == "asx_listed_companies":
            companies = asx_listed.parse_listed_file(path.read_bytes())
            result = asx_listed.apply_listing_snapshot(
                conn, companies, as_at, load.load_id,
                allow_shrink=args.allow_shrink,
            )
            mark_applied(conn, load.load_id, result.listed)
            conn.commit()
            print(json.dumps(result.__dict__, default=str, indent=2))
        elif args.source == "abn_bulk_extract":
            stats = abn_mod.load_abns_for_known_entities(conn, path, load.load_id)
            mark_applied(conn, load.load_id, stats["matched"])
            conn.commit()
            print(json.dumps(stats))


def cmd_coverage(_args) -> None:
    """Phase 0 acceptance evidence for the entity master."""
    from asx.reference.verify import coverage_report

    with db.connect() as conn:
        print(coverage_report(conn))


def cmd_universe(args) -> None:
    """Export the tracked universe as at a date, as CSV.

    Defaults to today, but the point of the --as-of argument is that an older
    date returns the universe as it actually stood then — delisted companies
    included — rather than today's survivors back-projected.
    """
    from asx.universe.export import universe_csv

    as_at = date.fromisoformat(args.as_at) if args.as_at else date.today()
    with db.connect() as conn:
        out = universe_csv(conn, as_at)
    if args.out:
        Path(args.out).write_text(out)
        print(f"{out.count(chr(10)) - 1} listings as at {as_at} -> {args.out}")
    else:
        sys.stdout.write(out)


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

    p = sub.add_parser("detect", help="read the alert mailbox for new announcements")
    p.add_argument("--peek", action="store_true",
                   help="do not mark messages seen (for testing)")
    p.set_defaults(fn=cmd_detect)

    p = sub.add_parser("capture", help="file personally-captured documents")
    p.add_argument("--capture-dir", required=True)
    p.add_argument("--archive-dir")
    p.add_argument("--ir", action="store_true",
                   help="also fetch documents linked on company IR sites")
    p.set_defaults(fn=cmd_capture)

    p = sub.add_parser("worklist", help="announcements awaiting manual capture")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--parseable-only", action="store_true", default=True)
    p.set_defaults(fn=cmd_worklist)

    p = sub.add_parser("load-index", help="load ETF holdings as the ASX300 proxy")
    p.add_argument("--file", required=True)
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    p.add_argument("--source-url", required=True)
    p.add_argument("--note")
    p.set_defaults(fn=cmd_load_index)

    p = sub.add_parser("spot-check", help="weekly manual completeness spot-check")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(fn=cmd_spot_check)

    p = sub.add_parser("load-reference", help="load a reference dataset")
    p.add_argument("--source", required=True,
                   choices=["asic_companies", "abn_bulk_extract", "asx_listed_companies"])
    p.add_argument("--file", required=True,
                   help="the file, or the first part of a multi-part extract")
    p.add_argument("--files", nargs="+",
                   help="all parts of a multi-part extract, loaded as one load")
    p.add_argument("--as-of", dest="as_at", required=True,
                   help="publisher's extract date, YYYY-MM-DD")
    p.add_argument("--source-url")
    p.add_argument("--note")
    p.add_argument("--force", action="store_true",
                   help="re-apply even if this exact file was already applied")
    p.add_argument("--allow-shrink", action="store_true",
                   help="permit a listing snapshot that would delist many entities")
    p.set_defaults(fn=cmd_load_reference)

    sub.add_parser("coverage", help="entity-master acceptance evidence").set_defaults(
        fn=cmd_coverage)

    p = sub.add_parser("universe", help="export the tracked universe as CSV")
    p.add_argument("--as-of", dest="as_at",
                   help="point-in-time date, YYYY-MM-DD (default today)")
    p.add_argument("--out", help="write here instead of stdout")
    p.set_defaults(fn=cmd_universe)

    sub.add_parser("build-signals").set_defaults(fn=cmd_build_signals)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
