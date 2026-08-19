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
 *   3. Run forwardAlerts() once by hand and approve the permission prompt.
 *   4. Triggers -> Add Trigger -> forwardAlerts, time-driven, hourly.
 */

var PROCESSED_LABEL = 'asx-ingested';
var DEFAULT_QUERY = 'from:marketindex.com.au newer_than:7d';

function forwardAlerts() {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('GITHUB_TOKEN');
  var repo = props.getProperty('GITHUB_REPO');
  if (!token || !repo) {
    throw new Error('Set GITHUB_TOKEN and GITHUB_REPO in Script Properties.');
  }
  var branch = props.getProperty('GITHUB_BRANCH') || 'main';
  var query = props.getProperty('GMAIL_QUERY') || DEFAULT_QUERY;

  var label = GmailApp.getUserLabelByName(PROCESSED_LABEL) ||
              GmailApp.createLabel(PROCESSED_LABEL);

  // Exclude what we have already committed. Deliberately NOT "is:unread":
  // reading an alert must not hide it from the platform.
  var threads = GmailApp.search(query + ' -label:' + PROCESSED_LABEL, 0, 100);
  var committed = 0, skipped = 0, failed = 0;

  for (var t = 0; t < threads.length; t++) {
    var messages = threads[t].getMessages();
    var threadOk = true;
    for (var m = 0; m < messages.length; m++) {
      var msg = messages[m];
      var path = repoPath(msg);
      try {
        if (commitMessage(token, repo, branch, path, msg)) {
          committed++;
        } else {
          skipped++;   // already present: the run is idempotent
        }
      } catch (err) {
        failed++;
        threadOk = false;
        console.error('failed ' + path + ': ' + err);
      }
    }
    // Label only a thread whose every message landed, so a partial failure
    // is retried next hour instead of being silently dropped.
    if (threadOk) threads[t].addLabel(label);
  }
  console.log('committed=' + committed + ' already_present=' + skipped +
              ' failed=' + failed);
  if (failed) throw new Error(failed + ' message(s) failed; see the log.');
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
