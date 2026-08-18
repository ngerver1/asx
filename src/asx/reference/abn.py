"""ABN Bulk Extract loader (SPEC §4 reference sources).

Source: the Australian Business Register's bulk extract, published on
data.gov.au as a set of large XML files. Used for one narrow purpose: filling
`entities.abn` for entities whose ACN we already hold.

It is deliberately a *filter* pass, not an import. The extract carries every
ABN in Australia; we take only the handful matching ACNs already in the entity
master, and never create entities from it. An ABN is an attribute of an entity
we track, never the reason we track one.

Streaming via iterparse with element clearing: the files are gigabytes, and a
DOM parse would not fit in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree

import psycopg


@dataclass
class AbnRecord:
    abn: str
    acn: str | None
    name: str | None


def _text(element, *paths: str) -> str | None:
    for path in paths:
        found = element.find(path)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return None


def iter_records(path: Path) -> Iterator[AbnRecord]:
    """Stream ABN records. Element structure varies between extract versions,
    so several known paths are tried per field and anything unreadable is
    skipped rather than guessed."""
    context = ElementTree.iterparse(str(path), events=("end",))
    for _event, element in context:
        if not element.tag.endswith("ABR"):
            continue
        abn = _text(element, "ABN", "./ABN")
        if abn:
            abn = "".join(ch for ch in abn if ch.isdigit()) or None
        acn = _text(element, "ASICNumber", "./ASICNumber",
                    "EntityType/ASICNumber")
        if acn:
            digits = "".join(ch for ch in acn if ch.isdigit())
            acn = digits.zfill(9) if 0 < len(digits) <= 9 else None
        name = _text(element, "MainEntity/NonIndividualName/NonIndividualNameText",
                     "LegalEntity/IndividualName/GivenName",
                     "./MainEntity/NonIndividualName/NonIndividualNameText")
        if abn and len(abn) == 11:
            yield AbnRecord(abn, acn, name)
        element.clear()


def load_abns_for_known_entities(
    conn: psycopg.Connection, path: Path, load_id: int
) -> dict:
    """Attach ABNs to entities we already track, matched on ACN.

    Entities are never created here, and an existing ABN is never overwritten
    with a different value — a conflict is reported for review instead, since
    two ABNs for one ACN means either a bad match or a real corporate change
    worth a human's attention.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT acn, abn FROM entities WHERE acn IS NOT NULL")
        known = {r["acn"]: r["abn"] for r in cur.fetchall()}
    if not known:
        return {"scanned": 0, "matched": 0, "updated": 0, "conflicts": 0}

    stats = {"scanned": 0, "matched": 0, "updated": 0, "conflicts": 0}
    updates: list[tuple[str, str]] = []
    for record in iter_records(path):
        stats["scanned"] += 1
        if not record.acn or record.acn not in known:
            continue
        stats["matched"] += 1
        existing = known[record.acn]
        if existing is None:
            updates.append((record.abn, record.acn))
        elif existing.strip() != record.abn:
            stats["conflicts"] += 1
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO review_items (kind, payload, reason)
                       VALUES ('resolution', %s, %s)""",
                    ({"acn": record.acn, "stored_abn": existing,
                      "extract_abn": record.abn},
                     "ABN extract disagrees with the stored ABN for this ACN"),
                )

    if updates:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE entities SET abn = %s WHERE acn = %s AND abn IS NULL",
                updates,
            )
            stats["updated"] = len(updates)
    return stats
