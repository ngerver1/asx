# Access decision — announcement and price data

**Status: ACCEPTED — Tier 0 composite (no paid feed).** Decided by the owner,
August 2026. This satisfies the SPEC §5.1 gate and unblocks Phase 0/1
implementation. It is a deliberately labour-priced, zero-cost configuration
with stated consequences, not a stopgap that ignores them.

Per Invariant 11 this document is the operative authority on what the
platform may and may not touch. Where it constrains behaviour, the constraint
is enforced in code (`asx/ingest/fetch_guard.py`), not merely documented.

---

## 1. Announcement source — Tier 0 composite

Detection and possession are handled **separately**, and the platform models
them as separate facts (`documents.parse_status = 'detected'` precedes
`'unparsed'`).

**Detection (automated).** Announcement alert emails delivered to a dedicated
mailbox owned by the owner, from services that send them by design: Market
Index watchlist alerts (all announcements, not only price-sensitive),
Listcorp alerts (added later if needed), and company IR mailing lists the
owner subscribes to. The ingester parses this mailbox only.

**Possession (documents).**
1. PDFs attached to, or linked from, IR emails **where the link points to the
   company's own website** — fetched politely under that company's terms,
   spot-checked per site.
2. Everything else via **human-triggered capture**: the owner personally opens
   flagged announcements on the ASX website in a capture browser profile; a
   local watcher files what was opened into the raw zone. **No automated
   device accesses asx.com.au.**

**Backfill.** Manual, labour-priced retrieval by the owner from the ASX
website's own announcement history (personal use), captured through the same
watcher. No library or paid archive is in scope at this time.

## 2. Backfill horizon

On demand, per vein, limited to what the ASX site exposes and what the
owner's time allows. Initial target: **~24 months of Appendix 3Y/3Z** for the
active universe, beginning with **~120 documents for the Appendix C gold
set**. Deep history and delisted-company documents are **out of scope** until
an archive route is adopted.

> Consequence, stated plainly: historical coverage will be
> survivorship-affected, because delisted companies' documents are not
> reachable. This is why backtesting is out of scope below — the two
> limitations compound, and a study over this document set would flatter
> results twice over. Forward coverage from go-live is unaffected and is
> where the value lies.

## 3. Price vendor — none (deferred)

No price/market-data subscription at this time. Three consequences, accepted
and **enforced in code**:

| Consequence | Enforcement |
|---|---|
| **Backtesting is out of scope.** Invariant 10 cannot be satisfied without survivorship-complete price data. | `asx/backtest/harness.py` raises `BacktestUnavailableError` citing this decision until a vendor is registered. |
| **Market-cap ceiling replaced by an index-membership proxy:** "not a member of the S&P/ASX 300", from ETF issuers' published daily holdings, refreshed weekly. | `asx/universe/index_membership.py`; cluster-buy signal v2 applies it and labels every row `size_ceiling_proxy`. |
| **Acceptance 0.7 reconciliation amended:** the 50-entity check runs against shares-on-issue figures read manually from the ASX website. | `manual_share_counts` table; `reconcile_against_manual()`. |

The proxy is not equivalent to a market-cap cut-off — the index rebalances
quarterly and screens for liquidity and free float as well as size. Screens
say so on every row.

Live forward screens are unaffected by all of the above.

## 4. Budget

Data: **$0**. Anthropic API usage for extraction and the classification
fallback only (single user; monthly spend limit set in the console). No other
recurring costs approved.

## 5. Upgrade paths (not exercised) and review triggers

ComNews/vendor quotes and the ASX written-consent request are **not being
pursued at this time**. Reopen this decision if any of the following occur:

- the daily capture sweep proves unsustainable — *monitored*: the
  `capture_rate` alarm fires below a 90% 14-day capture rate;
- backtesting begins (price vendor required);
- a vein needs deep or delisted-company backfill;
- a licensed feed becomes available for under ~AUD 100/month.

## 6. Terms basis — Invariant 11 sign-off

| Source | What the system does | Basis for use | Explicit exclusions |
|---|---|---|---|
| Alert emails (Market Index, IR lists, later Listcorp) | Automated parsing of the owner's own mailbox | Processing mail sent to the owner by services whose purpose is sending it | Ingester never fetches URLs on asx.com.au |
| Company IR websites | Fetch announcement PDFs the company publishes | Company's own dissemination terms; per-site spot-check; polite rates | No bulk crawling; skip any site whose terms object |
| ASX website (asx.com.au) | **Nothing automated.** Owner views and downloads personally | Personal, private decision-making use under ASX terms | No spider, scraper, or automated monitoring, ever; no auto-following emailed ASX links |
| ETF issuer holdings files | Weekly download of published holdings CSVs | Published by issuers for investors; per-site terms | Display/derive only; no redistribution |
| ASIC Offer Notice Board | Manual weekly check (from Phase 2) | Public regulator notice board | No automated polling unless terms permit |

### How the ASX exclusion is enforced

`asx/ingest/fetch_guard.py` is the single chokepoint for every automated
fetch. It raises `ProhibitedSourceError` on any asx.com.au URL — including
links extracted from alert emails, the path most likely to reach for one by
accident. The mailbox parser strips ASX links from the fetchable set at
detection time. Tests assert the block for the domain, its subdomains, and
emailed links. The capture watcher, which moves bytes a human already
opened, makes no network request at all.

---

## Sign-off

| Item | Decision | Date | Basis |
|---|---|---|---|
| Forward announcements | Tier 0 composite (alert-email detection + IR fetch + human capture) | Aug 2026 | §6 table above |
| Backfill horizon | ~24 months 3Y/3Z on demand; no delisted-company documents | Aug 2026 | §2 |
| Price vendor | None; backtesting out of scope, index proxy substituted | Aug 2026 | §3 |
| Budget | $0 data; Anthropic API only | Aug 2026 | §4 |

Per-site terms spot-checks for IR websites are the owner's standing
responsibility as new companies enter the watchlist; a site whose terms
object is skipped, not worked around.
