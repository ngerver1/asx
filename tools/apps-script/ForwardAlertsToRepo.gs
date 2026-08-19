/**
 * Market Index alert -> GitHub repository.
 *
 * Runs inside the owner's own Google account at script.google.com. No Google
 * Cloud project, no billing account, and no OAuth client: Apps Script's
 * built-in GmailApp service reads the mailbox under the account's own
 * authority, which is granted once by clicking Allow.
 *
 * Each new alert is written to the repository as a gzipped .eml under
 * alerts/YYYY/MM/. `asx detect --from-dir alerts` then ingests it from any
 * checkout. Three consequences worth stating plainly:
 *
 *   1. No credential ever reaches the machine running the platform. The
 *      GitHub token lives in this script's properties, inside the Google
 *      account. A cloud container that is reclaimed without warning holds
 *      nothing worth stealing.
 *   2. The repository becomes the raw zone for alerts. The bytes are the
 *      publisher's, unmodified, append-only, and content-addressed by git —
 *      which is what SPEC §3 asks of raw documents anyway.
 *   3. It survives the container. Alerts accumulate whether or not a session
 *      is running, so a week away does not become a week-shaped hole.
 *
 * The label is applied AFTER a successful commit, so a failed run retries the
 * same message rather than losing it. Read state is never used as the marker:
 * an alert the owner reads on their phone must still be ingested.
 *
 * SETUP
 *   1. script.google.com -> New project -> paste this file.
 *   2. Project Settings -> Script Properties, add:
 *        GITHUB_TOKEN   a fine-grained PAT with Contents: read and write,
 *                       scoped to this ONE repository and nothing else
 *        GITHUB_REPO    e.g. ngerver1/asx
 *        GITHUB_BRANCH  e.g. claude/go-is75md   (optional, defaults to main)
 *        GMAIL_QUERY    optional, defaults to the query below
 *   3. Run checkSetup() first. It proves the properties are right and
 *      commits nothing. Approve the permission prompt when asked.
 *   4. Run diagnoseMailbox() to see how much history is actually there.
 *   5. Run backfillAlerts() repeatedly until it reports 0 remaining. This
 *      ignores the date window and walks the whole mailbox; it stops at five
 *      minutes to avoid Apps Script's six-minute kill and resumes cleanly.
 *   6. Triggers -> Add Trigger -> forwardAlerts, time-driven, daily.
 *
 * forwardAlerts() only looks back GMAIL_QUERY's window (7 days by default),
 * which is right for a recurring trigger and wrong for a first run against a
 * mailbox with history. backfillAlerts() is the one to use for history.
 */

var PROCESSED_LABEL = 'asx-ingested';
var SKIPPED_LABEL = 'asx-skipped';
var ATTACHMENT_LABEL = 'asx-attachment-saved';
var DEFAULT_QUERY = 'from:marketindex.com.au newer_than:7d';

/**
 * Which announcements are worth committing.
 *
 * Set SUBJECT_FILTER in Script Properties to widen it. The default is the
 * director-interest forms, which is what Phase 1 parses.
 *
 * Nothing is DELETED by filtering. A skipped thread is labelled
 * 'asx-skipped' and stays in Gmail, so widening the filter later and running
 * rescanSkipped() recovers every message the old filter passed over. A filter
 * that silently discarded mail would make the platform's own completeness
 * checks meaningless — it could never tell "no such announcement existed"
 * from "we threw it away in July".
 */
var DEFAULT_SUBJECT_FILTER =
  "(director'?s?|officer'?s?)\\s+interest|appendix\\s*3[yz]\\b";

/** Emails that CARRY the document, rather than linking to it. */
var DEFAULT_ATTACHMENT_QUERY = 'has:attachment filename:pdf newer_than:7d';

/**
 * Validate the setup WITHOUT committing anything.
 *
 * Every failure below is one that would otherwise show up as an empty
 * alerts/ directory an unknown number of days later, which is
 * indistinguishable from a quiet market.
 */
