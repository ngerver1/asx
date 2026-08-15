"""Source interface abstractions (Invariant 11).

All external sources sit behind these protocols so providers can be swapped.
No implementation here scrapes anything: until the access decision
(docs/ACCESS_DECISION.md) is signed off, the only shipped AnnouncementSource is
the file-drop source, which reads documents a licensed feed (or the owner)
delivers to a local directory. If access is the blocker, the platform stops
and says so rather than working around terms of use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Protocol


@dataclass
class Announcement:
    content: bytes
    source: str
    source_ref: str | None = None
    ticker_as_lodged: str | None = None
    title: str | None = None
    asx_doc_types: list[str] = field(default_factory=list)
    price_sensitive: bool | None = None
    lodged_at: datetime | None = None


class AnnouncementSource(Protocol):
    def fetch_new(self) -> Iterable[Announcement]: ...


class PriceSource(Protocol):
    """End-of-day OHLCV including delisted securities — a purchased input
    (SPEC §4). Free sources that silently drop delisted names violate
    Invariant 4 at the root and must not implement this protocol."""

    def eod_bars(self, symbol: str, start: datetime, end: datetime) -> Iterable[dict]: ...

    def shares_outstanding(self, symbol: str, as_of: datetime) -> float | None: ...


class TenementSource(Protocol):
    def snapshot(self) -> Iterable[dict]: ...


class FileDropSource:
    """Reads announcements dropped into a directory, each with an optional
    sidecar `<name>.meta.json` carrying lodgement metadata:

        {"title": ..., "ticker": ..., "lodged_at": "2026-08-14T10:05:00+10:00",
         "asx_doc_types": [...], "price_sensitive": true, "source_ref": ...}

    Files are never deleted here — ingestion is idempotent on content hash, so
    re-reading the directory is always safe.
    """

    def __init__(self, drop_dir: Path, source_name: str = "filedrop"):
        self.drop_dir = Path(drop_dir)
        self.source_name = source_name

    def fetch_new(self) -> Iterable[Announcement]:
        for path in sorted(self.drop_dir.glob("**/*")):
            if not path.is_file() or path.name.endswith(".meta.json"):
                continue
            meta_path = path.with_name(path.name + ".meta.json")
            meta = {}
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
            lodged_at = None
            if meta.get("lodged_at"):
                lodged_at = datetime.fromisoformat(meta["lodged_at"])
            yield Announcement(
                content=path.read_bytes(),
                source=self.source_name,
                source_ref=meta.get("source_ref", str(path)),
                ticker_as_lodged=meta.get("ticker"),
                title=meta.get("title", path.stem),
                asx_doc_types=meta.get("asx_doc_types", []),
                price_sensitive=meta.get("price_sensitive"),
                lodged_at=lodged_at,
            )
