# Handover — 20 August 2026 (evening)

Written at the end of the session that got the network. The price column that
the previous handover was blocked on now exists. Read this first; it is the
state of play, not a history.

## First minute

The container is empty on arrival. The SessionStart hook (`.claude/hooks/`)
installs dependencies, repairs the PDF decryption backend, starts Postgres and
migrates — about 25 seconds, automatic. Then:

    asx snapshot --dir state --restore   # 1,124 docs, 311 trades, 1,830 entities, 300 index rows
    asx build-signals                    # 9 cluster + 19 conviction
    asx fetch-quotes                     # 22 delayed quotes, ~2 min (5s/host throttle)
    asx screen-html --out screen.html    # the published page, generated

`DATABASE_URL` in the environment points at a Neon instance the sandbox cannot
reach; the hook detects that and falls back to the local cluster, saying so.
That is expected, not a fault.

**`asx fetch-quotes` is now part of the restore, not an extra.** Quotes are
deliberately *not* snapshotted — a restored week-old price sitting in a column
headed by today's date is precisely the lie the as-at exists to prevent — so a
fresh container has no prices until you fetch them. The screen reports that
state rather than hiding it.

## What changed this session

**The price column is live.** stockanalysis.com is declared as a display-only
quote source (owner sign-off, ACCESS_DECISION §3 amendment). All 22 tickers on
the two screens quote. The published screen shows price, its as-at date, and
the move against what the director paid.

Three things about it are load-bearing, and a future edit should not undo them:

- **The as-at is per row, not per page.** These are sub-index companies and
  they do not all trade every day — on this build DUN, MIO and OMX carry
  quotes from 17–19 August while the rest are 20 August. A single date in the
  header would have misdated three of 28 rows.
- **It is not a `PriceSource` and must never become one.** The source cannot
  price a delisted company, so a backtest fed from it would measure survivors
  only. `StockAnalysisQuotes` has no `eod_bars`, the backtest harness contains
  no reference to `price_quotes`, and tests assert both. Backtesting is still
  out of scope and `BacktestUnavailableError` still fires.
- **A missing quote is a flagged blank, never a zero.** `price_unavailable`
  says we looked and could not price it; nulls sort last so an absent price
  cannot read as a cheap one.

**The cluster screen shows what the board holds.** `total_held` (and its value
at the current quote) is the participating directors' combined position after
buying. It is **not** `sum(held_after)` over the cluster's trades: a director
can lodge twice inside the 30-day window and the two notices report two states
of one holding. On the real CBE cluster that sum reads 29,923,551 against a
true 22,923,551 — a 31% overstatement, in the flattering direction. Each
director's most recent notice is resolved first, then those are added; a
director holding more than one security class is flagged `held_mixed_classes`
rather than blended (Invariant 8). The CSV and the published page share one
implementation (`HOLDINGS_LATERAL`) so they cannot drift apart.

**A guard bug, found by the column and worth more than it.** `fetch_guard`
read `robots.txt` via `RobotFileParser.read()`, which fetches under urllib's
default user-agent rather than ours. Robots rules are keyed *by* user-agent,
and Cloudflare-fronted sites reject the default one — stockanalysis.com
returns 403 to `Python-urllib/3.11` and 200 to our declared agent. Every quote
was refused, and the recorded reason said "robots.txt disallows this" about a
file nobody had managed to read. The guard now asks under the same honest,
unrotated identity it acts under. **This was never specific to quotes: any IR
site behind a CDN was silently unfetchable on the same false ground.**

**A restore was silently losing the size ceiling.** `index_membership` was not
in `snapshot.TABLES`, so a restored container had zero membership rows,
`is_index_member` could only answer "unknown", and every signal row gained
`membership_unknown` — the size cut stopped being applied at all and the flag
that exists to report a gap made the gap look handled. The published screen
said 18 of 19 unchecked; a rebuild from the documented restore path said 19 of
19. It is in the snapshot now, with a test that fails specifically on this
rather than on row counts (a table missing from `TABLES` is missing from both
sides of a round-trip, so the counts agreed).

**Two carried-forward figures were wrong and are corrected below**: the
Appendix 3X backlog is 122 documents, not 17, and the uncorroborated-readings
count does not survive a restore at all. Both were repeated from handover to
handover without being re-counted.

**The screen is generated, not hand-maintained.** `asx screen-html` renders it
from the database. It was previously hand-written HTML with the numbers typed
in, which let the page and the tables disagree. Two figures in its prose were
already wrong when regenerated honestly and are now derived: trades (`311`,
including superseded) and notices (`864`, from `doc_class IN ('app_3y',
'app_3z')` — `asx_doc_types` is unpopulated on this corpus).

