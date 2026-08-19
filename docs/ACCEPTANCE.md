# Phase acceptance checklists

Phases gate on **written acceptance evidence**, not on "it seems to work"
(SPEC §15). This file is the ledger: each item gets a date, evidence link, and
sign-off when met. Code shipped ≠ phase accepted.

Criteria amended by the Tier 0 access decision are marked **[amended]** with
the reason. Amendments change *how* a criterion is evidenced, never the
standard it sets.

## Phase 0 — Foundation (SPEC §5.5)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 0.1 | Access decision document exists and its chosen sources are live | ✅ decided Aug 2026 (Tier 0 composite); sources live once the mailbox and capture watcher run | docs/ACCESS_DECISION.md |
| 0.2 | Entity master covers every entity listed in the coverage window; ≥99% carry an explicit identity — a resolved ACN, an ARBN with `foreign`, or an enumerated open review item **[amended]** — see below for why the original ACN-only wording could not be met | ✅ **100% covered, 96.0% numbered** (18 Aug 2026): 1,830/1,830 entities exist and join; 1,630 ACN + 127 ARBN; 73 enumerated in review | `asx coverage`, run 2026-08-18 |
| 0.3 | Ticker history round-trips: every symbol maps to exactly one entity per date, zero unexamined collisions **[amended]** — no price-vendor symbol history, so the check runs against ASX listed-companies file codes plus name/code-change announcements | ✅ **0 collisions** across 1,834 open listings (18 Aug 2026). One real merge was caught and fixed in the process — see the Kingston/Nexus note | `asx coverage`, run 2026-08-18; tests/test_entity_master.py |
| 0.4 | Announcement feed runs 10 consecutive trading days with zero documents stuck in non-terminal status **[amended]** — under Tier 0 this means zero parseable **detections** older than the 96h capture SLA, i.e. the manual sweep kept pace | ☐ | `asx monitor` capture_gap alarm |
| 0.5 | Freshness alerts proven by a deliberately injected failure | ☐ inject by pausing the mailbox read for >72h and confirming the detections_all volume alarm fires | |
| 0.6 | Classifier ≥98% precision on standard-form classes vs a 200-document hand-labelled sample | ☐ needs captured documents | |
| 0.7 | Share-count replay within 0.5% for ≥95% of a 50-entity random sample **[amended]** — compared against shares-on-issue figures **read manually from the ASX website** and recorded via `manual_share_counts`, in place of a vendor file | ☐ | `reconcile_against_manual()` |
| 0.8 | **[new]** Weekly ten-ticker manual completeness spot-check running as a standing job, with misses recorded | ☐ | `asx spot-check` |

### 0.2 — why 96% is the ceiling from the company register alone

The 73 unresolved entities are not a parser defect and are not spread evenly.
They are three populations:

| Population | Count | Why the ASIC company register cannot identify them |
|---|---|---|
| Listed trusts, REITs, LICs and stapled groups | 42 | Registered managed investment schemes hold an **ARSN**, not an ACN, and do not appear in ASIC's *company* dataset at all. Stapled groups list under a group name (`GOODMAN GROUP`, `CHARTER HALL GROUP`) that is nobody's registered company name. |
| Name in the ASX file matches no single current registration | 28 | Two live public companies share a normalised name (`ASM`, `GWA`), or the listed name differs from the registered one (`ATM` = PT Antam, an Indonesian issuer). |
| Foreign issuers whose registered name differs from their listed name | 3 | `JHX`, `ONE`, `FCL` are registered here, but under a name the file does not print. |

**This is a conflict between criterion 0.2 as written and the composition of
the ASX.** The criterion assumes every listed entity is a company with an ACN.
About 4% of the market is a scheme or stapled group, and no amount of parser
work will find those in a company register.

**Decision (owner, Aug 2026): accepted as-is.** The criterion is amended to
count an enumerated open review item as an explicit identity, and the 73 stay
in the queue rather than being chased. The reasoning is that a missing
registration number costs very little here: every join in the platform is on
`entity_id`, not on ACN (Invariant 1), so these entities carry their listings,
their universe membership and their documents exactly like any other. The ACN
matters for linking a document that cites one and for cross-source dedupe —
neither of which is load-bearing for a listed trust that lodges under its own
ticker.

What that decision does *not* do is hide them. All 73 are enumerated by name
and ticker in `asx coverage`, and the number is expected to move when the file
refreshes. Two things reopen it:

- **A jump in the count.** 73 is the structural floor; a rise means the
  resolver broke, not that the market changed.
- **A downstream need.** If Phase 2 or 3 wants ACN-keyed joins, the ABN Bulk
  Extract (already supported by `asx load-reference --source abn_bulk_extract`)
  names registered schemes and would identify most of the 42.

