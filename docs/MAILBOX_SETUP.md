### Filtering to the announcements you want

`SUBJECT_FILTER` (a regex, in Script Properties) decides what gets committed.
It defaults to the director-interest forms, which is what Phase 1 parses:

```
(director'?s?|officer'?s?)\s+interest|appendix\s*3[yz]\b
```

Widen it as later phases need more — substantial holdings for Phase 2
registers, restricted securities for escrow, and so on.

**Filtering never deletes.** A message the filter passes over is labelled
`asx-skipped` and stays in Gmail. Widen `SUBJECT_FILTER`, run
`rescanSkipped()`, then `backfillAlerts()`, and everything the old filter
declined is reconsidered. That property is what makes filtering safe to do at
all: a filter that discarded mail would leave the platform unable to tell "no
such announcement was ever lodged" from "we threw it away in July", which is
precisely the distinction its completeness checks exist to make.

A thread is only skipped when *none* of its messages match, so a wanted
announcement can never be buried under an unwanted reply.

## Getting the documents themselves

`forwardAttachments()` commits PDF attachments to `captures/YYYY/MM/`, which
`asx capture --capture-dir captures` then files against the matching
detection.

**Nothing is fetched, because nothing may be.** asx.com.au is off limits to
any automated device under the access decision. Market Index's own pages are
not reachable from the platform's network either — the egress proxy returns
403, so their terms cannot even be read, and CLAUDE.md forbids acting beyond
a source's terms rather than assuming them. Both routes are closed.

What remains is a source that *sends* the document. Company
investor-relations mailing lists attach the PDF to the announcement email, so
receiving it involves no request to anybody: it is possession route 1 of the
access decision with the fetching removed, and it is stronger than fetching
because the company chose to send it.

**Its limit is coverage.** IR lists reach only the companies you subscribe to
individually, so this covers a watchlist, not the market. Everything outside
it still needs the manual capture sweep — which is exactly what the
capture-gap alarm measures, and why that alarm exists.

Set `ATTACHMENT_QUERY` to scope which emails are searched (default
`has:attachment filename:pdf newer_than:7d`), and add a second daily trigger
on `forwardAttachments`.

### Gmail API — if you would rather use Google Cloud — if you would rather use Google Cloud

