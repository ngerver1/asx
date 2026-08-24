"""The published director screen, generated from the database.

Until now this page was maintained by hand, which put it in conflict with the
prime directive that derived data must be regenerable: the numbers on it could
drift from the tables they came from and nothing would notice. It is generated
here so that republishing is `asx screen-html` and never a careful edit.

The design is deliberately unchanged from the published page. What is new is
the price column, and the rules it follows are in `director_signals`: the
quote carries its own as-at per row, a missing quote is a flagged blank rather
than an empty cell, and the source is named on the page.
"""

from __future__ import annotations

import json
from datetime import date

import psycopg

from asx.ingest.quote_source import latest_quotes
from asx.signals.director_signals import HOLDINGS_LATERAL, _holding_flags

TEMPLATE = """<title>ASX Director Screens</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,600;1,6..72,400&family=Archivo:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --paper:#f7f8f7; --surface:#ffffff; --ink:#161b1c; --muted:#5d6a6b;
  --rule:#dee4e2; --rule-strong:#c6cfcc;
  --accent:#0d5c63; --accent-soft:#0d5c631a; --accent-line:#0d5c6340;
  --caution:#8a5a12; --caution-soft:#8a5a1216;
  --ok:#1f6b3d; --ok-soft:#1f6b3d16;
  --up:#1f6b3d; --down:#a3341f;
  --shadow:0 1px 2px #161b1c0d, 0 8px 24px -16px #161b1c26;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --paper:#0f1415; --surface:#161d1e; --ink:#e7edea; --muted:#93a3a2;
    --rule:#26312f; --rule-strong:#36423f;
    --accent:#4fb3b8; --accent-soft:#4fb3b81f; --accent-line:#4fb3b84d;
    --caution:#d99b3f; --caution-soft:#d99b3f1c;
    --ok:#5fbe84; --ok-soft:#5fbe841c;
    --up:#5fbe84; --down:#e07a5f;
    --shadow:0 1px 2px #0006, 0 10px 28px -18px #000a;
  }
}
:root[data-theme="dark"]{
  --paper:#0f1415; --surface:#161d1e; --ink:#e7edea; --muted:#93a3a2;
  --rule:#26312f; --rule-strong:#36423f;
  --accent:#4fb3b8; --accent-soft:#4fb3b81f; --accent-line:#4fb3b84d;
  --caution:#d99b3f; --caution-soft:#d99b3f1c;
  --ok:#5fbe84; --ok-soft:#5fbe841c;
  --up:#5fbe84; --down:#e07a5f;
  --shadow:0 1px 2px #0006, 0 10px 28px -18px #000a;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:Archivo,"Helvetica Neue",Arial,sans-serif;
  font-size:15px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1240px; margin:0 auto; padding:40px 24px 72px; display:flex; flex-direction:column; gap:38px}
h1,h2{font-family:Newsreader,Georgia,"Times New Roman",serif; font-weight:600; text-wrap:balance; margin:0}
h1{font-size:2.35rem; line-height:1.12; letter-spacing:-.012em}
h2{font-size:1.4rem; line-height:1.2}
.lede{color:var(--muted); max-width:63ch; margin:0}
.eyebrow{
  font-size:.72rem; font-weight:600; letter-spacing:.13em; text-transform:uppercase;
  color:var(--accent); margin:0 0 10px;
}
header .stamp{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-size:.76rem;
  color:var(--muted); margin-top:14px; display:flex; flex-wrap:wrap; gap:6px 18px;
}
.tiles{display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:1px;
  background:var(--rule); border:1px solid var(--rule); border-radius:3px; overflow:hidden}
.tile{background:var(--surface); padding:16px 18px}
.tile .n{font-family:"IBM Plex Mono",monospace; font-size:1.75rem; font-weight:500;
  font-variant-numeric:tabular-nums; letter-spacing:-.02em; display:block}
.tile .k{font-size:.74rem; color:var(--muted); letter-spacing:.05em; text-transform:uppercase; margin-top:2px; display:block}
section{display:flex; flex-direction:column; gap:16px}
.shead{display:flex; flex-wrap:wrap; align-items:baseline; justify-content:space-between; gap:12px}
.note{
  border-left:2px solid var(--caution); background:var(--caution-soft);
  padding:11px 14px; font-size:.86rem; color:var(--ink); border-radius:0 3px 3px 0;
}
.note b{color:var(--caution)}
.controls{display:flex; gap:8px; align-items:center; flex-wrap:wrap; font-size:.82rem}
.controls label{display:inline-flex; gap:7px; align-items:center; color:var(--muted); cursor:pointer}
.controls input{accent-color:var(--accent); width:15px; height:15px}
.scroll{overflow-x:auto; border:1px solid var(--rule); border-radius:3px; background:var(--surface); box-shadow:var(--shadow)}
table{border-collapse:collapse; width:100%; min-width:900px}
th,td{text-align:left; padding:10px 14px; border-bottom:1px solid var(--rule); vertical-align:middle}
thead th{
  position:sticky; top:0; background:var(--surface); z-index:1;
  font-size:.7rem; letter-spacing:.09em; text-transform:uppercase; color:var(--muted);
  font-weight:600; white-space:nowrap; border-bottom:1px solid var(--rule-strong);
}
thead th.sortable{cursor:pointer; user-select:none}
thead th.sortable:hover{color:var(--ink)}
thead th .car{opacity:.32; font-size:.85em}
thead th[aria-sort] .car{opacity:1; color:var(--accent)}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--accent-soft)}
.num{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap}
.tick{font-family:"IBM Plex Mono",monospace; font-weight:600; letter-spacing:.02em}
.tick a{color:var(--ink); text-decoration:none; border-bottom:1px solid var(--accent-line)}
.tick a:hover{border-bottom-color:var(--accent)}
.co{display:block; font-size:.76rem; color:var(--muted); font-weight:400; letter-spacing:0;
  font-family:Archivo,sans-serif; max-width:26ch; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.who{font-size:.88rem}
/* price cell: the number, then the date it was struck, then the move */
.px{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap}
.px .asat{display:block; font-size:.68rem; color:var(--muted); font-weight:400; letter-spacing:.02em}
.px .asat.stale{color:var(--caution)}
.move{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums;
  text-align:right; white-space:nowrap; font-weight:600; font-size:.9rem}
.move.up{color:var(--up)} .move.down{color:var(--down)} .move.flat{color:var(--muted)}
.mag{display:flex; align-items:center; gap:10px; min-width:190px}
.mag .val{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums;
  font-weight:600; font-size:.9rem; width:60px; text-align:right; color:var(--accent)}
.track{position:relative; flex:1; height:9px; background:var(--rule); border-radius:1px; overflow:hidden}
.fill{position:absolute; inset:0 auto 0 0; background:var(--accent); border-radius:1px}
.dbl{position:absolute; top:-3px; bottom:-3px; width:1px; background:var(--rule-strong)}
.legend-tick{font-size:.7rem; color:var(--muted); font-family:"IBM Plex Mono",monospace}
.chips{display:flex; gap:5px; flex-wrap:wrap}
.chip{
  font-size:.68rem; letter-spacing:.04em; padding:2px 7px; border-radius:2px;
  border:1px solid var(--rule-strong); color:var(--muted); white-space:nowrap;
}
.chip.caution{color:var(--caution); border-color:var(--caution); background:var(--caution-soft)}
.chip.ok{color:var(--ok); border-color:var(--ok); background:var(--ok-soft)}
tr.dim td{opacity:.5}
footer{border-top:1px solid var(--rule); padding-top:18px; color:var(--muted); font-size:.82rem; max-width:70ch}
footer code{font-family:"IBM Plex Mono",monospace; font-size:.92em; color:var(--ink)}
a:focus-visible,th:focus-visible,input:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important; animation:none!important}}
@media (max-width:640px){ .wrap{padding:28px 16px 56px} h1{font-size:1.85rem} }
</style>
<div class="wrap">

<header>
  <p class="eyebrow">Appendix 3Y &middot; on-market cash buys</p>
  <h1>Where ASX directors put their own money</h1>
  <p class="lede">Two screens over the same __TRADES__ director transactions, parsed from __NOTICES__ lodged
  notices. Both count only on-market purchases for cash &mdash; not options vesting, not rights
  issues, not transfers. Each row is actionable no earlier than the date its notice became public.</p>
  <div class="stamp">
    <span>Built __BUILT__</span>
    <span>Signal definition v2</span>
    <span>__DOCS__ documents held</span>
    <span>Prices as at __PRICE_RANGE__</span>
  </div>
</header>

<div class="tiles">
  <div class="tile"><span class="n" id="t-conv">&mdash;</span><span class="k">Conviction buys</span></div>
  <div class="tile"><span class="n" id="t-clus">&mdash;</span><span class="k">Cluster buys</span></div>
  <div class="tile"><span class="n" id="t-spend">&mdash;</span><span class="k">Largest single buy</span></div>
  <div class="tile"><span class="n" id="t-max">&mdash;</span><span class="k">Largest stake rise</span></div>
  <div class="tile"><span class="n" id="t-new">&mdash;</span><span class="k">New this week</span></div>
</div>

<section id="new-sec">
  <div class="shead">
    <div>
      <h2>New since __NEW_CUTOFF__</h2>
      <p class="lede">Rows that entered a screen in the last seven days, so a returning reader
      can see what changed without re-reading both tables. Every row here also appears in its
      own screen below &mdash; this is a view, not a third screen.</p>
    </div>
  </div>
  <div id="new-empty" class="note" hidden></div>
  <div class="scroll" id="new-wrap">
    <table id="new">
      <thead><tr>
        <th class="sortable" data-k="added">Added <span class="car">&#9662;</span></th>
        <th class="sortable" data-k="kind">Screen <span class="car">&#9662;</span></th>
        <th class="sortable" data-k="ticker">Code <span class="car">&#9662;</span></th>
        <th class="sortable" data-k="who">Who <span class="car">&#9662;</span></th>
        <th>Why it qualified</th>
        <th class="sortable num" data-k="spend" data-num>Total A$ <span class="car">&#9662;</span></th>
        <th class="sortable" data-k="actionable">Actionable <span class="car">&#9662;</span></th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
</section>

<section>
  <div class="shead">
    <div>
      <h2>Conviction sizing</h2>
      <p class="lede">One director raising their own stake sharply. Ranked by how much they
      increased their holding &mdash; not by cheque size, because $500,000 against 83&nbsp;million
      shares moves nobody&rsquo;s exposure.</p>
    </div>
    <div class="controls">
      <label><input type="checkbox" id="hide-small"> Hide small spends</label>
    </div>
  </div>
  <div class="note">
    <b>Size check unavailable on __UNCHECKED__ of __CONV_N__.</b> The ASX&nbsp;300 exclusion needs a membership
    snapshot dated on or before the day a signal became public, and ours starts 20 Aug 2026.
    Only <b>__CHECKED_TICKERS__</b> was actually size-checked. The rest are not confirmed small &mdash; they are unchecked.
  </div>
  <div class="scroll">
    <table id="conv">
      <thead><tr>
        <th class="sortable" data-k="ticker">Code <span class="car">&#9662;</span></th>
        <th class="sortable" data-k="director">Director <span class="car">&#9662;</span></th>
        <th class="sortable" data-k="pct" data-num data-desc>Stake increase <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="paid" data-num>Paid /share <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="price" data-num>Price as at <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="vsPaid" data-num>vs paid <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="spend" data-num>Total A$ <span class="car">&#9662;</span></th>
        <th class="sortable" data-k="actionable">Actionable <span class="car">&#9662;</span></th>
        <th>Coverage</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <p class="legend-tick">Bar scaled to the largest rise; the hairline marks 100% &mdash; a director doubling their holding.</p>
</section>

<section>
  <div class="shead">
    <div>
      <h2>Cluster buys</h2>
      <p class="lede">Two or more directors of the same company buying on-market within 30 days.
      The information is the coordination, so the bar stays at two &mdash; a single buyer is the
      screen above, not a smaller cluster.</p>
    </div>
  </div>
  <div class="scroll">
    <table id="clus">
      <thead><tr>
        <th class="sortable" data-k="ticker">Code <span class="car">&#9662;</span></th>
        <th class="sortable" data-k="directors">Directors <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="spend" data-num data-desc>Total paid A$ <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="paid" data-num>Paid /share <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="price" data-num>Price as at <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="vsPaid" data-num>vs paid <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="shares" data-num>Shares bought <span class="car">&#9662;</span></th>
        <th class="sortable num" data-k="held" data-num>Held after <span class="car">&#9662;</span></th>
        <th class="sortable" data-k="actionable">Actionable <span class="car">&#9662;</span></th>
        <th>Coverage</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
</section>

<footer>
  <p><b>How to read these.</b> Every director figure comes from a lodged Appendix 3Y and nothing is
  inferred: where a form states no date or no holdings, the notice goes to a human rather than
  onto this page. &ldquo;Actionable&rdquo; is the lodgement date, never the trade date &mdash; the
  signal did not exist until the market could see it.</p>
  <p><b>The price column is the one number here that is not from a lodgement.</b> It is a delayed
  quote from <a href="https://stockanalysis.com/" target="_blank" rel="noopener">stockanalysis.com</a>,
  retrieved __RETRIEVED__, and it carries <b>its own as-at date on every row</b> rather than one date in
  this sentence. That matters more than it sounds: these are sub-index companies, and a thin
  explorer&rsquo;s last trade can be days older than a large cap&rsquo;s &mdash; __STALE_NOTE__
  <b>vs paid</b> compares it against what the director actually paid, which is the only comparison
  the page is making.</p>
  <p><b>&ldquo;Held after&rdquo; is the directors&rsquo; combined position, not a sum of the
  notices.</b> A director can lodge twice inside the 30&#8209;day window, and the two notices
  report two states of one holding &mdash; adding the rows up would overstate the board&rsquo;s
  stake (on the CBE cluster, by 31%). Each director&rsquo;s most recent notice in the window is
  taken first, then those are added. Where one director holds more than one class of security the
  row says so, because shares and options are not one holding.</p>
  <p><b>What these are not.</b> Not advice, and not a tested edge. A quote good enough to read on a
  screen is not good enough to backtest on: the source cannot price companies that have since
  delisted, and a study run over the survivors flatters every result. So backtesting remains out of
  scope and nothing here has been measured against what happened next. Regenerate with
  <code>asx build-signals</code>, <code>asx fetch-quotes</code>, then <code>asx screen-html</code>.</p>
</footer>

</div>

<script>
const DATA = __DATA__;

const fmt = (n, d=0) => n.toLocaleString('en-AU', {minimumFractionDigits:d, maximumFractionDigits:d});
const price = p => (p === null || p === undefined) ? '&mdash;' : '$' + (p >= 1 ? p.toFixed(2) : p.toFixed(4));
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const shortDate = s => { const [y,m,dd] = s.split('-'); return dd + ' ' + MON[+m-1]; };

const LATEST = DATA.latestQuoteDate;

// The price cell says WHEN as loudly as it says WHAT. A quote struck before the
// most recent trading day we hold is marked, because on this universe that is
// common and a reader comparing two rows needs to see it.
function priceCell(r){
  if (r.price === null || r.price === undefined)
    return '<td class="px">&mdash;<span class="asat">not priced</span></td>';
  const stale = r.priceAsAt < LATEST ? ' stale' : '';
  return `<td class="px">${price(r.price)}<span class="asat${stale}">${shortDate(r.priceAsAt)}</span></td>`;
}

function moveCell(r){
  if (r.vsPaid === null || r.vsPaid === undefined)
    return '<td class="move flat">&mdash;</td>';
  const cls = r.vsPaid > 0 ? 'up' : (r.vsPaid < 0 ? 'down' : 'flat');
  const sign = r.vsPaid > 0 ? '+' : '';
  return `<td class="move ${cls}">${sign}${r.vsPaid.toFixed(1)}%</td>`;
}

const maxPct = Math.max(...DATA.conviction.map(r => r.pct));
document.getElementById('t-conv').textContent = DATA.conviction.length;
document.getElementById('t-clus').textContent = DATA.cluster.length;
document.getElementById('t-spend').textContent = '$' +
  fmt(Math.max(...DATA.conviction.map(r => r.spend))/1000) + 'k';
document.getElementById('t-max').textContent = fmt(maxPct) + '%';
document.getElementById('t-new').textContent = DATA.new.length;

// The empty state says WHEN nothing changed since, not just "nothing". A bare
// "no new rows" is indistinguishable from a build that failed to run.
function newRow(r){
  const label = r.kind === 'conviction' ? 'Conviction' : 'Cluster';
  return `<tr>
    <td class="num">${shortDate(r.added)}</td>
    <td><span class="chip">${label}</span></td>
    <td class="tick">${r.ticker}<span class="co">${r.entity}</span></td>
    <td class="who">${r.who}</td>
    <td class="who">${r.detail}</td>
    <td class="num">${r.spend === null ? '&mdash;' : '$' + fmt(r.spend)}</td>
    <td class="num">${shortDate(r.actionable)}</td>
  </tr>`;
}
if (!DATA.new.length) {
  document.getElementById('new-wrap').hidden = true;
  const box = document.getElementById('new-empty');
  box.hidden = false;
  box.innerHTML = DATA.lastAddition
    ? `<b>Nothing new in the last seven days.</b> The most recent row to enter either
       screen did so on ${shortDate(DATA.lastAddition)}; both screens below are unchanged
       since then.`
    : `<b>Nothing new in the last seven days.</b> ${DATA.untracked} row(s) were already on
       the screens when arrival tracking began, so their arrival dates are unknown and they
       are not reported here. Anything that enters from the next build onward will appear.`;
}

function coverage(flags){
  const out = [];
  // membership_unknown sits on nearly every row; the exception is the information,
  // so it is the confirmed case that gets a chip.
  if (!flags.includes('membership_unknown')) out.push(['ok','size checked']);
  if (flags.includes('small_absolute_spend')) out.push(['caution','small spend']);
  if (flags.includes('consideration_not_stated')) out.push(['caution','no price stated']);
  if (flags.includes('price_unavailable')) out.push(['caution','no quote']);
  if (flags.includes('partial_price_coverage')) out.push(['caution','part-priced']);
  if (flags.includes('held_partial')) out.push(['caution','holding is a floor']);
  if (flags.includes('held_mixed_classes')) out.push(['caution','holding spans classes']);
  return out.map(([c,t]) => `<span class="chip ${c}">${t}</span>`).join('') ||
         '<span class="chip">&mdash;</span>';
}

function convRow(r){
  const w = Math.max(1.5, r.pct / maxPct * 100);
  const dbl = 100 / maxPct * 100;
  return `<tr class="${r.flags.includes('small_absolute_spend') ? 'dim' : ''}">
    <td class="tick"><a href="https://www.marketindex.com.au/asx/${r.ticker.toLowerCase()}"
        target="_blank" rel="noopener">${r.ticker}</a>
      <span class="co">${r.entity}</span></td>
    <td class="who">${r.director}</td>
    <td><div class="mag"><span class="val">${fmt(r.pct,0)}%</span>
      <span class="track"><span class="fill" style="width:${w}%"></span>
      <span class="dbl" style="left:${dbl}%"></span></span></div></td>
    <td class="num">${price(r.paid)}</td>
    ${priceCell(r)}
    ${moveCell(r)}
    <td class="num">${fmt(r.spend)}</td>
    <td class="num">${shortDate(r.actionable)}</td>
    <td><div class="chips">${coverage(r.flags)}</div></td></tr>`;
}

// What the participating directors hold between them once the buying is done,
// with its value at the current quote underneath. Resolved per director before
// being summed — see HOLDINGS_LATERAL for why a plain sum overstates it.
function heldCell(r){
  if (r.held === null || r.held === undefined)
    return '<td class="px">&mdash;</td>';
  const val = (r.heldValue === null || r.heldValue === undefined) ? ''
    : `<span class="asat">$${fmt(r.heldValue)}</span>`;
  return `<td class="px">${fmt(r.held)}${val}</td>`;
}

function clusRow(r){
  return `<tr>
    <td class="tick"><a href="https://www.marketindex.com.au/asx/${r.ticker.toLowerCase()}"
        target="_blank" rel="noopener">${r.ticker}</a>
      <span class="co">${r.entity}</span></td>
    <td class="who">${r.directors}</td>
    <td class="num">${fmt(r.spend)}</td>
    <td class="num">${price(r.paid)}</td>
    ${priceCell(r)}
    ${moveCell(r)}
    <td class="num">${fmt(r.shares)}</td>
    ${heldCell(r)}
    <td class="num">${shortDate(r.actionable)}</td>
    <td><div class="chips">${coverage(r.flags)}</div></td></tr>`;
}

const state = {conv:{k:'pct',dir:-1}, clus:{k:'spend',dir:-1},
               new:{k:'added',dir:-1}};
const SOURCE = {conv:'conviction', clus:'cluster', new:'new'};
const ROW = {conv:convRow, clus:clusRow, new:newRow};

function render(id){
  const isConv = id === 'conv';
  const st = state[id];
  let rows = DATA[SOURCE[id]].slice();
  if (isConv && document.getElementById('hide-small').checked)
    rows = rows.filter(r => !r.flags.includes('small_absolute_spend'));
  rows.sort((a,b) => {
    const x = a[st.k], y = b[st.k];
    // A row with no quote sorts last whichever way the column is pointed: it is
    // absent, not zero, and letting it read as the cheapest or the worst
    // performer would be the screen inventing a number.
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    const c = (typeof x === 'number') ? x - y : String(x).localeCompare(String(y));
    return c * st.dir;
  });
  document.querySelector('#' + id + ' tbody').innerHTML =
    rows.map(ROW[id]).join('');
  document.querySelectorAll('#' + id + ' thead th').forEach(th => {
    if (th.dataset.k === st.k) th.setAttribute('aria-sort', st.dir === 1 ? 'ascending' : 'descending');
    else th.removeAttribute('aria-sort');
  });
}

document.querySelectorAll('table').forEach(tbl => {
  tbl.querySelectorAll('th.sortable').forEach(th => {
    th.tabIndex = 0;
    const go = () => {
      const st = state[tbl.id];
      if (st.k === th.dataset.k) st.dir *= -1;
      else { st.k = th.dataset.k; st.dir = th.hasAttribute('data-num') ? -1 : 1; }
      render(tbl.id);
    };
    th.addEventListener('click', go);
    th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  });
});
document.getElementById('hide-small').addEventListener('change', () => render('conv'));
render('conv'); render('clus');
if (DATA.new.length) render('new');
</script>
"""


