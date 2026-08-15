"""Document classifier: rules-first, LLM-fallback (SPEC §5.3).

ASX type codes and title regexes catch the overwhelming majority of standard
forms; anything the rules can't place goes to the LLM fallback if configured,
else lands in 'other'. 'other' is a first-class category (Invariant 8), not a
failure.
"""

from __future__ import annotations

import re
from typing import Callable

TAXONOMY = [
    "app_3y", "app_3z", "app_3b", "app_2a", "lr_3_10a_notice",
    "substantial_603", "substantial_604", "substantial_605",
    "annual_report", "half_year", "quarterly_4c_5b", "capital_reorg",
    "notice_of_meeting", "prospectus", "cleansing_notice", "other",
]

# Order matters: more specific patterns first (3Z before 3Y — "final
# director's interest" must not fall through to the 3Y pattern).
_TITLE_RULES: list[tuple[str, re.Pattern]] = [
    ("app_3z", re.compile(r"appendix\s*3z|final\s+director'?s?\s+interest", re.I)),
    ("app_3y", re.compile(r"appendix\s*3y|change\s+of\s+director'?s?\s+interest", re.I)),
    ("app_3b", re.compile(r"appendix\s*3b|proposed\s+issue\s+of\s+securities", re.I)),
    ("app_2a", re.compile(r"appendix\s*2a|application\s+for\s+quotation", re.I)),
    ("lr_3_10a_notice", re.compile(
        r"3\.?10a|release\s+(of|from)\s+escrow|escrow\s+release|"
        r"restricted\s+securities.{0,40}(releas|cess?at|cease|end)|"
        r"(releas\w*|cessation)\s+(of\s+)?restricted\s+securities", re.I)),
    ("substantial_603", re.compile(r"form\s*603|becoming\s+a\s+substantial\s+(share)?holder", re.I)),
    ("substantial_604", re.compile(r"form\s*604|change\s+in\s+substantial\s+(share)?holding", re.I)),
    ("substantial_605", re.compile(r"form\s*605|ceasing\s+to\s+be\s+a\s+substantial\s+(share)?holder", re.I)),
    ("quarterly_4c_5b", re.compile(
        r"appendix\s*4c|appendix\s*5b|quarterly\s+(activities|cash\s*flow)\s+report", re.I)),
    ("half_year", re.compile(r"appendix\s*4d|half[\s-]?year(ly)?\s+(financial\s+)?report|interim\s+financial\s+report", re.I)),
    ("annual_report", re.compile(r"annual\s+report", re.I)),
    ("capital_reorg", re.compile(
        r"(share|capital|securities)\s+consolidation|consolidation\s+of\s+(capital|securities|shares)|"
        r"reorgani[sz]ation\s+of\s+capital|share\s+split", re.I)),
    ("notice_of_meeting", re.compile(
        r"notice\s+of\s+(annual\s+general\s+|general\s+|extraordinary\s+general\s+)?meeting", re.I)),
    ("cleansing_notice", re.compile(r"cleansing\s+(notice|statement)", re.I)),
    ("prospectus", re.compile(r"\bprospectus\b", re.I)),
]

# ASX-assigned report type codes, where the provider supplies them. This map is
# deliberately partial: verify codes against the chosen provider's
# documentation at access-decision time (SPEC §5.1) — training-data memory of
# code tables is not sufficient (CLAUDE.md).
_ASX_TYPE_CODE_MAP: dict[str, str] = {}

# LLM fallback: (title) -> taxonomy class. Injected for testability.
LLMClassifier = Callable[[str], str]


def classify(
    title: str,
    asx_doc_types: list[str] | None = None,
    llm: LLMClassifier | None = None,
) -> tuple[str, str]:
    """Return (doc_class, method) with method in {'code', 'rules', 'llm', 'default'}."""
    for code in asx_doc_types or []:
        if code in _ASX_TYPE_CODE_MAP:
            return _ASX_TYPE_CODE_MAP[code], "code"
    for doc_class, pattern in _TITLE_RULES:
        if pattern.search(title or ""):
            return doc_class, "rules"
    if llm is not None:
        result = llm(title or "")
        if result in TAXONOMY:
            return result, "llm"
    return "other", "default"
