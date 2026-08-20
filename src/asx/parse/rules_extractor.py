"""A single-pass extractor backed by the rules reader, not a model.

The parsing framework was designed around two independent LLM readings, where
DISAGREEMENT between them is the signal that a document was misread (SPEC §6).
A rules reader gives one reading, so that witness does not exist — and the
honest response is not to run the same deterministic function twice and call
the inevitable match "agreement". Two identical outputs from one function are
not corroboration, they are the same output twice.

What stands in its place is better. An Appendix 3Y states its own arithmetic:

    held after = held before + acquired - disposed

That is not two guesses agreeing. It is a sum printed on the document, and a
reading that satisfies it has been checked against the issuer's own numbers.
So a rules extraction is corroborated when the form's arithmetic reconciles,
and uncorroborated when there are no before/after figures to check it with —
in which case it routes to review exactly as a two-pass disagreement would.

This is why the rules path can run with no API key while still refusing to
assert numbers nothing has verified.
"""

from __future__ import annotations

from asx.parse.llm import ExtractionPass


class RulesExtractor:
    """Reads a document once, deterministically, using the parser's own rules.

    Implements enough of the StructuredExtractor interface for evaluate_doc,
    and declares single_pass so the framework knows there is no second reading
    to compare against and must not score this as an uncontested agreement.
    """

    single_pass = True

    def __init__(self, parser):
        self.parser = parser
        self.model = f"rules/{parser.name}@{parser.version}"

    def extract_text_pass(self, content: bytes, schema: dict,
                          task_prompt: str) -> ExtractionPass:
        return ExtractionPass(self.parser.read_rules(content), self.model, "rules")

    def extract_vision_pass(self, content: bytes, schema: dict,
                            task_prompt: str) -> ExtractionPass:
        raise NotImplementedError(
            "RulesExtractor reads once. A second call would return the same "
            "payload and be scored as two readings agreeing, which is exactly "
            "the false confidence the dual-pass design exists to prevent."
        )
