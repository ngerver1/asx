# Gold fixtures

Protocol (SPEC Appendix C): 100 hand-labelled documents per standard form,
50 per annual-report section, 60 for JORC, stratified by lodgement year, issuer
market-cap tercile, and format era. Label from the document alone; ambiguity is
labelled `unknown`, never resolved from outside knowledge. CI runs every parser
against its gold set on every change and **blocks merges on regression**.
Refresh each set with 10 new randomly sampled documents per year; never delete
old fixtures — formats recur.

## Current state

Real lodgement documents cannot be committed until the access decision
(docs/ACCESS_DECISION.md) confirms a redistribution-compatible source, so the
document-level gold sets are **not yet populated**. What exists now:

- `app3y/classification_gold.jsonl` — synthetic labelled cases for the
  trade-classification rules (the phase's entire point is `onmkt_buy_cash`
  precision, SPEC §7). These pin the rules' behaviour; they do not substitute
  for the 100-document gold set, which is a Phase 1 acceptance requirement.

When real documents arrive: store each fixture as `app3y/<sha256>.pdf` plus
`app3y/<sha256>.labels.json` (hand-labelled field values), and the gold-set
test will pick them up automatically.
