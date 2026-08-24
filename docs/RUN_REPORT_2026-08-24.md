# Run report — 24 August 2026

The documented process, run end to end on a fresh container. Every number below
was produced by this run; where it disagrees with `docs/HANDOVER.md` the
disagreement is called out rather than smoothed over.

## What was run

| Step | Result |
|---|---|
| `asx snapshot --dir state --restore` | 1,124 documents, 311 trades, 1,830 entities |
| `asx build-signals` | 9 cluster-buy, 19 conviction |
| `asx signals --kind conviction/cluster` | `conviction.csv` (19), `cluster.csv` (9) |
| `make test-all` | **310 passed**, exit 0, nothing skipped |
| `make monitor` | **5 alarms**, exit 1 (by design) |
| `make ops-report` | generated, reproduced below |

The pipeline reproduces the handover exactly: the same document, trade and
entity counts, and the same 9 + 19 signals. Nothing regressed.

## The five alarms

Three are artifacts of restoring a four-day-old snapshot. Two are real.

### Restore artifacts — freshness ×2, volume ×1

Newest lodgement is `2026-08-20 07:13`, newest fetch `2026-08-20 07:51`; today is
the 24th. No feed has run since the snapshot was taken, so "zero documents in 5d"
is arithmetic, not a broken parser. Daily volume up to the 20th is healthy
(5–123 documents/day). Per the prime directive these stay **open alarms until a
human confirms** — this report is the mechanical explanation, not the sign-off.

### Real — capture gap (5 documents)

Five parseable announcements detected but never captured: **BCA ×4, SGQ ×1**,
oldest 2026-08-19. Each is a hole in the dataset until opened in the capture
browser. `asx worklist` prints the URLs.

### Real — classification base rate, and the monitor's diagnosis is wrong

The alarm fires correctly. Its stated cause — "probable consideration-wording
drift" — is not what the data shows.

On-market share by month: **June 40.9% → July 24.3% → August 19.8%**, with
`unknown` absorbing the difference (June 14% → August 44%). Of 304 live trades,
**123 are `unknown`**. That bucket breaks down as:

| | rows | |
|---|---|---|
| no consideration text *and* no quantities | 84 | extraction failure — see review queue |
| text says "on-market" | 18 | **should be classifiable** |
| text says "…reinvestment plan" | 3 | **should be `drp`** |
| fees / incentive wording | 6 | correctly not a buy |
| other wording | 12 | mixed |

**The mechanism.** For 16 of the 18 on-market rows, `consideration_aud IS NULL`
*and* the nature text carries no cash token. `classify_trade` requires cash
evidence before it will call an acquisition `onmkt_buy_cash`, so it returns
`unknown` — which is Invariant 8 working as designed, not failing. Confirmed
directly:

```
classify_trade('On-market trade', qty_acquired=1000, consideration_aud=None)  -> unknown
classify_trade('On-market trade', qty_acquired=1000, consideration_aud=5000)  -> onmkt_buy_cash
```

The wording never drifted. **The form's separate value-of-consideration box is
not reaching `consideration_aud`.** This is a parser extraction gap, and the fix
belongs there — loosening the regex to rescue these rows would label a purchase
cash-settled on no evidence, which is the exact failure Invariant 8 exists to
prevent.

Worth 16 additional on-market buys against 73 currently classified: **+22% on the
population that feeds both screens**, without weakening a single rule.

### A second, narrower gap: trust vocabulary

The `drp` rule matches `dividend reinvestment` and `reinvestment of
distributions`, but not **`distribution reinvestment plan`** — the wording listed
trusts actually use, because trusts pay distributions and companies pay
dividends. Three rows sit in `unknown` for this reason.

This is the same root cause as the Phase 0 criterion 0.2 shortfall already
written up in `README.md` (listed trusts hold an ARSN, not an ACN): the
platform's vocabulary assumes a company. Worth auditing as one theme rather than
patching twice.

## The review queue is one defect class

682 open (580 extraction, 96 resolution, 6 detection). **422 of them — 62% of the
entire queue — are a single reason:** `arithmetic unverifiable (held before…)`.
Another 8 are the same on a second notice.

That ties directly to the 84 `unknown` trades holding no quantities, no
`held_before`/`held_after` and `security_class = 'unknown'`. One extraction
defect is generating the majority of the backlog and a majority of the unusable
trades. It is the highest-leverage single fix in the dataset.

## A number in the handover that does not reproduce

`docs/HANDOVER.md` reports **578 unverified readings** in
`uncorroborated_director_trades`. After a clean restore that view returns **0**.

Cause: the view is built on `parsed_records`, and `parsed_records` is not in the
snapshot's `TABLES` list. It is also **not** among the omissions the snapshot
docstring documents as deliberate (`asic_registry`, raw zone).

Severity is *undocumented omission*, not data loss: 990 of 1,124 documents retain
`document_text`, so the records are reprocessable. But they are not restored
automatically, and a reader comparing the handover to a fresh container would
conclude 578 unverified readings had been resolved when they simply are not
loaded. Either add `parsed_records` to the snapshot or document why it is
excluded and how to regenerate it.

## The network blocker has lifted

`curl -sI https://stockanalysis.com` now returns **HTTP/2 200**. The premise the
previous session was blocked on no longer holds.

**No price was fetched.** Invariant 11 requires the source's terms be checked
before automating against it, and `docs/ACCESS_DECISION.md` is not signed off for
any price source. Reachability is not permission. This needs the owner's
decision, and the handover's guidance still stands: store any quote with its
as-at date and source URL, label the column "price as at &lt;date&gt;", and do not
register a display-quote source as a `PriceSource` for backtests.

## Operations one-pager

```
DOCUMENTS THIS WEEK: 1005          PARSE STATUS (all time):
  app_3y               776           validated        285
  other                131           review           574
  app_3z                83           not_applicable   260
  reference             15           detected           5

REVIEW QUEUE: 682 open, oldest 2026-08-18
RECONCILIATIONS (7d): 0 checks, 0 exceptions
```

## Recommended order of work

1. **Fix the value-of-consideration box extraction**, gold fixtures and failing
   tests first. Recovers 16 on-market buys; does not touch a rule.
2. **Fix the holdings arithmetic extraction.** 62% of the review queue and 84
   dead trade rows trace to it.
3. **Capture the 5 outstanding announcements** (BCA ×4, SGQ ×1).
4. **Decide `parsed_records`** — snapshot it, or document the exclusion.
5. **Add `distribution reinvestment plan`** to the `drp` rule, as part of a
   trust-vocabulary audit rather than a spot patch.
6. **Owner decisions still open** and unchanged from the handover: index
   membership provenance, backdating the index snapshot, Appendix 3X scope
   extension, and now the price-source access decision.

Items 1, 2 and 5 are parser changes and therefore go through
`make reprocess PARSER=… ` — never a hand-edit of canonical tables.
