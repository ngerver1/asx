"""ASIC company registry loader (SPEC §4 reference sources).

Source: the ASIC "Company" dataset published on data.gov.au — ACN, company
name, type, class, status, registration date, and the historical-name records
that let entity_names be effective-dated honestly.

**Column mapping is header-driven, not positional.** The publisher has
changed column order and naming between releases, and CLAUDE.md forbids
relying on remembered field layouts: if a required column is absent the load
fails loudly, listing the headers it did find, rather than silently reading
the wrong column. Verify the header aliases below against the current file the
first time you load it and add any new spelling to the alias lists.

This table is reference data, not the entity master: ~3M registered Australian
companies live here so that the few thousand we actually track can be given an
ACN. Only tracked companies become `entities` rows.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import psycopg

from asx.ids.normalize import name_norm

# Accepted spellings per logical field, lowercased and stripped.
COLUMN_ALIASES: dict[str, list[str]] = {
    "acn": ["acn", "company acn", "acn/arbn", "abn/acn"],
    "name": ["company name", "name", "entity name"],
    "current_name_indicator": ["current name indicator", "current name ind",
                               "current_name_indicator"],
    # Verified against the 202608 extract: this dates the company's CURRENT
    # name and appears on FORMER-name rows. It is not this row's start date.
    "current_name_start_date": ["current name start date", "name start date",
                                "date of name change"],
    "status": ["status", "company status"],
    "company_type": ["type", "company type"],
    "company_class": ["class", "company class"],
    "registration_date": ["date of registration", "registration date",
                          "date registered"],
    "deregistration_date": ["date of deregistration", "deregistration date"],
    "abn": ["abn"],
}
REQUIRED = ("acn", "name")

_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d%m%Y", "%Y%m%d")


@dataclass
class AsicRow:
    acn: str
    name: str
    name_norm: str
    is_current_name: bool
    current_name_start_date: date | None
    status: str | None
    company_type: str | None
    company_class: str | None
    registration_date: date | None
    deregistration_date: date | None
    abn: str | None


class HeaderMappingError(RuntimeError):
    """Raised when the file's headers do not carry the required columns."""


def _sniff_delimiter(sample: str) -> str:
    counts = {d: sample.count(d) for d in ("|", "\t", ",")}
    return max(counts, key=counts.get)


def map_headers(headers: list[str]) -> dict[str, int]:
    lowered = [h.strip().lower().strip('"') for h in headers]
    mapping: dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                mapping[field] = lowered.index(alias)
                break
    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        raise HeaderMappingError(
            f"ASIC file is missing required column(s) {missing}. Headers found: "
            f"{lowered}. Add the publisher's current spelling to "
            f"COLUMN_ALIASES rather than reading by position."
        )
    return mapping


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None  # unparseable stays unknown, never guessed


