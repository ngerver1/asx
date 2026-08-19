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
| _(no valid comparison yet — see below)_ | | | |

### Why there is no entry yet, and a retracted one

On 20 Aug 2026 six Appendix 3Y documents were captured that had no detection,
and this log briefly recorded them as six completeness misses implying a
detection rate of ≤65%. **That was wrong and has been withdrawn.**

The alert feed had been collecting for about twenty-six hours — 18 Aug 17:39
to 19 Aug 19:19 Sydney. One of the six (Loyal Metals) carries a date of
change of **20 August**, so it cannot have been lodged before the window
closed; the others are equally consistent with being lodged after 19:19 on
19 August or on 20 August. "No detection" is fully explained by the documents
falling outside the collection window. Nothing about the feed's coverage
follows from it.

The error is worth keeping on the page rather than deleting, because it is
the exact mistake this log exists to prevent: a gap was observed, a structural
cause was inferred, and the inference outran the evidence by an order of
magnitude. A wrong entry here is worse than an empty one — three misses in a
fortnight is an access-decision review trigger (§5), so a fabricated rate can
set off a review of a decision that is working.

**What a valid comparison requires**, whenever the standing spot-check (0.8)
first runs:

1. A full trading day where the alert feed ran from before the market opened
   until after the evening lodgement batch — the platform's own alerts show
   3Y/3Z arriving as late as 19:17, so a window ending earlier proves nothing.
2. The ASX's own list of announcements lodged that day for the sampled
   entities, so "was it lodged" and "did we hear about it" are independent
   facts rather than the same one.
3. Misses counted only where the lodgement time falls **inside** the window.

Until then, the honest statement is that detection coverage is **unmeasured**,
not that it is complete and not that it is partial.

**One question does remain open**, and it is a property of the source rather
than of the data volume: Market Index alerts are driven by a watchlist of up
to 200 codes. Whether that bounds coverage below the ~1,475-code universe is
answerable by reading the account settings, not by counting documents — and
it should be checked, because a watchlist-scoped feed can confirm a thesis
about a company already on the list but can never surface one, which is the
opposite of what the cluster-buy screen is for.

## Phase 1 — Director transactions (SPEC §7)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1.1 | Gold-set field accuracy ≥98% on identifiers and quantities (100 hand-labelled 3Y forms; ~120 targeted per access decision §2) | ✅ **98.5% (135/137)** over 23 dual-read lodgements, no API key — rules only. Both misses are the same self-contradicting form, where the parser refuses. Sample is 23 of the 100 the criterion asks for, so this is *met on the sample measured*, not closed | tests/test_app3y_rules.py::test_field_accuracy_against_dual_read_ground_truth |
| 1.2 | `onmkt_buy_cash` classification precision ≥95% on a 100-notice labelled sample | ◐ 6/6 real transactions classified correctly; two aggregate lines correctly refused. Real documents already forced one fix — see below | tests/test_app3y_real_documents.py |
| 1.3 | Full forward coverage; freshness monitor detects an injected one-day outage | ☐ | |
| 1.4 | Backfill to the access-decision horizon (~24 months) complete, volume-per-week plotted and eyeballed | ☐ manual retrieval; expect this to be the long pole | |
| 1.5 | Amended-notice dedupe demonstrated on real examples | ☐ mechanism tested synthetically | tests/test_db_integration.py |
| 1.6 | **[new]** Cluster-buy screen output carries its coverage flags, including `size_ceiling_proxy` | ✅ enforced in signal v2 | src/asx/signals/director_signals.py |

### Criterion 1.1, measured — and what it cost to get there

Measured 20 Aug 2026 against `fixtures/app3y/documents/ground_truth.json`:
23 lodgements, each read once and then re-read by a second reader who checked
the quantities, dates and transaction counts against the PDF.

| Field | Correct |
|---|---|
| entity_name | 23/23 |
| form | 23/23 |
| director_name | 23/23 |
| date_of_change | 21/21 |
| consideration | 9/9 |
| qty_after | 19/20 |
| qty_before | 17/18 |
| **total** | **135/137 = 98.5%** |

**No model, no API key.** An Appendix 3Y is a form: the ASX prescribes the
layout, so extraction is locating printed labels and taking the text between
them. The LLM fallback (SPEC §5.3) remains the answer for the residue — a
scanned page, an issuer who invented their own headings — and that residue
becomes review items rather than guesses.

Across all 60 captured documents (71 Appendix 3Y forms, 2 3Z, 2 3X):
entity name and director name **100%**, date of change 60/71, holding before
60/71, holding after 61/71. Every refusal was inspected; each is a form that
states more than one thing.

**Both remaining misses are one document.** Aurora Labs (328627) declares the
interest "Indirect", prints an unlabelled block of 540,907 ordinary shares
above a block headed "Indirect:" holding 400,000, and puts the securities
actually acquired in the *unlabelled* one. The form never reconciles this.
The parser returns nothing and raises a review item. The ground-truth readers
recorded 540,907 and flagged the same contradiction as unresolved — so this
is not a parser that fails to read a legible form, it is a form that does not
say. Counting it as a miss is deliberate: **this number must not be allowed
to improve by refusing more**, so the test scores a refusal as wrong.

#### The defects the measurement found

Six, and only one of them was the quantity gap that prompted the exercise.

1. **The ASX's own guidance was eating the values it guides.** The template
   prints `Value/Consideration  Note: If consideration is non-cash, provide
   details and estimated valuation  $40,000.00`. The pattern removing that
   note ended in an open run, which walked past the note's last word, through
   the value, and stopped at the first full stop it found — the decimal point.
   `$40,000.00` became `00`. **Six of the nine measured considerations were
   destroyed this way, silently.** Every pattern is now anchored on its own
   closing words.

2. **Words split mid-token defeated the patterns.** Brightstar's PDF extracts
   as `Note: If c onsideration is non -cash`, Pivotal's as `secu rities`,
   FMR's as `Shar e Performance Rights`. Patterns and labels are now matched
   through a form that tolerates whitespace anywhere inside a word.

3. **Holdings cells are lists, not numbers.** On a third of real lodgements
   the cell enumerates several parcels by class and by holder. Taking the
   first number read Terra Critical Minerals' director as holding
   **1,205,155 shares instead of 27,765,832** — his direct parcel in place of
   the indirect one the notice was about, understating an insider by 23× in
   the direction that makes them look smaller. Cells are now read as parcels
   and a parcel is chosen by evidence printed on the form: its class, the
   form's own arithmetic (after − before = acquired − disposed), the stated
   interest, or the printed TOTAL. Failing all four, nothing.

4. **Dates.** `13th August 2026` read as nothing; `A. 17 August 2026
   B. 13 August 2026` read as the first of two, which would date a conversion
   to the day of an unrelated lapse. Ordinals are now read and enumerations
   refused — the same defect that once buried a $6.4m sale behind a vesting.

5. **A Word bookmark artifact hid two directors.** `0BName of Director Rowena
   Smith`: the digits are word characters, so the word-boundary anchor in
   front of the label never matched and the name was lost with no error.

6. **A label nested inside another label stole its cell.** The 3Z prints
   `Number & class of securities`, where the generic `Class` label matches
   nine characters in — so the holding was captured as the class and the
   holding read empty.

Five of the six were invisible to the parser: it produced a plausible number
or a clean blank, and nothing failed. They were found only by measuring
against documents a human had read.

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