function checkSetup() {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('GITHUB_TOKEN');
  var repo = props.getProperty('GITHUB_REPO');
  var branch = props.getProperty('GITHUB_BRANCH') || 'main';
  var query = props.getProperty('GMAIL_QUERY') || DEFAULT_QUERY;
  var problems = [];

  if (!token) problems.push('GITHUB_TOKEN is not set.');
  if (!repo) problems.push('GITHUB_REPO is not set (expected owner/repo).');
  else if (repo.indexOf('/') < 0)
    problems.push('GITHUB_REPO should look like owner/repo, got: ' + repo);
  if (token && token.indexOf(' ') >= 0)
    problems.push('GITHUB_TOKEN contains a space — it was probably pasted ' +
                  'with surrounding text.');
  if (problems.length) throw new Error(problems.join('\n'));

  var headers = {
    Authorization: 'Bearer ' + token,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28'
  };

  // 1. Does the token reach the repository at all?
  var r = UrlFetchApp.fetch('https://api.github.com/repos/' + repo,
                            {headers: headers, muteHttpExceptions: true});
  if (r.getResponseCode() === 401)
    throw new Error('GitHub rejected the token (401). It is wrong, revoked, ' +
                    'or expired.');
  if (r.getResponseCode() === 404)
    throw new Error('Cannot see ' + repo + ' (404). Either GITHUB_REPO is ' +
                    'wrong, or the fine-grained token was not granted access ' +
                    'to this repository.');
  if (r.getResponseCode() >= 300)
    throw new Error('GitHub returned ' + r.getResponseCode() + ': ' +
                    r.getContentText().slice(0, 200));

  // 2. Does the branch exist? A typo here silently commits nowhere.
  var b = UrlFetchApp.fetch('https://api.github.com/repos/' + repo +
                            '/branches/' + encodeURIComponent(branch),
                            {headers: headers, muteHttpExceptions: true});
  if (b.getResponseCode() === 404)
    throw new Error('Branch "' + branch + '" does not exist in ' + repo +
                    '. Set GITHUB_BRANCH to an existing branch.');

  // 3. Can it WRITE? Read access looks identical until the first commit
  //    fails, hours later, on a trigger nobody is watching.
  var probePath = '.github-write-probe-' + Date.now();
  var put = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + repo + '/contents/' + probePath,
    {method: 'put', headers: headers, contentType: 'application/json',
     muteHttpExceptions: true,
     payload: JSON.stringify({
       message: 'write probe from Apps Script (deleted immediately)',
       content: Utilities.base64Encode('probe'), branch: branch})});
  if (put.getResponseCode() === 403)
    throw new Error('The token can read ' + repo + ' but not write to it. ' +
                    'Give it Repository permissions -> Contents: ' +
                    'Read and write.');
  if (put.getResponseCode() >= 300)
    throw new Error('Write probe failed ' + put.getResponseCode() + ': ' +
                    put.getContentText().slice(0, 200));
  var sha = JSON.parse(put.getContentText()).content.sha;
  UrlFetchApp.fetch(
    'https://api.github.com/repos/' + repo + '/contents/' + probePath,
    {method: 'delete', headers: headers, contentType: 'application/json',
     muteHttpExceptions: true,
     payload: JSON.stringify({message: 'remove write probe', sha: sha,
                              branch: branch})});

  // 4. Does the Gmail query actually match anything?
  var threads = GmailApp.search(query, 0, 20);
  var waiting = GmailApp.search(query + ' -label:"' + PROCESSED_LABEL + '"', 0, 20);

  console.log('OK  repo=' + repo + ' branch=' + branch);
  console.log('OK  token can read and write');
  console.log('Gmail query: ' + query);
  console.log('  matches ' + threads.length + ' thread(s), of which ' +
              waiting.length + ' not yet ingested');
  if (threads.length === 0) {
    console.warn('The query matched NOTHING. Check the sender address on a ' +
                 'real alert — if it is not marketindex.com.au, set ' +
                 'GMAIL_QUERY to something that matches.');
  }
  return 'setup ok';
}

