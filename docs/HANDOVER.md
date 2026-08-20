# Handover — 20 August 2026

Written at the end of a session that could not reach the network. The next
session is expected to reach `stockanalysis.com`. Read this first; it is the
state of play, not a history.

## First minute

The container is empty on arrival. The SessionStart hook (`.claude/hooks/`)
installs dependencies, repairs the PDF decryption backend, starts Postgres and
migrates — about 25 seconds, automatic. Then:

    asx snapshot --dir state --restore     # 1,124 documents, 311 trades, 1,830 entities
    asx build-signals                      # 9 cluster + 19 conviction
    asx signals --kind conviction --out conviction.csv

Nothing else is needed. `DATABASE_URL` in the environment points at a Neon
instance the sandbox cannot reach; the hook detects that and falls back to the
local cluster, saying so. That is expected, not a fault.

## The task this session was blocked on

Add a current market price beside what each director paid. Every price source
returned `403 host_not_allowed`, so the column does not exist.

Check first — it costs a second and tells you whether the premise holds:

    curl -sI https://stockanalysis.com | head -1

If that succeeds, the 27 tickers needing a quote are the `ticker` column of
both screens. Two things matter more than the fetching:

- **Check the source's terms before automating against it** (Invariant 11).
  The access decision is strict about this and it has not been signed off for
  any price source. Ask the owner rather than assuming.
- **Store the quote with its as-at date and source URL**, and label the column
  "price as at <date>" rather than "latest". An undated number beside figures
  traced to specific lodgements is the weakest thing on the page.

There is no price table yet. `PriceSource` in `asx/ingest/sources.py` is the
protocol to implement, but note its docstring: a source that silently drops
delisted names must not implement it, because that is what makes a backtest
lie. A quote-for-display source is a different thing from a backtest price
source and should not be registered as one.

## What is standing

| | |
|---|---|
| documents | 1,124 (858 director notices) |
| canonical trades | 311, of which 73 on-market cash buys |
| unverified readings | 578 in `uncorroborated_director_trades` (a view, not canonical) |
| cluster-buy signals | 9 |
| conviction signals | 19 |
| open review items | 682 |

The published screen lives at
**https://claude.ai/code/artifact/228b70bf-0797-4c15-9f73-b473ebd818ba**

To change it, republish **passing that URL** as `url`. Publishing without it
creates a second artifact and the owner keeps the stale one.

**That artifact is now one column behind the CSV.** Both screens gained
`counter_evidence` on 20 August; the published page does not show it, and the
two rows it changes are the two rows a reader would otherwise most easily
misread. Republishing is a small job and has not been done.

## Read docs/DD_2026-08-20.md before trusting a screen row

Three of the 19 conviction rows do not mean what the ordering implies, and the
worst of them cannot be seen from a 3Y at all. BSA — the largest
single-director accumulation on the corpus — was funded by a $2,000,000 loan
**from the company to its own Chair**, whose only permitted purpose is buying
BSA shares (announced 9 June, drawn in Q4 FY26, on the Appendix 4C as
"Director's loan to acquire BSA shares"). Meanwhile BSA revenue fell 89% to
$32.1m. The screen ranked it #4 on conviction. Anyone reading the screen alone
would have had it backwards.

The DD note also carries what the announcements add to SPZ (bought the day
after FY26 results, alongside a $5m buy-back and Microequities moving to
14.97%) and to CBE (on-market status verified against source text, because a
A$90M raise sits right beside those dates).

## Every actionable date on both screens is early

`actionable_from` is `lodged_at`'s date, and for 971 of 1,109 dated documents
(88%) `lodged_at` is the PDF's *creation* time — when the company made the
notice, not when ASX released it. Proven exactly on SPZ: created 19 Aug 20:20
AEST, released by ASX 20 Aug 08:00 AEST. 11h40m early and on the wrong calendar
day. Across five BSA notices the skew runs 2 minutes to nearly 3 days, always
the same direction. It applied to all 28 signal rows.

Both screens now carry `actionable_from_may_be_early` rather than asserting a
date they cannot support. **That is a label, not a fix.** The fix is capturing a
release timestamp at detection, and it deserves priority: until then every
forward test the platform runs is flattered by up to a trading day, in the
column that exists to stop exactly that.

## Reading the screens: counter_evidence

