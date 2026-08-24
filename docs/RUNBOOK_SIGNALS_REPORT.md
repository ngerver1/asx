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

# 3. Turn detections into held documents, where a URL exists to fetch.
python -m asx.cli capture --capture-dir captures --asx

# 4. Read the held documents. --parser is required; a bare `parse` exits 2.
python -m asx.cli parse --parser app3y

# 5. Rebuild the signal tables.
python -m asx.cli build-signals

# 6. Refresh the display quotes. ~3 minutes: 29 tickers at 5s/host.
python -m asx.cli fetch-quotes

# 7. Check whether the feed is healthy BEFORE you publish. See below.
python -m asx.cli monitor

# 8. Render.
python -m asx.cli screen-html --out screen.html
```

Everything except step 6 finishes in seconds. Budget five minutes for the run.

Then publish `screen.html` to the URL above, and persist your work:

```bash
python -m asx.cli snapshot --dir state      # export, overwrites state/*.csv
git add state && git commit && git push -u origin <your-branch>
```

**Step 8 is not the end.** The container is reclaimed when the session ends.
A run that renders a beautiful page and never snapshots has thrown away every
document it fetched, and the next session starts from the same place you did.

---

## What each step actually told us on 24 Aug

| Step | Output | Read it as |
|---|---|---|
| `detect --from-dir alerts` | `{"new_detections": 27, "already_known": 130, "failed": 0}` | 27 announcements the platform had never heard of. Re-reading is free — detections are idempotent on the ASX announcement number |
| `capture --asx` | `{"asx": {"eligible": 1, "retrieved": 1, "no_url": 30}}` | **This is the whole story.** 30 of 31 open detections carry no URL, so nothing could be fetched for them |
| `parse --parser app3y` | `doc 143: validated (confidence 1.00)` | The one retrieved document read cleanly |
| `build-signals` | `built 12 cluster-buy and 23 conviction-buy signal rows` | **Unchanged.** 27 new detections moved the screen by nothing |
| `fetch-quotes` | `ok=29` | Every screen row priced |
| `screen-html` | `wrote screen.html (33,016 bytes)` | Stamp reads `Built 24 Aug 2026 / Signal definition v2 / 1,152 documents held` |

### The thing that will confuse you: detection is not possession

They are separate facts, deliberately. An alert email says an announcement
exists; it does not carry the PDF. A detected document sits at
`parse_status='detected'` — recorded, visible, and **not yet readable**.

So the ordinary outcome of a sweep is: new detections, no new signals. That is
the pipeline working. It is not a failure, and it is not something to fix by
loosening a parser or inventing a URL.

Right now the numbers are:

```
review         483     detected        30   (27 app_3y, 3 app_3z)
validated      377     of those, with a document URL:  0
not_applicable 262
```

Zero. Every open detection on the board right now is un-fetchable by any
route the access decision permits, until a human supplies a URL or the
InvestorPA grant lands.

**Nothing may construct a URL to close that gap.** Identifiers are sequential,
so `announcement-pdf/{YYYYMMDD}/{id}.pdf` can always be *built* — which is
exactly why nothing builds one. `docs/ACCESS_DECISION.md` permits targeted
retrieval from a URL already held and forbids discovery. The three legitimate
ways to close it:

1. `python -m asx.cli worklist --parseable-only` lists what is waiting and
   prints a Market Index link for each one:

   ```
   1736  CTM  app_3y  2026-08-21 08:34  Change of Director's Interest Notice
       open: https://www.marketindex.com.au/asx/ctm/announcements/...
   ```

   A human opens each link, downloads the PDF, and drops it in `captures/`.
   The next `capture` attaches it to the detection by filename, sidecar, or
   the ABN printed in the document.
2. `asx set-doc-url --announcement-id <id> --url <url>` records a URL a human
   found, after which `capture --asx` can fetch it. Note it keys on the **ASX
   announcement number**, not the internal doc id. This is the bottleneck the
   access decision meant by "targeted retrieval does not scale on its own".
3. The InvestorPA feed, which returns the document URL directly — built,
   tested, and **blocked on one OAuth token**. See `docs/SOURCE_INVESTORPA.md`.

---

## Read the monitor before you publish

`asx monitor` is the alarm the whole detection design exists to raise. On
24 Aug it fired four:

```
ALARM [freshness]    app_3y: newest document fetched 2026-08-20 exceeds 96h SLO
ALARM [freshness]    detections_all: newest 2026-08-21 exceeds 72h SLO
ALARM [capture_gap]  14 parseable announcements detected but never captured
ALARM [capture_rate] only 19% of parseable detections captured over 14d (7/37)
                     — below the 90% floor
```

**These are true, and they are not yours to silence.** Publish the page
anyway — but say so when you hand it over. A screen built on a feed that is
four days stale is still the best available view; a screen presented as
current when it is not is a lie the reader cannot detect.

`capture_rate` at 19% against a 90% floor is a **review trigger** under access
decision §5, not a number to note and move past. If it is still there, say
that the manual sweep is not keeping pace.

Zero lodgements in a trading period is a pipeline alarm until a human says
otherwise (CLAUDE.md). If `detect` returns `new_detections: 0` on a weekday,
check the alert feed before assuming a quiet market.

---

## Things that will bite

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
  `invalid pdf header: b'# cap'` on the way past. Harmless —
  never parsed, never signalled — but it inflates the "documents held" count
  on the page by one. Known; not yet fixed.
- **`state/` is 7.3 MB** and grows ~4.5 KB per document. A few thousand more
  and git is the wrong home for it.
- **The DD note covers a screen that has moved on.** `docs/DD_2026-08-20.md`
  was written against 19 conviction rows; the current screen builds 23. BSA,
  SPZ and AGC are still on it, CBE is not. Four rows have never been through
  due diligence. Do not present the DD as covering the current page.

---

## What must not happen

These are invariants, not preferences. A shortcut that conflicts with one is
wrong even if the page renders and the tests pass.

- **Never join on ticker.** `ALU` is Altium before August 2024 and Alurion
  Resources after. Entity resolution goes through `listings`, which is
  effective-dated.
- **Never hand-edit a canonical table or the rendered HTML.** Fix the parser
  or the SQL and reprocess — `make reprocess` is the only path for systematic
  parse errors.
- **Never drop a row to make the screen look clean.** Flag it and leave it
  visible. A silent exclusion does not let the owner disagree with you.
- **Never construct a document URL**, and never fetch outside `fetch_guard`.
- **Delisted entities stay in every universe.** A screen that quietly drops
  them is survivorship-shaped and worthless for anything historical.

---

## Verifying you did it right

1. `make test-all` — 448 tests, no skips. A skip looks like a pass in the
   summary line, which is how two green local runs shipped two red CI runs.
2. The stamp under the headline shows today's date, the document count, and
   the price as-at range. If the as-at is old, `fetch-quotes` did not run.
3. `build-signals` printed a count. If it printed 0 and the corpus is not
   empty, something is wrong — do not publish an empty screen quietly.
4. `git status` is clean after the snapshot commit, and `state/` moved. If it
   did not, nothing you fetched this session survives.
