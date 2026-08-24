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
