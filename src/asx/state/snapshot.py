"""Durable state for an ephemeral container.

A cloud session's VM is reclaimed after inactivity, taking Postgres with it.
That is fine for the ASIC company register — 4.4M rows, 1.1 GB, and
*regenerable* from the publisher's files, which is exactly what the prime
directive requires of derived data. It is not fine for the entity master and
the detection log, which are cheap to keep and impossible to reconstruct: a
detection records that an announcement existed at a moment now past.

The durable set is about 12,000 rows and 5 MB — small enough to live in git
next to the code that produced it, which is the only durable storage this
environment can reach. External Postgres is blocked by the same network
policy that blocks IMAP.

What is deliberately NOT snapshotted:

  asic_registry   Reference data. Reload with `asx load-reference`. A fresh
                  container resolves tickers to entities without it, because
                  that lookup goes through `listings`; the register is only
                  needed to re-run the listing loader.
  raw zone        Document bytes are content-addressed on disk and belong in
                  object storage, not git. Losing them loses possession, not
                  the record that possession happened — `documents` keeps the
                  sha256, so the gap becomes visible rather than silent.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import psycopg

# Parents before children: used in this order for restore, reversed for
# truncate. The dependency chain is not the obvious one — `reference_loads`
# points at `documents` (the raw file it came from) while `documents` points
# at `entities`, so the reference load cannot be restored first even though
# everything else cites it:
#
#     entities  <-  documents  <-  reference_loads
#         ^             ^                ^
#         +-------------+----------------+---- entity_names, listings,
#                                              listing_snapshots,
#                                              universe_membership
#
# Getting this wrong is a foreign-key error at restore time, which is exactly
# when it should surface. tests/test_state_snapshot.py pins it.
TABLES = [
    "entities",
    "documents",
    "reference_loads",
    "entity_names",
    "listings",
    "listing_snapshots",
    "universe_membership",
    "persons",
    "director_trades",
    "review_items",
]


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = %s
               ORDER BY ordinal_position""", (table,))
        return [r["column_name"] for r in cur.fetchall()]


def export_state(conn: psycopg.Connection, out_dir: Path) -> dict[str, int]:
    """Write the durable tables as CSV — one file per table, with a header,
    so a git diff of a snapshot is readable by a person."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for table in TABLES:
        cols = _columns(conn, table)
        if not cols:
            continue
        buf = io.StringIO()
        with conn.cursor() as cur, cur.copy(
            f"COPY (SELECT {', '.join(cols)} FROM {table} ORDER BY 1) "
            f"TO STDOUT WITH (FORMAT csv, HEADER true)"
        ) as copy:
            for chunk in copy:
                buf.write(bytes(chunk).decode())
        text = buf.getvalue()
        (out_dir / f"{table}.csv").write_text(text)
        counts[table] = max(text.count("\n") - 1, 0)
    return counts


def import_state(conn: psycopg.Connection, in_dir: Path,
                 *, truncate: bool = True) -> dict[str, int]:
    """Restore a snapshot into a migrated schema.

    Identity sequences are advanced past the restored ids. Without that, the
    first insert after a restore collides with a row that came back from the
    snapshot — and it would surface days later as a mystery constraint
    violation rather than here.
    """
    in_dir = Path(in_dir)
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        if truncate:
            cur.execute(f"TRUNCATE {', '.join(reversed(TABLES))} CASCADE")
        for table in TABLES:
            path = in_dir / f"{table}.csv"
            if not path.exists():
                continue
            text = path.read_text()
            rows = list(csv.reader(io.StringIO(text)))
            if len(rows) < 2:
                counts[table] = 0
                continue
            header = rows[0]
            with cur.copy(
                f"COPY {table} ({', '.join(header)}) "
                f"FROM STDIN WITH (FORMAT csv, HEADER true)"
            ) as copy:
                copy.write(text)
            counts[table] = len(rows) - 1
            cur.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s
                     AND is_identity='YES'""", (table,))
            for row in cur.fetchall():
                col = row["column_name"]
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
                    f"coalesce((SELECT max({col}) FROM {table}), 1))")
    return counts
