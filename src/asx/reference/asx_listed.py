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
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg

from asx.ids.normalize import name_norm
from asx.reference.asic import resolve_acn_for_name

TICKER_ALIASES = ["asx code", "code", "ticker", "asx code ", "security code"]
NAME_ALIASES = ["company name", "name", "issuer name", "entity name"]
SECTOR_ALIASES = ["gics industry group", "gics industry group ", "sector",
                  "gics sector", "industry group"]
LISTING_DATE_ALIASES = ["listing date", "date of listing", "listed date"]
MARKET_CAP_ALIASES = ["market cap", "market capitalisation", "market capitalization"]

# Refuse a sweep that would delist more than this share of the open universe
# in one go. A genuine market-wide delisting event does not exist; a truncated
# file does.
MAX_DELIST_FRACTION = 0.10
MIN_PLAUSIBLE_ROWS = 500


def listed_name_variants(name: str) -> list[str]:
    """Deterministic rewrites of an ASX-published company name.

    The ASX file moves a leading "The" to a trailing "(THE)":
    ENVIRONMENTAL GROUP LIMITED (THE) is registered with ASIC as THE
    ENVIRONMENTAL GROUP LIMITED. That is a publishing convention with one
    exact inverse, so undoing it is a rewrite, not a fuzzy match.

    Nothing else is attempted. Abbreviation guessing, token dropping and
    edit-distance matching against four million registrations all produce
    confident nonsense, and an unresolved company sitting in the review queue
    is a better outcome than a wrong ACN (Invariant 8).
    """
    variants = [name]
    stripped = name.strip()
    if stripped.upper().endswith("(THE)"):
        variants.append("THE " + stripped[: -len("(THE)")].strip())
    return variants


@dataclass
class ListedCompany:
    ticker: str
    name: str
    sector: str | None = None
    listing_date: date | None = None
    # NULL where the publisher prints '--'. Never coerced to zero: an unknown
    # market cap must not read as a nano-cap on a size screen (Invariant 8).
    market_cap_aud: float | None = None


class ImplausibleSnapshotError(RuntimeError):
    """Raised when a listing snapshot looks truncated rather than real."""


def parse_listed_file(content: bytes) -> list[ListedCompany]:
    """Parse the ASX listed-companies CSV. The publisher prefixes a few
    descriptive lines before the header, so the header row is located rather
    than assumed."""
    text = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = ticker_col = name_col = sector_col = None
    listing_col = mcap_col = None
    for i, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        t = next((lowered.index(a) for a in TICKER_ALIASES if a in lowered), None)
        n = next((lowered.index(a) for a in NAME_ALIASES if a in lowered), None)
        if t is not None and n is not None:
            header_idx, ticker_col, name_col = i, t, n
            sector_col = next((lowered.index(a) for a in SECTOR_ALIASES if a in lowered), None)
            listing_col = next((lowered.index(a) for a in LISTING_DATE_ALIASES if a in lowered), None)
            mcap_col = next((lowered.index(a) for a in MARKET_CAP_ALIASES if a in lowered), None)
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
        def cell(col):
            return (row[col].strip() if col is not None and len(row) > col else "")

        listing_date = None
        raw_date = cell(listing_col)
        if raw_date:
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    listing_date = datetime.strptime(raw_date, fmt).date()
                    break
                except ValueError:
                    continue
        raw_cap = cell(mcap_col).replace(",", "").replace("$", "")
        try:
            market_cap = float(raw_cap) if raw_cap else None
        except ValueError:
            market_cap = None   # the publisher prints '--' when unavailable
        out.append(ListedCompany(ticker, name, cell(sector_col) or None,
                                 listing_date, market_cap))
    return out