def _clean_acn(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits.zfill(9) if 0 < len(digits) <= 9 else None


def iter_rows(path: Path, encoding: str = "utf-8") -> Iterator[AsicRow]:
    """Stream the ASIC dataset. Rows without a usable ACN or name are skipped
    and counted by the caller; nothing is invented to fill a gap."""
    with open(path, "r", encoding=encoding, errors="replace", newline="") as fh:
        first = fh.readline()
        if not first:
            return
        delimiter = _sniff_delimiter(first)
        mapping = map_headers(next(csv.reader(io.StringIO(first), delimiter=delimiter)))
        reader = csv.reader(fh, delimiter=delimiter)

        def get(row: list[str], field: str) -> str | None:
            idx = mapping.get(field)
            if idx is None or idx >= len(row):
                return None
            value = row[idx].strip()
            return value or None

        for row in reader:
            if not row:
                continue
            acn = _clean_acn(get(row, "acn"))
            name = get(row, "name")
            if not acn or not name:
                continue
            # Verified against the real extract: the indicator is 'Y' on the
            # current name and BLANK on former names (~40% of rows). Treating a
            # blank as current would mark every historical name as live.
            indicator = (get(row, "current_name_indicator") or "").strip().upper()
            yield AsicRow(
                acn=acn,
                name=name,
                name_norm=name_norm(name),
                is_current_name=indicator == "Y",
                current_name_start_date=_parse_date(get(row, "current_name_start_date")),
                status=get(row, "status"),
                company_type=get(row, "company_type"),
                company_class=get(row, "company_class"),
                registration_date=_parse_date(get(row, "registration_date")),
                deregistration_date=_parse_date(get(row, "deregistration_date")),
                abn=(get(row, "abn") or "").replace(" ", "") or None,
            )


def load_asic_registry_parts(
    conn: psycopg.Connection,
    paths: list[Path],
    load_id: int,
) -> int:
    """Load a multi-part extract as ONE logical load.

    The publisher splits the register across numbered files, and a single
    company's name records can straddle a part boundary. All parts therefore
    stage together and upsert once; per-ACN name dating happens later, in SQL,
    over the complete table rather than per file.
    """
    total = 0
    for path in paths:
        total += load_asic_registry(conn, Path(path), load_id, _staged=True)
    _flush_stage(conn, load_id)
    return total


def load_asic_registry(
    conn: psycopg.Connection,
    path: Path,
    load_id: int,
    batch_size: int = 50_000,
    _staged: bool = False,
) -> int:
    """Load the registry via COPY into a temp table, then upsert.

    The dataset is millions of rows, so it streams: nothing is held in memory
    beyond one batch, and re-loading an unchanged file is a cheap no-op
    because every row upserts to the same values.
    """
    total = 0
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TEMP TABLE IF NOT EXISTS asic_stage (
                 acn CHAR(9), name TEXT, name_norm TEXT, is_current_name BOOLEAN,
                 current_name_start_date DATE, status TEXT, company_type TEXT,
                 company_class TEXT, registration_date DATE,
                 deregistration_date DATE, abn CHAR(11)
               ) ON COMMIT DROP"""
        )
        copy_sql = """COPY asic_stage (acn, name, name_norm, is_current_name,
                        current_name_start_date, status, company_type,
                        company_class, registration_date, deregistration_date,
                        abn) FROM STDIN"""
        with cur.copy(copy_sql) as copy:
            for row in iter_rows(path):
                copy.write_row((
                    row.acn, row.name, row.name_norm, row.is_current_name,
                    row.current_name_start_date, row.status, row.company_type,
                    row.company_class, row.registration_date,
                    row.deregistration_date,
                    (row.abn or None) if row.abn and len(row.abn) == 11 else None,
                ))
                total += 1

    if _staged:
        return total   # caller flushes once every part is staged
    _flush_stage(conn, load_id)
    return total


def _flush_stage(conn: psycopg.Connection, load_id: int) -> None:
    """Upsert the staged rows. De-duplicates across the whole extract: the
    dataset carries the same (acn, normalised name) more than once."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO asic_registry
                 (acn, name, name_norm, is_current_name, current_name_start_date,
                  status, company_type, company_class, registration_date,
                  deregistration_date, abn, load_id)
               SELECT DISTINCT ON (acn, name_norm)
                      acn, name, name_norm, is_current_name,
                      current_name_start_date, status, company_type,
                      company_class, registration_date, deregistration_date,
                      abn, %s
               FROM asic_stage
               ORDER BY acn, name_norm, is_current_name DESC,
                        current_name_start_date DESC NULLS LAST
               ON CONFLICT (acn, name_norm) DO UPDATE SET
                 name = EXCLUDED.name,
                 is_current_name = EXCLUDED.is_current_name,
                 current_name_start_date = EXCLUDED.current_name_start_date,
                 deregistration_date = EXCLUDED.deregistration_date,
                 status = EXCLUDED.status,
                 company_type = EXCLUDED.company_type,
                 company_class = EXCLUDED.company_class,
                 registration_date = EXCLUDED.registration_date,
                 abn = coalesce(EXCLUDED.abn, asic_registry.abn),
                 load_id = EXCLUDED.load_id""",
            (load_id,),
        )


def find_acn_by_name(conn: psycopg.Connection, name: str) -> list[dict]:
    """Candidate ACNs for a company name. Exact normalised match only —
    fuzzy matching against 4M registrations would produce confident nonsense,
    so anything unmatched goes to the resolver and then to review."""
    norm = name_norm(name)
    if not norm:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT acn, name, is_current_name, status, company_type,
                      deregistration_date
               FROM asic_registry WHERE name_norm = %s
               ORDER BY is_current_name DESC, acn""",
            (norm,),
        )
        return cur.fetchall()


