# investorpa.com — assessment

Investigated 20 Aug 2026 as a possible route to announcement documents.
**Not adopted. Two questions must be answered by a human first**, both of
which are stated at the end.

## What it is

An ASX announcement alert service, the same role Market Index plays today:
a watchlist of up to 200 codes, email alerts when a followed company lodges,
AI summaries of price-sensitive announcements ("Fast Feed"), and a daily
roundup. It ran from 2011 to 2017 with over 4,000 users — reportedly more
than 20,000 emails a day in reporting season — and has since relaunched.
([features](https://investorpa.com/), [about](https://investorpa.com/about/))

## Why it is interesting — it hosts the PDF

This is the whole reason to look at it. Market Index alerts carry **no
document link at all**: their emails link to a Market Index page, and the
exchange's own document URL is nowhere in them. That is the bottleneck the
platform keeps hitting — possession needs a human because nothing automated
knows where the document lives.

investorpa.com re-hosts the announcement PDF at a plain, direct URL:

```
https://investorpa.com/announcement-pdf/{YYYYMMDD}/{id}.pdf
```

Observed examples, and what they show about the identifier:

| Date | id | ids/day since previous |
|---|---|---|
| 2025-02-18 | 102576 | — |
| 2025-05-16 | 138658 | 415 |
| 2025-08-14 | 172561 | 377 |
| 2025-11-12 | 218997 | 516 |
| 2026-02-20 | 258878 | 399 |
| 2026-05-14 | 293079 | 414 |

Monotonic with date at roughly 400 a day: it is their own publication
sequence, not a function of the ASX announcement number, so a URL still
cannot be **derived** from anything the platform holds. It has to arrive.

**If their alert emails contain that link, the possession layer automates
end to end**: email → detection *and* document URL → retrieval → parse. That
is the difference between a manual sweep and a pipeline.

## Why it is not adopted yet

**1. The terms could not be read.** investorpa.com is unreachable from the
platform's network — the egress proxy blocks it, as it blocks the ASX and
HotCopper. `fetch_guard.DECLARED_SOURCES` requires a recorded basis *before*
a host is fetched, and "the site is useful" is not a basis. Nothing has been
declared.

**2. It is a re-host, not the exchange.** The same category question as
HotCopper: the owner's legal advice concerned asx.com.au, and taking
documents in bulk from someone else's copy is a different question from
taking them from the exchange. It is not the *same* as HotCopper — a paying
data vendor is a different thing from a forum, and the service is reported to
pay the ASX five figures a year for the feed — but a vendor's licence governs
what **they** may redistribute to **their** users, and does not by itself
extend to a third party fetching from them in volume.

**3. The identifiers are sequential, which is a trap worth naming.** About
400 a day, monotonic. Enumerating them would be trivial, would collect every
announcement on the exchange, and would be precisely the bulk crawl the
access decision forbids. It must never be built, and the ease of building it
is the reason to say so in writing.

## Prepared, and gated

The platform recognises `investorpa_alert` as a detection source, so its
emails are ingested the moment any arrive. Its links are recorded on the
detection for the owner and are **excluded from the automatic fetch set** by
`own_hosts`, so nothing is retrieved on a source nobody has cleared.

The sender rule is **uncalibrated** — no real email has been seen, so no
subject pattern is asserted and the conservative generic path applies. One
forwarded alert would fix that, exactly as five did for Market Index.

## The two questions for a human

1. **Do their terms of use permit automated retrieval of the PDFs by a
   subscriber?** If yes, declare the host in `DECLARED_SOURCES` with that
   basis quoted, and the retrieval path works immediately.
2. **Do their alert emails contain the `announcement-pdf` link?** If yes,
   possession automates for every followed company. If they link only to a
   page, the service is a Market Index equivalent and changes nothing.

Both are answered by subscribing, reading the terms page, and forwarding one
alert.