@dataclass
class SnapshotResult:
    as_at: date
    listed: int
    entities_created: int
    # "acn" here means "an ASIC registration number", which for a registered
    # foreign company is an ARBN. Both count as resolved identity.
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
    """Copy the ACN's registered names — current and former — into
    entity_names, effective-dated as honestly as the source allows.

    Former names matter: registers, old announcements and subsidiary notes
    name companies as they were called at the time, and the resolver searches
    former names precisely so those references land on the right entity
    (Invariant 1).

    **What ASIC actually publishes** (verified against the 202608 extract):
    each name is its own row; the current name carries indicator 'Y'; former
    names carry a BLANK indicator and repeat the company's *current* name and
    the date that current name began. So the file dates exactly one
    transition — when the present name started — and says nothing about when
    each earlier name began or ended.

    Therefore:
      - the current name starts at the published transition date, or at
        registration when the company never renamed;
      - former names are given the widest range the source supports
        (registration -> the day before the current name began). Where a
        company has several former names they share that range, because the
        boundaries between them are genuinely unknown. That is a documented
        over-approximation for LOOKUP; do not read these ranges as evidence
        of what a company was called on a particular date.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT name, name_norm, is_current_name, current_name_start_date,
                      registration_date
               FROM asic_registry WHERE acn = %s""",
            (acn,),
        )
        rows = cur.fetchall()
        if not rows:
            return

        registration = next((r["registration_date"] for r in rows
                             if r["registration_date"]), None)
        # Published on the former-name rows; absent when the name never changed.
        transition = next((r["current_name_start_date"] for r in rows
                           if not r["is_current_name"] and r["current_name_start_date"]),
                          None)

        for row in rows:
            if row["is_current_name"]:
                valid_from = transition or registration or date(1900, 1, 1)
                valid_to = None
                kind = "legal"
            else:
                valid_from = registration or date(1900, 1, 1)
                valid_to = (transition - timedelta(days=1)) if transition else None
                kind = "former"
            if valid_to is not None and valid_to < valid_from:
                valid_to = valid_from   # degenerate same-day rename
            cur.execute(
                """INSERT INTO entity_names
                     (entity_id, name, name_norm, name_kind, valid_from, valid_to,
                      source_load_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (entity_id, name_norm, valid_from) DO NOTHING""",
                (entity_id, row["name"], row["name_norm"], kind,
                 valid_from, valid_to, load_id),
            )