/**
 * Read-only census of the mailbox. Commits nothing, labels nothing.
 *
 * Answers "where did my emails go" from the only place the answer exists.
 * The default query carries `newer_than:7d`, which is right for a daily
 * trigger and wrong for a first run against a mailbox with history — this
 * shows exactly how much sits outside that window.
 */
function diagnoseMailbox() {
  var props = PropertiesService.getScriptProperties();
  var sender = 'from:marketindex.com.au';
  var windows = ['newer_than:1d', 'newer_than:7d', 'newer_than:30d',
                 'newer_than:90d', 'newer_than:1y', ''];

  console.log('Configured GMAIL_QUERY: ' +
              (props.getProperty('GMAIL_QUERY') || DEFAULT_QUERY + '  (default)'));
  console.log('');
  console.log('Messages from marketindex.com.au, by age:');
  for (var i = 0; i < windows.length; i++) {
    var q = sender + (windows[i] ? ' ' + windows[i] : '');
    var n = countMessages(q);
    console.log('  ' + rpad(windows[i] || 'all time', 16) + n.messages +
                ' message(s) in ' + n.threads + ' thread(s)');
  }

  var done = countMessages(sender + ' label:"' + PROCESSED_LABEL + '"');
  var skip = countMessages(sender + ' label:"' + SKIPPED_LABEL + '"');
  var todo = countMessages(sender + ' -label:"' + PROCESSED_LABEL +
                           '" -label:"' + SKIPPED_LABEL + '"');
  console.log('');
  console.log('Already committed  : ' + done.messages);
  console.log('Filtered out       : ' + skip.messages +
              '   (kept in Gmail; rescanSkipped() reconsiders them)');
  console.log('Not yet considered : ' + todo.messages +
              '   <- backfillAlerts() takes these');

  var atts = countMessages(props.getProperty('ATTACHMENT_QUERY') ||
                           DEFAULT_ATTACHMENT_QUERY);
  console.log('');
  console.log('Emails carrying a PDF attachment: ' + atts.messages +
              '   <- forwardAttachments() commits these to captures/');
  if (!atts.messages) {
    console.log('  None. Market Index alerts carry no attachment, by design.');
    console.log('  Subscribe to company IR mailing lists for the ones that do.');
  }

  var all = GmailApp.search(sender, 0, 500);
  if (all.length) {
    var oldest = all[all.length - 1].getMessages()[0];
    var newest = all[0].getMessages()[0];
    console.log('');
    console.log('Oldest: ' + oldest.getDate() + '  ' + oldest.getSubject().slice(0, 60));
    console.log('Newest: ' + newest.getDate() + '  ' + newest.getSubject().slice(0, 60));
  }

  // Search skips Spam and Trash, so an alert filed there is invisible to the
  // forwarder AND to this count unless asked for explicitly.
  var spam = GmailApp.search(sender + ' in:spam', 0, 100).length;
  var trash = GmailApp.search(sender + ' in:trash', 0, 100).length;
  if (spam || trash) {
    console.warn('IN SPAM: ' + spam + ' thread(s), IN TRASH: ' + trash +
                 ' thread(s). GmailApp.search() cannot see either, so these ' +
                 'will never be committed. Move them to the inbox.');
  }
  return 'diagnosis complete';
}

function countMessages(query) {
  var threads = GmailApp.search(query, 0, 500);
  var messages = 0;
  for (var i = 0; i < threads.length; i++) messages += threads[i].getMessageCount();
  return {threads: threads.length, messages: messages};
}

function rpad(s, n) { while (s.length < n) s += ' '; return s; }

/**
 * One-off backfill: every alert ever received, ignoring the date window.
 *
 * Apps Script kills an execution at six minutes, so this stops cleanly at
 * five and reports what is left. It is resumable by construction — committed
 * threads get the label, and the search excludes labelled threads — so
 * running it repeatedly until it reports 0 remaining walks the whole mailbox
 * without tracking any cursor.
 */
