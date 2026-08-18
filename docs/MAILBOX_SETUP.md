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

### B. IMAP — for the daily scheduled run

```bash
export ASX_IMAP_HOST=imap.gmail.com
export ASX_IMAP_USER=your-alert-account@gmail.com
export ASX_IMAP_PASSWORD=<16-character app password>
export ASX_IMAP_FOLDER=INBOX          # or a Gmail label name
asx detect
```

**Gmail specifics, verified August 2026.** Google stopped accepting a plain
account password for IMAP on 14 March 2025; third-party clients must use
OAuth, *with app passwords retained as the exception*. So:

- 2-Step Verification must be on — app passwords cannot be created without it.
- Create one at `myaccount.google.com/apppasswords` in a browser (the mobile
  app cannot). Google shows the 16 characters once.
- IMAP must be enabled: Gmail → Settings → *Forwarding and POP/IMAP*.
- Advanced Protection disables app passwords entirely. If the account is
  enrolled, use option A or implement OAuth.

Sources: [transition from less secure apps to OAuth](https://support.google.com/a/answer/14114704),
[sign in with app passwords](https://support.google.com/mail/answer/185833).

**A Gmail app password is not read-only.** It grants full mailbox access to
that account, and it bypasses 2-Step Verification by design. Two consequences:

- Use a **dedicated account that receives nothing but alerts**. If it leaks,
  the loss is a stream of public announcement notifications.
- Set it in the environment where the job actually runs. Never paste it into
  a chat transcript, a commit, or a remote container you do not control — a
  remote agent session is exactly such a container, and it is discarded
  without warning.

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
| `marketindex` | subject `TICKER - Title` | ☐ **not yet** |
| `listcorp` | subject `TICKER - Title` | ☐ not yet |
| anything else | `ir_email`, subject as title | ☐ not yet |

Until a rule is validated, treat its ticker and title extraction as
unproven. A wrong ticker attaches a detection to the wrong entity, which is
an Invariant 1 failure, so the rules are written to fail to `None` rather
than to a plausible-looking guess.

The digest question is open and matters more than the rest: if Market Index
sends **one email listing several announcements**, the current code reads it
as a single detection and silently drops the others. That cannot be
determined without a real email.