def _resolve_or_create_entity(
    conn: psycopg.Connection, company: ListedCompany, as_at: date, load_id: int,
) -> tuple[int, bool, bool]:
    """Return (entity_id, created, acn_resolved).

    Resolution order is deliberately **identity first, code last**:

    1. ASIC registry by exact normalised name -> ACN -> entity, ranking
       CURRENT registered names above former ones (see
       `asic.resolve_acn_for_name`). The ACN is the canonical identity, so
       this settles the overwhelming majority.
    2. An existing entity **currently** carrying that normalised name, and
       only when exactly one entity does.
    3. Otherwise a new entity.

    A match on an ASIC registration of type FNOS is a *registered foreign
    company*: the nine-digit number is an ARBN rather than an ACN, so it goes
    to `entities.arbn` and the entity is flagged `foreign`. That flag is the
    explicit one acceptance criterion 0.2 asks for; recording an ARBN as an
    ACN would satisfy the criterion by misstating the identifier.

    Step 2 deliberately ignores FORMER names. Matching a listed company
    against another entity's former name is not weak evidence, it is
    positively misleading: KINGSTON RESOURCES (KSN) was called NEXUS MINERALS
    NL until 2012, and the unrelated NEXUS MINERALS LIMITED (NXM) is listed
    today. The first version of this function matched NXM onto Kingston's
    entity, gave one entity two unrelated open codes and overwrote Kingston's
    legal name — Invariant 1's exact failure mode, caught only because the
    real file was loaded. Historical *document* references still resolve
    through former names; that is `ids.resolver`'s job, where matching a name
    as it stood at a past date is the correct behaviour.

    An open listing for the same ticker is NEVER used to claim identity
    either — that is how a recycled code merges two unrelated companies. When
    a ticker moves between entities the old listing is closed and a review
    item records the transition, so a genuine rename can be merged by a human
    — never automatically.
    """
    norm = name_norm(company.name)

    # listed_issuer=True: only registrations that can actually issue a listed
    # security are candidates.
    for candidate_name in listed_name_variants(company.name):
        match = resolve_acn_for_name(conn, candidate_name, listed_issuer=True)
        if match.resolved:
            break
    number = match.acn
    acns = {c["acn"] for c in match.candidates}
    # A registered foreign company's number is an ARBN and belongs in its own
    # column, with the entity explicitly flagged foreign.
    acn = None if match.is_foreign else number
    arbn = number if match.is_foreign else None

    with conn.cursor() as cur:
        if number:
            cur.execute(
                "SELECT entity_id FROM entities WHERE acn = %s OR arbn = %s",
                (acn, arbn),
            )
            row = cur.fetchone()
            if row:
                return row["entity_id"], False, True

        if not number:
            # Only a name currently held, and held by exactly one entity.
            cur.execute(
                """SELECT entity_id FROM entity_names
                   WHERE name_norm = %s AND valid_to IS NULL""",
                (norm,),
            )
            rows = cur.fetchall()
            if len({r["entity_id"] for r in rows}) == 1:
                return rows[0]["entity_id"], False, False

        kind = "foreign" if match.is_foreign else ("company" if acn else "other")
        # The ABN comes free with the ASIC match and is the identifier every
        # lodged form actually prints — "Name of entity / ABN 54 118 912 495".
        # Without it a captured document cannot be tied back to its entity by
        # reading the document, which is the only way to file a PDF that a
        # browser named "documentdownload (3).pdf".
        abn = None
        if number:
            cur.execute(
                "SELECT abn FROM asic_registry WHERE acn = %s AND abn IS NOT NULL "
                "LIMIT 1", (number,))
            row = cur.fetchone()
            abn = row["abn"].strip() if row and row["abn"] else None
        cur.execute(
            "INSERT INTO entities (acn, arbn, abn, entity_kind) "
            "VALUES (%s, %s, %s, %s) RETURNING entity_id",
            (acn, arbn, abn, kind),
        )
        entity_id = cur.fetchone()["entity_id"]

        if not number:
            if len(acns) > 1:
                reason = ("multiple ASIC registrations share this name and none "
                          "of them is an unambiguous current registered name")
            elif acns:
                reason = "the only ASIC match is ambiguous"
            else:
                reason = "no exact ASIC registration match"
            cur.execute(
                """INSERT INTO review_items (kind, payload, reason)
                   VALUES ('resolution', %s, %s)""",
                (json.dumps({"ticker": company.ticker, "name": company.name,
                             "entity_id": entity_id,
                             "candidate_acns": sorted(acns)}),
                 f"listed company without a resolved ACN: {reason}. Confirm the "
                 f"ACN, or set entity_kind='foreign' if incorporated overseas."),
            )

    if number:
        _seed_names_from_asic(conn, entity_id, number, load_id)
    return entity_id, True, bool(number)


