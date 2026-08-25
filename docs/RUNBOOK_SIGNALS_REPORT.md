# Runbook — updating the buy-signals report

**Read this before touching the screen.** It is written for a session that
arrives in an empty container and is asked to "update the buy signals report".
Every command below was run end to end on 24 Aug 2026 and the outputs quoted
are the real ones from that run, not examples.

For what the platform is and where it stands, read `docs/HANDOVER.md` first.
This document is narrower: it is the loop that refreshes one page.

---

## What you are updating

The report is a **published Artifact** the owner already holds:

    ASX Director Screens
    https://claude.ai/code/artifact/228b70bf-0797-4c15-9f73-b473ebd818ba

**Redeploy to that URL. Do not publish a new one.** Pass it as `url` to the
Artifact tool, or the owner gets a second link and the first goes stale
without saying so. These parameters are fixed — reuse them exactly:

    url         https://claude.ai/code/artifact/228b70bf-0797-4c15-9f73-b473ebd818ba
    favicon     📈
    title       comes from the file's own <title>; do not override it
    label       a short version name, e.g. 24-aug-refresh

The favicon must not change between refreshes: the owner finds the tab by its
icon, so a new emoji reads as a different page. `screen-html` emits the page
with no `<!doctype>`, `<html>`, `<head>` or `<body>` wrapper, which is the
shape the Artifact publisher expects — do not add one.

Before publishing, confirm the render is sound: every CSS custom property used
by the page is defined in the bare `:root` block (a token defined only inside
a `@media` or `[data-theme]` block is invisible in the viewer's default
"system" state, which is the classic unreadable-artifact bug), `body` sets an
explicit background from a token, and the wide tables sit inside the
`overflow-x:auto` container. The generated page satisfies all of these today;
the check is for after someone edits `screen_html.py`.

The page is **generated, never edited**. `src/asx/signals/screen_html.py`
renders it from the database, which is the prime directive at work: derived
data must be regenerable. If a number on the page looks wrong, fix the parser
or the signal SQL and re-render. Never hand-edit the HTML — the next render
would silently discard the edit, and until then the page and the tables it
claims to show would disagree.

---

## The loop

The container arrives empty. The SessionStart hook installs dependencies,
repairs the PDF backend, starts Postgres and migrates — about 25 seconds,
automatic. It does **not** load any data. Then:

```bash
# 1. Get the database back. The container has nothing until you do this.
python -m asx.cli snapshot --dir state --restore

# 2. PRIMARY FEED — the whole exchange. See "The whole-exchange sweep":
#    run the three MCP searches, save each response to its own file, then
python -m asx.cli detect --since-days 3 --from-search sweep/*.txt

# 3. SECONDARY FEED — the alert mailbox. Watchlist-bounded, and measured
#    against the sweep it adds nothing; it runs because two independent
#    feeds are what make coverage a measurement (ACCEPTANCE 0.8).
python -m asx.cli detect --source mailbox --from-dir alerts

# 4. Get the documents. Sweep detections already carry their PDF URL, so
#    this needs no matching step. 25 per run — repeat until attempted is 0.
python -m asx.cli capture --capture-dir captures --investorpa
python -m asx.cli capture --capture-dir captures --asx

# 5. Read the held documents. --parser is required; a bare `parse` exits 2.
python -m asx.cli parse --parser app3y

# 6. Rebuild the signal tables.
python -m asx.cli build-signals

# 7. Refresh the display quotes. ~3 minutes: ~35 tickers at 5s/host.
python -m asx.cli fetch-quotes

# 8. Check whether the feed is healthy BEFORE you publish. See below.
python -m asx.cli monitor

# 9. Render.
python -m asx.cli screen-html --out screen.html
```

Everything except step 7 finishes in seconds. Budget fifteen minutes for the
run: the capture throttle is one request per five seconds per host, and a
whole-exchange day is ~45 documents.

Then publish `screen.html` to the URL above, and persist your work:

```bash
python -m asx.cli snapshot --dir state      # export, overwrites state/*.csv
git add state && git commit && git push -u origin <your-branch>
```

