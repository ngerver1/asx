# ASX Structural Alpha Data Platform — Design Specification

**Status:** Draft for implementation · **Audience:** Claude Code (implementing agent) and the owner (reviewing human) · **Version:** 1.0 · **Date:** August 2026

This document is the authoritative design for a personal-scale data platform that builds and maintains five proprietary ASX datasets: director transactions, escrow schedules, longitudinal shareholder registers, and — as optional modules — tenement/approvals data and a JORC resource database. It is written to be consumed by an implementing agent. Every design decision that looks like over-engineering has a stated reason. **If a section explains why a shortcut fails, the shortcut is prohibited even if it would pass tests.** When this document and convenience conflict, this document wins; when this document and a primary source (ASX Listing Rules, ASIC guidance, JORC Code) conflict, the primary source wins and this document should be amended with a citation.

---

## 1. Purpose and investment context

The owner is a private Australian investor. The thesis this platform serves has three parts.

First, the durable retail advantages are structural, not analytical: the ability to hold positions too small for institutional capacity, the absence of any mandate or tracking-error constraint, permanent capital that is never force-sold, and willingness to do unscalable work that no fund would pay an analyst to do. None of these advantages involve predicting prices, and this platform must never drift toward price prediction.

Second, the historical bottleneck on exploiting those advantages was grunt work on unstructured public disclosures at a scale too small to justify hiring anyone. LLM-based extraction collapsed that bottleneck. The product of this platform is therefore **datasets that do not exist elsewhere because nobody could previously be bothered building them**, plus the joins between them. The competitive moat is correctness, coverage, and freshness of boring data — not model sophistication.

Third, the alpha lives at the *joins*: an escrow release into a register that is quietly consolidating; directors buying on-market into forced selling; tenement accumulation by a subsidiary adjacent to a competitor's resource growth. Single-dataset signals are commodity; cross-dataset point-in-time joins on a correct entity graph are not. This is why identity resolution and bitemporal correctness receive disproportionate design attention below: **an error in entity identity or in "when was this knowable" silently manufactures fake alpha, which is worse than no data at all.**

What this platform is not: it is not a trading system (it never places orders), not a real-time system (end-of-day cadence is sufficient everywhere), not a price-prediction system, and not an attempt to access anything non-public. Every input is a public regulatory disclosure or open government dataset.

---

## 2. Design invariants — the anti-shortcut charter

These invariants apply to every phase and every module. They exist because each one blocks a specific, tempting shortcut that has a specific, documented failure mode. An implementation that violates an invariant is wrong even if its tests pass. Each invariant states the shortcut it prohibits.

**Invariant 1 — Identity keys on the entity, never the ticker.** ASX tickers are display aliases: they change on renames, restructures, and relistings, and codes are recycled onto unrelated companies within a few years in the small-cap segment. The canonical key is an internal `entity_id` anchored to ACN (Australian Company Number) where one exists, with tickers held in an effective-dated mapping table. *Prohibited shortcut: using ticker as a primary or join key anywhere in the schema.* The failure mode is silent cross-contamination — trades, tenements, and registers from two unrelated companies merged under a recycled code, which is undetectable downstream.

**Invariant 2 — Every fact is bitemporal.** Every stored fact carries two dates: `event_date` (what the fact is true *of*) and `knowable_at` (the timestamp at which a market participant could first have known it — normally the lodgement timestamp). All analytics and backtests join on `knowable_at`, never `event_date`. *Prohibited shortcut: single-dated tables, or backtests joining on event_date.* The failure mode is look-ahead bias: an annual report's Top 20 register is "as at" a date typically two to three months before the report is published; a backtest that acts on the as-at date is trading on information that did not exist yet, and the resulting "alpha" is fiction.

**Invariant 3 — Raw is immutable; everything derived is regenerable.** Every fetched document is stored as original bytes with a SHA-256 hash, source URL, and fetch timestamp, in an append-only raw zone. No process ever mutates or deletes raw. All parsed and derived tables must be fully rebuildable from raw by rerunning pipelines. *Prohibited shortcut: parsing in-flight and discarding the source, or "fixing" parsed data by hand-editing derived tables.* The failure mode is unrecoverable corruption: when a parser bug is found (it will be), there is nothing to reprocess, and hand-edits are silently destroyed by the next pipeline run.

**Invariant 4 — Delisted entities are permanent citizens.** Companies that delist, are acquired, or fail remain in the entity master, the document archive, and every historical universe snapshot. Universe membership is itself an effective-dated table. *Prohibited shortcut: building the universe from "currently listed companies".* The failure mode is survivorship bias — the single largest source of overstated small-cap backtest returns, because the companies that would have lost you money are precisely the ones missing from a current-listings universe.

**Invariant 5 — Corporate actions are events, not overwrites.** Share counts, prices, and per-share quantities are stored raw, alongside a table of capital-reorganisation events (consolidations, splits, buyback cancellations) with their ratios and dates. Adjusted series are computed at read time. Australian small caps consolidate constantly — 1:10 and 1:20 consolidations are routine — so this is not an edge case. *Prohibited shortcut: storing pre-adjusted values, or applying adjustments destructively.* The failure mode: one missed or double-applied consolidation corrupts every historical percentage-of-float and per-share figure for that entity, invisibly.

**Invariant 6 — "Parsed" is not "true".** Every extraction carries a confidence score, must pass schema validation (types, ranges, enum membership, arithmetic consistency), and where an independent source exists must be reconciled against it (e.g., share counts from Appendix 2A lodgements versus the annual report versus the ASX shares-on-issue figure). Failures route to a human review queue; nothing failing validation enters canonical tables. *Prohibited shortcut: treating a successful LLM call as a successful extraction, or skipping the review queue "for now".* The failure mode is confident garbage: a misread table feeds a screen, the screen produces a position, and the error is discovered by the P&L.

**Invariant 7 — Silence is an alarm.** Every feed has a freshness SLO and an expected-volume baseline (e.g., the count of Appendix 3Y lodgements per week across the universe has a known seasonal distribution). Zero lodgements is treated as a probable pipeline failure, not a quiet market, until a human confirms otherwise. *Prohibited shortcut: alerting only on exceptions raised in code.* The failure mode is the stale-parser problem: a source changes its HTML, the fetcher returns empty successfully, and the platform serves months-old data as current. **A stale dataset presented as fresh is strictly worse than no dataset**, because it is trusted.