Nothing is auto-classified in the meantime. A name ending in "PLC" is a hint
printed in the coverage report, never a written `foreign` flag — inferring
incorporation from a suffix is exactly the substantive default Invariant 8
forbids.

**Consequence handled in code.** The stop rule in SPEC §15 halts parser
auto-accept whenever the review queue goes undrained for a week, and it
counted these items. Left alone, 73 permanently-open entries would have
silently stalled every parser in Phase 1 over a question the company register
cannot answer. `auto_accept_halted()` now ignores reference-load identity
items — they carry no `doc_id`, because no parse produced them. A stale item
raised by an actual extraction still halts, which is the behaviour the stop
rule was written for.

### The Kingston/Nexus merge — what loading real data caught

Worth recording because it is the failure mode Invariant 1 exists for, and it
survived the synthetic test suite.

`KINGSTON RESOURCES LIMITED` (KSN, ACN 009148529) was called `NEXUS MINERALS
NL` until 2012. An unrelated `NEXUS MINERALS LIMITED` (NXM, ACN 122074006) is
listed today. Both names normalise to `NEXUS MINERALS`. The first loader
version, finding the name ambiguous in the register, fell back to matching
against existing entity names — including former ones — and put **two
unrelated listed companies on one entity**, then overwrote Kingston's legal
name with Nexus's.

Three changes, each with a regression test:

- ACN resolution ranks **current** registered names above former ones, and
  discards registrations that cannot issue a listed security (proprietary
  companies — Corporations Act 2001 (Cth) s 113(3)). This alone lifted
  resolution from 79.7% to 96.0%, because a listed company and its
  same-named `PTY LTD` subsidiary were previously just "ambiguous".
- Entity lookup by name matches **currently-held** names only. Historical
  document references still resolve through former names; that is
  `ids.resolver`'s job, where matching a past name is the correct behaviour.
- A guard refuses to give one entity two open codes under different names,
  raising a review item instead. Genuine dual-class listings (`NWS`/`NWSLV`)
  are unaffected — the publisher prints the same name against both.

### 0.8 — the standing completeness job

Tier 0's characteristic failure is an announcement that was **never detected
at all** (alert not sent, watchlist gap, subscription lapsed). No automated
check can see that, because the platform has no independent list of what
exists. So it is checked by hand, weekly:

1. Run `asx spot-check --n 10 --days 7`.
2. For each of the ten entities, open its announcement list on the ASX site
   and compare against what the platform reports.
3. Record every announcement present on the site but absent from the platform
   as a **completeness miss**, with its date and class, in the log below.
4. Three or more misses in a fortnight is an access-decision review trigger
   (§5): the detection layer is not covering the universe.

**Completeness miss log**

| Week | Sample size | Misses | Notes |
|---|---|---|---|
| 19 Aug 2026 | 17 known 3Y/3Z lodged that day | **6** | Found accidentally, not by the spot-check: six Appendix 3Y documents were downloaded from another source and none had a detection. See below. |

### The 19 Aug 2026 miss — detection is watchlist-scoped

Six Appendix 3Y forms lodged on 19 August — ASM, CNQ, FSI, T92, MBG, LLM —
reached the platform as documents and had **no detection at all**. Not late,
not misclassified: no alert for those companies has *ever* arrived.

| | 3Y/3Z on 19 Aug 2026 |
|---|---|
| Detected via alerts | 11 |
| Held but never detected | 6 |
| **Detection rate** | **≤65%**, and that is an upper bound — it counts only the misses that happened to be found |

The cause is structural, not a bug: **Market Index alerts follow a watchlist**
(up to 200 codes). The platform therefore detects announcements for companies
the owner already follows, and is blind to the rest of the market by design.
Nothing in the pipeline could have reported this, because detection is what
makes a gap visible and there was no detection.

This matters beyond completeness. The cluster-buy signal looks for several
directors buying the same company — and a company you do not already follow
can never produce that signal, no matter how strong it is. A watchlist-scoped
feed can confirm a thesis about a company already on the list; it cannot
surface one.

Three ways to close it, for the owner's decision:

1. **Widen the watchlist** to the coverage universe. If Market Index caps at
   200 codes and the small-cap universe is ~1,475, this does not close it.
2. **Add a market-wide alert source** whose alerts are not watchlist-scoped.
   This is the strongest argument yet for evaluating investorpa.com or a
   paid feed — not for the PDFs, but for *unfiltered detection*.
3. **Accept the scope** and state it: the platform covers a followed list,
   not the market, and the acceptance criteria should say so rather than
   implying market-wide coverage.

