# CLAUDE.md — ASX Structural Alpha Platform

## Prime directives
- SPEC.md is authoritative. If a shortcut conflicts with a stated invariant, the shortcut is wrong even if tests pass. If the spec conflicts with a primary source (ASX Listing Rules, ASIC, JORC Code, legislation), the primary source wins: implement per the source, cite it in a code comment, and flag the spec for amendment.
- Never join on ticker. entity_id only. Tickers are effective-dated aliases.
- Every fact gets event_date AND knowable_at. Analytics join on knowable_at.
- Raw documents are immutable. Derived data must be regenerable. Never hand-edit canonical tables; fix the parser and reprocess.
- Delisted entities stay in every universe and every backfill.
- Share counts are replayed from events; never store adjusted values.
- An LLM extraction is a claim, not a fact: validate, reconcile, score, route. Ambiguous → 'unknown', never a substantive default.
- Zero lodgements in a period is a pipeline alarm until a human says otherwise.
- No order execution. No price prediction. No scraping beyond a source's terms — if access is the blocker, stop and say so.

## Working style
- Before writing any parser: build the gold fixture set and the failing tests first.
- Before writing any signal SQL: state the knowable_at logic in a comment; reviews check Invariant 2 first.
- When citing a rule number, form field, or tax parameter: verify against the primary source at implementation time and record the citation. Training-data memory of rule numbers is not sufficient.
- Uncertainty is reportable output: prefer a smaller correct dataset with coverage flags over a complete-looking one.

## Current state
- Read docs/HANDOVER.md first: what is built, what is blocked, and the decisions still open.

## Commands
- make test          # includes gold-set regression for every parser
- make reprocess ... # the only path for fixing systematic parse errors
- make ops-report    # weekly operations one-pager