/**
 * Un-skip everything the subject filter passed over, so a widened
 * SUBJECT_FILTER can reconsider it. Nothing was deleted, so nothing is lost:
 * this is what makes filtering a safe thing to do at all.
 */
function rescanSkipped() {
  var label = GmailApp.getUserLabelByName(SKIPPED_LABEL);
  if (!label) return 'nothing has been skipped';
  var n = 0;
  while (true) {
    var threads = label.getThreads(0, 100);
    if (!threads.length) break;
    for (var i = 0; i < threads.length; i++) { threads[i].removeLabel(label); n++; }
  }
  console.log('un-skipped ' + n + ' thread(s); run backfillAlerts() to reconsider them');
  return 'un-skipped ' + n + ' thread(s)';
}

function backfillAlerts() {
  return runForwarder('from:marketindex.com.au', 5 * 60 * 1000);
}

function forwardAlerts() {
  var props = PropertiesService.getScriptProperties();
  var query = props.getProperty('GMAIL_QUERY') || DEFAULT_QUERY;
  return runForwarder(query, 5 * 60 * 1000);
}

/**
 * Commit every message matching `query` that is not already labelled.
 *
 * Stops at `deadlineMs` rather than being killed mid-commit by Apps Script's
 * six-minute limit, and says how many are left. Resumable by construction:
 * committed threads are labelled and labelled threads are excluded, so there
 * is no cursor to lose.
 */
function runForwarder(query, deadlineMs) {
  var started = Date.now();
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('GITHUB_TOKEN');
  var repo = props.getProperty('GITHUB_REPO');
  if (!token || !repo) {
    throw new Error('Set GITHUB_TOKEN and GITHUB_REPO in Script Properties. ' +
                    'Run checkSetup() first.');
  }
  var branch = props.getProperty('GITHUB_BRANCH') || 'main';

  var label = GmailApp.getUserLabelByName(PROCESSED_LABEL) ||
              GmailApp.createLabel(PROCESSED_LABEL);
  var skipLabel = GmailApp.getUserLabelByName(SKIPPED_LABEL) ||
                  GmailApp.createLabel(SKIPPED_LABEL);
  var filter = new RegExp(
    props.getProperty('SUBJECT_FILTER') || DEFAULT_SUBJECT_FILTER, 'i');
  var pending = query + ' -label:"' + PROCESSED_LABEL +
                '" -label:"' + SKIPPED_LABEL + '"';

  var committed = 0, skipped = 0, failed = 0, filtered = 0, timedOut = false;

  // Always start at 0: labelling a thread removes it from this search, so
  // the next page walks up to meet us. Paging with an offset instead would
  // skip threads as the result set shrinks underneath.
  while (!timedOut) {
    var threads = GmailApp.search(pending, 0, 25);
    if (!threads.length) break;

    for (var t = 0; t < threads.length; t++) {
      if (Date.now() - started > deadlineMs) { timedOut = true; break; }
      var messages = threads[t].getMessages();
      // A thread is only skippable if NONE of its messages are wanted, so a
      // wanted announcement can never be buried by an unwanted reply.
      var wanted = [];
      for (var w = 0; w < messages.length; w++) {
        if (filter.test(messages[w].getSubject() || '')) wanted.push(messages[w]);
      }
      if (!wanted.length) {
        filtered += messages.length;
        threads[t].addLabel(skipLabel);   // kept in Gmail, never committed
        continue;
      }

      var threadOk = true;
      for (var m = 0; m < wanted.length; m++) {
        var msg = wanted[m];
        var path = repoPath(msg);
        try {
          if (commitMessage(token, repo, branch, path, msg)) committed++;
          else skipped++;              // already present: the run is idempotent
        } catch (err) {
          failed++;
          threadOk = false;
          console.error('failed ' + path + ': ' + err);
        }
      }
      // Label only a thread whose every message landed, so a partial failure
      // is retried next run instead of being silently dropped.
      if (threadOk) threads[t].addLabel(label);
    }
  }

  var remaining = GmailApp.search(pending, 0, 500).length;
  console.log('committed=' + committed + ' already_present=' + skipped +
              ' filtered_out=' + filtered + ' failed=' + failed +
              ' threads_remaining=' + remaining);
  console.log('Filter: /' + filter.source + '/i   (SUBJECT_FILTER property)');
  if (filtered) {
    console.log('Filtered messages are labelled "' + SKIPPED_LABEL + '" and ' +
                'still in Gmail. Widen SUBJECT_FILTER and run ' +
                'rescanSkipped() to reconsider them.');
  }
  if (timedOut) {
    console.warn('Stopped at the ' + (deadlineMs / 60000) + '-minute mark with ' +
                 remaining + ' thread(s) left. Run again to continue — nothing ' +
                 'is lost, labelled threads are skipped.');
  }
  if (failed) throw new Error(failed + ' message(s) failed; see the log.');
  return 'committed ' + committed + ', ' + remaining + ' thread(s) remaining';
}

