# Phase acceptance checklists

Phases gate on **written acceptance evidence**, not on "it seems to work"
(SPEC §15). This file is the ledger: each item gets a date, evidence link, and
sign-off when met. Code shipped ≠ phase accepted.

## Phase 0 — Foundation (SPEC §5.5)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 0.1 | Access decision document exists and its chosen sources are live | ☐ blocked on docs/ACCESS_DECISION.md sign-off | |
| 0.2 | Entity master covers every entity listed in the coverage window; ≥99% with resolved ACN or explicit `foreign` flag | ☐ needs ASIC dataset load | |
| 0.3 | Ticker history round-trips: every price-vendor symbol maps to exactly one entity per date; zero unexamined many-to-one collisions | ☐ needs price vendor | |
| 0.4 | Announcement feed ran 10 consecutive trading days with zero documents stuck in non-terminal status | ☐ needs live feed | |
| 0.5 | Freshness alerts proven by a deliberately injected failure | ☐ | |
| 0.6 | Classifier ≥98% precision on standard-form classes vs a 200-document hand-labelled sample | ☐ needs real documents | |
| 0.7 | Share-count replay within 0.5% of vendor for ≥95% of a 50-entity random sample; every miss investigated in writing | ☐ needs price vendor | |

## Phase 1 — Director transactions (SPEC §7)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1.1 | Gold-set field accuracy ≥98% on identifiers and quantities (100 hand-labelled 3Y forms per Appendix C) | ☐ synthetic rules gold in place; document gold needs real lodgements | fixtures/app3y/ |
| 1.2 | `onmkt_buy_cash` classification precision ≥95% on a 100-notice labelled sample | ☐ rules pinned by synthetic gold; measure on real sample | tests/test_gold_fixtures.py |
| 1.3 | Full forward coverage; freshness monitor detects an injected one-day outage | ☐ | |
| 1.4 | Backfill to access-decision horizon; volume-per-week plotted and eyeballed | ☐ | |
| 1.5 | Amended-notice dedupe demonstrated on real examples | ☐ mechanism tested synthetically | tests/test_db_integration.py::test_amended_notice_supersedes_earlier |

## Stop rules in force (SPEC §15)

- Access decision not landed within two weeks of starting it → project halts.
- Gold-set accuracy plateaus below target after two parser iterations → phase
  stops for design review; never ship with a lowered bar.
- Review queue not drained weekly → auto-accept halts automatically
  (enforced in code: `asx.parse.framework.auto_accept_halted`).