def _f(value) -> float | None:
    return None if value is None else float(value)


def _quote_fields(quote: dict | None, paid: float | None) -> dict:
    """Price, its as-at, and the move against what the director paid.

    `None` throughout when there is no usable quote — never 0, never a
    carried-forward older price. The page renders `None` as an em dash and
    sorts it last, so an absent price cannot read as a cheap one.
    """
    if not quote or quote["status"] != "ok" or quote["price"] is None:
        return {"price": None, "priceAsAt": None, "vsPaid": None}
    price = float(quote["price"])
    vs = None
    if paid:
        vs = round((price / paid - 1) * 100, 1)
    return {"price": price,
            "priceAsAt": quote["as_at_date"].isoformat(),
            "vsPaid": vs}


def build_data(conn: psycopg.Connection) -> dict:
    quotes = latest_quotes(conn)
    conviction, cluster = [], []

    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.ticker, n.name AS entity, s.entity_id, s.person_name_raw,
                      s.event_date, s.knowable_at, s.consideration_aud,
                      s.qty_acquired, s.held_before, s.stake_increase,
                      t.price_per_unit, s.coverage_flags,
                      fs.first_seen_at, fs.backfilled
                 FROM signal_conviction_buys s
                 JOIN director_trades t ON t.trade_id = s.trade_id
                 LEFT JOIN signal_first_seen fs
                        ON fs.signal_version = s.signal_version
                       AND fs.kind = 'conviction'
                       AND fs.natural_key = s.trade_id::text
                 LEFT JOIN listings l
                        ON l.entity_id = s.entity_id AND l.valid_to IS NULL
                 LEFT JOIN entity_names n
                        ON n.entity_id = s.entity_id AND n.valid_to IS NULL
                ORDER BY s.stake_increase DESC""")
        for r in cur.fetchall():
            paid = (round(float(r["price_per_unit"]), 4)
                    if r["price_per_unit"] is not None else None)
            q = _quote_fields(quotes.get(r["entity_id"]), paid)
            flags = list(r["coverage_flags"] or [])
            if q["price"] is None:
                flags.append("price_unavailable")
            conviction.append({
                "ticker": r["ticker"] or "", "entity": r["entity"] or "",
                "director": r["person_name_raw"],
                "event": r["event_date"].isoformat(),
                "actionable": r["knowable_at"].date().isoformat(),
                "spend": _f(r["consideration_aud"]),
                "paid": paid,
                "qty": _f(r["qty_acquired"]), "held": _f(r["held_before"]),
                "pct": round(float(r["stake_increase"]) * 100, 1),
                "added": (r["first_seen_at"].date().isoformat()
                          if r["first_seen_at"] and not r["backfilled"] else None),
                "flags": flags, **q})

        cur.execute(
            """SELECT l.ticker, n.name AS entity, s.entity_id,
                      s.window_start, s.window_end, s.n_directors,
                      s.total_consideration_aud, s.knowable_at,
                      s.coverage_flags,
                      p.shares, p.priced_consideration, p.n_trades, p.n_priced,
                      hold.total_held, hold.n_holdings, hold.n_held_missing,
                      hold.n_holders,
                      (SELECT string_agg(DISTINCT t.person_name_raw, '; ')
                         FROM director_trades t
                        WHERE t.trade_id = ANY(s.trade_ids)) AS directors,
                      fs.first_seen_at, fs.backfilled
                 FROM signal_cluster_buys s
                 CROSS JOIN LATERAL (""" + HOLDINGS_LATERAL + """) AS hold
                 CROSS JOIN LATERAL (
                   SELECT count(*) AS n_trades,
                          count(*) FILTER (WHERE t.qty_acquired IS NOT NULL
                                             AND t.consideration_aud IS NOT NULL)
                            AS n_priced,
                          sum(t.qty_acquired) FILTER (
                            WHERE t.qty_acquired IS NOT NULL
                              AND t.consideration_aud IS NOT NULL) AS shares,
                          sum(t.consideration_aud) FILTER (
                            WHERE t.qty_acquired IS NOT NULL
                              AND t.consideration_aud IS NOT NULL)
                            AS priced_consideration
                     FROM director_trades t
                    WHERE t.trade_id = ANY(s.trade_ids)
                 ) AS p
                 LEFT JOIN listings l
                        ON l.entity_id = s.entity_id AND l.valid_to IS NULL
                 LEFT JOIN entity_names n
                        ON n.entity_id = s.entity_id AND n.valid_to IS NULL
                 LEFT JOIN signal_first_seen fs
                        ON fs.signal_version = s.signal_version
                       AND fs.kind = 'cluster'
                       AND fs.natural_key = s.entity_id || ':' || s.window_start
                ORDER BY s.knowable_at DESC, s.total_consideration_aud DESC""")
        for r in cur.fetchall():
            shares, spend = r["shares"], r["priced_consideration"]
            paid = round(float(spend) / float(shares), 4) if shares else None
            q = _quote_fields(quotes.get(r["entity_id"]), paid)
            flags = list(r["coverage_flags"] or [])
            if r["n_priced"] < r["n_trades"]:
                flags.append("partial_price_coverage")
            if q["price"] is None:
                flags.append("price_unavailable")
            flags.extend(_holding_flags(r))
            held = _f(r["total_held"])
            # The board's whole position valued at the current quote. Needs
            # both, so it is absent rather than approximated when either is.
            held_value = (round(held * q["price"], 2)
                          if held is not None and q["price"] is not None
                          else None)
            cluster.append({
                "held": held, "heldValue": held_value,
                "ticker": r["ticker"] or "", "entity": r["entity"] or "",
                "directors": r["directors"] or "", "n": r["n_directors"],
                "first": r["window_start"].isoformat(),
                "last": r["window_end"].isoformat(),
                "actionable": r["knowable_at"].date().isoformat(),
                "spend": _f(r["total_consideration_aud"]),
                "shares": _f(shares), "paid": paid,
                "added": (r["first_seen_at"].date().isoformat()
                          if r["first_seen_at"] and not r["backfilled"] else None),
                "flags": flags, **q})

    dates = [r["priceAsAt"] for r in conviction + cluster if r["priceAsAt"]]
    return {"conviction": conviction, "cluster": cluster,
            "latestQuoteDate": max(dates) if dates else "",
            **_new_additions(conviction, cluster)}


# How far back "new" reaches. A fixed window rather than "since the previous
# build", because builds are not evenly spaced: three rebuilds in one
# afternoon would leave two of them reporting nothing new and hide a genuine
# addition from anyone who looked between them. Seven days covers a weekly
# read, and the exact date is printed on the page so the reader never has to
# infer what the window was.
NEW_WINDOW_DAYS = 7


def _new_additions(conviction: list[dict], cluster: list[dict]) -> dict:
    """Rows that entered a screen within the window, newest first.

    A row with no `added` date is one that predates signal_first_seen; it is
    treated as old, never as new. Guessing the other way would announce the
    whole screen once and teach the reader to distrust the table.
    """
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=NEW_WINDOW_DAYS)).isoformat()
    rows = []
    for kind, src in (("conviction", conviction), ("cluster", cluster)):
        for r in src:
            if r["added"] and r["added"] > cutoff:
                rows.append({
                    "kind": kind, "ticker": r["ticker"], "entity": r["entity"],
                    "who": r.get("director") or r.get("directors") or "",
                    "spend": r["spend"], "actionable": r["actionable"],
                    "added": r["added"],
                    "detail": (f"{r['pct']}% stake increase" if kind == "conviction"
                               else f"{r['n']} directors"),
                })
    rows.sort(key=lambda r: (r["added"], r["ticker"]), reverse=True)
    seen = sorted({r["added"] for r in conviction + cluster if r["added"]})
    untracked = sum(1 for r in conviction + cluster if not r["added"])
    return {"new": rows, "newCutoff": cutoff,
            "lastAddition": seen[-1] if seen else "",
            "untracked": untracked}


def render(conn: psycopg.Connection, built_on: date | None = None) -> str:
    data = build_data(conn)
    rows = data["conviction"] + data["cluster"]
    priced = [r for r in rows if r["priceAsAt"]]
    dates = sorted({r["priceAsAt"] for r in priced})

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM documents")
        docs = cur.fetchone()["n"]
        # All canonical trades, superseded ones included: the count describes
        # the corpus the screens were derived from, and an amended notice did
        # not stop being a parsed transaction.
        cur.execute("SELECT count(*) AS n FROM director_trades")
        trades = cur.fetchone()["n"]
        # `asx_doc_types` is the exchange's own labelling and is unpopulated on
        # this corpus; `doc_class` is what the parser assigns and is the field
        # with the answer in it.
        cur.execute("SELECT count(*) AS n FROM documents "
                    "WHERE doc_class IN ('app_3y', 'app_3z')")
        notices = cur.fetchone()["n"]
        cur.execute("SELECT max(retrieved_at)::date AS d FROM price_quotes "
                    "WHERE status = 'ok'")
        retrieved = cur.fetchone()["d"]

    def pretty(iso: str) -> str:
        y, m, d = iso.split("-")
        return f"{int(d)} {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(m)-1]} {y}"

    # The header says a RANGE when the quotes disagree about their date, which
    # on this universe they usually do. Printing only the newest would let the
    # freshest row speak for the stalest.
    if not dates:
        price_range = "&mdash; (no quotes held)"
    elif len(dates) == 1:
        price_range = pretty(dates[0])
    else:
        price_range = f"{pretty(dates[0])} &ndash; {pretty(dates[-1])}"

    stale = [r for r in priced if r["priceAsAt"] < data["latestQuoteDate"]]
    if stale:
        codes = ", ".join(sorted({r["ticker"] for r in stale}))
        stale_note = (f"on this build {len(stale)} row(s) carry an older quote "
                      f"({codes}), marked in amber.")
    else:
        stale_note = "on this build every quote is from the same trading day."

    unchecked = [r for r in data["conviction"]
                 if "membership_unknown" in r["flags"]]
    checked = sorted({r["ticker"] for r in data["conviction"]
                      if "membership_unknown" not in r["flags"]})

    html = TEMPLATE
    for token, value in [
        ("__DATA__", json.dumps(data)),
        ("__BUILT__", pretty((built_on or date.today()).isoformat())),
        ("__DOCS__", f"{docs:,}"),
        ("__TRADES__", f"{trades:,}"),
        ("__NOTICES__", f"{notices:,}"),
        ("__PRICE_RANGE__", price_range),
        ("__RETRIEVED__", pretty(retrieved.isoformat()) if retrieved else "&mdash;"),
        ("__STALE_NOTE__", stale_note),
        ("__UNCHECKED__", str(len(unchecked))),
        ("__CONV_N__", str(len(data["conviction"]))),
        ("__CHECKED_TICKERS__", ", ".join(checked) if checked else "none"),
        ("__NEW_CUTOFF__", pretty(data["newCutoff"])),
    ]:
        html = html.replace(token, value)
    return html
