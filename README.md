# ASX Structural Alpha Data Platform

A personal-scale data platform that builds and maintains proprietary ASX datasets
from public regulatory disclosures: director transactions (Appendix 3Y/3Z), escrow
schedules, longitudinal shareholder registers, and — as optional modules —
tenement/approvals data and a JORC resource database.

**[SPEC.md](SPEC.md) is the authoritative design document.** Every design decision
here traces back to it. Its invariants (bitemporality, entity-keyed joins,
immutable raw zone, survivorship-complete universes, etc.) override convenience:
an implementation that violates an invariant is wrong even if its tests pass.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Foundation: entity master, document ingestion, security master, share-count replay | Implemented — awaiting access decision + live-feed acceptance |
| 1 | Director transactions (Appendix 3Y/3Z) | Implemented — awaiting gold fixtures from real lodgements |
| 2 | Escrow schedules and true float | Schema in place; parsers pending Phase 1 acceptance |
| 3 | Annual report extraction | Not started (gated on Phase 2) |
| A/B | Tenements / JORC | Not started (gated on Phase 3) |

Phase gates and acceptance criteria are in SPEC.md §15. Phase 0 cannot be
*accepted* until the access decision (docs/ACCESS_DECISION.md) is resolved and the
live feed has run for 10 consecutive trading days.

## Architecture

Four zones (SPEC §3):

- **raw/** — append-only object store of original document bytes, keyed by SHA-256,
  indexed by the `documents` table. Never mutated.
- **parsed** — per-document extraction outputs as JSON, versioned by parser version
  (`parsed_records` table). Reprocessing appends new versions.
- **canonical** — validated, reconciled relational model (`entities`, `listings`,
  `share_events`, `director_trades`, `escrow_parcels`, …). Mutated only by pipeline
  upserts with full provenance.
- **derived** — computed views and signal tables. Disposable, rebuilt on schedule.

Stack: PostgreSQL 16, Python 3.11+ with plain SQL (psycopg 3), Anthropic API for
LLM extraction with structured outputs, cron-driven idempotent jobs.

## Setup

```bash
pip install -e ".[dev]"
createdb asx                       # or use an existing PG 16
export DATABASE_URL=postgresql://asx:asx@localhost:5432/asx
make migrate
make test
```

`ANTHROPIC_API_KEY` (or an `ant auth login` profile) is required only for the LLM
extraction paths; everything else, including the full test suite, runs without it.

## Commands

```bash
make migrate       # apply db/migrations in order (idempotent)
make test          # unit + integration tests incl. gold-set regression
make monitor       # freshness / volume / parser-health checks (Invariant 7)
make ops-report    # weekly operations one-pager
make reprocess PARSER=app3y SINCE=2026-01-01          # dry-run diff report
make reprocess PARSER=app3y SINCE=2026-01-01 APPLY=1  # apply after review
```

## Compliance posture

All inputs are public regulatory disclosures and open government data. Source
access respects each provider's terms and rate limits (Invariant 11) — the
announcement source is a pluggable interface, and **no scraper ships until the
access decision in docs/ACCESS_DECISION.md is signed off**. The platform never
places orders, never predicts prices, and never pierces nominee holdings.
