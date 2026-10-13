/* Throughput controls and UTC period comparison against meaningful saved samples. */
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
const at = h => `2026-10-13T${String(h).padStart(2, '0')}:00:00Z`;
const merge = (h, id) => ({at: at(h).replace(':00:00', ':30:00'), sha: id.repeat(40), identity: '/repo:' + id.repeat(40)});
detail.throughput = {observed_at: at(16), limit: 64, event_limit: 2048, byte_limit: 262144,
  truncated: false, unavailable: 0, journal_incomplete: 0, runs: [
    {run: 'a', started: at(9), ended: at(10), completed_at: at(10), duration_s: 3600, merge_source: true, merge_complete: true, merges: [merge(9, 'a')]},
    {run: 'b', started: at(10), ended: at(12), completed_at: at(12), duration_s: 7200, merge_source: true, merge_complete: true, merges: [merge(10, 'b'), merge(11, 'c')]},
    {run: 'c', started: at(12), ended: at(16), completed_at: null, duration_s: null, merge_source: true, merge_complete: true, merges: [merge(12, 'd'), merge(13, 'e')]},
  ]};
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = []; page.on('pageerror', e => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/wf/s29|%2Frepo`);
    const section = page.locator('#wf-view .cf2-throughput'); await section.waitFor();
    const set = async (key, value) => {const input = section.locator(`[data-metric-field="${key}"]`); await input.fill(value.slice(0, 16)); await input.dispatchEvent('change');};
    await set('start', at(9).slice(0, -1)); await set('end', at(16).slice(0, -1));
    await set('beforeStart', at(9).slice(0, -1)); await set('beforeEnd', at(11).slice(0, -1));
    await set('afterStart', at(11).slice(0, -1)); await set('afterEnd', at(16).slice(0, -1));
    const comparison = section.locator('.cf2-throughput-comparison');
    assert.match(await comparison.textContent(), /Before: 1 observed completed runs · 2.00 hours · 0.50 \/ hour/);
    assert.match(await comparison.textContent(), /After: 1 observed completed runs · 5.00 hours · 0.20 \/ hour/);
    assert.match(await comparison.textContent(), /improvement: -60.00%/);
    assert.match(await comparison.textContent(), /mean duration 7200.00 seconds \(n=1\)/);
    await section.locator('[data-metric-field="metric"]').selectOption('merges');
    assert.match(await comparison.textContent(), /Before: 2 observed reported merges · 2.00 hours · 1.00 \/ hour/);
    assert.match(await comparison.textContent(), /After: 3 observed reported merges · 5.00 hours · 0.60 \/ hour/);
    assert.match(await comparison.textContent(), /improvement: -40.00%/);
    await section.locator('summary').click();
    assert.equal(await section.locator('table tr').count(), 8);
    await page.locator('body').click({position: {x: 1, y: 1}});
    detail.journal.push({event: 'loop_iteration'}); await page.evaluate(() => refreshWf());
    assert.equal(await section.locator('[data-metric-field="metric"]').inputValue(), 'merges');
    assert.match(await section.locator('[data-metric-field="beforeStart"]').inputValue(), /^2026-10-13T09:00/);
    assert(await section.locator('details').evaluate(e => e.open), 'bins disclosure survives poll');
    await set('beforeEnd', '2026-10-13T09:15:00');
    assert.match(await comparison.textContent(), /not calculable \(zero baseline or insufficient coverage\)/);
    await set('beforeEnd', at(11).slice(0, -1));
    const pure = await page.evaluate(data => {
      const a = Date.parse('2026-10-13T09:00:00Z'), b = Date.parse('2026-10-13T11:00:00Z');
      const dup = {...data, runs: [...data.runs, ...data.runs]};
      const gap = {...data, runs: data.runs.filter(r => r.run !== 'a')};
      const missing = {...data, unavailable: 1};
      return {dup: cflow2MetricPeriod(dup, 'runs', a, b), gap: cflow2MetricPeriod(gap, 'runs', a, b),
        missing: cflow2MetricPeriod(missing, 'runs', a, b), empty: cflow2MetricPeriod({runs: []}, 'runs', a, b)};
    }, detail.throughput);
    assert.equal(pure.dup.count, 1); assert.equal(pure.dup.rate, .5);
    assert.equal(pure.gap.rate, null); assert.equal(pure.missing.rate, null); assert.equal(pure.empty.rate, null);
    detail.throughput.runs[1].merge_complete = false;
    await page.locator('body').click({position: {x: 1, y: 1}}); await page.evaluate(() => refreshWf());
    assert.match(await comparison.textContent(), /Before: 2 observed reported merges · 2.00 hours · unconfirmed/);
    assert.match(await comparison.textContent(), /not calculable/);
    assert(await section.locator('.cf2-metric-unknown').count());
    await section.locator('[data-metric-field="span"]').selectOption('168');
    await section.locator('[data-metric-field="bin"]').selectOption('900000');
    assert.match(await section.textContent(), /Chart limit: 128 bins/);
    if (process.env.CFLOW_METRICS_FIXTURE) {
      detail.throughput = JSON.parse(fs.readFileSync(process.env.CFLOW_METRICS_FIXTURE));
      detail.run.run = 'real-s29-example';
      await page.locator('body').click({position: {x: 1, y: 1}}); await page.evaluate(() => refreshWf());
      await section.locator('[data-metric-field="metric"]').selectOption('merges');
    }
    if (process.env.CLAUNCH_SCRATCH) await page.screenshot({path: path.join(process.env.CLAUNCH_SCRATCH, 'v2-throughput.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 900});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile fits');
    delete detail.throughput; await page.locator('body').click({position: {x: 1, y: 1}}); await page.evaluate(() => refreshWf());
    assert.match(await section.textContent(), /Throughput unconfirmed/);
    assert.equal(posts, 0); assert.deepEqual(errors, []);
    console.log('cflow2throughput_browser: unequal periods, units, zero baseline, missing coverage, duplicates, poll preservation, real fixture and mobile passed');
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(e => {console.error(e); process.exit(1);});
