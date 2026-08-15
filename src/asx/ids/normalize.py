"""Name normalisation — one pure function used identically everywhere
(SPEC §5.2). A resolver that normalises inconsistently is a resolver that
merges unrelated entities.
"""

from __future__ import annotations

import re
import unicodedata

# Corporate suffixes removed only as trailing tokens, repeatedly, per SPEC §5.2.
# "ACME HOLDINGS PTY LTD" -> "ACME". Never strip a name down to nothing.
_CORP_SUFFIXES = {
    "LIMITED",
    "LTD",
    "PTY",
    "PROPRIETARY",
    "NL",
    "INC",
    "HOLDINGS",
}

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _fold(name: str) -> str:
    # Unicode-fold: decompose accents and drop combining marks, then uppercase.
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.upper()


def name_norm(name: str) -> str:
    """Normalise a company name: uppercase, unicode-fold, strip punctuation,
    collapse whitespace, remove corporate suffixes as trailing tokens."""
    s = _fold(name)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    tokens = s.split(" ") if s else []
    while len(tokens) > 1 and tokens[-1] in _CORP_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def person_name_norm(name: str) -> str:
    """Normalise a person name: as name_norm but without corporate-suffix
    stripping (a director surnamed "Holdings" would otherwise vanish)."""
    s = _fold(name)
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()
