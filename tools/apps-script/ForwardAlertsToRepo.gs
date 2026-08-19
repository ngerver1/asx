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
 *   4. Run forwardAlerts() once by hand.
 *   5. Triggers -> Add Trigger -> forwardAlerts, time-driven, hourly.
 */

var PROCESSED_LABEL = 'asx-ingested';
var DEFAULT_QUERY = 'from:marketindex.com.au newer_than:7d';

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
  var threads = GmailApp.search(query + ' -label:"' + PROCESSED_LABEL + '"', 0, 100);
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
