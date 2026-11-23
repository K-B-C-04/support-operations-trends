/**
 * Support Operations Trends — Google Apps Script pipeline
 *
 *  Raw ─► Clean ─► Tag (keyword rules on title + description, Gemini fallback)
 *      ─► Review (accuracy spot-check)
 *      ─► Trending   (last N days vs prior baseline, by application + tag)
 *      ─► App Health (tickets, records impacted, downtime per application)
 *      ─► Departments (what each business unit needs to reach a resolution)
 *      ─► Routing    (direct vs reassigned: time and downtime lost to handoffs)
 *
 * Setup:
 *   1. Import data/tickets_synthetic.csv into a tab named "Raw".
 *   2. Import data/taxonomy.csv into a tab named "Taxonomy" (tag, keywords, resolver_team).
 *   3. Optional: Project Settings > Script Properties > GEMINI_API_KEY for the LLM pass.
 *   4. Reload the sheet and use the "Support Ops" menu.
 */

const CFG = {
  RAW: 'Raw', CLEAN: 'Clean', TAXONOMY: 'Taxonomy', REVIEW: 'Review',
  TRENDING: 'Trending', HEALTH: 'App Health', DEPTS: 'Departments', ROUTING: 'Routing',
  GEMINI_MODEL: 'gemini-2.0-flash',
  GEMINI_BATCH: 40,
  REVIEW_SAMPLE: 50,
  WINDOW_DAYS: 30,       // "what's trending" window
  BASELINE_DAYS: 90,     // compared against the rate in the 90 days before it
  TREND_MIN_TICKETS: 8,  // ignore tiny counts
  TREND_MIN_LIFT: 3,     // flag at 3x the expected rate
  FOCUS_DEPTS: ['Legal', 'Finance', 'HR'],
};

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Support Ops')
    .addItem('Rebuild everything', 'rebuildAll')
    .addSeparator()
    .addItem('1. Clean', 'cleanTickets')
    .addItem('2. Tag', 'tagTickets')
    .addItem('3. Review sample', 'buildReview')
    .addItem('4. Trending', 'buildTrending')
    .addItem('5. App health', 'buildAppHealth')
    .addItem('6. Departments', 'buildDepartments')
    .addItem('7. Routing', 'buildRouting')
    .addToUi();
}

function rebuildAll() {
  cleanTickets();
  tagTickets();
  buildReview();
  buildTrending();
  buildAppHealth();
  buildDepartments();
  buildRouting();
}

/* ------------------------------------------------------------- 1. Clean */

function cleanTickets() {
  const raw = sheet_(CFG.RAW).getDataRange().getValues();
  const h = headers_(raw[0]);
  const out = [['ticket_id', 'submitted_at', 'year_month', 'status', 'requester_department', 'application',
                'title', 'description', 'priority', 'first_assigned_team', 'resolver_team', 'reassignments',
                'records_impacted', 'downtime_hours', 'days_to_close', 'tag', 'tag_source', 'recommended_team']];

  raw.slice(1).forEach(r => {
    if (!r[h.ticket_id]) return;
    const submitted = new Date(r[h.submitted_at]);
    const closed = r[h.closed_at] ? new Date(r[h.closed_at]) : null;
    out.push([
      r[h.ticket_id], submitted, Utilities.formatDate(submitted, tz_(), 'yyyy-MM'),
      closed ? 'Closed' : 'Open',
      r[h.requester_department], String(r[h.application]).trim(),
      String(r[h.title]).trim(), String(r[h.description]).trim(), r[h.priority],
      r[h.first_assigned_team], r[h.resolver_team], Number(r[h.reassignments]) || 0,
      Number(r[h.records_impacted]) || 0, Number(r[h.downtime_hours]) || 0,
      closed ? round_((closed - submitted) / 864e5, 2) : '',
      '', '', '',
    ]);
  });
  write_(CFG.CLEAN, out);
}

/* --------------------------------------------------------------- 2. Tag */