**Invariant 8 — Categories never blend.** Classification enums (JORC resource categories, Appendix 3Y trade classifications, Appendix 9B escrow categories) always include `other`/`unknown`, and no process coerces an ambiguous case into a substantive category to make a number aggregate. Aggregations across categories are explicit, flagged operations. *Prohibited shortcut: defaulting ambiguous director trades to "on-market buy", or summing Inferred resources into reserve-like totals.* The failure mode is signal pollution: the on-market-cash-buy signal only works because it excludes option exercises and DRP participation; one lazy default poisons it.

**Invariant 9 — Units and bases are explicit columns.** Every quantity carries its unit (g/t, %, ppm, tonnes, AUD, shares) and, where applicable, its basis (franking balances are reported at a stated company tax rate basis, resource figures at a stated equity share and cut-off grade). Normalisation to comparable units is a separate derived layer that preserves the as-reported values. *Prohibited shortcut: normalising at parse time and discarding the reported form.* The failure mode: unresolvable disputes with the source document, and silent errors when the normalisation assumption (e.g., 100% equity share) was wrong.

**Invariant 10 — Performance figures are point-in-time, after costs, after tax, or they are not reported.** The backtest harness enforces `knowable_at` lags (act no earlier than the next session after lodgement), models spread and brokerage by liquidity bucket, and applies a pluggable Australian tax regime (see §12). *Prohibited shortcut: gross-return backtests, or same-day execution on lodgement-day information.* The failure mode is self-deception expensive enough to fund the whole project several times over.

**Invariant 11 — Data access is licensed and rate-respectful.** All external sources sit behind interface abstractions (`AnnouncementSource`, `PriceSource`, `TenementSource`) so providers can be swapped. The implementation must respect each source's terms of use and rate limits, and must not assume free unlimited scraping of the ASX website, whose terms restrict it. Where licensed data is required, the system says so and stops rather than working around it. *Prohibited shortcut: hard-coding scrapers against ToU, or rotating IPs/user-agents to evade limits.* The failure mode is legal exposure and source cut-off, i.e., the whole platform dying at its root.

**Invariant 12 — Provenance on every row.** Every row in every canonical and derived table is traceable to the `doc_id`(s) it came from. *Prohibited shortcut: derived tables without source references.* The failure mode: an anomalous number that cannot be audited must be treated as wrong, which makes the whole dataset untrustworthy.

**Invariant 13 — The system never executes.** No order placement, no broker write-APIs, no automation that commits capital. Output is data, screens, alerts, and reports for a human decision-maker.

---

## 3. Architecture overview

The architecture is deliberately boring. Novelty budget is spent on parsers and data quality, nowhere else.

**Zones.** Data flows through four zones. The **raw zone** is an append-only object store (local filesystem or S3-compatible) holding original document bytes, keyed by hash, with a `documents` index table in Postgres. The **parsed zone** holds per-document extraction outputs as JSON, versioned by parser version, so reprocessing produces a new version rather than overwriting. The **canonical zone** is the relational model described per-phase below — validated, reconciled, review-cleared facts. The **derived zone** holds computed views: float series, adjusted share counts, signals, screens. Raw and parsed are append-only; canonical is mutable only via pipeline upserts with full provenance; derived is disposable and rebuilt on schedule.

**Stack.** PostgreSQL 16 (PostGIS extension added only if Module A is built). Python 3.12 with plain SQL or a thin query layer — no heavyweight ORM magic that obscures the effective-dating logic. Orchestration via a simple scheduler (cron or a lightweight tool like Prefect); every job idempotent, so reruns are always safe. LLM extraction via the Anthropic API with structured outputs at temperature 0. A minimal internal web UI (or even CLI + SQL views) for the review queue; do not gold-plate this.

**Two clocks everywhere.** All timestamps stored in UTC with a stated market-time convention (ASX lodgements interpreted in Australia/Sydney, converted on ingest). The `knowable_at` convention: for ASX announcements, the ASX release timestamp; for annual-report contents, the release timestamp of the report announcement, not any date printed inside the document; for government datasets, the dataset's published extract date, not the record's internal date.

**Reprocessing as a first-class operation.** `reprocess --parser=app3y --version=N --since=DATE` re-parses raw documents with the current parser, writes new parsed-zone versions, re-runs validation and reconciliation, and produces a diff report of canonical changes for human review before applying. This must exist from Phase 1; it is how parser bugs get fixed safely.

---

## 4. Universe and reference data

**Universe definition.** The working universe is all entities that have been ASX-listed at any point during the coverage window (target: 2015-present for backfill where source access permits; forward coverage complete from go-live). Universe membership is stored as `universe_membership(entity_id, listed_from, listed_to, delist_reason)`. Analytical sub-universes (e.g., "sub-$300m market cap ex-ASX200") are defined as dated queries over this table plus the price/share-count series — never as static lists.

**Prices.** End-of-day OHLCV including delisted securities is a purchased input, not a scraping project. A survivorship-complete ASX EOD vendor (Norgate Data is the commonly used retail example; any equivalent with delisted coverage and corporate-action data is acceptable) should be budgeted from day one. *Prohibited shortcut: free price sources that silently drop delisted names — this violates Invariant 4 at the root.* Vendor corporate-action data is reconciled against our own capital-reorganisation events as a cross-check on both.

**Reference sources (all free, all stable):** the ASIC Companies dataset (data.gov.au, refreshed regularly) for ACN, legal name, status, and registration data; the ABN Bulk Extract for ABN↔entity-name mapping; the ASX listed-companies file for the current code↔name snapshot; ASIC's daily short-position reports (used later as a derived-signal input, not a core dataset).

---

## 5. Phase 0 — Foundation

Phase 0 produces no investment output. Its acceptance criteria are therefore mechanical and strict, because every later phase inherits its defects.

### 5.1 Access strategy — resolve before writing pipeline code

