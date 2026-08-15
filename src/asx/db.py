"""Database access: a thin psycopg 3 layer plus the migration runner.

Plain SQL by design (SPEC §3) — no ORM magic that obscures the
effective-dating and bitemporal logic.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from asx.config import database_url

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), row_factory=dict_row)


def migrate(conn: psycopg.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Apply migrations in filename order. Idempotent: applied filenames are
    recorded in schema_migrations and skipped on rerun."""
    migrations_dir = migrations_dir or MIGRATIONS_DIR
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                 filename   TEXT PRIMARY KEY,
                 applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        cur.execute("SELECT filename FROM schema_migrations")
        applied = {r["filename"] for r in cur.fetchall()}

    ran: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if not re.match(r"^\d{4}_", path.name):
            raise ValueError(f"migration filename must start with NNNN_: {path.name}")
        if path.name in applied:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text())
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
        conn.commit()
        ran.append(path.name)
    return ran