function tagTickets() {
  const sheet = sheet_(CFG.CLEAN);
  const data = sheet.getDataRange().getValues();
  const h = headers_(data[0]);
  const rules = taxonomy_();
  const tags = rules.map(r => r.tag);
  const teamFor = Object.fromEntries(rules.map(r => [r.tag, r.team]));

  // Pass 1: keyword rules over title + description; first match wins, so rule order matters
  const leftovers = [];
  for (let i = 1; i < data.length; i++) {
    const text = (data[i][h.title] + ' ' + data[i][h.description]).toLowerCase();
    const hit = rules.find(r => r.keywords.some(k => text.includes(k)));
    if (hit) {
      data[i][h.tag] = hit.tag;
      data[i][h.tag_source] = 'Rule';
    } else {
      leftovers.push(i);
    }
  }

  // Pass 2: Gemini, constrained to the fixed taxonomy
  const key = PropertiesService.getScriptProperties().getProperty('GEMINI_API_KEY');
  for (let b = 0; b < leftovers.length; b += CFG.GEMINI_BATCH) {
    const batch = leftovers.slice(b, b + CFG.GEMINI_BATCH);
    const texts = batch.map(i => data[i][h.title] + ' — ' + data[i][h.description]);
    const labels = key ? gemini_(texts, tags, key) : [];
    batch.forEach((row, j) => {
      const ok = tags.includes(labels[j]);
      data[row][h.tag] = ok ? labels[j] : 'Other';
      data[row][h.tag_source] = ok ? 'Gemini' : 'Unresolved';
    });
  }

  // Route: the team that can actually resolve each tag
  for (let i = 1; i < data.length; i++) data[i][h.recommended_team] = teamFor[data[i][h.tag]] || 'Service Desk';
  sheet.getRange(1, 1, data.length, data[0].length).setValues(data);
}

