/* Human gate waits under the throughput chart (claunch-sbfsi.1): bars over UTC time at their gate's callstack depth, sharing the chart period. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const at = (h, m = 0) => `2026-10-13T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00Z`;
const gate = (node, depth, opened, closed, open = false, kind = 'ask', within = null) =>
  ({node, label: node.split('/').pop(), within, kind, opened, closed, depth, open});
const detail = {
  cwd: '/repo', scope: 's1', sessions: ['s1'],
  run: {run: 'c', workflow: 'gate-demo', round: 1, started: at(12), status: 'running', tasks: []},
  chart: {root: 'work', name: 'gate-demo', nodes: {work: {id: 'work', type: 'task', label: 'work', children: [], roles: []}}},
  instances: [], visits: {}, journal: [],
  throughput: {observed_at: at(16), limit: 64, event_limit: 2048, byte_limit: 262144,
    truncated: false, unavailable: 0, journal_incomplete: 0, runs: [
      {run: 'a', started: at(8), ended: at(10), completed_at: at(10), duration_s: 7200, merge_source: false, merges: [],
        gates: [gate('round/item/coding-approval', 15, at(9), at(9, 50), false, 'ask', 'coding-approval-check')]},
      {run: 'b', started: at(10), ended: at(12), completed_at: null, duration_s: 7200, merge_source: false, merges: [],
        gates: [gate('round/pick', 12, at(10), at(10, 20), false, 'choice'),
          gate('round/item/coding-approval', 15, at(11), at(12), true)]},
      {run: 'c', started: at(12), ended: at(16), completed_at: null, duration_s: null, merge_source: false, merges: [],
        gates: [gate('settle/end-decision.call/ask-pose', 9, at(14), at(16), true, 'ask', 'end-decision')]},
    ]},
};
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname === '/api/batch') {res.statusCode = 404; return res.end('{}');}
    let data = {};
    if (url.pathname === '/api/daemon') data = {version: 'fixture', boot_id: 'gates'};
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
    const errors = []; page.on('pageerror', e => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/wf/s1|%2Frepo`);
    const panel = page.locator('#wf-view .cf2-throughput .cf2-gates'); await panel.waitFor();
    // Past 6 hours (10:00-16:00): run a's gate ended before it, the other three are in.
    const bars = panel.locator('svg rect');
    assert.equal(await bars.count(), 3);
    const tiles = await panel.locator('.cf2-throughput-tile').allTextContents();
    assert.deepEqual(tiles, ['Gates3waited in this period', 'Waiting time3h 20minside this period',
      'Longest wait2hwhole wait', 'Waiting now1']);
    assert.deepEqual((await bars.evaluateAll(rs => rs.map(r => r.getAttribute('class')))).sort(),
      ['cf2-gate-closed', 'cf2-gate-cut', 'cf2-gate-open']);
    // Height is the callstack depth: deeper gates draw higher, the axis spans the depths present.
    const y = depth => panel.locator(`svg rect[data-depth="${depth}"]`).first().evaluate(r => Number(r.getAttribute('y')));
    assert((await y(15)) < (await y(12)) && (await y(12)) < (await y(9)), 'deeper is higher');
    const labels = await panel.locator('svg .cf2-axis-label').allTextContents();
    assert.deepEqual(labels.slice(0, 3), ['9', '12', '15'], 'depth axis labels the depths with a bar: ' + labels);
    assert.deepEqual(labels.slice(3), ['10:00', '13:00', '16:00'], 'time axis: ' + labels);
    // A bar's full wait and its frame are in its tooltip, clipped or not.
    const open = await panel.locator('svg rect.cf2-gate-open title').textContent();
    assert.match(open, /^end-decision › ask-pose \(ask\) · depth 9/);
    assert.match(open, /waiting now/); assert.match(open, /waited 2h/);
    assert.match(await panel.locator('svg rect.cf2-gate-cut title').textContent(), /run ended before an answer was recorded/);
    assert.match(await panel.locator('.cf2-throughput-legend').textContent(), /Height: callstack depth/);
    // The panel follows the chart's period.
    const section = page.locator('#wf-view .cf2-throughput');
    const set = async (key, value) => {const input = section.locator(`[data-metric-field="${key}"]`); await input.fill(value); await input.dispatchEvent('change');};
    await section.locator('[data-metric-field="span"]').selectOption('custom');
    await set('start', '2026-10-13T08:00'); await set('end', '2026-10-13T16:00');
    assert.equal(await bars.count(), 4);
    await set('start', '2026-10-13T12:30'); await set('end', '2026-10-13T13:30');
    assert.equal(await bars.count(), 0);
    assert.match(await panel.textContent(), /No gate waited on a person in this period/);
    await set('start', '2026-10-13T08:00'); await set('end', '2026-10-13T16:00');
    if (process.env.CLAUNCH_SCRATCH) await section.screenshot({path: path.join(process.env.CLAUNCH_SCRATCH, 'v2-gates.png')});
    await page.setViewportSize({width: 390, height: 900});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile fits');
    // Runs without gates say so instead of drawing an empty chart.
    for (const run of detail.throughput.runs) run.gates = [];
    await page.locator('body').click({position: {x: 1, y: 1}}); await page.evaluate(() => refreshWf());
    assert.match(await panel.textContent(), /No gate waited on a person in the saved runs/);
    assert.deepEqual(errors, []);
    console.log('cflow2gates_browser: depth lanes, clipping, open/cut/answered, period follow, empty and mobile passed');
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(e => {console.error(e); process.exit(1);});
