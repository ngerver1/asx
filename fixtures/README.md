# Gold fixtures

Protocol (SPEC Appendix C): 100 hand-labelled documents per standard form,
50 per annual-report section, 60 for JORC, stratified by lodgement year, issuer
market-cap tercile, and format era. Label from the document alone; ambiguity is
labelled `unknown`, never resolved from outside knowledge. CI runs every parser
against its gold set on every change and **blocks merges on regression**.
Refresh each set with 10 new randomly sampled documents per year; never delete
old fixtures — formats recur.

## Stratification under the Tier 0 access decision

Appendix C calls for stratification by lodgement year, issuer market-cap
tercile, and format era. Two of those are constrained by the access decision,
and the response is to **record the limitation, not to relax the labelling
standard**:

- **Year / format era** — limited to the years reachable on the ASX website's
  own announcement history (~24 months per ACCESS_DECISION §2). Older format
  eras are therefore unrepresented, so a parser regression on a pre-2024
  layout would not be caught by this set. Reopen when an archive route is
  adopted.
- **Market-cap tercile** — no price data, so size is approximated by the
  ASX 300 membership proxy plus a coarse manual judgement recorded per
  fixture. Terciles are not computable; the stratification note on each
  fixture says which basis was used.
- **Labelling standard is unchanged**: label from the document alone,
  ambiguity labelled `unknown`, every label spot-checked once.

Sample size target: ~120 Appendix 3Y/3Z documents (ACCESS_DECISION §2),
against the Appendix C minimum of 100.

## Current state

Document-level gold sets are **not yet populated** — they need documents
captured through the Tier 0 route (owner opens announcements personally; the
watcher files them). What exists now:

- `app3y/classification_gold.jsonl` — synthetic labelled cases for the
  trade-classification rules (the phase's entire point is `onmkt_buy_cash`
  precision, SPEC §7). These pin the rules' behaviour; they do not substitute
  for the 100-document gold set, which is a Phase 1 acceptance requirement.

When real documents arrive: store each fixture as `app3y/<sha256>.pdf` plus
`app3y/<sha256>.labels.json` (hand-labelled field values), and the gold-set
test will pick them up automatically.
