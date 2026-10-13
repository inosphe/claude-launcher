/* Render the actual v2 page with saved-record fixtures; no workflow writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const saved = (run, current, reloaded) => ({run, current, workflow: 'history-demo', round: current ? 4 : 3,
  status: current ? 'running' : 'done',
  started: current ? '2026-10-13T12:26:39+00:00' : '2026-10-13T12:06:49+00:00',
  finished: current ? null : '2026-10-13T12:25:24+00:00', transition: {
    event: 'round_auto_started',
    at: current ? '2026-10-13T12:26:39+00:00' : '2026-10-13T12:06:49+00:00',
    previous: current ? '20261013-120649-d4f3ac' : '20261013-090450-c39cbb', reloaded,
  }});
const detail = {
  cwd: '/repo', scope: 's29', sessions: ['s29'],
  run: {run: '20261013-122639-851fe8', workflow: 'history-demo', round: 4,
    started: '2026-10-13T12:26:39+00:00', status: 'running', tasks: []},
  chart: {root: 'work', name: 'history-demo', meta: {recur: {auto: true, min_gap: 60}},
    nodes: {work: {id: 'work', type: 'task', label: 'work', children: [], roles: []}}},
  instances: [], visits: {work: 50}, journal: [{event: 'forced_move', node: 'work'}],
  history: {limit: 10, truncated: false, unavailable: 0, runs: [
    saved('20261013-122639-851fe8', true, true), saved('20261013-120649-d4f3ac', false, false),
  ]},
};
let posts = 0;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    if (req.method === 'POST' && /^\/api\/cflow2?\//.test(url.pathname)) posts++;
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname === '/api/batch') {res.statusCode = 404; return res.end('{}');}
    let data = {};
    if (url.pathname === '/api/daemon') data = {version: 'fixture', boot_id: 'history'};
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
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/wf/s29|%2Frepo`);
    const view = page.locator('#wf-view');
    await view.locator('.cf2-run-history').waitFor();
    const meta = () => view.locator('.cf2-meta').textContent();
    assert.match(await meta(), /recurautomatic · min_gap 60s/);
    assert.match(await meta(), /round4/);
    const history = view.locator('.cf2-run-history');
    assert.match(await history.textContent(), /Current run 20261013-122639-851fe8/);
    assert.match(await history.textContent(), /previous: 20261013-120649-d4f3ac · reloaded: true/);
    await history.locator('summary').click();
    assert.match(await history.innerText(), /reloaded: false · saved IR reused after definition load failure/);
    assert.equal(await history.locator('.cf2-card').count(), 2, 'visits and force events do not create runs');
    if (process.env.CLAUNCH_SCRATCH) await page.screenshot({path: path.join(process.env.CLAUNCH_SCRATCH, 'v2-run-history.png'), fullPage: true});
    detail.chart.meta.recur = {auto: false, min_gap: 0};
    detail.history.runs[0].transition = {event: 'started', at: null, previous: null, reloaded: null};
    await page.evaluate(() => refreshWf());
    assert.match(await meta(), /recurmanual · min_gap 0s/);
    assert.match(await history.textContent(), /previous: unconfirmed · reloaded: unconfirmed/);
    delete detail.chart.meta.recur;
    await page.evaluate(() => refreshWf());
    assert.match(await meta(), /recurnone/);
    delete detail.history;
    await page.evaluate(() => refreshWf());
    assert.match(await history.textContent(), /Run history unconfirmed/);
    await page.setViewportSize({width: 390, height: 900});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile fits viewport');
    assert.equal(posts, 0);
    assert.deepEqual(errors, []);
    console.log('cflow2history_browser: auto/manual/none, reload true/false/unknown, same-run events and missing history passed');
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(e => {console.error(e); process.exit(1);});