/**
 * Commit PDF ATTACHMENTS — the only automated route to the documents.
 *
 * There is no compliant way to fetch these. asx.com.au is off limits to any
 * automated device by the access decision itself, and Market Index's terms
 * cannot be verified from the platform's network, so scraping their pages is
 * out under CLAUDE.md's rule about acting beyond a source's terms.
 *
 * What is left is a source that SENDS the document. Company investor-relations
 * mailing lists attach the PDF to the announcement email, so receiving it
 * involves no request to anyone: it is possession route 1 of the access
 * decision, with the fetching removed. Subscribe on the investor pages of the
 * companies you follow and the documents arrive on their own.
 *
 * Files land in captures/YYYY/MM/ and are ingested with:
 *     asx capture --capture-dir captures
 *
 * The announcement number is not in an attachment, so `asx capture` falls
 * back to matching on ticker and lodgement date. Naming the file with the
 * announcement number where you know it gives an exact match instead.
 */
function forwardAttachments() {
  var started = Date.now();
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('GITHUB_TOKEN');
  var repo = props.getProperty('GITHUB_REPO');
  if (!token || !repo) throw new Error('Run checkSetup() first.');
  var branch = props.getProperty('GITHUB_BRANCH') || 'main';
  var query = props.getProperty('ATTACHMENT_QUERY') || DEFAULT_ATTACHMENT_QUERY;

  var doneLabel = GmailApp.getUserLabelByName(ATTACHMENT_LABEL) ||
                  GmailApp.createLabel(ATTACHMENT_LABEL);
  var pending = query + ' -label:"' + ATTACHMENT_LABEL + '"';

  var committed = 0, skipped = 0, failed = 0;
  var threads = GmailApp.search(pending, 0, 25);

  for (var t = 0; t < threads.length; t++) {
    if (Date.now() - started > 5 * 60 * 1000) {
      console.warn('Stopped at five minutes; run again to continue.');
      break;
    }
    var messages = threads[t].getMessages();
    var threadOk = true;
    for (var m = 0; m < messages.length; m++) {
      var msg = messages[m];
      var atts = msg.getAttachments();
      for (var a = 0; a < atts.length; a++) {
        var att = atts[a];
        if (att.getContentType() !== 'application/pdf' &&
            !/\.pdf$/i.test(att.getName())) continue;
        var path = attachmentPath(msg, att);
        try {
          if (putFile(token, repo, branch, path, att.getBytes(),
                      'capture: ' + att.getName().slice(0, 80))) committed++;
          else skipped++;
        } catch (err) {
          failed++; threadOk = false;
          console.error('failed ' + path + ': ' + err);
        }
      }
    }
    if (threadOk) threads[t].addLabel(doneLabel);
  }
  console.log('attachments committed=' + committed + ' already_present=' +
              skipped + ' failed=' + failed);
  if (failed) throw new Error(failed + ' attachment(s) failed; see the log.');
  return 'committed ' + committed + ' attachment(s)';
}