def _would_merge_differently_named_listings(
    conn: psycopg.Connection, entity_id: int, company: ListedCompany,
) -> bool:
    """True when attaching this code to this entity would give one entity two
    open ASX codes under *different* names.

    Defence in depth behind the resolution rules. Two codes on one entity is
    normal and correct for dual-class securities (NWS/NWSLV, AUQ/AUQN) — the
    publisher prints the same company name against both. Two codes with
    different names is not a dual listing, it is two companies wrongly fused,
    and it must never happen silently (Invariant 1).
    """
    norm = name_norm(company.name)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM listings
               WHERE entity_id = %s AND exchange = 'ASX' AND valid_to IS NULL
                 AND ticker <> %s LIMIT 1""",
            (entity_id, company.ticker),
        )
        if cur.fetchone() is None:
            return False
        cur.execute(
            """SELECT 1 FROM entity_names
               WHERE entity_id = %s AND valid_to IS NULL AND name_norm = %s
               LIMIT 1""",
            (entity_id, norm),
        )
        return cur.fetchone() is None


def _split_off_entity(
    conn: psycopg.Connection, merged_into: int, company: ListedCompany,
) -> int:
    """Give the company its own entity rather than fusing it with another, and
    queue the decision for a human. The new entity carries no ACN: the ACN
    that led here belongs to the other company, and guessing a replacement is
    exactly the confident nonsense the spec forbids."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (acn, entity_kind) VALUES (NULL, 'other') "
            "RETURNING entity_id"
        )
        entity_id = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO review_items (kind, payload, reason)
               VALUES ('resolution', %s, %s)""",
            (json.dumps({"ticker": company.ticker, "name": company.name,
                         "would_have_merged_into": merged_into,
                         "entity_id": entity_id}),
             f"{company.ticker} ({company.name}) resolved onto entity "
             f"{merged_into}, which already holds a different open ASX code "
             f"under a different name. Refused to merge: confirm the ACN for "
             f"this company, or merge the two entities by hand if they really "
             f"are one issuer."),
        )
    return entity_id


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
        if not created and _would_merge_differently_named_listings(
            conn, entity_id, company
        ):
            entity_id = _split_off_entity(conn, entity_id, company)
            created, acn_ok = True, False
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
            # Back-date the listing to the company's real listing date, but
            # only when this code has no earlier history — otherwise a
            # recycled ticker would be back-dated across its predecessor's
            # period and manufacture the overlap Invariant 1 forbids.
            cur.execute(
                """SELECT max(coalesce(valid_to, valid_from)) AS last_seen
                   FROM listings WHERE exchange = 'ASX' AND ticker = %s""",
                (company.ticker,),
            )
            prior = cur.fetchone()["last_seen"]
            listing_from = company.listing_date or as_at
            if prior is not None:
                listing_from = max(listing_from, prior + timedelta(days=1))
            listing_from = min(listing_from, as_at)
            cur.execute(
                """INSERT INTO listings
                     (entity_id, exchange, ticker, security_class, valid_from,
                      source, source_load_id)
                   VALUES (%s, 'ASX', %s, 'ORD', %s, 'asx_file', %s)
                   ON CONFLICT (entity_id, exchange, ticker, security_class)
                     WHERE valid_to IS NULL DO NOTHING""",
                (entity_id, company.ticker, listing_from, load_id),
            )
            if cur.rowcount:
                result.tickers_opened += 1

            # The file publishes the company's actual listing date, so
            # membership starts when it really started rather than when we
            # first observed it. This is what lets a historical universe
            # query reach back before the platform existed (Invariant 4).
            listed_from = min(company.listing_date or as_at, as_at)
            cur.execute(
                """INSERT INTO universe_membership
                     (entity_id, listed_from, source_load_id)
                   SELECT %s, %s, %s
                   WHERE NOT EXISTS (
                     SELECT 1 FROM universe_membership
                     WHERE entity_id = %s AND listed_to IS NULL)
                   ON CONFLICT (entity_id, listed_from) DO NOTHING""",
                (entity_id, listed_from, load_id, entity_id),
            )

            # Dated snapshot of the free published figures. Market cap is
            # point-in-time with no history, so it supports live screens only.
            cur.execute(
                """INSERT INTO listing_snapshots
                     (entity_id, as_at, ticker, market_cap_aud, sector,
                      listing_date, source_load_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (entity_id, as_at, ticker) DO UPDATE SET
                     market_cap_aud = EXCLUDED.market_cap_aud,
                     sector = EXCLUDED.sector,
                     listing_date = EXCLUDED.listing_date""",
                (entity_id, as_at, company.ticker, company.market_cap_aud,
                 company.sector, company.listing_date, load_id),
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