function gemini_(texts, tags, key) {
  const prompt =
    'You classify internal support tickets. Each line is "title — description". For each numbered ' +
    'ticket choose exactly one tag from the list, or "Other" if none fits. Never invent a tag.\n\n' +
    'Tags:\n' + tags.map(t => '- ' + t).join('\n') + '\n\nTickets:\n' +
    texts.map((t, i) => (i + 1) + '. ' + t).join('\n') +
    '\n\nReturn only a JSON array of strings, one tag per ticket, in order.';
  const url = 'https://generativelanguage.googleapis.com/v1beta/models/' + CFG.GEMINI_MODEL +
              ':generateContent?key=' + encodeURIComponent(key);
  const res = UrlFetchApp.fetch(url, {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    payload: JSON.stringify({
      contents: [{ parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0, responseMimeType: 'application/json' },
    }),
  });
  if (res.getResponseCode() !== 200) {
    console.warn('Gemini ' + res.getResponseCode() + ': ' + res.getContentText().slice(0, 300));
    return [];
  }
  try {
    const labels = JSON.parse(JSON.parse(res.getContentText()).candidates[0].content.parts[0].text);
    return Array.isArray(labels) && labels.length === texts.length ? labels : [];
  } catch (e) {
    console.warn('Unparseable Gemini response: ' + e);
    return [];
  }
}

function taxonomy_() {
  return sheet_(CFG.TAXONOMY).getDataRange().getValues().slice(1).filter(r => r[0]).map(r => ({
    tag: String(r[0]).trim(),
    keywords: String(r[1]).split('|').map(k => k.trim().toLowerCase()).filter(String),
    team: String(r[2] || 'Service Desk').trim(),
  }));
}

/* ------------------------------------------------------------ 3. Review */

function buildReview() {
  const { rows, h } = clean_();
  const sample = shuffle_(rows).slice(0, CFG.REVIEW_SAMPLE)
    .map(r => [r[h.ticket_id], r[h.title], r[h.description], r[h.tag], r[h.tag_source], '']);
  const sh = write_(CFG.REVIEW, [['ticket_id', 'title', 'description', 'tag', 'tag_source', 'verdict']].concat(sample), 3);
  sh.getRange('A1').setValue('Tag accuracy (marked rows):').setFontWeight('bold');
  sh.getRange('B1').setFormula('=IFERROR(COUNTIF(F4:F,"Correct")/COUNTA(F4:F),"-")').setNumberFormat('0%');
  sh.getRange(4, 6, sample.length, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(['Correct', 'Wrong'], true).build());
}

/* ---------------------------------------------------------- 4. Trending */

function buildTrending() {
  const { rows, h, asOf } = clean_();
  const winStart = addDays_(asOf, -CFG.WINDOW_DAYS);
  const baseStart = addDays_(winStart, -CFG.BASELINE_DAYS);
  const groups = {};
  rows.forEach(r => {
    const t = r[h.submitted_at];
    if (t < baseStart) return;
    const k = r[h.application] + '|' + r[h.tag];
    const g = groups[k] = groups[k] || { recent: 0, base: 0, records: 0, depts: new Set() };
    if (t >= winStart) {
      g.recent++;
      g.records += r[h.records_impacted];
      g.depts.add(r[h.requester_department]);
    } else {
      g.base++;
    }
  });

  const out = [['Application', 'Tag', 'Last ' + CFG.WINDOW_DAYS + ' days', 'Expected', 'Lift',
                'Records impacted', 'Departments reporting', 'Trending?']];
  Object.keys(groups).forEach(k => {
    const g = groups[k];
    const expected = g.base * CFG.WINDOW_DAYS / CFG.BASELINE_DAYS;
    const lift = (g.recent + 1) / (expected + 1);
    const flag = g.recent >= CFG.TREND_MIN_TICKETS && lift >= CFG.TREND_MIN_LIFT;
    out.push(k.split('|').concat([g.recent, round_(expected, 1), round_(lift, 2), g.records, g.depts.size, flag ? 'YES' : '']));
  });
  const body = out.slice(1).sort((a, b) => b[4] - a[4]);
  const sh = write_(CFG.TRENDING, [out[0]].concat(body), 3);
  sh.getRange('A1').setValue('Trending issues · ' + CFG.WINDOW_DAYS + ' days to ' + fmt_(asOf) +
    ' vs prior ' + CFG.BASELINE_DAYS + '-day rate').setFontWeight('bold').setFontSize(13);
  sh.getRange(4, 8, body.length, 1).setFontColor('#dc2626').setFontWeight('bold');
  chart_(sh, sh.newChart().asBarChart()
    .addRange(sh.getRange(3, 1, Math.min(9, body.length + 1), 1))
    .addRange(sh.getRange(3, 3, Math.min(9, body.length + 1), 2))
    .setOption('title', 'Top movers: last ' + CFG.WINDOW_DAYS + ' days vs expected'), 3, 10);
}

/* -------------------------------------------------------- 5. App health */

function buildAppHealth() {
  const { rows, h, asOf } = clean_();
  const months = unique_(rows.map(r => r[h.year_month])).sort();
  const apps = unique_(rows.map(r => r[h.application])).sort();

  // monthly downtime hours per application
  const pivot = [['Month'].concat(apps)];
  months.forEach(m => pivot.push([m].concat(apps.map(a =>
    round_(sum_(rows.filter(r => r[h.year_month] === m && r[h.application] === a).map(r => r[h.downtime_hours])), 1)))));

  // last-30-day scorecard vs each app's own baseline
  const winStart = addDays_(asOf, -CFG.WINDOW_DAYS);
  const baseStart = addDays_(winStart, -CFG.BASELINE_DAYS);
  const card = [['Application', 'Tickets (30d)', 'Records impacted (30d)', 'Downtime hrs (30d)', 'vs baseline', 'Status', 'Top issue (30d)']];
  apps.forEach(a => {
    const mine = rows.filter(r => r[h.application] === a);
    const recent = mine.filter(r => r[h.submitted_at] >= winStart);
    const base = mine.filter(r => r[h.submitted_at] >= baseStart && r[h.submitted_at] < winStart);
    const down = sum_(recent.map(r => r[h.downtime_hours]));
    const ratio = (down + 1) / (sum_(base.map(r => r[h.downtime_hours])) * CFG.WINDOW_DAYS / CFG.BASELINE_DAYS + 1);
    card.push([a, recent.length, sum_(recent.map(r => r[h.records_impacted])), round_(down, 1), round_(ratio, 2),
               ratio >= 2 ? 'Degraded' : ratio >= 1.3 ? 'Watch' : 'Healthy', mode_(recent.map(r => r[h.tag]))]);
  });

  const sh = write_(CFG.HEALTH, card, 3);
  sh.getRange('A1').setValue('Application health · through ' + fmt_(asOf)).setFontWeight('bold').setFontSize(13);
  const status = sh.getRange(4, 6, apps.length, 1);
  sh.setConditionalFormatRules([
    SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo('Degraded').setBackground('#fecaca').setRanges([status]).build(),
    SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo('Watch').setBackground('#fde68a').setRanges([status]).build(),
    SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo('Healthy').setBackground('#d1fae5').setRanges([status]).build(),
  ]);
  const top = card.length + 3;
  sh.getRange(top, 1, pivot.length, pivot[0].length).setValues(pivot);
  header_(sh.getRange(top, 1, 1, pivot[0].length));
  chart_(sh, sh.newChart().asLineChart().addRange(sh.getRange(top, 1, pivot.length, pivot[0].length))
    .setOption('title', 'Downtime hours per month, by application'), 3, 9);
}

/* ------------------------------------------------------- 6. Departments */

function buildDepartments() {
  const { rows, h } = clean_();
  const tags = taxonomy_();
  const out = [['Department', 'Tag', 'Tickets', 'Median days to resolve', 'Records impacted',
                'Reassigned %', 'Resolving team', 'Top application']];
  CFG.FOCUS_DEPTS.forEach(d => {
    tags.forEach(t => {
      const sel = rows.filter(r => r[h.requester_department] === d && r[h.tag] === t.tag);
      if (!sel.length) return;
      out.push([d, t.tag, sel.length, round_(median_(sel.map(r => r[h.days_to_close]).filter(v => v !== '')), 2),
                sum_(sel.map(r => r[h.records_impacted])),
                sel.filter(r => r[h.reassignments] > 0).length / sel.length,
                t.team, mode_(sel.map(r => r[h.application]))]);
    });
  });
  const sh = write_(CFG.DEPTS, out, 3);
  sh.getRange('A1').setValue('What ' + CFG.FOCUS_DEPTS.join(', ') + ' need to reach a resolution')
    .setFontWeight('bold').setFontSize(13);
  sh.getRange(4, 6, out.length - 1, 1).setNumberFormat('0%');
  sh.setConditionalFormatRules([SpreadsheetApp.newConditionalFormatRule()
    .setGradientMaxpoint('#1d4ed8').setGradientMinpoint('#eff6ff')
    .setRanges([sh.getRange(4, 4, out.length - 1, 1)]).build()]);
}

/* ----------------------------------------------------------- 7. Routing */

function buildRouting() {
  const { rows, h } = clean_();
  const tags = unique_(rows.map(r => r[h.tag]));
  const out = [['Tag', 'Direct: median days', 'Reassigned: median days', 'Reassigned tickets',
                'Avg downtime direct (hrs)', 'Avg downtime reassigned (hrs)', 'Avoidable downtime (hrs)']];
  let total = 0;
  tags.forEach(t => {
    const sel = rows.filter(r => r[h.tag] === t && r[h.days_to_close] !== '');
    const direct = sel.filter(r => r[h.reassignments] === 0);
    const moved = sel.filter(r => r[h.reassignments] > 0);
    if (!direct.length || !moved.length) return;
    const dDown = avg_(direct.map(r => r[h.downtime_hours]));
    const mDown = avg_(moved.map(r => r[h.downtime_hours]));
    const avoidable = Math.max(0, mDown - dDown) * moved.length;
    total += avoidable;
    out.push([t, round_(median_(direct.map(r => r[h.days_to_close])), 2), round_(median_(moved.map(r => r[h.days_to_close])), 2),
              moved.length, round_(dDown, 1), round_(mDown, 1), Math.round(avoidable)]);
  });
  const body = out.slice(1).sort((a, b) => b[6] - a[6]);
  const sh = write_(CFG.ROUTING, [out[0]].concat(body), 3);
  sh.getRange('A1').setValue('Routing · ' + Math.round(total) + ' downtime hours lost to tickets that went to the wrong team first')
    .setFontWeight('bold').setFontSize(13);
  chart_(sh, sh.newChart().asBarChart().addRange(sh.getRange(3, 1, out.length, 3))
    .setOption('title', 'Median days to resolve: direct vs reassigned'), 3, 9);
}

/* ------------------------------------------------------------ helpers */

function clean_() {
  const data = sheet_(CFG.CLEAN).getDataRange().getValues();
  const h = headers_(data[0]);
  const rows = data.slice(1).filter(r => r[h.ticket_id]);
  const latest = rows.reduce((m, r) => (r[h.submitted_at] > m ? r[h.submitted_at] : m), new Date(0));
  const asOf = new Date(latest.getFullYear(), latest.getMonth(), latest.getDate() + 1);
  return { rows, h, asOf };
}

function sheet_(name) {
  const ss = SpreadsheetApp.getActive();
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

function write_(name, values, startRow) {
  const sh = sheet_(name);
  sh.clear();
  sh.getCharts().forEach(c => sh.removeChart(c));
  const row = startRow || 1;
  sh.getRange(row, 1, values.length, values[0].length).setValues(values);
  header_(sh.getRange(row, 1, 1, values[0].length));
  sh.setFrozenRows(row);
  sh.autoResizeColumns(1, values[0].length);
  return sh;
}

function chart_(sh, builder, row, col) {
  sh.insertChart(builder.setPosition(row, col, 0, 0).setOption('width', 720).setOption('height', 380).build());
}

function header_(range) {
  range.setFontWeight('bold').setBackground('#1f2937').setFontColor('#ffffff');
}

function headers_(row) {
  return Object.fromEntries(row.map((name, i) => [String(name).trim(), i]));
}

const tz_ = () => Session.getScriptTimeZone();
const fmt_ = d => Utilities.formatDate(d, tz_(), 'MMM d, yyyy');
const addDays_ = (d, n) => new Date(d.getTime() + n * 864e5);
const unique_ = arr => [...new Set(arr)];
const sum_ = arr => arr.reduce((a, b) => a + (Number(b) || 0), 0);
const avg_ = arr => (arr.length ? sum_(arr) / arr.length : 0);
const round_ = (n, d) => Math.round(n * Math.pow(10, d)) / Math.pow(10, d);

function median_(arr) {
  if (!arr.length) return 0;
  const s = arr.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function mode_(arr) {
  const c = {};
  arr.forEach(v => (c[v] = (c[v] || 0) + 1));
  return Object.keys(c).sort((a, b) => c[b] - c[a])[0] || '';
}

function shuffle_(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}