/** captures/2026/08/20260819-AHL-2A1690463.pdf, or the sender's own name. */
function attachmentPath(msg, att) {
  var d = msg.getDate();
  var pad = function (n) { return (n < 10 ? '0' : '') + n; };
  var stamp = d.getUTCFullYear() + pad(d.getUTCMonth() + 1) + pad(d.getUTCDate());
  // Prefix with the message id so two companies sending "Appendix3Y.pdf" on
  // the same day cannot collide.
  var safe = att.getName().replace(/[^A-Za-z0-9._-]/g, '_').slice(-60);
  return 'captures/' + d.getUTCFullYear() + '/' + pad(d.getUTCMonth() + 1) +
         '/' + stamp + '-' + msg.getId() + '-' + safe;
}

/** alerts/2026/08/20260819T002103Z-<message-id-hash>.eml.gz */
function repoPath(msg) {
  var d = msg.getDate();
  var pad = function (n) { return (n < 10 ? '0' : '') + n; };
  var stamp = d.getUTCFullYear() + pad(d.getUTCMonth() + 1) + pad(d.getUTCDate()) +
              'T' + pad(d.getUTCHours()) + pad(d.getUTCMinutes()) +
              pad(d.getUTCSeconds()) + 'Z';
  // getId() is Gmail's own stable message id — unique, and it keeps two
  // alerts sent in the same second from overwriting each other.
  return 'alerts/' + d.getUTCFullYear() + '/' + pad(d.getUTCMonth() + 1) + '/' +
         stamp + '-' + msg.getId() + '.eml.gz';
}

/** Returns true if a new file was created, false if it already existed. */
function putFile(token, repo, branch, path, bytes, message) {
  var api = 'https://api.github.com/repos/' + repo + '/contents/' + path;
  var headers = {
    Authorization: 'Bearer ' + token,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28'
  };
  var probe = UrlFetchApp.fetch(api + '?ref=' + encodeURIComponent(branch),
                                {headers: headers, muteHttpExceptions: true});
  if (probe.getResponseCode() === 200) return false;
  if (probe.getResponseCode() !== 404) {
    throw new Error('probe ' + probe.getResponseCode() + ': ' +
                    probe.getContentText().slice(0, 200));
  }
  var put = UrlFetchApp.fetch(api, {
    method: 'put', headers: headers, contentType: 'application/json',
    muteHttpExceptions: true,
    payload: JSON.stringify({message: message,
                             content: Utilities.base64Encode(bytes),
                             branch: branch})});
  if (put.getResponseCode() >= 300) {
    throw new Error('put ' + put.getResponseCode() + ': ' +
                    put.getContentText().slice(0, 200));
  }
  return true;
}

/** Returns true if a new file was created, false if it already existed. */
function commitMessage(token, repo, branch, path, msg) {
  var api = 'https://api.github.com/repos/' + repo + '/contents/' + path;
  var headers = {
    Authorization: 'Bearer ' + token,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28'
  };

  var probe = UrlFetchApp.fetch(api + '?ref=' + encodeURIComponent(branch), {
    headers: headers, muteHttpExceptions: true
  });
  if (probe.getResponseCode() === 200) return false;   // already committed
  if (probe.getResponseCode() !== 404) {
    throw new Error('probe ' + probe.getResponseCode() + ': ' +
                    probe.getContentText().slice(0, 200));
  }

  // getRawContent() is the full RFC822 message, so the repository holds
  // exactly what the publisher sent — headers, both MIME parts, everything.
  var raw = Utilities.newBlob(msg.getRawContent(), 'message/rfc822');
  var gz = Utilities.gzip(raw);

  var put = UrlFetchApp.fetch(api, {
    method: 'put', headers: headers, contentType: 'application/json',
    muteHttpExceptions: true,
    payload: JSON.stringify({
      message: 'alert: ' + msg.getSubject().slice(0, 100),
      content: Utilities.base64Encode(gz.getBytes()),
      branch: branch
    })
  });
  if (put.getResponseCode() >= 300) {
    throw new Error('put ' + put.getResponseCode() + ': ' +
                    put.getContentText().slice(0, 200));
  }
  return true;
}
