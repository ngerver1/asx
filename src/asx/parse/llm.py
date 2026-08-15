"""LLM extraction via the Anthropic API with structured outputs (SPEC §6).

Rules encoded here:
- Structured outputs against a JSON schema mirroring the target table.
- Dual-pass extraction: one text-based call and one document(vision)-based
  call per document; field-level disagreement routes to review. Parse cost is
  capital expenditure, not operating expense.
- Every field in every schema must be nullable, and the prompt instructs the
  model to return null rather than guess — "couldn't read it" is representable
  (Invariant 8 at field level).
- SPEC §3 says "temperature 0"; current Claude models (claude-opus-5) removed
  sampling parameters entirely, so determinism comes from structured outputs
  and a fixed prompt instead. Primary source (Anthropic API) wins over the
  spec's wording per the SPEC's own precedence rule.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from asx.config import extraction_model
from asx.parse.text import document_text

SYSTEM_PROMPT = (
    "You are a meticulous data-extraction engine for Australian market "
    "disclosures. Extract only what the document actually states. If a field "
    "is absent, ambiguous, or illegible, return null for it and explain in "
    "the extraction_notes field — never guess and never compute values the "
    "form does not print. Dates are ISO 8601. Quantities are plain numbers "
    "with no separators."
)


def prompt_hash(task_prompt: str) -> str:
    return hashlib.sha256((SYSTEM_PROMPT + "\n" + task_prompt).encode()).hexdigest()


@dataclass
class ExtractionPass:
    payload: dict
    model_id: str
    mode: str  # 'text' | 'vision'


class StructuredExtractor:
    """Two independent extraction passes over one document.

    The Anthropic client is created lazily so everything else in the platform
    imports and tests without an API key.
    """

    def __init__(self, model: str | None = None):
        self.model = model or extraction_model()
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _call(self, content_blocks: list[dict], schema: dict, task_prompt: str) -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content_blocks + [{"type": "text", "text": task_prompt}]}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("extraction request was refused by the model")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def extract_text_pass(self, content: bytes, schema: dict, task_prompt: str) -> ExtractionPass:
        text = document_text(content)
        blocks = [{"type": "text", "text": f"<document>\n{text}\n</document>"}]
        return ExtractionPass(self._call(blocks, schema, task_prompt), self.model, "text")

    def extract_vision_pass(self, content: bytes, schema: dict, task_prompt: str) -> ExtractionPass:
        # Layout is information: financial tables lose column alignment in
        # text-only extraction, so the second pass sends the original document
        # for the model to read with layout intact (SPEC §6).
        if content[:5] == b"%PDF-":
            blocks = [{
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(content).decode(),
                },
            }]
        else:
            # Non-PDF payloads (structured online forms fetched as HTML/JSON)
            # have no layout channel; fall back to a differently-framed text
            # pass so the two passes still disagree on misreads.
            text = document_text(content)
            blocks = [{
                "type": "text",
                "text": "Independently re-read the document below from scratch.\n"
                        f"<document>\n{text}\n</document>",
            }]
        return ExtractionPass(self._call(blocks, schema, task_prompt), self.model, "vision")


def field_disagreements(a: dict, b: dict, ignore: set[str] = frozenset({"extraction_notes"})) -> list[str]:
    """Field-level diff of two extraction payloads. Lists are compared
    positionally; any mismatch is a disagreement. Disagreement is an automatic
    route-to-review (SPEC §6)."""
    diffs: list[str] = []

    def walk(x, y, path: str):
        if isinstance(x, dict) and isinstance(y, dict):
            for key in sorted(set(x) | set(y)):
                if key in ignore:
                    continue
                walk(x.get(key), y.get(key), f"{path}.{key}" if path else key)
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                diffs.append(f"{path}[len {len(x)} != {len(y)}]")
                return
            for i, (xi, yi) in enumerate(zip(x, y)):
                walk(xi, yi, f"{path}[{i}]")
        else:
            if _norm_scalar(x) != _norm_scalar(y):
                diffs.append(path or "<root>")

    walk(a, b, "")
    return diffs


def _norm_scalar(v):
    if isinstance(v, str):
        v = v.strip()
        try:
            return float(v.replace(",", "")) if v.replace(",", "").replace(".", "", 1).replace("-", "", 1).isdigit() else v.lower()
        except ValueError:
            return v.lower()
    if isinstance(v, (int, float)):
        return float(v)
    return v