**Step 9 is not the end.** The container is reclaimed when the session ends.
A run that renders a beautiful page and never snapshots has thrown away every
document it fetched, and the next session starts from the same place you did.
It also throws away the arrival dates behind the "New since" table, which is
the one thing on the page that cannot be recomputed later.

---

## The whole-exchange sweep

This is the primary feed and the step that decides what the report can see.

**Detection is not possession.** An alert email says an announcement exists;
it does not carry the PDF. A detected document sits at
`parse_status='detected'`: recorded, visible, and unreadable. It produces no
trade and therefore no signal.

**The InvestorPA MCP server is connected to your session.** Its tools appear
as `mcp__InvestorPA__*`. This is easy to miss, because a separate thing with a
similar name is genuinely blocked: an unattended GitHub Actions run needs
`ASX_INVESTORPA_REFRESH_TOKEN`, and that OAuth grant does not exist. **That
blocks the cron, not you.**

The vendor advertises exactly this use — "connects ASX announcements directly
to any MCP-compatible AI harnesses... Works with Claude Desktop & Mobile,
ChatGPT Desktop & Mobile, Claude Code, Codex" — which is the Invariant 11
basis recorded in `docs/SOURCE_INVESTORPA.md`.

### Run it

**Three searches, not one.** `DIRECTOR_INTEREST_KEYWORDS` holds three
spellings because issuers do not agree on what to call the form, and the API
filters on title only. Measured on 24 Aug, one keyword returned 33 of 46.

For each of `Director's Interest Notice`, `Appendix 3Y`, `Appendix 3Z`, and
for **each day** in the window, call:

```
mcp__InvestorPA__search_announcements(
    keywords="Appendix 3Y", date_from="2026-08-25", date_to="2026-08-25",
    limit=500)
```

One day at a time, because the response carries a single `Found N
announcements` header and the parser reads the first one — concatenating two
days into one file silently understates the vendor's own count and defeats
the `missing` check. Save each response verbatim to its own file, then:

```bash
python -m asx.cli detect --since-days 3 --from-search sweep/*.txt
```

`--from-search` takes the same code path as the HTTP client from
`detections_from_text` onward: same parser, same keyword union, same audit
counts. Only the transport differs. It exists as real code
(`PastedSearchClient`) rather than a script pasted into a session, so nobody
re-invents the matching and gets it subtly wrong.

**Cover the window you declare.** `--since-days N` records a window in its
stats; paste searches for every day in it. The floor is 2024-06-15 and the
client refuses anything earlier rather than returning a short answer that
looks complete.

**Read the audit line.** `{"found": 46, "new": 46, "missing": 0, "truncated":
false}` — `missing` is lines the vendor counted that the parser never
recognised, and it is the check that catches a transcription slip or a
changed output format. Non-zero means a hole; `truncated: true` means the
window was under-reported and needs splitting.

### Then possession needs no matching

Sweep detections already carry the stated PDF URL in `fetch_candidate_urls`,
because `record_detection` writes `Detection.document_urls` there. So:

```bash
python -m asx.cli capture --capture-dir captures --investorpa
```

is the whole of it — 25 documents per run, so repeat until `attempted` is 0.

**This is only true when the sweep did the detecting.** A mailbox detection
carries no URL, and reconciling one against the vendor's search by ticker,
lodgement time and title is the manual dance this runbook used to describe.
It is no longer the main path; it is only needed for the alert feed's own
rows, and the coverage numbers below say those are a subset anyway.

### Two feeds, one lodgement

Each feed keys detections differently — InvestorPA on the stated PDF URL, the
mailbox on the email Message-ID — so a lodgement seen by both produces **two
`documents` rows** and `duplicate: 0` in the detect stats. That is expected;
they share no identifier.

It resolves at possession: the second row to be fetched finds its bytes
already held under the first doc_id and closes as `not_applicable` with
`[duplicate of doc N]` in `source_ref`, rather than double-storing. On 26 Aug,
38 attempted gave 26 captured and 12 duplicates. Check `source_ref` before
treating a `not_applicable` as a failure.

### What the coverage actually is

Measured 26 Aug over 25–26 Aug, the first window both feeds covered:

| Bucket | Documents | Tickers |
|---|---|---|
| `investorpa_only` | 30 | 21 |
| `both` | 26 | 9 |
| **`market_index_only`** | **0** | **0** |
| `unresolved_entity` | 3 | 3 |

`market_index_only` is the number the view was built to produce, and it is
zero: the watchlist found nothing the whole-exchange sweep missed, while the
sweep found 30 documents the watchlist never saw. The mailbox caught roughly
30% of the lodgements in the window.

Keep running it anyway. It has the shorter latency, it is the independent
check that makes these numbers a measurement rather than InvestorPA marking
its own homework, and one clean window is not a rate.

## The "New since" table

The page opens with a **New since &lt;date&gt;** table listing rows that
entered either screen in the last seven days, so a returning reader can see
what changed without re-reading both screens. It is a view over the two
screens below, not a third screen.

It is driven by `signal_first_seen`, and there are three things to know:

- **A rebuild announces nothing.** The signal tables are DELETEd and rewritten
  every build, so arrival dates cannot live on them; `signal_first_seen` keys
  on something stable (`entity_id:window_start` for a cluster, `trade_id` for
  a conviction row) and an existing key keeps its original timestamp.
- **It is snapshotted, and it must be.** The signal tables deliberately are
  not — they are regenerable — but arrival dates are not recomputable from
  the corpus at any later date. If you skip the closing snapshot, the next
  container has no arrival history and reports nothing as new.
- **Rows predating the table say so.** 37 rows were on the screens when
  tracking began on 24 Aug 2026; they carry `backfilled = true`, have no
  arrival date, and are never reported as new. The empty state says this out
  loud rather than showing a bare "nothing new", which is indistinguishable
  from a build that failed to run.

---

## Read the monitor before you publish

`asx monitor` is the alarm the whole detection design exists to raise. On
24 Aug it fired four; on 26 Aug, after the sweep, it reports:

```
ok: no alarms
```

The four that cleared — `capture_gap`, `capture_rate` at 19% against a 90%
floor, and freshness on both `app_3y` and `detections_all` — cleared because
the documents were actually obtained and the feed caught up. **If you see them
again, the sweep above is what fixes them**, not a tolerance adjustment.

A green monitor is not proof of coverage. It says every document we KNOW about
was fetched in time; it cannot see a lodgement nothing detected. That is what
`detection_feed_coverage` is for, and why the mailbox keeps running.

**Alarms are not yours to silence.** Publish the page anyway — a screen built
on a stale feed is still the best available view — but say so when you hand it
over. A screen presented as current when it is not is a lie the reader cannot
detect.

Zero lodgements in a trading period is a pipeline alarm until a human says
otherwise (CLAUDE.md). If `detect` returns `new_detections: 0` on a weekday,
check the alert feed before assuming a quiet market.

---

## Things that will bite

- **A per-security price in the Value/Consideration box.** Appendix 3Y's
  consideration field is filled inconsistently by issuers: some print a total,
  some print a price per security. The parser takes it as a total either way.
  **26 trades across 12 entities** currently carry an arithmetically
  impossible per-unit price — under $0.001, below the ASX minimum tick, so it
  cannot be a total. One of them is on the published cluster screen: WRK's
  cluster totals `$11,100.077`, where the `.077` is Trent Lund's 190,000
  shares recorded as a 7.7-cent *total* rather than 7.7 cents *each*. The page
  therefore shows an average paid of 3.26c against a 7.7c market — a ~136%
  gain that does not exist. **Known, unfixed, and live.** The fix that matches
  the codebase's own rule (ambiguous → 'unknown', never a substantive default)
  is to flag the row and refuse to assert the total or the price comparison,
  not to silently recompute as `qty × price`, which would be inferring what
  the issuer meant. Find them with:

  ```sql
  SELECT * FROM director_trades
   WHERE price_per_unit > 0 AND price_per_unit < 0.001;
  ```

