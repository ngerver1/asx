"""Runtime configuration. Everything comes from the environment; no config
files to drift out of sync with deployment."""

import os
from pathlib import Path


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def raw_zone_root() -> Path:
    """Root of the append-only raw object store (Invariant 3).

    Local filesystem by default; an S3-compatible store can be mounted here.
    """
    return Path(os.environ.get("ASX_RAW_ROOT", "data/raw"))


def extraction_model() -> str:
    # Extraction quality is capital expenditure — documents are parsed once and
    # stored forever (SPEC §6) — so default to the strongest generally
    # available model rather than a cheap one.
    return os.environ.get("ASX_EXTRACTION_MODEL", "claude-opus-5")
