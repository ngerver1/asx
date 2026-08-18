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
    "name": ["company name", "name", "current name", "entity name"],
    "current_name_indicator": ["current name indicator", "current name ind",
                               "current_name_indicator"],
    "name_start_date": ["date of name change", "name start date",
                        "current name start date", "name_start_date"],
    "status": ["status", "company status"],
    "company_type": ["type", "company type"],
    "company_class": ["class", "company class"],
    "registration_date": ["date of registration", "registration date",
                          "date registered"],
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
    name_start_date: date | None
    status: str | None
    company_type: str | None
    company_class: str | None
    registration_date: date | None
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
            indicator = (get(row, "current_name_indicator") or "Y").upper()
            yield AsicRow(
                acn=acn,
                name=name,
                name_norm=name_norm(name),
                is_current_name=indicator.startswith("Y"),
                name_start_date=_parse_date(get(row, "name_start_date")),
                status=get(row, "status"),
                company_type=get(row, "company_type"),
                company_class=get(row, "company_class"),
                registration_date=_parse_date(get(row, "registration_date")),
                abn=(get(row, "abn") or "").replace(" ", "") or None,
            )


def load_asic_registry(
    conn: psycopg.Connection,
    path: Path,
    load_id: int,
    batch_size: int = 50_000,
) -> int:
    """Load the registry via COPY into a temp table, then upsert.

    The dataset is millions of rows, so it streams: nothing is held in memory
    beyond one batch, and re-loading an unchanged file is a cheap no-op
    because every row upserts to the same values.
    """
    total = 0
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TEMP TABLE asic_stage (
                 acn CHAR(9), name TEXT, name_norm TEXT, is_current_name BOOLEAN,
                 name_start_date DATE, status TEXT, company_type TEXT,
                 company_class TEXT, registration_date DATE, abn CHAR(11)
               ) ON COMMIT DROP"""
        )
        copy_sql = """COPY asic_stage (acn, name, name_norm, is_current_name,
                        name_start_date, status, company_type, company_class,
                        registration_date, abn) FROM STDIN"""
        with cur.copy(copy_sql) as copy:
            for row in iter_rows(path):
                copy.write_row((
                    row.acn, row.name, row.name_norm, row.is_current_name,
                    row.name_start_date, row.status, row.company_type,
                    row.company_class, row.registration_date,
                    (row.abn or None) if row.abn and len(row.abn) == 11 else None,
                ))
                total += 1

        # De-duplicate within the file before upserting: the dataset can carry
        # the same (acn, normalised name) twice across historical records.
        cur.execute(
            """INSERT INTO asic_registry
                 (acn, name, name_norm, is_current_name, name_start_date, status,
                  company_type, company_class, registration_date, abn, load_id)
               SELECT DISTINCT ON (acn, name_norm)
                      acn, name, name_norm, is_current_name, name_start_date,
                      status, company_type, company_class, registration_date,
                      abn, %s
               FROM asic_stage
               ORDER BY acn, name_norm, is_current_name DESC, name_start_date DESC NULLS LAST
               ON CONFLICT (acn, name_norm) DO UPDATE SET
                 name = EXCLUDED.name,
                 is_current_name = EXCLUDED.is_current_name,
                 name_start_date = EXCLUDED.name_start_date,
                 status = EXCLUDED.status,
                 company_type = EXCLUDED.company_type,
                 company_class = EXCLUDED.company_class,
                 registration_date = EXCLUDED.registration_date,
                 abn = coalesce(EXCLUDED.abn, asic_registry.abn),
                 load_id = EXCLUDED.load_id""",
            (load_id,),
        )
    return total


def find_acn_by_name(conn: psycopg.Connection, name: str) -> list[dict]:
    """Candidate ACNs for a company name. Exact normalised match only —
    fuzzy matching against 3M registrations would produce confident nonsense,
    so anything unmatched goes to the resolver and then to review."""
    norm = name_norm(name)
    if not norm:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT acn, name, is_current_name, status
               FROM asic_registry WHERE name_norm = %s
               ORDER BY is_current_name DESC, acn""",
            (norm,),
        )
        return cur.fetchall()
