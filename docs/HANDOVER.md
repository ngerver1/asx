# Handover — 20 August 2026 (evening)

Written at the end of the session that got the network. The price column that
the previous handover was blocked on now exists. Read this first; it is the
state of play, not a history.

## First minute

The container is empty on arrival. The SessionStart hook (`.claude/hooks/`)
installs dependencies, repairs the PDF decryption backend, starts Postgres and
migrates — about 25 seconds, automatic. Then:

    asx snapshot --dir state --restore   # 1,124 docs, 421 trades, 1,830 entities, 300 index rows
    asx build-signals                    # 12 cluster + 23 conviction
    asx fetch-quotes                     # 29 delayed quotes, ~3 min (5s/host throttle)
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

**The holdings extraction was the real lever, and it is fixed.** Appendix 3Y
parser v4 reads the holding for the class that **changed**, not only for
ordinary shares. A third of the corpus reports an options or performance-
rights change beside an unchanged ordinary parcel; those notices had their
before/after nulled, so the form's own arithmetic could check nothing and they
sat in review permanently. Reprocessed under v4:

| | before | after |
|---|---|---|
| documents validated | 285 | **376** |
| documents in review | 574 | **483** |
| canonical trades | 311 | **421** |
| on-market cash buys | 73 | **95** |
| cluster signals | 9 | **12** |
| conviction signals | 19 | **23** |

Three new clusters (QXR, WRK, NHU), and QXR is the second row on the whole
screen whose size ceiling could actually be applied.

Two rules, both accepted **only** where the issuer's printed movement confirms
them — nothing uncorroborated leaves the reader:

1. one parcel of the changed class each side, whose difference is the movement;
2. several parcels of that class (direct and indirect, or two family trusts)
   whose **total** moves by the movement;
3. a cell reading only `Nil` — the issuer stating a holding of nothing, which
   is reading the word, not inferring a zero. Only when the cell is nothing
   else: "Direct Nil Indirect 5,901,982" states two parcels, one empty, and
   zeroing the whole cell there would erase a real indirect holding.

Rule 3 is what finally parsed **INF doc 522** — Matthew O'Kane's placement
subscription, held nothing before, 500,000 after. It classifies as
`placement_participation` and is correctly absent from the buy screens, which
is what the notice says it is.

Rule 2 is **ordinary-only, deliberately.** "Ordinary" is one class, so adding a
director's direct and indirect parcels gives their real interest in it.
"Other" is not a class — it is everything that is not ordinary, and cells
routinely hold Performance Rights and Listed Options side by side. Their total
moves by exactly the printed movement whenever the untouched class cancels out
of the difference, so the arithmetic *looks* like corroboration while the
levels describe the holding of nothing. That would have put a fabricated
denominator under conviction sizing. Two gold cases (docs 806, 344) exist
purely to hold that line.

**Reprocessing was broken by the screens, and nobody knew.** `apply_trades`
replaces a document's rows with DELETE-then-INSERT, and
`signal_conviction_buys.trade_id` was a RESTRICT foreign key — so the first
reprocess after any signal build died with a ForeignKeyViolation at the fourth
document, having already rewritten three. **Building a screen from the parser
disabled the ability to fix the parser**, which is the one recovery path
CLAUDE.md sanctions. Migration 0025 makes it `ON DELETE CASCADE`: a signal
exists only while its trade does, and is rebuilt, never patched.

    asx reprocess --parser=app3y --apply && asx build-signals   # always both

**What is still in review: 483 notices.** The buckets, measured not guessed:
176 state no parcel of the changed class in one of the cells; 103 have parcels
that reconcile with nothing; 48 print no movement to check against. These are
genuinely unverified, not mis-parsed — the honest place for them.

## What is standing

| | |
|---|---|
| documents | 1,124 (864 director notices: 781 3Y, 83 3Z) |
| canonical trades | 421, of which 95 on-market cash buys |
| unverified readings | **reads 0 after a restore** — the view is built on `parsed_records`, which is not snapshotted. Reprocess to repopulate it. |
| cluster-buy signals | 12 |
| conviction signals | 23 |
| quotes held | 29 of 29 screen tickers |
| open review items | 702 |
| tests | 389, no skips (`make test-all`; a skip looks like a pass) |

The published screen lives at
**https://claude.ai/code/artifact/228b70bf-0797-4c15-9f73-b473ebd818ba**

To change it, regenerate with `asx screen-html` and republish **passing that
URL** as `url`. Publishing without it creates a second artifact and the owner
keeps the stale one.

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

Unchanged from the last handover except where noted.

- **Classification coverage.** 145 of 421 trades are `unknown`. SPEC §7 says
  "classification is the product". With the holdings fix landed this is now
  the largest remaining lever that needs no decision. Note it grew in absolute
  terms (127 -> 145) because the corpus grew by 110 trades; the share fell from
  41% to 34%.
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
  looks like a pass. `make test-all`, or check the count is 389.

## Commands added this session

    asx fetch-quotes                   # refresh display quotes for screen rows
    asx screen-html --out screen.html  # render the published screen from the db