The single most common death of projects like this is discovering, three weekends in, that announcement access doesn't work at the required scale. Therefore the first deliverable of Phase 0 is a short written access decision covering: (a) the chosen announcement source for forward daily coverage; (b) the chosen approach and realistic horizon for historical backfill; (c) confirmation that both comply with the source's terms; (d) cost. Options to evaluate, in order of preference: a licensed market-data provider that redistributes ASX announcements; the ASX website's public announcement pages used strictly within its terms and at polite request rates (which likely constrains backfill depth — accept a shorter backfill rather than violate terms); company investor-relations pages which republish their own announcements (useful for targeted gap-filling, unsuitable as the primary feed). If no compliant path supports the desired backfill horizon, **shorten the horizon — do not escalate the scraping.** Forward coverage from go-live is worth more than deep history obtained badly, because forward data is what generates live signals and the archive compounds automatically from day one.

### 5.2 Entity master

```sql
CREATE TABLE entities (
  entity_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  acn            CHAR(9) UNIQUE,          -- null only for foreign-incorporated listcos
  abn            CHAR(11),
  entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('company','trust','stapled','foreign','person','other')),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE entity_names (
  entity_id      BIGINT NOT NULL REFERENCES entities,
  name           TEXT   NOT NULL,
  name_norm      TEXT   NOT NULL,         -- normalised form, see below
  name_kind      TEXT   NOT NULL CHECK (name_kind IN ('legal','former','trading','alias')),
  valid_from     DATE   NOT NULL,
  valid_to       DATE,                    -- null = current
  source_doc_id  BIGINT REFERENCES documents
);

CREATE TABLE listings (
  listing_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      BIGINT NOT NULL REFERENCES entities,
  exchange       TEXT   NOT NULL DEFAULT 'ASX',
  ticker         TEXT   NOT NULL,
  security_class TEXT   NOT NULL DEFAULT 'ORD',   -- ORD, restricted classes, notes, etc.
  valid_from     DATE   NOT NULL,
  valid_to       DATE,
  source_doc_id  BIGINT REFERENCES documents
);
```

Name normalisation (`name_norm`) is a pure function used identically everywhere: uppercase, unicode-fold, strip punctuation, collapse whitespace, remove corporate suffixes (LIMITED, LTD, PTY, PROPRIETARY, NL, INC, HOLDINGS as a trailing token). The resolver pipeline for any free-text name is: exact match on `name_norm` → alias table → fuzzy match (token-set similarity with a conservative threshold) → LLM adjudication with the candidate list → human review queue for anything below the LLM-confidence threshold. Every non-exact resolution is stored in the alias table with its method and confidence, so the same string never needs re-adjudication. *Why this much machinery: three of the five veins (registers, tenements, and the subsidiary tree) are fundamentally "messy name → entity" problems, and nominee/subsidiary names are adversarially unhelpful. A resolver that guesses is a resolver that merges unrelated entities.*

Ticker history is seeded from the current ASX file and then maintained from name/code-change announcements. Where backfill announcements are unavailable, ticker history for the backfill period is reconstructed from the price vendor's symbol history and flagged `source='vendor'`.

### 5.3 Announcement ingestion

```sql
CREATE TABLE documents (
  doc_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source           TEXT NOT NULL,             -- provider identifier
  source_ref       TEXT,                      -- provider's id/url
  entity_id        BIGINT REFERENCES entities,-- null until resolved
  ticker_as_lodged TEXT,                      -- verbatim, for audit
  title            TEXT,
  asx_doc_types    TEXT[],                    -- ASX-assigned type codes if provided
  doc_class        TEXT,                      -- our classifier output, see taxonomy
  price_sensitive  BOOLEAN,
  lodged_at        TIMESTAMPTZ,               -- knowable_at for its contents
  fetched_at       TIMESTAMPTZ NOT NULL,
  sha256           CHAR(64) NOT NULL,
  storage_path     TEXT NOT NULL,
  parse_status     TEXT NOT NULL DEFAULT 'unparsed'
                   CHECK (parse_status IN ('unparsed','parsed','validated','review','rejected','not_applicable'))
);
CREATE UNIQUE INDEX ON documents (sha256);
```

Every fetched document reaches a terminal `parse_status`; a nightly job counts documents stuck in `unparsed` beyond SLA (Invariant 7). Classification is rules-first (ASX type codes and title regexes catch the overwhelming majority of standard forms), LLM-fallback for the remainder, with the classifier's taxonomy including at minimum: `app_3y`, `app_3z`, `app_3b`, `app_2a`, `lr_3_10a_notice`, `substantial_603`, `substantial_604`, `substantial_605`, `annual_report`, `half_year`, `quarterly_4c_5b`, `capital_reorg`, `notice_of_meeting`, `prospectus`, `cleansing_notice`, `other`. Note that ASX has migrated some appendices from PDF to structured online forms in recent years; the ingestion layer must detect and prefer structured payloads where the provider supplies them, falling back to PDF parsing otherwise — **check the current lodgement format per form before writing each parser; do not assume PDF.**

### 5.4 Security master and share-count history

```sql
CREATE TABLE security_classes (
  entity_id   BIGINT NOT NULL REFERENCES entities,
  class_code  TEXT   NOT NULL,        -- e.g. ORD ticker, restricted-class code, notes code
  class_kind  TEXT   NOT NULL CHECK (class_kind IN ('ordinary','restricted','option','note','pref','other')),
  description TEXT,
  valid_from  DATE NOT NULL,
  valid_to    DATE,
  PRIMARY KEY (entity_id, class_code, valid_from)
);

CREATE TABLE share_events (
  event_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id    BIGINT NOT NULL REFERENCES entities,
  class_code   TEXT   NOT NULL,
  event_kind   TEXT   NOT NULL CHECK (event_kind IN
               ('quotation','issue_proposed','buyback_cancel','consolidation','split',
                'escrow_release_reclass','delist_cancel','other')),
  event_date   DATE   NOT NULL,
  knowable_at  TIMESTAMPTZ NOT NULL,
  qty_delta    NUMERIC,               -- signed share count change, null for ratio events
  ratio_num    NUMERIC,               -- e.g. consolidation 1:10 → num=1, den=10
  ratio_den    NUMERIC,
  source_doc_id BIGINT NOT NULL REFERENCES documents
);
```