# ASIC registration types that can be the issuer of an ASX-listed security.
# Codes per the published ASIC company bulk-extract data dictionary:
#   APUB  Australian public company
#   FNOS  Foreign company (incorporated outside Australia, registered here)
#   CCIV  Corporate Collective Investment Vehicle
# APTY (Australian proprietary company) is deliberately excluded: a
# proprietary company cannot raise funds from the public in a way that
# requires a disclosure document (Corporations Act 2001 (Cth) s 113(3)), so it
# cannot be the issuer of an ASX-listed security.
#
# CCIV post-dates the dictionary consulted and is included on the strength of
# the code alone. That direction fails safe: a wrongly-included type can only
# add a candidate, and extra candidates make a name ambiguous, which routes to
# review rather than to a confident wrong answer.
LISTABLE_TYPES = ("APUB", "FNOS", "CCIV")

# Registered foreign companies are issued an ARBN, not an ACN.
FOREIGN_TYPE = "FNOS"


@dataclass
class AcnMatch:
    """Outcome of resolving a company name to an ASIC registration."""
    acn: str | None
    basis: str            # current_name | current_name_live | former_name | none
    candidates: list[dict]
    registration_type: str | None = None

    @property
    def resolved(self) -> bool:
        return self.acn is not None

    @property
    def is_foreign(self) -> bool:
        """True when the number is an ARBN for a registered foreign company."""
        return self.registration_type == FOREIGN_TYPE


def resolve_acn_for_name(
    conn: psycopg.Connection, name: str, *, listed_issuer: bool = False,
) -> AcnMatch:
    """Resolve a company name to a single ACN, ranking *current* registered
    names above former ones.

    Found the hard way against the real register: ASX-listed NEXUS MINERALS
    LIMITED (ACN 122074006) shares a normalised name with a FORMER name of
    KINGSTON RESOURCES LIMITED (ACN 009148529, called NEXUS MINERALS NL until
    2012) and with a deregistered NEXUS MINERALS PTY LTD. Treating all three
    as equally good candidates made the name ambiguous, the ACN unresolvable,
    and sent resolution down a name-matching path that merged two unrelated
    listed companies into one entity — exactly the Invariant 1 failure.

    Ranking, in order, stopping at the first rank that yields exactly one ACN:

    1. ACNs carrying this as their CURRENT registered name.
    2. Of those, the ones ASIC has not deregistered. (Deregistration is a
       published date, not a decoded status code, so this tie-break rests on
       the source rather than on remembered enum meanings.)
    3. Failing any current-name match, a unique FORMER-name match — which
       covers the real case where the ASX file still prints a name ASIC has
       already superseded.

    `listed_issuer=True` first discards registrations that cannot be the
    issuer of a listed security (see LISTABLE_TYPES). On the real file this
    is what separates CSL LIMITED from CSL HOLDINGS PTY LTD, AMP LIMITED from
    AMP HOLDINGS PTY LIMITED, and ANSELL LIMITED from ANSELL HOLDINGS PTY LTD
    — same normalised name, different company, and without the filter every
    one of them was simply "ambiguous" and went unresolved. It also stops the
    former-name fallback attaching GOODMAN GROUP to a proprietary shell that
    used to carry the name.

    Ambiguity at every rank returns no ACN, and the caller must send it to
    review. A wrong ACN is worse than an unresolved one (Invariant 8).
    """
    candidates = find_acn_by_name(conn, name)
    if not candidates:
        return AcnMatch(None, "none", [])

    eligible = candidates
    if listed_issuer:
        eligible = [c for c in candidates if c["company_type"] in LISTABLE_TYPES]
        if not eligible:
            return AcnMatch(None, "none", candidates)

    def pick(rows: list[dict], basis: str) -> AcnMatch | None:
        acns = {c["acn"] for c in rows}
        if len(acns) != 1:
            return None
        acn = acns.pop()
        rtype = next((c["company_type"] for c in rows if c["acn"] == acn), None)
        return AcnMatch(acn, basis, candidates, rtype)

    current = [c for c in eligible if c["is_current_name"]]
    if current:
        return (pick(current, "current_name")
                or pick([c for c in current if c["deregistration_date"] is None],
                        "current_name_live")
                or AcnMatch(None, "none", candidates))

    return pick(eligible, "former_name") or AcnMatch(None, "none", candidates)
