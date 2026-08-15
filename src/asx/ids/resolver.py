"""Entity resolver: messy free-text name -> entity_id (SPEC §5.2).

Pipeline: exact match on name_norm -> alias table -> fuzzy (conservative
threshold) -> LLM adjudication against the candidate list -> review queue.
Every non-exact resolution is stored in entity_aliases with its method and
confidence, so the same string never needs re-adjudication.

A resolver that guesses is a resolver that merges unrelated entities: anything
ambiguous routes to review rather than being forced to a match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

import psycopg
from rapidfuzz import fuzz

from asx.ids.normalize import name_norm

# Conservative by design: a high absolute score AND clear daylight to the
# runner-up. Nominee/subsidiary names are adversarially unhelpful.
FUZZY_ACCEPT_SCORE = 93.0
FUZZY_MIN_GAP = 3.0
LLM_ACCEPT_CONFIDENCE = 0.9

# An LLM adjudicator receives (raw_name, candidates) where candidates is a list
# of {"entity_id": int, "name": str}; it returns (entity_id | None, confidence,
# rationale). Injected so the resolver is testable without API access.
LLMAdjudicator = Callable[[str, list[dict]], tuple[int | None, float, str]]


@dataclass
class Resolution:
    entity_id: int | None
    method: str  # 'exact' | 'alias' | 'fuzzy' | 'llm' | 'review'
    confidence: float
    evidence: str = ""

    @property
    def needs_review(self) -> bool:
        return self.entity_id is None


def _exact_matches(conn: psycopg.Connection, norm: str) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT entity_id FROM entity_names WHERE name_norm = %s",
            (norm,),
        )
        return [r["entity_id"] for r in cur.fetchall()]


def _alias_matches(conn: psycopg.Connection, norm: str) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT entity_id FROM entity_aliases WHERE alias_norm = %s",
            (norm,),
        )
        return [r["entity_id"] for r in cur.fetchall()]


def _candidates(conn: psycopg.Connection, limit: int = 2000) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT entity_id, name, name_norm FROM entity_names
               WHERE valid_to IS NULL ORDER BY entity_id LIMIT %s""",
            (limit,),
        )
        return cur.fetchall()


def record_alias(
    conn: psycopg.Connection,
    norm: str,
    entity_id: int,
    method: str,
    confidence: float,
    evidence: str = "",
    source_doc_id: int | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO entity_aliases
                 (alias_norm, entity_id, method, confidence, evidence, source_doc_id)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (alias_norm, entity_id) DO NOTHING""",
            (norm, entity_id, method, confidence, evidence, source_doc_id),
        )


def _queue_review(
    conn: psycopg.Connection, raw_name: str, norm: str, reason: str,
    source_doc_id: int | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO review_items (kind, doc_id, payload, reason)
               VALUES ('resolution', %s, %s, %s)""",
            (source_doc_id, json.dumps({"raw_name": raw_name, "name_norm": norm}), reason),
        )


def resolve_name(
    conn: psycopg.Connection,
    raw_name: str,
    *,
    llm: LLMAdjudicator | None = None,
    source_doc_id: int | None = None,
) -> Resolution:
    norm = name_norm(raw_name)
    if not norm:
        _queue_review(conn, raw_name, norm, "empty name after normalisation", source_doc_id)
        return Resolution(None, "review", 0.0, "empty after normalisation")

    # 1. Exact on name_norm. Multiple distinct entities sharing a norm is
    # ambiguity, not a match.
    exact = _exact_matches(conn, norm)
    if len(exact) == 1:
        return Resolution(exact[0], "exact", 1.0)
    if len(exact) > 1:
        _queue_review(conn, raw_name, norm, f"name_norm collides across entities {exact}", source_doc_id)
        return Resolution(None, "review", 0.0, f"collision: {exact}")

    # 2. Alias table — prior adjudications.
    alias = _alias_matches(conn, norm)
    if len(alias) == 1:
        return Resolution(alias[0], "alias", 1.0)
    if len(alias) > 1:
        _queue_review(conn, raw_name, norm, f"alias collides across entities {alias}", source_doc_id)
        return Resolution(None, "review", 0.0, f"alias collision: {alias}")

    # 3. Fuzzy: token-set similarity, conservative threshold plus daylight to
    # the runner-up.
    cands = _candidates(conn)
    if cands:
        scored = sorted(
            ((fuzz.token_set_ratio(norm, c["name_norm"]), c) for c in cands),
            key=lambda t: t[0],
            reverse=True,
        )
        best_score, best = scored[0]
        second_score = 0.0
        for s, c in scored[1:]:
            if c["entity_id"] != best["entity_id"]:
                second_score = s
                break
        if best_score >= FUZZY_ACCEPT_SCORE and (best_score - second_score) >= FUZZY_MIN_GAP:
            evidence = f"token_set_ratio={best_score:.1f} vs next {second_score:.1f}"
            record_alias(conn, norm, best["entity_id"], "fuzzy", best_score / 100.0,
                         evidence, source_doc_id)
            return Resolution(best["entity_id"], "fuzzy", best_score / 100.0, evidence)

        # 4. LLM adjudication with the top candidates, if configured.
        if llm is not None:
            top = [
                {"entity_id": c["entity_id"], "name": c["name"]}
                for s, c in scored[:10]
                if s >= 60.0
            ]
            if top:
                entity_id, confidence, rationale = llm(raw_name, top)
                if entity_id is not None and confidence >= LLM_ACCEPT_CONFIDENCE:
                    record_alias(conn, norm, entity_id, "llm", confidence,
                                 rationale, source_doc_id)
                    return Resolution(entity_id, "llm", confidence, rationale)

    # 5. Review queue for everything else.
    _queue_review(conn, raw_name, norm, "no confident match", source_doc_id)
    return Resolution(None, "review", 0.0, "no confident match")
