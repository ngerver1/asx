# Access decision — announcement and price data

**Status: DRAFT — blocked on owner decisions.** SPEC §5.1 makes this the first
deliverable of Phase 0 and a hard gate: *"If the access decision cannot land on
a compliant announcement source within two weeks, the project halts there."*
Per Invariant 11, no scraper ships from this repository until this document is
signed off; the only implemented `AnnouncementSource` is the compliant
file-drop source.

This draft evaluates the options the spec names. The four **DECISION** boxes
need the owner (who holds the budget and the licence relationships) — nothing
here can be resolved from the codebase.

## (a) Forward daily announcement coverage

| Option | Compliance | Coverage | Indicative cost | Assessment |
|---|---|---|---|---|
| Licensed market-data provider redistributing ASX announcements (e.g. a retail-grade announcements API) | Clean — the provider holds the redistribution licence; our use is governed by our subscription terms | Full market, same-day, usually with ASX type codes and price-sensitivity flags | Varies widely; verify current pricing with 2–3 providers | **Preferred.** Type codes remove most classifier ambiguity; timestamps give clean `knowable_at` |
| ASX website public announcement pages, used strictly within its terms at polite request rates | Must be verified against the **current** ASX website terms of use before any code is written; historic terms have restricted systematic downloading | Full market forward | Free | Acceptable only if the current ToU permits it; likely constrains backfill depth. If ToU does not clearly permit it, this option is off the table — do not rationalise |
| Company investor-relations pages | Companies republish their own announcements; generally unproblematic | Per-company, patchy formats | Free | Gap-filling only, unsuitable as primary feed (per spec) |

> **DECISION 1 (owner):** which provider for the forward feed, after checking
> current terms and pricing. Until then, forward coverage runs through the
> file-drop source.

## (b) Historical backfill

Target per SPEC §4 is 2015-present *where source access permits*. The spec is
explicit: **if no compliant path supports the desired horizon, shorten the
horizon — do not escalate the scraping.** Forward coverage from go-live is
worth more than deep history obtained badly.

Options, in order of preference: a licensed provider with historical archive
access (often an add-on to the forward subscription); a shorter compliant
backfill (e.g. whatever window the chosen provider includes by default);
no backfill (forward-only), letting the archive compound from day one.

> **DECISION 2 (owner):** backfill horizon, determined by what the chosen
> provider licenses — not by what is technically fetchable.

## (c) Prices (EOD OHLCV, survivorship-complete)

A purchased input, not a scraping project (SPEC §4). The vendor must cover
delisted securities and supply corporate-action data. Norgate Data is the
commonly used retail example; any equivalent with delisted coverage is
acceptable. Free sources that silently drop delisted names violate Invariant 4
at the root and are prohibited regardless of cost savings.

> **DECISION 3 (owner):** price vendor and subscription tier. The
> `PriceSource` protocol in `src/asx/ingest/sources.py` is the integration
> point.

## (d) Reference data (all free, all stable)

No decision needed; these are open government datasets used within their
published licences:

- ASIC Companies dataset (data.gov.au) — ACN, legal name, status.
- ABN Bulk Extract — ABN ↔ entity-name mapping.
- ASX listed-companies file — current code ↔ name snapshot.
- ASIC daily short-position reports — later derived-signal input.

> **DECISION 4 (owner):** confirm the total annual data budget
> (announcements + prices) so options above can be shortlisted.

## Sign-off

| Item | Decision | Date | Evidence (ToU link / licence ref) |
|---|---|---|---|
| Forward announcements | _pending_ | | |
| Backfill horizon | _pending_ | | |
| Price vendor | _pending_ | | |
| Budget | _pending_ | | |

When all four rows are filled, update the Status line at the top to ACCEPTED,
implement the provider-specific `AnnouncementSource`, and start the Phase 0
acceptance clock (10 consecutive trading days of live feed).
