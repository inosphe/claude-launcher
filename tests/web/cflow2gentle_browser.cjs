/* A poll that brings changed data rebuilds the v2 run page; what the reader
   opened, closed, scrolled or selected stays as it was (claunch-qb42y). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const report = (node, ts, text, value) => ({event: 'reported', node, ts, outcome: 'ok', by: 's29', text, value});
const long = Array.from({length: 60}, (_, i) => `line ${i} of a long value`).join('\n');
const detail = {
  cwd: '/repo', scope: 's29', sessions: ['s29'],
  run: {run: '20261028-052957-535166', workflow: 'gentle-demo', round: 1, status: 'running', tasks: [],
    started: '2026-10-28T05:29:57+00:00',
    inputs: {goal: 'keep the page still', issue: 'claunch-qb42y', scope: [], max_rounds: null}},
  chart: {root: 'work', name: 'gentle-demo', meta: {},
    nodes: {work: {id: 'work', type: 'task', label: 'work', children: [], roles: []}}},
  instances: [], visits: {work: 1},
  journal: [
    {event: 'entered', node: 'work', ts: '2026-10-28T05:30:00+00:00'},
    report('round/intake', '2026-10-28T05:31:00+00:00', 'intake text', {goal: 'g', note: long}),
    report('round/plan', '2026-10-28T05:32:00+00:00', 'plan text', {brief: 'b'}),
  ],
};
let posts = 0;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    if (req.method === 'POST' && /^\/api\/cflow2?\//.test(url.pathname)) posts++;
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname === '/api/batch') {res.statusCode = 404; return res.end('{}');}
    let data = {};
    if (url.pathname === '/api/daemon') data = {version: 'fixture', boot_id: 'gentle'};
    if (url.pathname === '/api/sessions') data = {sessions: []};
    if (url.pathname === '/api/observer') data = {sessions: [], enabled: false};
    if (url.pathname === '/api/cflow/run') data = {status: 'idle', v2: true};
    if (url.pathname === '/api/cflow2/run') data = detail;
    return res.end(JSON.stringify(data));
  }
  const file = path.join(root, url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/static\//, ''));
  try {
    res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(file));
  } catch {res.statusCode = 404; res.end();}
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 700}});
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/wf/s29|%2Frepo`);
    const view = page.locator('#wf-view');
    await view.locator('.cf2-run-inputs').waitFor();
    const inputs = view.locator('details.cf2-run-inputs');
    const reports = view.locator('details.cf2-report');
    const isOpen = (loc) => loc.evaluate(e => e.open);
    const reportNamed = (label) => reports.filter({has: page.locator('.cf2-head', {hasText: label})});
    assert.equal(await reports.count(), 2);
    assert(await isOpen(reportNamed('plan')), 'the newest report opens by default');
    assert(!(await isOpen(inputs)), 'the inputs fold starts shut');

    // the reader: open the inputs and the older report with its value, shut the newest
    await inputs.locator('summary').first().click();
    await reportNamed('plan').locator('summary').first().click();
    await reportNamed('intake').locator('summary.cf2-report-head').click();
    await reportNamed('intake').locator('details.cf2-value summary').click();
    await view.locator('details.cf2-journal summary').click();
    // a scrolled box inside the view (the harness gives the value a height
    // to scroll in), and the page around it
    await page.addStyleTag({content: '.cf2-value pre { max-height: 120px; overflow: auto; }'});
    const pre = () => reportNamed('intake').locator('details.cf2-value pre');
    await pre().evaluate(e => { e.scrollTop = 200; });
    const preTop = await pre().evaluate(e => e.scrollTop);
    assert(preTop > 0, 'the fixture value scrolls');
    const pageScroll = () => page.evaluate(() => {
      for (let n = document.querySelector('#wf-view'); n; n = n.parentElement) if (n.scrollTop) return n.scrollTop;
      return document.scrollingElement.scrollTop;
    });
    await page.evaluate(() => {
      for (let n = document.querySelector('#wf-view'); n; n = n.parentElement) {
        if (n.scrollHeight > n.clientHeight + 300 && getComputedStyle(n).overflowY !== 'visible') { n.scrollTop = 300; return; }
      }
      document.scrollingElement.scrollTop = 300;
    });
    const pageTop = await pageScroll();
    assert(pageTop > 0, 'the page scrolls');

    // the poll: a new report in front, a count moving in the inputs' title,
    // a reason line in the meta, more journal
    detail.journal.push({event: 'waiting', node: 'work', ts: '2026-10-28T05:33:00+00:00'});
    detail.journal.push(report('round/branch', '2026-10-28T05:34:00+00:00', 'branch text', {branch: 'x'}));
    detail.run.inputs.relation = 'stacked';
    detail.run.reason = 'ask';
    await page.evaluate(() => refreshWf());
    assert.equal(await reports.count(), 3, 'the changed poll drew');
    assert(await isOpen(inputs), 'inputs stay open though their count moved');
    assert(!(await isOpen(reportNamed('plan'))), 'the report the reader shut stays shut');
    assert(await isOpen(reportNamed('intake')), 'the report the reader opened stays open');
    assert(await isOpen(reportNamed('intake').locator('details.cf2-value')), 'its value stays open');
    assert(await isOpen(reportNamed('branch')), 'a new report takes its default');
    assert(await isOpen(view.locator('details.cf2-journal')), 'the journal stays open');
    assert.equal(await pre().evaluate(e => e.scrollTop), preTop, 'an inner scroll position stays');
    assert.equal(await pageScroll(), pageTop, 'the page scroll stays');

    // one node reporting the same text again: each report keeps its own fold
    // as the next one arrives in front (the reviewers' repro on f2b06627)
    detail.journal.push(report('round/item/work', '2026-10-28T05:40:00+00:00', 'same text', {n: 1}));
    detail.journal.push(report('round/item/work', '2026-10-28T05:41:00+00:00', 'same text', {n: 2}));
    await page.evaluate(() => refreshWf());
    const works = reportNamed('work');
    assert.equal(await works.count(), 2);
    await works.nth(0).locator('summary.cf2-report-head').click();   // shut the newest (05:41)
    await works.nth(1).locator('summary.cf2-report-head').click();   // open the older (05:40)
    assert.deepEqual(await works.evaluateAll(ds => ds.map(d => d.open)), [false, true]);
    detail.journal.push(report('round/item/work', '2026-10-28T05:42:00+00:00', 'same text', {n: 3}));
    await page.evaluate(() => refreshWf());
    assert.equal(await works.count(), 3);
    assert.deepEqual(await works.evaluateAll(ds => ds.map(d => d.open)), [true, false, true],
      'a repeated report: the new one opens, the shut one stays shut, the opened one stays open');

    // a selection on the view holds the poll back until it ends
    const root0 = await view.locator('.cf2').elementHandle();
    await page.evaluate(() => {
      const t = document.querySelector('.cf2-report .cf2-body');
      const r = document.createRange(); r.selectNodeContents(t);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    });
    detail.journal.push({event: 'waiting', node: 'work', ts: '2026-10-28T05:36:00+00:00'});
    await page.evaluate(() => refreshWf());
    assert(await root0.evaluate(e => e.isConnected), 'no rebuild while text is selected');
    assert(await page.evaluate(() => !getSelection().isCollapsed), 'the selection survives');
    await page.evaluate(() => getSelection().removeAllRanges());
    await page.evaluate(() => refreshWf());
    assert(!(await root0.evaluate(e => e.isConnected)), 'the held poll draws once the selection ends');

    // another run in the slot starts from the defaults
    detail.run.run = 'another-run';
    await page.evaluate(() => refreshWf());
    assert(!(await isOpen(inputs)), 'a new run resets the folds');
    assert.equal(posts, 0);
    assert.deepEqual(errors, []);
    console.log('cflow2gentle_browser: folds, repeated reports, scroll, selection hold and run reset passed');
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(e => {console.error(e); process.exit(1);});
