# Connecting the alert mailbox

Detection under the Tier 0 access decision (§1) is: *alert emails tell the
platform an announcement exists*. The bytes arrive separately, from a company
IR site or from the owner personally opening the announcement. This document
is about the first half only.

**What the ingester does with an email**

1. Reads the subject and body.
2. Extracts ticker, title and a lodgement time, leaving any field it cannot
   read as `None` rather than guessing.
3. Records a `detected` row in `documents`, keyed so re-reading the mailbox
   is idempotent.
4. Records any non-ASX links (company IR sites) for later fetching.
5. **Discards asx.com.au links.** They are never followed, never queued, and
   `fetch_guard` raises if any code tries. The announcement instead appears on
   `asx worklist` for the owner to open personally.

Nothing here logs into the ASX, and nothing follows a link out of an email
automatically.

## Two ways in

### A. Saved emails — no credentials (start here)

```bash
asx detect --from-dir ~/asx-alerts        # reads *.eml
```

Export the alerts from your mail client (in Gmail: open the message →
⋮ → *Download message*, which saves a `.eml`) into a directory. Subdirectories
are read too, and non-email files are ignored.

This path exists for three reasons, and it is not a lesser option:

- **Calibration.** CLAUDE.md requires the gold fixture set *before* the
  parser. The per-sender rules in `mailbox.py` are currently unvalidated
  guesses about Market Index's subject format; they get pinned to real emails
  saved this way.
- **No secret leaves your machine.** Nothing to configure, nothing to revoke.
- **Recovery.** The IMAP path reads `UNSEEN` messages only. An alert you have
  already opened in Gmail is no longer unseen and IMAP will never hand it
  over — saving it to disk is how it gets ingested rather than silently lost.
  See "The read-first hole" below.

### B. Gmail API — the only automated route from a cloud container

**IMAP does not work from a Claude Code cloud session, and no credential
fixes it.** Direct TCP is unavailable; everything leaves through an HTTPS
policy proxy. That proxy accepts a CONNECT to `imap.gmail.com:993` and then
resets the connection during the TLS handshake, because IMAP is not HTTPS.
Measured, with an HTTPS control through the same tunnel:

```
gmail.googleapis.com:443   CONNECT 200 -> TLS ok -> HTTP 401
imap.gmail.com:993         CONNECT 200 -> ConnectionResetError
```

External Postgres (port 5432) is blocked by the same policy.

The Gmail REST API is ordinary HTTPS, so it passes — and it is the better
credential anyway. An app password grants full account access and bypasses
2-Step Verification; this uses OAuth scoped to `gmail.readonly`, which
**cannot send, delete, or mark anything read**. The read-first hole below is
not merely avoided by convention, it is impossible. The grant is revocable on
its own without touching the account password.

**Setup, once:**

1. At [console.cloud.google.com](https://console.cloud.google.com), create a
   project and enable the **Gmail API**.
2. Under *APIs & Services → Credentials*, create an **OAuth client ID** of
   type *Desktop app*. Note the client ID and secret.
3. On the *OAuth consent screen*, add the alert account as a test user. Note:
   while the app is in **testing** mode Google expires refresh tokens after
   seven days — publish the app (no verification is needed for a single test
   user on a read-only scope) if you want it to keep running.
4. On any machine with a browser, get a refresh token:

   ```bash
   python -m asx.ingest.gmail_consent --client-id ... --client-secret ...
   # open the printed URL, approve, copy the code= from the address bar
   python -m asx.ingest.gmail_consent --client-id ... --client-secret ... --code ...
   ```

5. Set the three values as **environment variables on the cloud environment**
   (claude.ai → environment settings → environment variables), not in the
   repo and not in a chat message:

   ```
   ASX_GMAIL_CLIENT_ID
   ASX_GMAIL_CLIENT_SECRET
   ASX_GMAIL_REFRESH_TOKEN
   ASX_GMAIL_LABEL          # optional, to scope the search to one label
   ```

Then `asx detect` uses the API automatically:

```bash
asx detect                      # Gmail API when ASX_GMAIL_REFRESH_TOKEN is set
asx detect --since-days 3       # narrow the search window
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
