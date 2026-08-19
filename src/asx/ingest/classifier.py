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
    # "change of" and "change in" are both in live use; requiring one wording
    # silently drops the other class of lodgement.
    ("app_3y", re.compile(
        r"appendix\s*3y|change\s+(of|in|to)\s+director'?s?\s+interest|"
        r"director'?s?\s+interest\s+notice", re.I)),
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

# Titles that clearly disclose someone's interest in securities but are NOT
# one of the standard director appendices. Found in real alerts: Alcoa (AAI),
# a foreign issuer listed via CDIs, lodges "Change of Officer's Interest
# Notice" — an officer, not a director, so the 3Y pattern does not and should
# not match it.
#
# Filing it as 'other' is not a neutral act. 'other' means no parser handles
# it, which makes the detection terminal: never on the capture worklist,
# never captured, never counted as a gap. That is a substantive default on an
# ambiguous case, which Invariant 8 forbids. It stays 'other' — inventing a
# class would be worse — but the method is reported as ambiguous so the
# caller can route it to a human.
_AMBIGUOUS_INTEREST_RE = re.compile(
    r"(interest\s+notice|notice\s+of\s+interest|"
    r"change\s+(of|in|to)\s+.{0,30}\binterest\b)", re.I)

# LLM fallback: (title) -> taxonomy class. Injected for testability.
LLMClassifier = Callable[[str], str]


def make_llm_classifier(model: str | None = None) -> LLMClassifier:
    """Production LLM fallback for titles the rules miss (SPEC §5.3).
    Constrained to the taxonomy via structured outputs; anything the model is
    unsure about stays 'other'."""
    import json

    import anthropic

    from asx.config import extraction_model

    client = anthropic.Anthropic()
    model = model or extraction_model()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["doc_class"],
        "properties": {"doc_class": {"enum": TAXONOMY}},
    }

    def classify_title(title: str) -> str:
        response = client.messages.create(
            model=model,
            max_tokens=256,
            system=(
                "Classify an ASX announcement by its title into exactly one "
                "class. Use 'other' unless the title clearly indicates one of "
                "the specific standard forms."
            ),
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": f"Title: {title}"}],
        )
        if response.stop_reason == "refusal":
            return "other"
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)["doc_class"]

    return classify_title


def classify(
    title: str,
    asx_doc_types: list[str] | None = None,
    llm: LLMClassifier | None = None,
) -> tuple[str, str]:
    """Return (doc_class, method).

    method is one of {'code', 'rules', 'llm', 'default', 'ambiguous'}.
    'ambiguous' means the rules could not place the title but it looks like a
    disclosure this platform cares about — the caller should put a human on
    it rather than treat the 'other' class as settled.
    """
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
    if _AMBIGUOUS_INTEREST_RE.search(title or ""):
        return "other", "ambiguous"
    return "other", "default"