- **The alert feed writes to a branch nothing merges.** The Apps Script's
  `GITHUB_BRANCH` is set to `claude/go-is75md`. If `alerts/` looks frozen,
  fetch that branch and merge it before concluding the feed died. This matters
  less now that the mailbox is the secondary feed, but a stale alert feed
  makes `detection_feed_coverage` meaningless — it would report the sweep as a
  perfect superset of a feed that simply stopped. Its default
  is `main`, and **this repository has no `main`** (the default branch is
  `claude/database-env-vars-h63r7v`), so clearing the property breaks it.
  The trigger runs about 18:05 AEST daily; a gap until the next evening is
  the schedule, not an outage.
- **Quotes are not snapshotted, on purpose.** A restored week-old price under
  a column headed today's date is the exact lie the as-at exists to prevent.
  A fresh restore therefore flags `price_unavailable` on every row until
  `fetch-quotes` runs. Do not skip step 7 and do not read those flags as a
  pricing failure.
- **Neon is unreachable from the sandbox and allowlisting will not fix it.**
  Egress is HTTPS/443 only; the Postgres wire protocol needs raw TCP on 5432.
  The hook falls back to a local cluster and says so. That is why step 1 and
  the closing snapshot exist at all.
- **`captures/README.md` gets filed as a document.** The capture scanner does
  not skip it, so it lands as `doc_class='other'` and
  `parse_status='not_applicable'`, and pypdf prints
  `invalid pdf header: b'# cap'` on the way past. It is filed once and
  deduplicates by SHA-256 on later runs. Harmless — never parsed, never
  signalled — but it inflates the "documents held" count by one.
- **`state/` is 7.3 MB** and grows ~4.5 KB per document. A few thousand more
  and git is the wrong home for it.
- **The DD note covers a screen that has moved on.** `docs/DD_2026-08-20.md`
  was written against 19 conviction rows. Do not present it as covering the
  current page; check which of its subjects are still on the screen.

---

## What must not happen

These are invariants, not preferences. A shortcut that conflicts with one is
wrong even if the page renders and the tests pass.

- **The sweep is the default; do not quietly narrow it.** `asx detect` with no
  `--source` searches the whole exchange. Passing `stock_codes` to the MCP
  search — the way the 24 Aug run did — turns it back into a possession tool
  for whatever the watchlist already knew, which is how coverage sat at ~30%
  without anyone noticing.
- **Never join on ticker.** `ALU` is Altium before August 2024 and Alurion
  Resources after — and InvestorPA's own `search_stocks` resolves it to
  Alurion. Entity resolution goes through `listings`, which is
  effective-dated. A test asserts no source file names `search_stocks` as a
  callable tool.
- **Never construct a document URL.** Copy stated ones only.
- **Never hand-edit a canonical table or the rendered HTML.** Fix the parser
  or the SQL and reprocess — `make reprocess` is the only path for systematic
  parse errors.
- **Never drop a row to make the screen look clean.** Flag it and leave it
  visible. A silent exclusion does not let the owner disagree with you.
- **Delisted entities stay in every universe.** A screen that quietly drops
  them is survivorship-shaped and worthless for anything historical.

---

## Verifying you did it right

1. `make test-all` — no skips. A skip looks like a pass in the summary line,
   which is how two green local runs shipped two red CI runs.
2. The detect stats show `missing: 0` and `truncated: false`. A non-zero
   `missing` means lines the vendor counted that the parser never saw.
3. `SELECT count(*) FROM documents WHERE parse_status='detected'` returns 0,
   or you know why each survivor could not be retrieved.
4. `SELECT coverage, count(*) FROM detection_feed_coverage WHERE lodged_at >=
   <window start> GROUP BY 1` — a non-empty `market_index_only` bucket means
   the sweep missed something the watchlist caught, which would be the first
   evidence that the keyword list is short.
5. The stamp under the headline shows today's date, the document count, and
   the price as-at range. If the as-at is old, `fetch-quotes` did not run.
6. `build-signals` printed a count. If it printed 0 and the corpus is not
   empty, something is wrong — do not publish an empty screen quietly.
7. `git status` is clean after the snapshot commit, and `state/` moved —
   including `state/signal_first_seen.csv`. If it did not, nothing you
   fetched this session survives and the next reader sees no arrivals.