Both screens now carry what the insiders of the same company were doing in
the 90 days before the signal — `onmkt_sell` and `unclassified` counts with
consideration, pipe-delimited, empty when there is nothing. It is windowed
under Invariant 2, so it holds only what was knowable when the signal was; a
sell lodged afterwards is absent and is not claimed to be absent.

Two of 28 rows carry it, and both change how the row reads:

- **ALQ** `onmkt_sell:1:1285097|unclassified:1:unstated` — the +28.9% is a
  director going from 8,490 shares to 10,944 ($57,178), fourteen days after
  Malcolm Deane sold $1,285,097 on-market (doc 1066, "On market trades made
  on 29 July 2026", the day after a vesting). Read the row as insider selling
  with a rounding error attached, not as a buy signal.
- **AGC** `unclassified:2:979637` — the screened parcel is $15,237 while
  $979,637 of the same director's July activity is unclassified (docs 865,
  903, both multi-vehicle notices). The screen is showing the small parcel,
  not the story.

Neither was dropped. Both are on the screen with the reason visible, which is
the point — a silent exclusion would not let the owner disagree.

## Decisions left with the owner

- **Index membership provenance.** `reference/asx300_2026-08-20.csv` was pasted,
  not downloaded, so `source_url` records that fact instead of an address. It
  needs the real URL. See `reference/README.md`.
- **Backdating the index snapshot.** 18 of 19 conviction rows and all 9
  clusters carry `membership_unknown`: the size ceiling cannot be applied
  because the only snapshot postdates them. Backdating it to the June
  rebalance would filter them, at the cost of asserting a membership we have
  not verified. The owner has not decided.
- **Appendix 3X.** 17 held documents are Initial Director's Interest Notices
  and are unparsed. A 3X states a holding at appointment, not a trade, so it
  needs its own table — forcing it into `director_trades` would fabricate a
  purchase and corrupt the cluster signal. SPEC §7 covers only 3Y/3Z, so this
  is a scope extension to flag for amendment, not to slip in.
- **Classification coverage.** 127 of 311 trades are `unknown`. SPEC §7 says
  "classification is the product"; this is the largest single lever on signal
  count and does not require weakening any rule.

- **BSA doc 1288 — the one review item worth doing by hand.** BSA carries the
  largest single-director accumulation on the corpus: David Geraghty, five
  notices between 3 and 22 July, $1,199,913 of on-market buying through
  Roologic Pty Ltd, paying up from $0.30 to $0.32 for the biggest parcel.
  Only four of the five are canonical. Doc 1288 ($206,718.90 for 689,063
  shares) is held on *arithmetic unverifiable: held before/after missing* —
  correctly, because the notice discloses three vehicles at once (Geraghty
  personally 150,000; Roologic 403,336; Mandarin Rock holding unlisted options
  at $0.50/$0.75/$1.00 expiring 1/05/2029) and states its date as a range,
  "6-10 July 2026".

  Resolving it is worth a human's time for a second reason: the disclosed
  chain does not reconcile. Doc 838 leaves Roologic at 403,336 on 3 July; doc
  885 opens at 1,732,509 on 14 July; 1288's 689,063 closes only part of that,
  leaving ~490k–640k shares unexplained depending on which vehicles the later
  figure aggregates. Either a notice is missing or the later notice changed
  its aggregation basis, and the screen understates BSA until it is known
  which. Do not resolve it by picking the reading that closes the gap.

  The gap is in the direction of more buying, not less — but "probably fine"
  is not a reconciliation, and BSA is the name where it matters most.

## Things that will bite

- **Neon is unreachable and allowlisting will not fix it.** Egress is HTTPS/443
  only; the Postgres wire protocol needs raw TCP on 5432. Proven: `github.com:22`
  is blocked while `github.com:443` is not.
- **The raw zone is gone and that is fine.** PDFs were deleted after their text
  was stored (984 of 999 documents). `read_document` falls back to the text.
  But the text does **not** carry the PDF's creation timestamp, which is the
  fallback source for `lodged_at` — so never delete a PDF that has not been
  through `asx capture` first.
- **`state/` is 7.2 MB** against the 5 MB its own docstring assumes, and grows
  ~4.5 KB per document. A few thousand more and git is the wrong home.
- **Run the suite with a database.** Without one, 44 tests skip and a skip
  looks like a pass. `make test-all`, or check the count is 310.