## What is standing

| | |
|---|---|
| documents | 1,124 (864 director notices: 781 3Y, 83 3Z) |
| canonical trades | 311, of which 73 on-market cash buys |
| unverified readings | **reads 0 after a restore** — the view is built on `parsed_records`, which is not snapshotted. Reprocess to repopulate it. |
| cluster-buy signals | 9 |
| conviction signals | 19 |
| quotes held | 22 of 22 screen tickers |
| open review items | 682 |
| tests | 353, no skips (`make test-all`; a skip looks like a pass) |

The published screen lives at
**https://claude.ai/code/artifact/228b70bf-0797-4c15-9f73-b473ebd818ba**

To change it, regenerate with `asx screen-html` and republish **passing that
URL** as `url`. Publishing without it creates a second artifact and the owner
keeps the stale one.

## Decisions left with the owner

Unchanged from the last handover except where noted.

- **Classification coverage.** 127 of 311 trades are `unknown`. SPEC §7 says
  "classification is the product"; this is the largest single lever on signal
  count and does not require weakening any rule. **This is the obvious next
  piece of work** — it needs no decision, only doing.
- **Index membership provenance.** `reference/asx300_2026-08-20.csv` was
  pasted, not downloaded, so `source_url` records
  `owner-supplied:pasted-into-session-2026-08-20` instead of an address. It
  needs the real URL. See `reference/README.md`.
- **Backdating the index snapshot.** 18 of 19 conviction rows and all 9
  clusters carry `membership_unknown`: the size ceiling cannot be applied
  because the only snapshot postdates them. Backdating it to the June
  rebalance would filter them, at the cost of asserting a membership we have
  not verified. Still undecided.
- **Appendix 3X — bigger than previously recorded.** The last handover said
  17 held documents are Initial Director's Interest Notices. Counted against
  the corpus this session it is **122** (documents whose text opens
  `Appendix 3X Initial Director's Interest Notice`, Rule 3.19A.1); they sit in
  `doc_class = 'other'`, unparsed. The 17 predates the 789-announcement
  ingest. That changes the size of the decision, not its shape: a 3X states a
  holding at appointment, not a trade, so it needs its own table — forcing it
  into `director_trades` would fabricate 122 purchases and corrupt the cluster
  signal. SPEC §7 covers only 3Y/3Z, so this is a scope extension to flag for
  amendment, not to slip in.

## Things that will bite

- **Neon is unreachable and allowlisting will not fix it.** Egress is HTTPS/443
  only; the Postgres wire protocol needs raw TCP on 5432. Proven: `github.com:22`
  is blocked while `github.com:443` is not.
- **asx.com.au is REACHABLE from this session — that changed today.** Every
  previous handover said the egress proxy 403s CONNECT for every ASX host, and
  that is no longer true: `https://www.asx.com.au` returns 200 and
  `cdn-api.markitdigital.com` resolves. The targeted-retrieval route
  (`possession.fetch_asx_documents()`, signed off in ACCESS_DECISION §6) has
  never actually run against the exchange, and now could.

  Before anyone does: **only 1 of 1,124 documents has an `asx_document_url`
  recorded**, and 119 sit at `parse_status='detected'` with no URL. Targeted
  retrieval works from a URL we already hold and nothing may construct one, so
  the route unblocks exactly one document today. The bottleneck is recording
  URLs (`asx set-doc-url`), not network access — which is what the access
  decision meant by "targeted retrieval does not scale on its own". Do not
  treat the open door as a reason to go looking for the other 118.
- **The quote parser reads an embedded JS object, not an API.** It will break
  when the source changes its page, and it is built to break loudly:
  `fixtures/quotes/stockanalysis_asx_tne.html` is a real captured page and the
  parser refuses a partial quote rather than assembling one. If that fixture's
  tests fail, re-capture the page and re-read the parse — do not loosen the
  parser until it passes.
- **The raw zone is gone and that is fine.** PDFs were deleted after their text
  was stored (984 of 999 documents). `read_document` falls back to the text.
  But the text does **not** carry the PDF's creation timestamp, which is the
  fallback source for `lodged_at` — so never delete a PDF that has not been
  through `asx capture` first.
- **`state/` is 7.3 MB** against the 5 MB its own docstring assumes, and grows
  ~4.5 KB per document. A few thousand more and git is the wrong home.
- **Run the suite with a database.** Without one, 44 tests skip and a skip
  looks like a pass. `make test-all`, or check the count is 353.

## Commands added this session

    asx fetch-quotes                   # refresh display quotes for screen rows
    asx screen-html --out screen.html  # render the published screen from the db
