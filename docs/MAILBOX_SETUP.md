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

### B. Apps Script → repository (free, no Google Cloud)

**Use this one.** It needs no Google Cloud project, no billing account and no
OAuth client, and it is the only option where **no credential ever reaches the
machine running the platform**.

A ~140-line script (`tools/apps-script/ForwardAlertsToRepo.gs`) runs inside
your own Google account at [script.google.com](https://script.google.com),
free with any consumer Gmail. Apps Script's built-in `GmailApp` service reads
the mailbox under the account's own authority — granted once by clicking
Allow. On an hourly trigger it commits each new alert to this repository as a
gzipped `.eml` under `alerts/YYYY/MM/`.

Three consequences, beyond dodging the billing prompt:

- **The container holds nothing worth stealing.** The GitHub token lives in
  the script's properties inside your Google account. A cloud VM that is
  reclaimed without warning takes no secret with it.
- **The repository becomes the raw zone for alerts.** The bytes are the
  publisher's, unmodified, append-only, content-addressed by git — which is
  what SPEC §3 asks of raw documents anyway. Gzip takes a Market Index alert
  from 68 KB to 11 KB, so a year of them is tens of megabytes, not hundreds.
- **It survives the container.** Alerts accumulate whether or not a session is
  running, so a week away is not a week-shaped hole in the dataset.

**Setup:**

1. [script.google.com](https://script.google.com) → **New project** → paste
   `tools/apps-script/ForwardAlertsToRepo.gs`.
2. **Project Settings → Script Properties → Add script property**, four times.
   Two of these are fixed for this repository and can be copied verbatim:

   | Property | Value | Where it comes from |
   |---|---|---|
   | `GITHUB_REPO` | `ngerver1/asx` | fixed — the repo's `owner/name` |
   | `GITHUB_BRANCH` | `claude/go-is75md` | fixed — the only branch that exists |
   | `GITHUB_TOKEN` | `github_pat_…` | created on GitHub, see below |
   | `GMAIL_QUERY` | *(optional)* | defaults to `from:marketindex.com.au newer_than:7d` |

   **Creating the token** — [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new):

   | Field | Set it to |
   |---|---|
   | Token name | `asx alert forwarder` |
   | Resource owner | `ngerver1` |
   | Expiration | your choice — see the warning below |
   | Repository access | **Only select repositories** → `ngerver1/asx` |
   | Permissions → Repository → **Contents** | **Read and write** |
   | Every other permission | leave at *No access* |

   Generate, then copy the `github_pat_…` value. GitHub shows it once. Paste
   it straight into `GITHUB_TOKEN` with no quotes and no trailing spaces.

   Contents is the only permission needed: the script writes files and does
   nothing else. Scoping it to the single repository means a leak costs you
   this repository and nothing else in your account.

3. **Run `checkSetup()`** from the editor's function dropdown and approve the
   permission prompt. It commits nothing and proves each thing that would
   otherwise fail silently on a trigger nobody is watching:

   ```
   OK  repo=ngerver1/asx branch=claude/go-is75md
   OK  token can read and write
   Gmail query: from:marketindex.com.au newer_than:7d
     matches 12 thread(s), of which 12 not yet ingested
   ```

   It distinguishes the failures that look alike from the outside — a wrong
   repo name and a token without access both return 404, and a read-only
   token looks perfect until the first commit fails hours later. If the Gmail
   query matches nothing, check the *From* address on a real alert and set
   `GMAIL_QUERY` accordingly.

4. Run `diagnoseMailbox()`. It commits nothing and reports how much history
   is actually in the mailbox — by age, how much is already committed, and
   whether any alerts have been filed into Spam or Trash, which
   `GmailApp.search()` cannot see at all:

   ```
   Messages from marketindex.com.au, by age:
     newer_than:7d   26 message(s) in 26 thread(s)
     newer_than:30d  118 message(s) in 118 thread(s)
     all time        118 message(s) in 118 thread(s)

   Already committed : 26
   Not yet committed : 92   <- backfillAlerts() takes these
   ```

5. **Run `backfillAlerts()` repeatedly until it reports `0 remaining`.**
   `forwardAlerts()` only looks back `GMAIL_QUERY`'s window — seven days by
   default, which is right for a recurring trigger and wrong for a first run
   against a mailbox with history. `backfillAlerts()` ignores the window
   entirely, stops at the five-minute mark to avoid Apps Script's six-minute
   kill, and resumes cleanly: committed threads get labelled and labelled
   threads are excluded, so there is no cursor to lose.

6. **Triggers → Add Trigger →** `forwardAlerts`, time-driven. **Daily is
   enough**; hourly buys latency you do not need.

   The 96-hour capture SLA is measured from `detected_at`, which is the
   *alert's own send time*, not when the script picked it up — so a daily
   run costs at most 24 hours of a 96-hour budget and leaves three days to
   capture. The `newer_than:7d` window means a failed run is retried on the
   next six days rather than losing anything.

   Note what a trigger does and does not do: it keeps alerts accumulating in
   git without a session running, which is the part that has to be
   unattended. Ingesting them (`asx detect --from-dir alerts`) still needs a
   session, and catching up on a week at once is one command — detections are
   keyed on the ASX announcement number, so nothing double-counts.

> **Two things will silently stop this feed.**
> **Token expiry** — a fine-grained PAT must have one. When it lapses the
> script throws, and Apps Script emails you about a failed trigger, so put
> the renewal date in a calendar rather than relying on noticing.
> **Branch deletion** — `claude/go-is75md` is where alerts are committed. If
> it is ever merged and deleted, `checkSetup()` will say so; update
> `GITHUB_BRANCH` before that happens.

Then, in any session:

```bash
asx detect --from-dir alerts
```

The script labels a thread `asx-ingested` only after every message in it
commits successfully, so a failed run retries rather than losing an alert. It
never uses read state as the marker — reading an alert on your phone must not
hide it from the platform.

### C. Gmail API — if you would rather use Google Cloud

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