`shares_outstanding(entity_id, class_code, date)` is a **derived view** computed by replaying `share_events` from an anchored opening balance — never a stored column that pipelines overwrite (Invariant 5). Appendix 2A (application for quotation) is the authoritative "these shares now trade" event; Appendix 3B records proposed issues and is used for anticipation, not counts. Capital reorganisations are detected via the `capital_reorg` document class and parsed for their ratio and effective date. Restricted security classes (e.g., a code like `XXXAE` alongside ordinary `XXX`) are first-class rows in `security_classes` — their existence and size is the cheap, reliable escrow signal that Phase 2 builds on.

Reconciliation job (weekly): replayed share count per entity versus (a) the price vendor's shares-outstanding figure and (b) the most recent annual report's issued-capital note once Phase 3 exists. Discrepancies beyond a small tolerance open review items. *Why: every downstream percentage — float released, register concentration, EV — divides by this number.*

### 5.5 Phase 0 acceptance criteria

Phase 0 is complete when: the access decision document exists and its chosen sources are live; the entity master covers every entity listed at any point in the coverage window with ≥99% having a resolved ACN or an explicit `foreign` flag; ticker history round-trips (every price-vendor symbol maps to exactly one entity per date, with zero many-to-one collisions unexamined); the announcement feed has run for 10 consecutive trading days with zero documents stuck in non-terminal status and freshness alerts proven by a deliberate injected failure; the classifier achieves ≥98% precision on standard-form classes measured against a 200-document hand-labelled sample; and share-count replay matches the vendor figure within 0.5% for ≥95% of a 50-entity random sample, with every miss investigated and explained in writing.

---

## 6. Shared parsing framework

All five veins are parser-heavy; the framework is built once, in Phase 1, and every later parser is a schema plus prompts plus fixtures plugged into it. The pipeline stages for any document are: **locate** (find the relevant section/table within the document — trivial for one-page forms, the hard half of the problem for annual reports), **extract** (structured output against a typed schema), **validate** (types, enum membership, ranges, and arithmetic self-consistency — e.g., "held after = held before + acquired − disposed" for a 3Y), **reconcile** (cross-check against independent sources where they exist), **score** (confidence from extractor agreement and validation results), and **route** (auto-accept above threshold, else review queue).