Works, and is what I would have recommended if the Console were free of
friction. The Gmail API itself costs nothing — *"All standard use of the Gmail
API is available at no additional cost"*
([usage limits](https://developers.google.com/workspace/gmail/api/reference/quota))
— and reading a few dozen emails a day is far inside the free quota. Whether
the Console forces a billing account on you before it will enable the API is
not something these docs settle, so treat option B as the default.

If you do go this way, OAuth scoped to `gmail.readonly` **cannot send, delete
or mark anything read**, which makes the read-first hole below structurally
impossible rather than merely avoided.

1. [console.cloud.google.com](https://console.cloud.google.com) → new project
   → enable the **Gmail API**.
2. *APIs & Services → Credentials* → **OAuth client ID**, type *Desktop app*.
3. *OAuth consent screen* → add the alert account as a test user. In
   **testing** mode Google expires refresh tokens after seven days; publish
   the app (no verification needed for a read-only scope) to avoid redoing
   this weekly.
4. On a machine with a browser:

   ```bash
   python -m asx.ingest.gmail_consent --client-id ... --client-secret ...
   python -m asx.ingest.gmail_consent --client-id ... --client-secret ... --code ...
   ```

5. Set `ASX_GMAIL_CLIENT_ID`, `ASX_GMAIL_CLIENT_SECRET`,
   `ASX_GMAIL_REFRESH_TOKEN` (and optionally `ASX_GMAIL_LABEL`) as environment
   variables on the cloud environment — never in the repo or a chat message.

`asx detect` then uses the API automatically.

**IMAP does not work from a cloud container, whichever credential you hold.**
Direct TCP is unavailable; everything leaves through an HTTPS policy proxy,
which accepts a CONNECT to `imap.gmail.com:993` and then resets the
connection during the TLS handshake because IMAP is not HTTPS. Measured, with
an HTTPS control through the same tunnel:

```
gmail.googleapis.com:443   CONNECT 200 -> TLS ok -> HTTP 401
imap.gmail.com:993         CONNECT 200 -> ConnectionResetError
```

### C. IMAP — from a machine of your own

Unchanged and still supported where raw TCP is available:

```bash
export ASX_IMAP_HOST=imap.gmail.com
export ASX_IMAP_USER=your-alert-account@gmail.com
export ASX_IMAP_PASSWORD=<16-character app password>
asx detect
```

Google stopped accepting a plain account password for IMAP on 14 March 2025;
third-party clients must use OAuth, *with app passwords retained as the
exception*. 2-Step Verification must be on to create one, at
`myaccount.google.com/apppasswords` in a browser. Advanced Protection
disables app passwords entirely.

Sources: [transition from less secure apps to OAuth](https://support.google.com/a/answer/14114704),
[sign in with app passwords](https://support.google.com/mail/answer/185833).

## Nothing here survives the container

A cloud session's VM is reclaimed after inactivity and takes Postgres with
it. The ASIC company register (4.4M rows, 1.1 GB) is *regenerable* and is
meant to be rebuilt with `asx load-reference`. The entity master and the
detection log are not: a detection records that an announcement existed at a
moment now past, and no amount of reloading brings it back.

That durable set is ~12,000 rows and about 700 KB, so it lives in git:

```bash
asx snapshot --dir state              # export, then commit it
asx snapshot --dir state --restore    # rebuild a fresh container
```

Verified: a new database, migrated and restored from the snapshot with **zero
ASIC rows**, resolves every ticker and reports identical coverage — ticker
resolution goes through `listings`, not the register. Re-running `asx detect`
after a restore records nothing new, because detections are keyed on the ASX
announcement number.

**Snapshot at the end of any session that ingested detections**, or they are
lost when the VM goes.

## The read-first hole

`asx detect` searches `UNSEEN`. This is the single most likely way to lose
data silently, and it has nothing to do with the code being wrong:

> You get the alert on your phone at 09:31, read it, and decide to look at
> it properly later. It is now SEEN. `asx detect` runs at 18:00 and does not
> return it. Nothing errors. The announcement is simply absent, and the
> weekly completeness spot-check (acceptance 0.8) is the only thing that will
> ever notice.

Three ways to close it, in order of preference:

1. **Read the alerts somewhere else.** Filter them into a label you never
   open, and read announcements from `asx worklist` instead of from email.
2. **Run `asx detect` before you read your mail**, on a schedule early enough
   to win the race.
3. **Save anything you opened first** into the `--from-dir` directory.

## Calibration status

| Sender | Rule | Validated against real email? |
|---|---|---|
| `marketindex` | `ASX:{TICKER} - {Announcement\|Sensitive Ann}: {title}` | ✅ **5 real alerts, 19 Aug 2026** — `fixtures/mailbox/`, pinned by `tests/test_marketindex_gold.py` |
| `listcorp` | subject `TICKER - Title` | ☐ still a guess |
| anything else | `ir_email`, subject as title | ☐ still a guess |

### What the real Market Index format turned out to be

```
Subject: ASX:AXP - Announcement: Final Director's Interest Notice
Body:    | AXP Energy Ltd (AXP)   Published: 18/08/26, 05:37pm (AEST)
         <https://www.marketindex.com.au/asx/axp/announcements/
          final-directors-interest-notice-2A1690214?utm_source=...>
```

**The digest question is answered: one announcement per email.** No detection
is being lost to several announcements collapsing into one row.

Three things only the real emails could have revealed:

- **"Sensitive Ann" is the ASX price-sensitive flag.** `documents.price_sensitive`
  existed and nothing populated it. It now carries the market's own
  materiality marker, for free, at detection time.
- **The announcement URL carries the ASX announcement number**
  (`2A1690214`). That is a better identity than the email's Message-ID, which
  belongs to Market Index's mail provider: the ASX number is the same across
  providers and across resends, so it deduplicates where an ESP identifier
  cannot. It is now the detection key.
- **Every link in the HTML part is a Mandrill click-tracker.** Fetching one
  would register a click on the provider's system — a side effect on someone
  else's infrastructure, from a platform whose whole access posture is not
  touching things it has no business touching. URLs are therefore read from
  the plain-text part, where they are genuine but hard-wrapped across lines
  and have to be rejoined before use.

### One operational consequence

Market Index alerts carry **no PDF and no asx.com.au link**. The only route
from an alert to the document is their announcement page, which is now
recorded as the capture link and printed by `asx worklist`:

```
    60  AXP     app_3z   2026-08-18 07:37  Final Director's Interest Notice
          open: https://www.marketindex.com.au/asx/axp/announcements/...
```

So possession route 1 of the access decision (fetch from the company's own
website) is fed by **company IR mailing lists, not by these alerts**. If you
want that route to carry weight, subscribe to IR lists for the companies you
follow; those emails do link directly to PDFs on company servers.
