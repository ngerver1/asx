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

# 2. Read the alert emails the Apps Script has committed since last time.
python -m asx.cli detect --from-dir alerts

# 3. Get the documents. See "Closing the possession gap" — this is the step
#    that decides whether the report actually moves.
python -m asx.cli capture --capture-dir captures --investorpa
python -m asx.cli capture --capture-dir captures --asx

# 4. Read the held documents. --parser is required; a bare `parse` exits 2.
python -m asx.cli parse --parser app3y

# 5. Rebuild the signal tables.
python -m asx.cli build-signals

# 6. Refresh the display quotes. ~3 minutes: ~30 tickers at 5s/host.
python -m asx.cli fetch-quotes

# 7. Check whether the feed is healthy BEFORE you publish. See below.
python -m asx.cli monitor

# 8. Render.
python -m asx.cli screen-html --out screen.html
```

Everything except step 6 finishes in seconds. Budget ten minutes for the run.

Then publish `screen.html` to the URL above, and persist your work:

```bash
python -m asx.cli snapshot --dir state      # export, overwrites state/*.csv
git add state && git commit && git push -u origin <your-branch>
```

**Step 8 is not the end.** The container is reclaimed when the session ends.
A run that renders a beautiful page and never snapshots has thrown away every
document it fetched, and the next session starts from the same place you did.
It also throws away the arrival dates behind the "New since" table, which is
the one thing on the page that cannot be recomputed later.

---

## Closing the possession gap — use the MCP

This is the step that was missed for four days, so it gets its own section.

**Detection is not possession.** An alert email says an announcement exists;
it does not carry the PDF. I checked one: the only links in it are Market
Index's website, their social accounts, and an unsubscribe link — no `.pdf`
anywhere. A detected document sits at `parse_status='detected'`: recorded,
visible, and unreadable. It produces no trade and therefore no signal.

**The InvestorPA MCP server is connected to your session.** Its tools appear
as `mcp__InvestorPA__*`. This is easy to miss, because a separate thing with
a similar name is genuinely blocked: `asx detect --source investorpa` needs
`ASX_INVESTORPA_REFRESH_TOKEN` in the environment for the unattended GitHub
Actions run, and that OAuth grant does not exist. **That blocks the cron, not
you.** A session with the MCP connected can retrieve documents today.

The vendor advertises exactly this use — "connects ASX announcements directly
to any MCP-compatible AI harnesses... Works with Claude Desktop & Mobile,
ChatGPT Desktop & Mobile, Claude Code, Codex" — which is the Invariant 11
basis recorded in `docs/SOURCE_INVESTORPA.md`.

The procedure, which resolved 30 of 30 stranded documents on 24 Aug:

1. List what is stranded, with ticker and lodgement time:

   ```sql
   SELECT d.doc_id, l.ticker, d.lodged_at, d.title
     FROM documents d
     LEFT JOIN listings l ON l.entity_id=d.entity_id AND l.valid_to IS NULL
    WHERE d.parse_status='detected' ORDER BY d.lodged_at;
   ```

2. Call `mcp__InvestorPA__search_announcements` with **exactly those tickers
   and exactly that date range**. Proportionality is the point: ask for the
   companies you already detected, not the whole exchange.

3. Match on ticker + lodgement time + title. The DB stores UTC; the feed
   returns +10:00 (AEST). Market Index truncates to the minute and InvestorPA
   reports to the second, so the feed timestamp runs **0–60 seconds later**
   than ours for the same lodgement. A ±120s tolerance with the title as
   tie-breaker matched 30/30, 29 of them on title *and* time. Where several
   identical titles fall inside the window (three BCA notices minutes apart),
   assign nearest-first and let each feed row be used once.

4. **Copy the stated URL. Never build one.** Their ids are sequential at
   ~400/day, so `announcement-pdf/{YYYYMMDD}/{id}.pdf` can always be
   constructed — which is exactly why nothing may construct it. A test
   asserts no source file does. Take the URL verbatim from the search result.

5. Write the URLs onto the detections and let the platform's own guarded,
   throttled fetch do the retrieval, so provenance records honestly as
   `possession_source='investorpa'`:

   ```sql
   UPDATE documents SET fetch_candidate_urls =
       (SELECT array_agg(DISTINCT u)
          FROM unnest(coalesce(fetch_candidate_urls,'{}') || ARRAY[:url]) u)
    WHERE doc_id = :doc_id;
   ```

   Then `asx capture --capture-dir captures --investorpa`. It handles **25 per
   run** (`fetch_investorpa_documents(conn, limit=25)`), so run it twice if
   more are waiting.

Do not skip the guard and fetch the PDF yourself. Going through `capture` is
what applies the throttle, records the source, hashes the original bytes, and
refuses an HTML login wall instead of storing it as a document.

### What "captured but not parseable" looks like

Some documents will close as `not_applicable` rather than `unparsed`. On
24 Aug three BCA notices did: their bytes were already held under another
doc_id, so `attach_document` closed them as duplicates rather than
double-storing. That is correct — double-storing enters one director purchase
twice and inflates the cluster signal. Check `source_ref` for the
`[duplicate of doc N]` marker before treating it as a failure.

---

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

`asx monitor` is the alarm the whole detection design exists to raise. Before
the 24 Aug capture it fired four; afterwards, one:

```
ALARM [freshness] detections_all: newest document fetched 2026-08-21T08:34:00+00:00
                  exceeds staleness SLO of 72h
```

The three that cleared — `capture_gap`, `capture_rate` at 19% against a 90%
floor, and `app_3y` freshness — cleared because the documents were actually
obtained. **If you see them again, the possession step above is what fixes
them**, not a tolerance adjustment.

The survivor is genuine: no alert has arrived since Friday evening. That is
the Apps Script's schedule (about 18:05 AEST daily), not an outage.

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
  fetch that branch and merge it before concluding the feed died. Its default
  is `main`, and **this repository has no `main`** (the default branch is
  `claude/database-env-vars-h63r7v`), so clearing the property breaks it.
  The trigger runs about 18:05 AEST daily; a gap until the next evening is
  the schedule, not an outage.
- **Quotes are not snapshotted, on purpose.** A restored week-old price under
  a column headed today's date is the exact lie the as-at exists to prevent.
  A fresh restore therefore flags `price_unavailable` on every row until
  `fetch-quotes` runs. Do not skip step 6 and do not read those flags as a
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

1. `make test-all` — 452 tests, no skips. A skip looks like a pass in the
   summary line, which is how two green local runs shipped two red CI runs.
2. `SELECT count(*) FROM documents WHERE parse_status='detected'` returns 0,
   or you know why each survivor could not be retrieved.
3. The stamp under the headline shows today's date, the document count, and
   the price as-at range. If the as-at is old, `fetch-quotes` did not run.
4. `build-signals` printed a count. If it printed 0 and the corpus is not
   empty, something is wrong — do not publish an empty screen quietly.
5. `git status` is clean after the snapshot commit, and `state/` moved —
   including `state/signal_first_seen.csv`. If it did not, nothing you
   fetched this session survives and the next reader sees no arrivals.
