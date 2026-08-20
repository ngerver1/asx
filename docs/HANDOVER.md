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
| open review items | 679 |

The published screen lives at
**https://claude.ai/code/artifact/228b70bf-0797-4c15-9f73-b473ebd818ba**

To change it, republish **passing that URL** as `url`. Publishing without it
creates a second artifact and the owner keeps the stale one.

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
