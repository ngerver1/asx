# investorpa.com — assessment and adoption

Assessed 20 Aug 2026 and **declined**. Re-assessed the same day against the
vendor's MCP API and **adopted**, as a second detection feed running alongside
Market Index. **Promoted to the PRIMARY detection feed on 26 Aug 2026**, with
the alert mailbox as the secondary. This document records what changed and
what it does not change.

The promotion was a measurement, not a preference. Over 25–26 Aug — the first
window both feeds covered — `detection_feed_coverage` reports
`market_index_only` = **0** against `investorpa_only` = **30**: the watchlist
found nothing the whole-exchange sweep missed, and missed 30 documents across
21 companies that the sweep found. The mailbox saw roughly 30% of the
lodgements in the window. `asx detect` now defaults to `--source investorpa`.

## What it is

An ASX announcement alert service, the same role Market Index plays: a
watchlist of up to 200 codes, email alerts, AI summaries of price-sensitive
announcements ("Fast Feed"), a daily roundup. It ran from 2011 to 2017 with
over 4,000 users and has since relaunched. Currently in beta.
([features](https://investorpa.com/features/), [about](https://investorpa.com/about/))

## Why the first assessment declined it, and what changed

The original gate was two questions. Both are now answered, one of them
because the premise was wrong.

**1. "The terms could not be read."** This was true of the network, not the
site. investorpa.com now returns 200 from the platform's egress; the earlier
403 is gone. Reading it produced a more interesting answer than expected:

- **There is no terms-of-use page at all.** `/terms/`, `/terms-of-use/`,
  `/terms-and-conditions/`, `/tos/`, `/legal/`, `/privacy/`, `/disclaimer/` all
  return 404 (checked 20 Aug 2026). The footer carries a bare
  "© 2024 investorpa. All rights reserved."
- **`robots.txt` is absent** (404), which under RFC 9309 §2.3.1.3 means no
  restrictions rather than an unanswered question.
- **What exists instead is an affirmative published offer.**
  `https://investorpa.com/features/` advertises, as a product feature:

  > **Remote MCP Server — Ask your AI about the ASX.**
  > "InvestorPA's MCP Server connects ASX announcements directly to any
  > MCP-compatible AI harnesses. Works with Claude Desktop & Mobile, ChatGPT
  > Desktop & Mobile, Claude Code, Codex, LM Studio and more. No local package
  > installs necessary. Just connect and ask away."

That is the recorded basis, quoted in `fetch_guard.DECLARED_SOURCES`. **The
honest limit of it, recorded because it is a judgement and not a quotation:**
the grant is written for AI harnesses asking questions. Reading it to cover a
scheduled ingest is this platform's inference. The proportionality rules below
exist to keep the use recognisably the thing that was offered.

**2. "Do their alert emails contain the PDF link?"** Moot. The API returns the
announcement and its document URL directly, which is strictly better than a
link in an email, and is not watchlist-bounded the way the emails are.

## What was verified, not assumed

| Probe | Result |
|---|---|
| `search_announcements` by title keyword | Cross-market, **no watchlist**. ~15–20 director-interest notices a day |
| Coverage floor | **2024-06-15**, stated by the API and confirmed at the boundary |
| Delisted entities — CSR, MRM, APM, ALU | **Fully retained**, including each `Removal from Official List` and CSR's Final Director's Interest Notices |
| `get_announcement_detail` | Full transcribed text, page by page, plus PDF `metadata.creation_date` |
| `search_stocks("ALU")` | **Returns Alurion Resources, not Altium** — see the trap below |
| ASX announcement number | **Not exposed anywhere** — not in the API, not on the detail page |

Two of those are load-bearing and are worth stating as consequences rather
than observations.

### Delisted coverage is complete, which reopens something

`ACCESS_DECISION` §2 put delisted-company documents out of scope and named
that as half of why backtesting was ruled out — historical coverage would be
survivorship-affected. For announcements from 2024-06-15 onward, it no longer
is. Backtesting remains out of scope on §3 (no survivorship-complete price
vendor), but on one limitation now rather than two compounding.

### Their stock master is a ticker trap

`search_stocks` resolves `ALU` to Alurion Resources Limited. Every ALU
announcement before August 2024 is Altium's. Their stock endpoint is
current-state; the announcement records themselves carry the right historical
association. **Nothing resolves an entity through it**, here or anywhere:
tickers from this source are inputs to `entity_for_ticker`, which is
effective-dated through `listings` (Invariant 1). A test asserts no source
file names `search_stocks` as a callable tool.

### Their announcement id is not the exchange's

They expose their own publication counter (`330559`), not the ASX announcement
number (`2A1690462`). It is therefore **not** written to
`documents.asx_announcement_id`: that column means the exchange's identifier,
and a vendor's counter in it would make rows from two feeds collide or diverge
at random. The consequence is that the cross-feed dedupe built on that column
does not apply here: two feeds reporting one lodgement produce two rows, and
nothing can merge them on identity because they share no identifier. They are
matched instead on entity, form and a tolerance around the lodgement instant —
a weaker claim, made deliberately and with its limits recorded. See "Running
both feeds" below.

## How it is used, and the limits that keep it defensible

- **Appendix 3Y/3Z only.** The exchange publishes ~400 announcements a day;
  this asks for the tens the platform parses, by title keyword — around 40–46
  a day, measured.
- **Three keyword searches, not one.** Issuers do not agree on what to call
  the form and the API filters on title only, so `Director's Interest Notice`
  alone returned 33 of 46 on 24 Aug: everything titled `Appendix 3Y - <name>`
  or bare `Appendix 3Z` was invisible. The union is taken on the stated PDF
  URL, which is also the detection key, so a notice matching two keywords is
  one detection. `missing` on each page is what will say if a fourth spelling
  appears.
- **Their search, never our enumeration.** Identifiers are sequential at
  ~400/day, so `announcement-pdf/{YYYYMMDD}/{id}.pdf` can always be *built* —
  which is why nothing builds one. This was named in the first assessment as a
  crawl that "must never be built"; it still must not be, and the vendor's own
  search endpoint removes any reason to. A test asserts no source file
  constructs such a URL.
- **Through the guard, not around it.** Every request goes through
  `fetch_guard.fetch`, which throttles to one per five seconds per host and
  identifies the platform honestly.
- **The PDF, not their transcription.** The API returns transcribed text, and
  the platform deliberately does not use it as the document. The gold set
  calibrates `App3YParser` against pypdf output; taking the bytes keeps
  `documents.sha256` the hash of the original artifact (migration 0020 is
  emphatic on this) and keeps the extractor the parser was tuned against.
  Their text remains available as an independent second reading — see
  "Opportunity" below.
- **A re-host, not the exchange.** `possession_source='investorpa'` and
  `lodged_at_source='investorpa'`, never `'asx'`. That value stays reserved
  for the exchange's own feed.

### On the discovery prohibition

The access decision forbids discovery and permits only targeted retrieval.
Searching this source is not an exception smuggled past that rule: the
prohibition is specific to the exchange, whose terms offer no search API. A
vendor whose published product *is* a search endpoint is offering exactly that
use. `fetch_guard` cannot enforce the distinction itself — a JSON-RPC method
name lives in the request body, not the URL — so it is recorded here and in
the guard's own docstring rather than left implicit.

## Two routes, and only one of them is blocked

This distinction cost four days, because the two were conflated and both
reported as blocked.

**The session route works today.** The InvestorPA MCP server can be connected
to a Claude Code session directly, where its tools appear as
`mcp__InvestorPA__*`. It needs no repository secret and no stored refresh
token — the session holds the grant. On 24 Aug 2026 this route resolved all
30 stranded detections in one pass: `search_announcements` for the tickers and
dates already detected, the stated PDF URLs written onto those detections, and
the platform's own guarded fetch doing the retrieval.
`docs/RUNBOOK_SIGNALS_REPORT.md` has the procedure. This is also the use the
vendor's published grant describes most directly — an AI harness asking about
the ASX — so it needs less inference than the scheduled ingest below.

**The unattended route is blocked.** `asx detect --source investorpa` runs in
GitHub Actions with no session and no browser, so it needs
`ASX_INVESTORPA_REFRESH_TOKEN` in the environment. That grant does not exist,
for the reasons below. A cron cannot borrow a session's MCP connection.

So: the daily sweep is dormant, and a human-driven session is not. Anyone
reading "blocked on OAuth" should check which of the two is meant.

## Setup for the unattended route

The MCP endpoint is OAuth-protected (`mcp:read`, a read-only scope the server
defines and enforces). It is a public client supporting Dynamic Client
Registration and PKCE, so consent should be a one-off browser step:

    python -m asx.ingest.investorpa_consent          # registers, prints a URL
    python -m asx.ingest.investorpa_consent --code ... --client-id ... --verifier ...

**This has never completed, and the platform holds no grant.** Registration
returns 201; the authorization endpoint then rejects that `client_id` with a
bare 400 naming no reason, across four parameter combinations, and it refuses
to validate anything before login so it cannot be diagnosed from outside an
authenticated session.

What is known: `claude mcp login` succeeds against the same endpoint, and its
request uses no DCR client at all — its `client_id` is a URL,
`https://claude.ai/oauth/claude-code-client-metadata`, the Client ID Metadata
Document flow this server advertises as
`client_id_metadata_document_supported: true`. The likely reading is that the
beta implements CIMD and not the DCR clients its own registration endpoint
issues. Publishing a metadata document and passing its URL as `client_id` is
the next thing to try; failing that, ask the vendor, since an endpoint
refusing the clients it just issued is a bug they would want. `state` and
`resource` are required regardless and were missing from the first attempts.

### The transport handshake

The client speaks the MCP Streamable HTTP transport properly: it sends
`initialize`, then `notifications/initialized`, echoes any `Mcp-Session-Id`
the server assigns, carries `MCP-Protocol-Version` on every later request, and
re-initialises once on a 404. Those are MUSTs of the specification, read at
implementation time from the primary source rather than remembered
(revision 2025-06-18, read 22 Aug 2026) and quoted in
`asx/ingest/investorpa.py`.

An earlier version sent `tools/call` cold. A server that assigns a session
would have answered 400 to the very first request, and no test here could have
caught it — every transport test injected a stand-in that returned the same
canned body regardless of what it was sent, so it asserted what the fake did
rather than what a server would. The stand-in now implements the transport's
MUSTs and rejects a request that omits them, the way a session-requiring
server does.

**Unverified against investorpa specifically**, and it cannot be until a grant
exists: the endpoint answers 401 to everything including `initialize`, so its
lifecycle behaviour is unobservable from outside an authenticated session. The
specification is the authority, not a guess about the beta — but the first
authenticated run is the real test, and the error messages say so.

Everything downstream is built and tested. The **scheduled** sweep waits on
that one token; a session with the MCP connected does not. When it exists, the
flow prints a refresh token once. Put it in the environment where the platform
runs — never in the repo, never in a chat:

    ASX_INVESTORPA_CLIENT_ID=...
    ASX_INVESTORPA_REFRESH_TOKEN=...

Then:

    asx detect --source investorpa --since-days 3   # detection, whole exchange
    asx capture --capture-dir captures --investorpa # possession, stated URLs only

## Running both feeds

Market Index keeps running. `ACCEPTANCE.md` criterion **0.8** — a weekly
ten-ticker manual completeness spot-check — is unticked, and detection
coverage is therefore unmeasured. Two independent feeds are what make it
measurable, and by a stronger instrument than 0.8 names: every lodgement
compared rather than ten sampled tickers. That is a criterion to propose
amending, not one to tick on this evidence — 0.8 as written asks for a
manual check.

The `detection_feed_coverage` view (migration 0029) is **one row per
detection, never a merge**, and asks of each one whether the other feed saw
the same lodgement. Buckets: *both / investorpa_only / market_index_only /
unresolved_entity*. A non-empty `market_index_only` bucket is the interesting
one — it would mean InvestorPA is not the superset it is assumed to be, which
is the whole reason to keep both feeds running.

The first version of that view grouped on
`(entity_id, date_trunc('minute', lodged_at))` and was wrong in both
directions at once. Too coarse: two *different* directors of one company
lodging a second apart collapsed into a single row flagged as a duplicate —
and a company whose directors file together is precisely the batch-lodgement
pattern the cluster-buy screen exists to detect, so the view cried wolf on the
platform's best signal. Too fine: Market Index reports lodgement to the minute
and InvestorPA to the second, so one lodgement seen by both could straddle a
minute boundary and split into two rows, inflating the one number the view
exists to produce.

A partner is therefore found within a **±90 second tolerance** rather than by
bucketing. That bound is 59 seconds of precision difference plus margin, and
it is **uncalibrated**: no lodgement has yet been observed by both feeds,
because InvestorPA has never run. The residual ambiguity is real — two
directors of one company filing within 90 seconds, one seen by each feed,
would pair wrongly — and it should be revisited against the first genuine
overlap rather than trusted now.

`unresolved_entity` is its own bucket rather than an exclusion. The old view
filtered `entity_id IS NOT NULL`, which dropped exactly the rows most likely
to *be* a coverage gap and reported perfect agreement over what was left.
Against a database whose `listings` table has not been loaded it returned
nothing whatsoever and looked like a clean bill of health.

Two feeds still produce two `documents` rows for one lodgement, because they
share no identifier — InvestorPA exposes only its own publication counter,
never the ASX announcement number that `documents.asx_announcement_id` means.
The view makes that visible; it does not resolve it. Deciding which row wins
before `director_trades` is a design question for the owner, and parsing both
would enter one director purchase twice and inflate the cluster signal.

The coverage numbers are now measured rather than expected. Over 25–26 Aug,
the first window both feeds ran as *detection* feeds:

| Bucket | Documents | Tickers |
|---|---|---|
| `investorpa_only` | 30 | 21 |
| `both` | 26 | 9 |
| **`market_index_only`** | **0** | **0** |
| `unresolved_entity` | 3 | 3 |

`market_index_only` = 0 is the result the view exists to produce. It says the
watchlist is a strict subset in this window, which is why the default flipped.

Two cautions on reading it. One clean window is not a rate — a single day when
every watchlist company happened to also match a keyword would look identical.
And the sweep marking its own homework is exactly the failure the second feed
prevents, so the mailbox keeps running: if the keyword list ever goes short,
`market_index_only` going non-zero is the only thing that will say so.

The duplicate-row problem resolves itself at possession rather than needing a
rule. Two feeds produce two `documents` rows for one lodgement because they
share no identifier, but the second row to be fetched finds its bytes already
held under the first doc_id and closes as `not_applicable` with
`[duplicate of doc N]`. On 26 Aug that was 38 attempted, 26 captured, 12
duplicates — and no director purchase entered `director_trades` twice.
