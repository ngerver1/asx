# reference/

Small reference inputs that are **not** regenerable from a publisher's live
site, kept here so a load can be repeated on a fresh container.

`index_membership` is deliberately outside the state snapshot (it is derived,
not observed), so without the file it was loaded from, a container rebuild
loses the ASX 300 size proxy silently — and a screen with no membership data
does not fail, it just stops excluding large caps. That is the failure mode
worth spending 8 KB to avoid.

## asx300_2026-08-20.csv

The S&P/ASX 300 constituent list as at 20 August 2026, supplied by the owner.

**Provenance is incomplete.** It was pasted into a session rather than
downloaded, so the source URL is not recorded and `index_membership.source_url`
carries `owner-supplied:pasted-into-session-2026-08-20` rather than a real
address. Correct it and re-run before treating this as evidence (Invariant 12);
the load is idempotent per (index, ticker, date).

Verified on load: all 300 codes resolve to listings open on that date, so the
list is current — an earlier list supplied for the same purpose had 85 codes
(28%) already delisted and was rejected.

Contains about 12 LICs and trusts (AFI, AUI, BKI, BWP, GCI, GLS, LGF, LSF,
MXT, PGF, PL8, QRI) that are not true index constituents. They are large
regardless, so excluding them from a smallcap screen costs nothing.

Reload with:

    asx load-index --file reference/asx300_2026-08-20.csv --as-of 2026-08-20 \
        --source-url <the real URL, once known>
