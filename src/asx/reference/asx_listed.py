"""ASX listed-companies file loader — builds the entity master.

This is the survivorship-critical loader (Invariant 4). The ASX file lists
only companies listed *today*. Applying it naively in either direction is a
trap:

- Treating it as the universe deletes every company that ever delisted, which
  is the single largest source of overstated small-cap backtest returns.
- Ignoring absences leaves delisted companies looking live forever.

So the file is applied as a *snapshot diff* against effective-dated tables:
companies present open or keep an open listing and universe membership;
companies previously open and now absent get their listing and membership
CLOSED with a date. Nothing is ever deleted, and a delisted company keeps its
entity, its names, its documents, and its place in every historical universe.

**Effective-dating convention** (used by every lookup in the platform):
`valid_to` / `listed_to` are INCLUSIVE — the last date on which the row was
true. A company absent from the snapshot dated D was last believed listed on
D-1, so its listing closes there and any successor opens on D. An exclusive
convention would make a lookup on the transition date match both rows and
resolve to nothing.

A truncated or partial download would delist hundreds of companies at once
and record that as fact, so the sweep refuses to run when the snapshot is
implausibly smaller than the last one (Invariant 7: stop rather than serve
something wrong).
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import psycopg

from asx.ids.normalize import name_norm
from asx.reference.asic import find_acn_by_name

TICKER_ALIASES = ["asx code", "code", "ticker", "asx code ", "security code"]
NAME_ALIASES = ["company name", "name", "issuer name", "entity name"]
SECTOR_ALIASES = ["gics industry group", "sector", "gics sector", "industry group"]

# Refuse a sweep that would delist more than this share of the open universe
# in one go. A genuine market-wide delisting event does not exist; a truncated
# file does.
MAX_DELIST_FRACTION = 0.10
MIN_PLAUSIBLE_ROWS = 500


@dataclass
class ListedCompany:
    ticker: str
    name: str
    sector: str | None = None


class ImplausibleSnapshotError(RuntimeError):
    """Raised when a listing snapshot looks truncated rather than real."""


def parse_listed_file(content: bytes) -> list[ListedCompany]:
    """Parse the ASX listed-companies CSV. The publisher prefixes a few
    descriptive lines before the header, so the header row is located rather
    than assumed."""
    text = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = ticker_col = name_col = sector_col = None
    for i, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        t = next((lowered.index(a) for a in TICKER_ALIASES if a in lowered), None)
        n = next((lowered.index(a) for a in NAME_ALIASES if a in lowered), None)
        if t is not None and n is not None:
            header_idx, ticker_col, name_col = i, t, n
            sector_col = next((lowered.index(a) for a in SECTOR_ALIASES if a in lowered), None)
            break
    if header_idx is None:
        raise ValueError(
            "ASX listed-companies file: could not locate a header row with both "
            f"a code and a name column. First rows seen: {rows[:5]}"
        )

    out: list[ListedCompany] = []
    for row in rows[header_idx + 1:]:
        if len(row) <= max(ticker_col, name_col):
            continue
        ticker = row[ticker_col].strip().upper()
        name = row[name_col].strip()
        if not ticker or not name:
            continue
        sector = (row[sector_col].strip() if sector_col is not None
                  and len(row) > sector_col else None)
        out.append(ListedCompany(ticker, name, sector or None))
    return out


@dataclass
class SnapshotResult:
    as_at: date
    listed: int
    entities_created: int
    acn_resolved: int
    acn_unresolved: int
    tickers_opened: int
    tickers_closed: int
    delisted: int


def _open_universe_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM universe_membership WHERE listed_to IS NULL"
        )
        return cur.fetchone()["n"]


def _seed_names_from_asic(conn: psycopg.Connection, entity_id: int, acn: str,
                          load_id: int) -> None:
    """Copy the ACN's registered names — current and historical — into
    entity_names with their ASIC start dates.

    Former names matter: registers, old announcements and subsidiary notes
    refer to companies by the name they had at the time, and the resolver
    searches former names precisely so those references land on the right
    entity (Invariant 1).
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT name, name_norm, is_current_name, name_start_date
               FROM asic_registry WHERE acn = %s""",
            (acn,),
        )
        for row in cur.fetchall():
            cur.execute(
                """INSERT INTO entity_names
                     (entity_id, name, name_norm, name_kind, valid_from, source_load_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (entity_id, name_norm, valid_from) DO NOTHING""",
                (entity_id, row["name"], row["name_norm"],
                 "legal" if row["is_current_name"] else "former",
                 row["name_start_date"] or date(1900, 1, 1), load_id),
            )
            if not row["is_current_name"]:
                cur.execute(
                    """UPDATE entity_names SET valid_to = coalesce(valid_to, %s)
                       WHERE entity_id = %s AND name_norm = %s AND name_kind = 'former'""",
                    (date(1900, 1, 1), entity_id, row["name_norm"]),
                )


def _resolve_or_create_entity(
    conn: psycopg.Connection, company: ListedCompany, as_at: date, load_id: int,
) -> tuple[int, bool, bool]:
    """Return (entity_id, created, acn_resolved).

    Resolution order is deliberately **identity first, code last**:

    1. ASIC registry by exact normalised name -> ACN -> entity. The ACN is the
       canonical identity, so this settles the overwhelming majority.
    2. An existing entity carrying that normalised name (current or former).
    3. Otherwise a new entity.

    An open listing for the same ticker is NEVER used to claim identity. That
    lookup looks helpful and is the precise mechanism by which a recycled code
    merges two unrelated companies (Invariant 1's stated failure mode). When a
    ticker moves between entities the old listing is closed and a review item
    records the transition, so a genuine rename can be merged by a human —
    never automatically.
    """
    norm = name_norm(company.name)

    candidates = find_acn_by_name(conn, company.name)
    acns = {c["acn"] for c in candidates}
    acn = next(iter(acns)) if len(acns) == 1 else None

    with conn.cursor() as cur:
        if acn:
            cur.execute("SELECT entity_id FROM entities WHERE acn = %s", (acn,))
            row = cur.fetchone()
            if row:
                return row["entity_id"], False, True

        cur.execute(
            "SELECT entity_id FROM entity_names WHERE name_norm = %s LIMIT 1", (norm,)
        )
        row = cur.fetchone()
        if row:
            return row["entity_id"], False, bool(acn)

        kind = "company" if acn else "other"
        cur.execute(
            "INSERT INTO entities (acn, entity_kind) VALUES (%s, %s) RETURNING entity_id",
            (acn, kind),
        )
        entity_id = cur.fetchone()["entity_id"]

        if not acn:
            reason = ("multiple ASIC registrations share this name"
                      if len(acns) > 1 else "no exact ASIC registration match")
            cur.execute(
                """INSERT INTO review_items (kind, payload, reason)
                   VALUES ('resolution', %s, %s)""",
                (json.dumps({"ticker": company.ticker, "name": company.name,
                             "entity_id": entity_id,
                             "candidate_acns": sorted(acns)}),
                 f"listed company without a resolved ACN: {reason}. Confirm the "
                 f"ACN, or set entity_kind='foreign' if incorporated overseas."),
            )

    if acn:
        _seed_names_from_asic(conn, entity_id, acn, load_id)
    return entity_id, True, bool(acn)


def apply_listing_snapshot(
    conn: psycopg.Connection,
    companies: list[ListedCompany],
    as_at: date,
    load_id: int,
    *,
    allow_shrink: bool = False,
) -> SnapshotResult:
    """Apply the current-listings file as a dated snapshot diff."""
    if len(companies) < MIN_PLAUSIBLE_ROWS and not allow_shrink:
        raise ImplausibleSnapshotError(
            f"listing snapshot has only {len(companies)} companies; the ASX has "
            f"~2,000. Refusing to treat absences as delistings from what looks "
            f"like a truncated file. Re-download, or pass allow_shrink=True if "
            f"this really is the whole file."
        )
    open_before = _open_universe_count(conn)
    if open_before:
        implied_delistings = open_before - len(companies)
        if implied_delistings > max(open_before * MAX_DELIST_FRACTION, 25) and not allow_shrink:
            raise ImplausibleSnapshotError(
                f"snapshot would delist {implied_delistings} of {open_before} open "
                f"entities ({implied_delistings / open_before:.0%}). Refusing: a "
                f"partial download is far likelier than a market-wide delisting. "
                f"Pass allow_shrink=True to override deliberately."
            )

    result = SnapshotResult(as_at, len(companies), 0, 0, 0, 0, 0, 0)
    seen_entities: set[int] = set()

    for company in companies:
        entity_id, created, acn_ok = _resolve_or_create_entity(
            conn, company, as_at, load_id
        )
        seen_entities.add(entity_id)
        result.entities_created += int(created)
        result.acn_resolved += int(acn_ok)
        result.acn_unresolved += int(not acn_ok)

        with conn.cursor() as cur:
            # Name, effective-dated. valid_from is the file's extract date for
            # a name we are seeing for the first time — never back-dated to an
            # assumed epoch, because we do not know when it started.
            # A superseded legal name is closed, not deleted, so historical
            # references still resolve. Re-running the loader with an
            # unchanged name inserts nothing.
            norm = name_norm(company.name)
            cur.execute(
                """UPDATE entity_names SET valid_to = %s
                   WHERE entity_id = %s AND name_kind = 'legal'
                     AND valid_to IS NULL AND name_norm <> %s""",
                (as_at, entity_id, norm),
            )
            cur.execute(
                """INSERT INTO entity_names
                     (entity_id, name, name_norm, name_kind, valid_from, source_load_id)
                   SELECT %s, %s, %s, 'legal', %s, %s
                   WHERE NOT EXISTS (
                     SELECT 1 FROM entity_names
                     WHERE entity_id = %s AND name_norm = %s AND valid_to IS NULL)""",
                (entity_id, company.name, norm, as_at, load_id, entity_id, norm),
            )

            # Listing. A ticker that moved to a different entity closes on the
            # old one first: two entities must never hold the same open code
            # (Invariant 1).
            cur.execute(
                """UPDATE listings SET valid_to = %s
                   WHERE exchange = 'ASX' AND ticker = %s AND valid_to IS NULL
                     AND entity_id <> %s
                   RETURNING entity_id""",
                (as_at - timedelta(days=1), company.ticker, entity_id),
            )
            for moved in cur.fetchall():
                cur.execute(
                    """INSERT INTO review_items (kind, payload, reason)
                       VALUES ('resolution', %s, %s)""",
                    (json.dumps({"ticker": company.ticker,
                                 "previous_entity_id": moved["entity_id"],
                                 "new_entity_id": entity_id,
                                 "new_name": company.name,
                                 "as_at": as_at.isoformat()}),
                     f"ticker {company.ticker} moved from entity "
                     f"{moved['entity_id']} to {entity_id}. If this is a rename "
                     f"the two entities should be merged by hand; if the code "
                     f"was recycled onto an unrelated company, the split is "
                     f"correct as recorded. Never merged automatically."),
                )
            cur.execute(
                """INSERT INTO listings
                     (entity_id, exchange, ticker, security_class, valid_from,
                      source, source_load_id)
                   VALUES (%s, 'ASX', %s, 'ORD', %s, 'asx_file', %s)
                   ON CONFLICT (entity_id, exchange, ticker, security_class)
                     WHERE valid_to IS NULL DO NOTHING""",
                (entity_id, company.ticker, as_at, load_id),
            )
            if cur.rowcount:
                result.tickers_opened += 1

            cur.execute(
                """INSERT INTO universe_membership
                     (entity_id, listed_from, source_load_id)
                   SELECT %s, %s, %s
                   WHERE NOT EXISTS (
                     SELECT 1 FROM universe_membership
                     WHERE entity_id = %s AND listed_to IS NULL)""",
                (entity_id, as_at, load_id, entity_id),
            )

    # The sweep: everything previously open and absent from this snapshot is
    # closed as at the extract date. Closed, never deleted.
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE listings SET valid_to = %s
               WHERE exchange = 'ASX' AND valid_to IS NULL
                 AND source = 'asx_file' AND NOT (entity_id = ANY(%s))
               RETURNING listing_id""",
            (as_at - timedelta(days=1), list(seen_entities) or [0]),
        )
        result.tickers_closed = len(cur.fetchall())
        cur.execute(
            """UPDATE universe_membership
               SET listed_to = %s,
                   delist_reason = coalesce(delist_reason, 'absent_from_listing_file')
               WHERE listed_to IS NULL AND NOT (entity_id = ANY(%s))
               RETURNING entity_id""",
            (as_at - timedelta(days=1), list(seen_entities) or [0]),
        )
        result.delisted = len(cur.fetchall())
    return result