Until one is chosen, criterion 0.4's "zero parseable detections older than
the capture SLA" measures the manual sweep keeping up with *what was
detected*, which is a weaker claim than it appears.

## Phase 1 — Director transactions (SPEC §7)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1.1 | Gold-set field accuracy ≥98% on identifiers and quantities (100 hand-labelled 3Y forms; ~120 targeted per access decision §2) | ◐ **4 of ~120** real forms captured and hand-labelled (19 Aug 2026); field extraction not yet measured — needs `ANTHROPIC_API_KEY` on the environment | fixtures/app3y/documents/gold.json |
| 1.2 | `onmkt_buy_cash` classification precision ≥95% on a 100-notice labelled sample | ◐ 6/6 real transactions classified correctly; two aggregate lines correctly refused. Real documents already forced one fix — see below | tests/test_app3y_real_documents.py |
| 1.3 | Full forward coverage; freshness monitor detects an injected one-day outage | ☐ | |
| 1.4 | Backfill to the access-decision horizon (~24 months) complete, volume-per-week plotted and eyeballed | ☐ manual retrieval; expect this to be the long pole | |
| 1.5 | Amended-notice dedupe demonstrated on real examples | ☐ mechanism tested synthetically | tests/test_db_integration.py |
| 1.6 | **[new]** Cluster-buy screen output carries its coverage flags, including `size_ceiling_proxy` | ✅ enforced in signal v2 | src/asx/signals/director_signals.py |

### What the first four real forms changed

Captured 19 Aug 2026: two Appendix 3Z and two Appendix 3Y, hand-labelled in
`fixtures/app3y/documents/gold.json` with 13 recorded traps. The classifier
handled six of six individual transactions correctly and correctly refused
both aggregate lines. One real defect surfaced immediately.

**Catalyst Metals (CYL) enumerated two events in one field:**

```
Number disposed
  1. 106,838 STI and 427,350 LTI Performance Rights were converted ...
  2. 1,000,000 fully paid ordinary shares were disposed of through
     on market trades
Value/Consideration
  2. Total consideration received for shares sold $6,410,050
```

The rules matched the first mechanism and returned `vesting_incentive` —
true of one event, and it buried the other: a **1,000,000-share on-market
sale for $6,410,050**, the exact opposite of the buy signal this platform
exists to find. A field that enumerates several transactions is now
`unknown`, because no single label is honest about it, and the line goes to
review to be split. The guard is anchored to list markers so a price
(`$1.215`) or a share count (`1,000,000`) can never trigger it.

Two more traps worth knowing before trusting any extraction:

- **Adrad (AHL)** nets to zero — 13,107 held before, 13,107 after — while
  four real trades worth ~$30k occurred inside it. Reconciling on before/after
  totals alone would see nothing happen.
- **Adrad's header `Date of change` reads 29 July 2026, but two of its four
  transactions occurred on 19 August.** Taking the header field as the event
  date dates half the notice wrongly. That is an Invariant 2 failure that no
  test on quantities would ever catch.

## Standing operational jobs

| Job | Cadence | Command |
|---|---|---|
| Alert-mailbox detection | daily | `asx detect` |
| Capture sweep (owner opens flagged announcements) | daily | `asx worklist` then `asx capture` |
| Reference refresh — ASIC registry, ASX listed file | monthly / weekly | `asx load-reference` |
| Index proxy refresh (ETF holdings) | weekly | `asx load-index` |
| Completeness spot-check (0.8) | weekly | `asx spot-check` |
| Monitoring + ops report | daily / weekly | `asx monitor`, `asx ops-report` |
| Coverage evidence | monthly | `asx coverage` |

The listing snapshot is the one job that can do irreversible damage if the
input is bad, so it refuses implausible files rather than recording mass
delistings — override with `--allow-shrink` only when the shrink is real.

## Out of scope under the current access decision

These are **not** open checkboxes — they are deliberately deferred with a
recorded reason, and the code refuses rather than approximating:

| Item | Why | Reopens when |
|---|---|---|
| Backtesting (event study, portfolio simulation, after-tax reporting) | Invariant 10 unsatisfiable without survivorship-complete prices; Invariant 4 unsatisfiable for the document set | A price vendor is subscribed |
| True market-cap screens and EV-based comparables | No price data | Same |
| Delisted-company document coverage | Not reachable on the ASX site | An archive route is adopted |

## Stop rules in force (SPEC §15)

- Gold-set accuracy plateaus below target after two parser iterations → phase
  stops for a design review; never ship with a lowered bar.
- Review queue not drained weekly → auto-accept halts automatically
  (enforced in code: `asx.parse.framework.auto_accept_halted`).
- Capture rate below 90% over 14 days, or ≥3 completeness misses per
  fortnight → access decision reopens (§5 review triggers).
