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


def _extractor_for(parser, *, use_model: bool = False):
    """How a parser should read a document.

    A parser that brings its own rules reader is read with it. That path is
    deterministic, needs no API key, and is corroborated by the form's own
    arithmetic — held after = held before + acquired - disposed — rather than
    by a second model agreeing with the first, which is why it satisfies the
    dual-pass intent of SPEC §6 without a second pass. A reading whose
    arithmetic does not reconcile routes to review exactly as a disagreement
    would (asx.parse.rules_extractor).

    The model path stays available with --model, and is the only path for a
    parser that has no rules reader.
    """
    from asx.parse.llm import StructuredExtractor

    if use_model or not hasattr(parser, "read_rules"):
        return StructuredExtractor()
    from asx.parse.rules_extractor import RulesExtractor

    return RulesExtractor(parser)


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

    parser = _get_parser(args.parser)
    extractor = _extractor_for(parser, use_model=args.model)
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
    from asx.parse.reprocess import reprocess

    parser = _get_parser(args.parser)
    since = date.fromisoformat(args.since) if args.since else None
    with db.connect() as conn:
        report = reprocess(conn, parser, _extractor_for(parser, use_model=args.model),
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
    from asx.ingest.mailbox import EmlDirectory, IMAPMailbox, detection_from_email

    if args.from_dir:
        mailbox = EmlDirectory(Path(args.from_dir))
    elif args.gmail_api or os.environ.get("ASX_GMAIL_REFRESH_TOKEN"):
        # The only mailbox route that works from a sandboxed cloud container:
        # IMAP is not reachable there, the Gmail REST API is. Scope is
        # read-only, so this cannot mark anything seen.
        from asx.ingest.gmail_api import GmailAPIMailbox

        mailbox = GmailAPIMailbox(since_days=args.since_days)
    else:
        missing = [v for v in ("ASX_IMAP_HOST", "ASX_IMAP_USER", "ASX_IMAP_PASSWORD")
                   if not os.environ.get(v)]
        if missing:
            raise SystemExit(
                f"missing {', '.join(missing)}. Either set them, or run against "
                f"saved emails with --from-dir <directory of .eml files>, which "
                f"needs no credentials."
            )
        mailbox = IMAPMailbox(
            host=os.environ["ASX_IMAP_HOST"],
            user=os.environ["ASX_IMAP_USER"],
            password=os.environ["ASX_IMAP_PASSWORD"],
            folder=os.environ.get("ASX_IMAP_FOLDER", "INBOX"),
            since_days=args.since_days,
            unseen_only=args.unseen_only,
        )
    new = seen = failed = 0
    with db.connect() as conn:
        for msg in mailbox.fetch_new():
            # Commit per message. One malformed email must not roll back the
            # alerts already read in this run, and must not stop the run —
            # under Tier 0 a dropped alert is a permanent dataset hole.
            try:
                detection = detection_from_email(msg)
                _doc_id, is_new = record_detection(conn, detection)
                conn.commit()
                new += int(is_new)
                seen += int(not is_new)
            except Exception as exc:  # noqa: BLE001 - deliberate: keep going
                conn.rollback()
                failed += 1
                print(f"could not read message "
                      f"{msg.get('Message-ID') or msg.get('Subject')!r}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
    out = {"new_detections": new, "already_known": seen, "failed": failed}
    print(json.dumps(out))
    if failed:
        raise SystemExit(1)


def cmd_capture(args) -> None:
    """File documents the owner captured personally, and optionally fetch
    those available from company IR sites."""
    from asx.ingest.possession import (fetch_asx_documents, fetch_ir_documents,
                                    file_captured_documents)

    with db.connect() as conn:
        stats = file_captured_documents(
            conn, Path(args.capture_dir),
            archive_dir=Path(args.archive_dir) if args.archive_dir else None,
        )
        if args.ir:
            stats["ir"] = fetch_ir_documents(conn)
        if args.asx:
            stats["asx"] = fetch_asx_documents(conn)
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
        # The link to open personally. Printing it is the difference between
        # a two-second capture and re-finding the announcement by hand, and
        # the capture rate is what ACCESS_DECISION §5 reopens the whole
        # decision over.
        for url in (r.get("manual_open_urls") or []):
            print(f"          open: {url}")


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
    from asx.universe.export import SizeFilter, universe_csv

    as_at = date.fromisoformat(args.as_at) if args.as_at else date.today()
    size = SizeFilter(max_market_cap=args.max_market_cap,
                      exclude_top=args.exclude_top)
    with db.connect() as conn:
        out = universe_csv(conn, as_at, size)
    if args.out:
        Path(args.out).write_text(out)
        print(f"{size.note()} as at {as_at} -> {args.out}")
    else:
        sys.stdout.write(out)
        if size.active:
            print(size.note(), file=sys.stderr)


def cmd_set_doc_url(args) -> None:
    """Record the document URL for an announcement already detected.

    This is the only way a URL becomes retrievable. It is a manual step by
    design: the platform must be told where a document lives by a source that
    states it, rather than deriving addresses the ASX never gave it (access
    decision §6 amendment).
    """
    from asx.ingest.fetch_guard import (is_discovery_url, is_document_url,
                                        normalise_document_url)

    args.url = normalise_document_url(args.url)
    if is_discovery_url(args.url):
        raise SystemExit(f"{args.url} looks like a search or listing endpoint; "
                         f"record the document's own URL.")
    if not is_document_url(args.url):
        raise SystemExit(f"{args.url} does not address a document (a PDF). "
                         f"Targeted retrieval is for documents, not pages.")
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE documents SET asx_document_url = %s
               WHERE asx_announcement_id = %s AND parse_status = 'detected'
               RETURNING doc_id, ticker_as_lodged, title""",
            (args.url, args.announcement_id),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        raise SystemExit(f"no detection awaiting capture with announcement id "
                         f"{args.announcement_id!r}")
    print(f"doc {row['doc_id']} ({row['ticker_as_lodged']}): {row['title']}")
    print(f"  -> {args.url}")
    print("Retrieve with: asx capture --capture-dir <dir> --asx")


def cmd_snapshot(args) -> None:
    """Export or restore the durable state (see asx.state.snapshot)."""
    from asx.state.snapshot import export_state, import_state

    out = Path(args.dir)
    with db.connect() as conn:
        if args.restore:
            counts = import_state(conn, out)
            conn.commit()
            print(json.dumps({"restored": counts}, indent=2))
        else:
            counts = export_state(conn, out)
            print(json.dumps({"exported": counts}, indent=2))


def cmd_backfill(args) -> None:
    """Give older documents what today's pipeline would have recorded."""
    from asx.raw.store import backfill

    with db.connect() as conn:
        counts = backfill(conn, [Path(d) for d in (args.source or [])])
        conn.commit()
    print(json.dumps(counts, indent=2))
    if counts["bytes_lost"]:
        print(f"\n{counts['bytes_lost']} document(s) have no findable bytes and "
              f"stay unreadable. Point --from at a directory that holds them, "
              f"or re-capture them.")


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
    p.add_argument("--model", action="store_true",
                   help="read with the LLM extractor instead of the parser's rules reader")
    p.set_defaults(fn=cmd_parse)

    p = sub.add_parser("reprocess")
    p.add_argument("--parser", required=True)
    p.add_argument("--since")
    p.add_argument("--apply", action="store_true",
                   help="apply canonical changes; without this, dry-run diff report only")
    p.add_argument("--model", action="store_true",
                   help="read with the LLM extractor instead of the parser's rules reader")
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
                   help="deprecated: reads never mark messages seen")
    p.add_argument("--since-days", type=int, default=7,
                   help="how far back to search (default 7). Re-reading is "
                        "free: detections are idempotent on message identity")
    p.add_argument("--gmail-api", action="store_true",
                   help="read via the Gmail REST API (required in cloud "
                        "containers, where IMAP is unreachable)")
    p.add_argument("--unseen-only", action="store_true",
                   help="search UNSEEN instead of by date. Only safe for a "
                        "mailbox you never open yourself")
    p.add_argument("--from-dir",
                   help="read saved .eml files instead of connecting to IMAP; "
                        "needs no credentials")
    p.set_defaults(fn=cmd_detect)

    p = sub.add_parser("capture", help="file personally-captured documents")
    p.add_argument("--capture-dir", required=True)
    p.add_argument("--archive-dir")
    p.add_argument("--ir", action="store_true",
                   help="also fetch documents linked on company IR sites")
    p.add_argument("--asx", action="store_true",
                   help="also retrieve ASX documents whose URL is recorded on "
                        "a detection (targeted retrieval only — access "
                        "decision §6 amendment, 20 Aug 2026)")
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
    p.add_argument("--max-market-cap", type=float,
                   help="keep listings at or below this market cap, in AUD")
    p.add_argument("--exclude-top", type=int, metavar="N",
                   help="drop the N largest by market cap (e.g. 300 for the "
                        "size ceiling in the access decision)")
    p.set_defaults(fn=cmd_universe)

    p = sub.add_parser("set-doc-url",
                       help="record an announcement's document URL for "
                            "targeted retrieval")
    p.add_argument("--announcement-id", required=True)
    p.add_argument("--url", required=True)
    p.set_defaults(fn=cmd_set_doc_url)

    p = sub.add_parser("snapshot",
                       help="export/restore durable state for an ephemeral host")
    p.add_argument("--dir", default="state", help="directory of CSVs")
    p.add_argument("--restore", action="store_true",
                   help="load a snapshot into an empty schema instead")
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("backfill",
                       help="give older documents their text layer and date")
    p.add_argument("--from", dest="source", action="append", metavar="DIR",
                   help="also look for document bytes under DIR (repeatable)")
    p.set_defaults(fn=cmd_backfill)

    sub.add_parser("build-signals").set_defaults(fn=cmd_build_signals)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