**LLM extraction rules.** Temperature 0, structured outputs against a JSON schema mirroring the target table, page images supplied alongside text for anything with table layout (layout is information; text-only extraction of financial tables loses column alignment). For every parser, run **dual-pass extraction** — two independent calls, ideally one text-based and one vision-based — and treat field-level disagreement as an automatic route-to-review. This roughly doubles parse cost and is worth it; documents are parsed once and stored forever, so parse cost is capital expenditure, not operating expense. The extraction prompt must instruct the model to return `null` with a reason rather than guess, and the schema must make every field nullable so that "couldn't read it" is representable (Invariant 8's `unknown` at field level).

**Gold fixtures.** Before a parser processes production documents, build a hand-labelled gold set: minimum 100 documents for standard forms, 50 for annual-report sections, stratified across years (form layouts drift), issuer size, and format era (PDF vs structured online form). Gold sets live in the repo under `fixtures/`, and CI runs every parser against its gold set on every change: **a parser change that reduces gold-set accuracy does not merge.** Accuracy is measured per field, exact-match for identifiers and quantities, tolerance-band for derived numbers. The labelling protocol: label from the document alone, record ambiguities as `unknown` rather than resolving them from outside knowledge, and have every gold label spot-checked once (self-review after a gap is acceptable at personal scale).

**Review queue.** A single table `review_items(item_id, kind, doc_id, payload, reason, created_at, resolved_at, resolution, resolver_note)` served by the simplest possible UI. Resolutions write back through the same validation gate as automated extraction. Weekly SLA: the queue is drained weekly; a queue older than two weeks halts the affected feed's auto-accept path (better to stop than to let the auto-accept threshold quietly become the only gate). Track and review the auto-accept rate per parser monthly: a rising review rate is a drift alarm (Invariant 7); a suspiciously falling one may mean validation has weakened.

**Parser versioning.** Every parsed-zone record carries `parser_name`, `parser_version`, `model_id`, and `prompt_hash`. Reprocessing (§3) is the only path for correcting systematic errors.

---

## 7. Phase 1 — Director transactions (Appendix 3Y / 3Z)

**Why this phase is first:** Appendix 3Y (change of director's interest) is the simplest standard form in scope, arrives at meaningful volume, and exercises every part of the platform — feed, classifier, parser framework, entity resolution, review queue, monitoring — at low ambiguity. It is the cheapest way to prove the whole pipeline before the expensive parsers are written. Appendix 3Z (final director's interest) closes out a director's tenure and is parsed with the same machinery.

**Source and timing.** Directors' interest changes must be disclosed within five business days (ASX Listing Rule 3.19B — verify the current rule text at implementation and cite it in code comments). The lag between `event_date` (date of change) and `knowable_at` (lodgement) is therefore up to a week and must be preserved exactly; the on-market-buy signal only exists after lodgement.

```sql
CREATE TABLE director_trades (
  trade_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id       BIGINT NOT NULL REFERENCES entities,
  person_name_raw TEXT   NOT NULL,
  person_id       BIGINT REFERENCES persons,       -- soft link, see below
  doc_id          BIGINT NOT NULL REFERENCES documents,
  supersedes_doc  BIGINT REFERENCES documents,     -- amended notices
  event_date      DATE   NOT NULL,
  knowable_at     TIMESTAMPTZ NOT NULL,
  interest_nature TEXT   CHECK (interest_nature IN ('direct','indirect','unknown')),
  indirect_detail TEXT,                            -- trust/super fund/spouse text, verbatim
  security_class  TEXT   NOT NULL,
  qty_acquired    NUMERIC,
  qty_disposed    NUMERIC,
  consideration_text TEXT,                         -- verbatim
  consideration_aud  NUMERIC,
  price_per_unit  NUMERIC,                         -- derived where safely computable
  held_before     NUMERIC,
  held_after      NUMERIC,
  classification  TEXT NOT NULL CHECK (classification IN
    ('onmkt_buy_cash','onmkt_sell','exercise','placement_participation','spp_participation',
     'drp','rights_participation','vesting_incentive','offmkt_transfer','margin_or_forced',
     'buyback_into','other','unknown')),
  confidence      NUMERIC NOT NULL,
  review_status   TEXT NOT NULL DEFAULT 'auto'
);
```

**Classification is the product.** The raw 3Y feed has near-zero signal value; the value is separating on-market cash purchases from everything else. Classification is rules-first on the consideration and nature text ("on-market" / "on market trade", "exercise of options", "participation in placement", "dividend reinvestment", "nil consideration", "off-market transfer") with LLM fallback, and per Invariant 8 anything ambiguous is `unknown`, never defaulted. Track base rates: on-market cash buys are a minority of lodgements; if the classifier's output distribution drifts from the historical base rate, alarm.

**Known gotchas, all mandatory to handle:** amended and replacement notices (dedupe via `supersedes_doc`, detected from title and same-director-same-date heuristics, latest wins); multiple securities per notice (options and shares in one form → multiple rows, one per class); indirect interests via trusts and super funds (keep the verbatim detail — a purchase by the director's SMSF is still a cash buy, a transfer between the director's own vehicles is not a trade at all); aggregate consideration over multiple trades (derive `price_per_unit` only when the form supports it, else null); joint directorships (the same human on multiple boards — `persons` is a soft identity table keyed on normalised name with manual merge only, because names collide and no DOB is available; never auto-merge persons).

**Derived signals (definitions live in code, versioned):** cluster buying (≥2 distinct directors, `onmkt_buy_cash`, within a 30-day window, in an entity below a market-cap ceiling); conviction sizing (consideration relative to the director's disclosed prior holding); first-ever buys by long-tenured directors. Signal tables are derived-zone and carry `signal_version`.

**Acceptance criteria.** Gold-set field accuracy ≥98% on identifiers and quantities; classification precision ≥95% specifically on `onmkt_buy_cash` measured against a 100-notice labelled sample (this class's precision is the phase's entire point); full forward coverage with the freshness monitor demonstrating detection of an injected one-day outage; backfill to the access-decision horizon complete with volume-per-week plotted and eyeballed against expectations; amended-notice dedupe demonstrated on real examples.

---

## 8. Phase 2 — Escrow schedules and true float

**Why this phase matters most per unit of effort:** escrow releases are dated, mechanical supply shocks disclosed in advance, in exactly the segment (recent small-cap listings) where the holder base is least able to absorb them. The deliverable is a forward calendar of release events sized as a percentage of true free float, with the holder category attached.

**Sources, in order of authority:** (1) the existence and size of restricted security classes in the security master — ASX-mandated escrow shows up as a separate restricted class code, giving quantity essentially for free; (2) Listing Rule 3.10A notices, which companies must lodge ahead of restricted securities being released, giving the dated forward calendar; (3) Appendix 2A quotation applications on release, confirming the reclassification and quantity actually hitting the quoted class; (4) the prospectus escrow section, which is the *only* source for **voluntary** escrow terms (holder names, quantities, and release conditions agreed contractually rather than imposed by ASX). Items 1–3 are standard-form parsing on infrastructure that already exists after Phase 1. Item 4 requires the long-document section-retrieval machinery built properly in Phase 3 — therefore voluntary-escrow coverage is implemented as a Phase 3 deliverable feeding Phase 2's tables, and until then the escrow calendar carries an explicit `coverage='mandatory_only'` flag. *Prohibited shortcut: presenting the mandatory-only calendar as complete float coverage. Voluntary escrow on IPOs is frequently larger than mandatory escrow.*

```sql
CREATE TABLE escrow_parcels (
  parcel_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      BIGINT NOT NULL REFERENCES entities,
  class_code     TEXT,                    -- restricted class where applicable
  escrow_kind    TEXT NOT NULL CHECK (escrow_kind IN ('asx_mandatory','voluntary')),
  holder_category TEXT CHECK (holder_category IN
    ('promoter','seed_capitalist','vendor','related_party','professional','employee','other','unknown')),
  holder_name_raw TEXT,
  holder_entity_id BIGINT REFERENCES entities,
  qty            NUMERIC NOT NULL,
  escrow_start   DATE,
  release_date   DATE,                    -- null for condition-based voluntary escrow
  release_condition TEXT,                 -- verbatim for milestone-based releases
  source_doc_id  BIGINT NOT NULL REFERENCES documents,
  knowable_at    TIMESTAMPTZ NOT NULL
);
```

Holder categories follow Appendix 9B's classification of who received the securities and for what consideration; the category matters because sell propensity differs — seed capitalists holding stock issued at a deep discount to the IPO price are the classic forced-supply cohort, founders less so. **These propensity priors are heuristics and must be labelled as such in any screen output, never presented as data.**

**True-float series (derived):** `float_series(entity_id, date, shares_quoted, shares_restricted, escrowed_voluntary_est, strategic_holdings_est, free_float_est, coverage_flags)`. Strategic holdings estimates come from substantial-holder notices (parsed with the same standard-form machinery; Forms 603/604/605 under the Corporations Act, generally lodged within two business days of the change) and, after Phase 3, the Top 20 register. Every estimate column carries its coverage flag; a float estimate without stated coverage is prohibited output.

**Derived signals:** forward calendar of releases where released quantity exceeds a threshold percentage of current free float within a horizon window; cliff versus staggered release structure; the cross-join flags (release into a consolidating register, directors buying through a release window) once Phases 1 and 3 supply the other sides.

**Acceptance criteria.** Every restricted class in the security master is either linked to at least one escrow parcel or flagged for review; 3.10A→2A pairing rate measured and unpaired events investigated (a release notice with no subsequent quotation is either a parser miss or genuinely interesting); release-quantity reconciliation between the 3.10A notice, the 2A, and the restricted-class balance within tolerance; a rolling 12-month forward calendar renders for the full universe; gold-set accuracy per the framework standard.

---

## 9. Phase 3 — Annual report extraction (registers, subsidiary tree, franking)

**Why this phase is a step-change in difficulty:** everything before it parses one-to-three-page standard forms; annual reports are 100–300 page documents where the extraction problem is dominated by *finding* the right pages. Feeding whole reports to an LLM is prohibited — not for cost reasons alone, but because retrieval precision is what makes extraction auditable. The locate stage here is real engineering: parse the PDF outline/ToC where present, detect headings by layout heuristics, fuzzy-match against a target-section vocabulary, and hand the extractor only the located pages (with images). Every extraction records which pages it came from.

Three target extractions, one retrieval problem, in priority order:

**9.1 Top 20 shareholders (→ longitudinal registers).** The statutory shareholder-information section lists the twenty largest registered holders "as at" a stated date. Critical semantics: the as-at date is the `event_date` and is typically two-to-three months before the report's release (`knowable_at`) — Invariant 2's canonical example. Registered holders are dominated by custodian nominees (HSBC Custody Nominees, J P Morgan Nominees, Citicorp Nominees, National Nominees, BNP Paribas Noms and the like). **Custodians are not beneficial holders and no attempt is made to pierce them** (prohibited shortcut: treating nominee accounts as investors, or netting them against substantial-holder notices as if they were reconcilable — they are different measurement systems). Instead: flag `is_custodian` from a maintained custodian-name list, and analyse (a) the non-custodian residual, where small-cap founders, boards, and strategic holders actually appear, and (b) custodian-bucket *changes* over time, which proxy institutional flow without claiming to identify the institution. Beneficial-holder views come from substantial-holder notices, which are their own dataset. Also extract the distribution schedule (holdings by size band) and the report's own substantial-shareholder disclosure for cross-checking against lodged 604s.

```sql
CREATE TABLE holder_snapshots (
  snapshot_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      BIGINT NOT NULL REFERENCES entities,
  as_at_date     DATE   NOT NULL,          -- event_date
  knowable_at    TIMESTAMPTZ NOT NULL,     -- report release
  rank           SMALLINT NOT NULL,
  holder_name_raw TEXT  NOT NULL,
  holder_entity_id BIGINT REFERENCES entities,   -- resolved where non-custodian
  is_custodian   BOOLEAN NOT NULL,
  units          NUMERIC NOT NULL,
  pct_stated     NUMERIC,                  -- as printed
  source_doc_id  BIGINT NOT NULL,
  source_pages   INT[]
);
```

Longitudinal linking of holders across years uses the name resolver with a custodian-aware equivalence layer (the same custodian appears under a dozen punctuation variants). The derived register-dynamics layer computes concentration (top-5/top-20 share), non-custodian churn, and consolidation trends — all keyed on as-at dates but only *actionable* from knowable_at.

**9.2 Controlled-entities note (→ subsidiary tree).** The financial-statements note listing subsidiaries with country of incorporation and ownership percentage. This is the unlock for Module A: tenements are held by subsidiaries, and without this table the tenement→ticker join does not exist. Stored as `subsidiaries(parent_entity_id, child_entity_id, child_name_raw, country, ownership_pct, as_at_date, knowable_at, source_doc_id, source_pages)`, with child entities created in the entity master (resolved to ACN via the ASIC dataset where the name matches an Australian registration; foreign subsidiaries get `entity_kind='foreign'`). Effective-dating across report years captures acquisitions, disposals, and renames of subsidiaries — the diffs are themselves informative.

**9.3 Franking account balance.** Extracted from the dividends/franking note. **Mandatory basis capture:** the balance is stated on a tax-rate basis (30% or the 25% base-rate-entity rate) and sometimes as "franking credits available for subsequent years" versus a raw balance — store the figure as printed plus `stated_basis` and `stated_form` columns (Invariant 9), and normalise in the derived layer. Derived screen: franking balance relative to market cap, joined with net cash and payout history, as a special-dividend/capital-management predictor.

**Acceptance criteria.** Section-retrieval recall ≥98% on a 50-report stratified sample (the target section is found, even if extraction then routes to review — retrieval misses are the silent killer here); Top 20 field accuracy per framework standard with the as-at date captured on 100% of accepted extractions (an accepted snapshot without an as-at date is invalid by definition); custodian flagging ≥99% on the labelled sample; subsidiary extraction validated against ASIC registration data for Australian-incorporated children on a sample; franking figures spot-reconciled against the prior year's comparative column within each report (reports print prior-year figures — a free internal consistency check the validator must use).


---

## 10. Optional Module A — Tenements and approvals

**Build gate:** do not start this module until Phase 3's subsidiary tree exists and has passed acceptance. The module's entire differentiation is the tenement→listed-parent join, and tenements are overwhelmingly held by subsidiary vehicles ("Pilbara Exploration Pty Ltd", not the listed parent). Without the tree, this module reproduces what the state portals already offer. *Prohibited shortcut: joining tenement holders to listed companies on name similarity alone without the subsidiary table — the false-match rate on generic exploration-company names makes the output worse than useless.*

**Sources.** Each state and territory publishes free tenement data in a different format; the ingestion layer wraps each behind a common `TenementSource` interface producing a normalised record. Start with WA and Queensland (the bulk of activity), add others only when a use case demands it:

| Jurisdiction | Portal (verify current name/URL at implementation) | Notes |
|---|---|---|
| WA | DEMIRS Mineral Titles Online / data.wa.gov.au spatial extracts | Richest dataset; regular spatial refresh |
| QLD | GeoResGlobe / QLD open-data portal | Good structured extracts |
| NSW | MinView / DIGS | Add on demand |
| SA | SARIG | Add on demand |
| NT | STRIKE | Add on demand |
| VIC | GeoVic | Add on demand |

Records: tenement number, type (exploration/mining/prospecting licence etc. — keep jurisdiction-specific codes plus a normalised kind), status, holder name(s) verbatim, grant/expiry dates, and geometry (PostGIS added to the stack here, not before). **Snapshot-diff architecture:** each refresh is stored as a dated snapshot; a diff engine emits `tenement_events` (applied, granted, transferred, expired, surrendered, renamed holder) with `knowable_at` = the extract's publication date. The events are the product; the current state is just the latest fold. Approvals side: the EPBC public notices portal (referrals and decisions, keyed to proponent names) ingested with the same resolver and event model.

**Holder resolution** runs the standard pipeline (exact → alias → fuzzy → LLM → review) against the union of listed entities and the subsidiary table, and every resolved link stores its path. Unresolved holders are a first-class analytical category (private explorers pegging ground are context, and occasionally become listings).

**Derived signals:** ground-position accumulation by a listed group within a radius of another group's deposits (needs Module B for deposit coordinates, or a manually-seeded deposit list until then); tenement transfers from private vehicles into listed groups; expiry/relinquishment patterns preceding raise announcements.

**Acceptance criteria.** Round-trip integrity per jurisdiction (a snapshot reloaded and re-diffed against itself yields zero events); holder-resolution precision ≥97% on a 200-holder labelled sample with all fuzzy/LLM resolutions carrying stored evidence; spatial sanity (all geometries valid, within jurisdiction bounds); a demonstrated end-to-end example query — "all tenement events in the last quarter attributable to listed group X via subsidiaries" — audited by hand against the source portal.

---

## 11. Optional Module B — JORC resources and reserves

**Difficulty warning, to be taken literally:** this is the hardest parser in the platform by a wide margin — nested multi-page tables, per-issuer layout anarchy, cut-off grades buried in footnotes, and figures that are meaningless without their qualifiers. It is also the module where domain knowledge, not code, determines whether the output has any value. **Build gate: do not start without either genuine mining-domain competence on the owner's side or a committed plan to acquire it; a JORC database read naively is a machine for buying stranded low-grade tonnes.**

**Scope.** Resource and reserve statements under the JORC Code (2012 edition as at drafting; a long-consulted update may have been adopted — verify the operative edition and its transitional rules at implementation). Source documents: annual statements of resources and reserves, and standalone resource/reserve announcements (classified in Phase 0's taxonomy once this module activates).

```sql
CREATE TABLE resource_statements (
  stmt_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id     BIGINT NOT NULL REFERENCES entities,
  project       TEXT NOT NULL,
  deposit       TEXT,
  commodity     TEXT NOT NULL,               -- normalised symbol: Au, Cu, Li2O, U3O8...
  category      TEXT NOT NULL CHECK (category IN
                ('measured','indicated','inferred','proved','probable','exploration_target')),
  tonnes        NUMERIC,
  tonnes_unit   TEXT,                        -- t, kt, Mt as printed
  grade         NUMERIC,
  grade_unit    TEXT,                        -- g/t, %, ppm, kg/t... whitelist per commodity
  contained     NUMERIC,                     -- as printed, if printed
  contained_unit TEXT,
  cutoff        NUMERIC,
  cutoff_unit   TEXT,
  equity_pct    NUMERIC,                     -- attributable share; null = unstated
  equity_stated BOOLEAN NOT NULL,
  as_at_date    DATE,
  knowable_at   TIMESTAMPTZ NOT NULL,
  supersedes    BIGINT REFERENCES resource_statements,
  source_doc_id BIGINT NOT NULL,
  source_pages  INT[]
);
```

**Integrity rules — each blocks a specific way this dataset lies:**

Categories never sum silently (Invariant 8's sharpest instance): Measured+Indicated+Inferred totals are produced only by an explicit aggregation function that labels its inclusion set, and Inferred is excluded from anything reserve-flavoured by default. **Exploration targets are not resources**: the JORC Code is explicit that an exploration target is a conceptual range, and any pipeline that lets one aggregate into resource totals has committed the module's cardinal sin — hence it is a `category` value that every aggregate excludes unless a flag screams otherwise. Contained metal is **recomputed** from tonnes × grade with unit conversion and compared to the printed figure; disagreement beyond rounding routes to review (this single check catches most extraction errors, unit misreads, and issuer typos). Equity share defaults to nothing: `equity_pct` null with `equity_stated=false` keeps the statement out of attributable-EV screens rather than silently assuming 100%. Grade units validate against a per-commodity whitelist (a gold grade in % is a parse error, not a bonanza). Supersession chains link updated statements so the time series per deposit is a chain, not a pile.

**Normalised comparables (derived, clearly labelled):** attributable contained metal by category set; EV per attributable unit using the share-count and price layers; resource growth per exploration dollar (joining quarterly 4C/5B exploration spend). Every comparable output carries its category-inclusion set and equity treatment in its column names — `ev_per_oz_MI_attrib`, not `ev_per_oz` — so a screen can never be misread.

**Acceptance criteria.** Gold set of 60 statements stratified by commodity and issuer size with per-field accuracy ≥97%; the recompute-and-compare check live with its disagreement rate tracked; zero exploration targets present in any default aggregate (tested); category-set labelling present on every derived comparable; supersession linking demonstrated on three real multi-year deposit histories audited by hand.

---

## 12. Cross-vein signal layer and backtest harness

The joins are the payoff and they only work because everything upstream shares the entity graph and the bitemporal convention. Representative queries the platform must be able to answer (each becomes a versioned, tested view):

```sql
-- Escrow releases into director conviction: releases >10% of float in the next 60 days
-- where a director on-market cash buy has lodged within the prior 90 days
SELECT e.entity_id, e.release_date, f.free_float_est, dt.knowable_at AS director_buy_lodged
FROM escrow_calendar e
JOIN float_series f USING (entity_id)              -- f.date = current date
JOIN director_trades dt USING (entity_id)
WHERE dt.classification = 'onmkt_buy_cash'
  AND dt.knowable_at > now() - interval '90 days'
  AND e.qty / NULLIF(f.free_float_est,0) > 0.10
  AND e.release_date BETWEEN current_date AND current_date + 60;
```

Analogous views: register consolidation (falling non-custodian holder count, rising top-5 share across the last two snapshots) intersected with upcoming escrow supply; substantial-holder accumulation during announced forced-selling windows; and, with the optional modules, subsidiary tenement accumulation adjacent to a peer's growing resource in a name whose register shows quiet building. **Every signal view's SQL is code-reviewed against Invariant 2 specifically** — the recurring bug in this genre is a join that leaks `event_date` into an actionability filter.

**Backtest harness requirements.** Event-study first (cumulative abnormal returns versus a size-and-sector-matched benchmark around `knowable_at`), portfolio simulation second. Non-negotiable mechanics: execution no earlier than the first session after `knowable_at`, at a modelled price (default: next open, with a closing-auction option because rebalance-adjacent studies transact there); cost model of brokerage plus spread by liquidity bucket estimated from the price vendor's data, with microcap spreads treated honestly (3–8% round trips are normal at the bottom); delisted names carried to their terminal value including administration outcomes; and multiple-testing discipline — a registered log of every hypothesis tested, because the platform will generate hundreds and the owner needs to know how many draws produced the survivors.

**After-tax reporting.** Tax is a pluggable regime object applied to parcel-level simulated trades: `RegimeDiscount2017` (50% discount ≥12 months, applicable to disposals before 1 July 2027), `RegimeIndexation2027` (CPI cost-base indexation ≥12 months plus the 30% minimum tax on gains, per the Treasury Laws Amendment (Tax Reform No. 1) Act 2026, applicable from 1 July 2027, with the transitional pre/post gain split for assets held across the boundary using the 1 July 2027 closing price for listed securities). **The 2027 regime's parameters must be encoded from the legislation/ATO guidance at implementation time, marked with citations, and reviewed by the owner's accountant before any after-tax figure is relied on** — this document's summary is not a tax authority. Backtests report gross, after-cost, and after-tax lines side by side; publishing only gross is prohibited (Invariant 10).

---

## 13. Monitoring and data-quality operations

Monitoring is a deliverable of every phase, not a hardening pass at the end. The minimum standing checks: **freshness** per feed against SLO (announcements: same-day; reference datasets: per their publication cadence; state tenement extracts: per jurisdiction); **volume baselines** per document class with seasonal awareness (3Y volume spikes around results and AGM seasons; near-zero weeks outside holidays are alarms); **parser health** (validation-failure and review-routing rates per parser, alarmed on trend change in either direction); **reconciliation reports** weekly (share-count replay vs vendor; 3.10A↔2A pairing; franking vs prior-year comparatives; register percentages vs share counts at the as-at date); and **review-queue SLA** with the auto-accept halt rule from §6. A weekly one-page operations report (counts, alarms, queue depth, reconciliation exceptions) is generated automatically; if producing it requires manual work, the monitoring is incomplete.

---

## 14. Legal, compliance, and scope guards

All inputs are public regulatory disclosures and open government data; the platform must keep it that way — no data behind logins that aren't the owner's own licensed subscriptions, no circumvention of technical or contractual access controls (Invariant 11). Market-data licensing is respected: announcement and price redistribution terms bind even personal use in some cases; the access decision (§5.1) documents the basis for each source. Director-trade and substantial-holder data are statutory public disclosures about public roles; the platform stores them as lodged and does not enrich them with non-public personal information. Insider-trading law is not a data problem but a conduct one: the platform's design keeps every input public precisely so its outputs are clean. Output framing: screens and reports are decision support for the owner, carry their coverage flags and heuristic labels, and are not investment advice to anyone. And the standing scope guards, restated because scope creep is the likeliest failure mode of the whole project: no order execution, no intraday data, no price-prediction modelling, no nominee piercing, no social/sentiment scraping.

---

## 15. Delivery plan and gates

Sequencing is dependency-driven and each phase gates on written acceptance evidence, not on "it seems to work". Indicative effort assumes competent agent-assisted development in evenings/weekends.

| # | Phase | Gate to start | Indicative effort | Standalone value shipped |
|---|---|---|---|---|
| 0 | Foundation | Access decision written | 2–4 weekends | None (by design) |
| 1 | Director transactions | Phase 0 accepted | 1–2 weekends | Cluster-buy screen |
| 2 | Escrow (mandatory coverage) | Phase 1 accepted | 1–2 weekends | Forward release calendar |
| 3 | Annual reports | Phase 2 accepted | 3–5 weekends | Registers, subsidiaries, franking; voluntary-escrow completion |
| A | Tenements (optional) | Phase 3 subsidiary tree accepted | 2–4 weekends | Tenement-event feed with listed-group attribution |
| B | JORC (optional) | Domain-competence gate + Module A recommended | 4–8 weekends | Comparables with integrity guarantees |

**Stop rules.** If the access decision cannot land on a compliant announcement source within two weeks, the project halts there — everything else is moot. If any phase's gold-set accuracy plateaus below target after two parser iterations, the phase stops for a design review rather than shipping with a lowered bar. If the review queue is not being drained (the human side failing), auto-accept halts per §6 — the platform degrades to slower-but-true rather than fast-but-false.

**Ongoing operations budget:** plan for two to four hours per week of review-queue and exceptions work in steady state. If the owner cannot commit that, build fewer veins rather than looser ones.

---

## Appendix A — CLAUDE.md stub (place at repo root)

```markdown
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

## Commands
- make test          # includes gold-set regression for every parser
- make reprocess ... # the only path for fixing systematic parse errors
- make ops-report    # weekly operations one-pager
```

## Appendix B — Form and source glossary

Appendix 3Y — change of director's interest notice (LR 3.19B, five business days). Appendix 3Z — final director's interest on ceasing. Appendix 3B — proposed issue of securities. Appendix 2A — application for quotation (authoritative share-count event). LR 3.10A notice — advance notification of restricted securities' release from escrow. Appendix 9B — categories and periods of ASX-mandated escrow by recipient type and consideration. Forms 603/604/605 — becoming/changing/ceasing substantial holder (Corporations Act s671B, generally two business days). Appendix 4C / 5B — quarterly cash flow reports (commitments-basis exploration spend lives here). JORC categories — Measured/Indicated/Inferred resources; Proved/Probable reserves; exploration target = a conceptual range that is not a resource.

## Appendix C — Gold-fixture protocol

Sample sizes: 100 per standard form, 50 per annual-report section, 60 for JORC. Stratify by lodgement year, issuer market-cap tercile, and format era. Label from the document alone; ambiguity is labelled `unknown`, never resolved from outside knowledge. Store fixtures and labels in-repo; CI runs every parser against its set on every change and blocks merges on regression. Refresh each set with 10 new randomly sampled documents per year to catch format drift; never delete old fixtures — formats recur.

---

*This specification is general information supporting a personal data-engineering project. It is not financial, legal, or tax advice; rule numbers, portal names, and the 2027 tax mechanics must be verified against primary sources at implementation time, and the after-tax module reviewed by a qualified accountant before reliance.*
