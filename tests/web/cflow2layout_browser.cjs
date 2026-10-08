/* Real v2 page and CSS with fixture APIs; layout never mutates the run. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const detail = {
  cwd: '/repo', scope: 'demo', sessions: ['demo'],
  run: { run: 'r-layout', workflow: 'layout-demo', status: 'running',
    tasks: [{ node: 'task', instance: 'task@1', label: 'Implement' }] },
  chart: { root: 'root', nodes: {
    root: { id: 'root', type: 'seq', children: ['task'], roles: [''] },
    task: { id: 'task', type: 'task', label: 'Implement', title: 'A wide chart label for layout testing', children: [], roles: [] },
  } },
  instances: [{ node: 'task', instance: 'task@1', await: 'report' }], journal: [],
};
let posts = 0;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    if (req.method === 'POST' && /^\/api\/cflow2?\//.test(url.pathname)) posts++;
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname === '/api/batch') { res.statusCode = 404; return res.end('{}'); }
    let data = {};
    if (url.pathname === '/api/daemon') data = { version: 'fixture', boot_id: 'layout' };
    if (url.pathname === '/api/sessions') data = { sessions: [] };
    if (url.pathname === '/api/cflow/run') data = { status: 'idle', v2: true };
    if (url.pathname === '/api/cflow2/run') data = detail;
    if (url.pathname === '/api/observer') data = { sessions: [], enabled: false };
    return res.end(JSON.stringify(data));
  }
  const file = path.join(root, url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/static\//, ''));
  try {
    res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem('claunch_token:/', 'fixture-token'));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/wf/demo%7C%2Frepo`);
    const view = page.locator('#wf-view');
    const one = () => view.getByRole('button', { name: '1 column', exact: true });
    const two = () => view.getByRole('button', { name: '2 columns', exact: true });
    await two().waitFor();
    assert.equal(await two().getAttribute('aria-pressed'), 'true');
    const bounds = () => view.locator('.cf2-cols').evaluate(e => {
      const chart = e.querySelector('.cf2-col-chart').getBoundingClientRect();
      const main = e.querySelector('.cf2-col-main').getBoundingClientRect();
      return { chart: { x: chart.x, y: chart.y, width: chart.width, bottom: chart.bottom },
        main: { x: main.x, y: main.y, width: main.width }, width: e.clientWidth };
    });
    const initial = await bounds();
    assert(initial.chart.x > initial.main.x, 'default desktop columns');
    const input = view.locator('.cf2-reason').first();
    await input.fill('Keep this unsent reason');
    await view.getByRole('button', { name: 'Expand all', exact: true }).click();
    const rows = await view.locator('.cf2-row').count();
    await one().click();
    const stacked = await bounds();
    assert(stacked.chart.width > initial.chart.width * 1.8, 'chart uses full width');
    assert(stacked.chart.bottom <= stacked.main.y, 'chart above details');
    assert.equal(await input.inputValue(), 'Keep this unsent reason');
    assert.equal(await view.locator('.cf2-row').count(), rows);
    assert.equal(await one().getAttribute('aria-pressed'), 'true');
    if (process.env.CLAUNCH_SCRATCH) await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'v2-layout-stacked.png'), fullPage: true });
    await page.evaluate(() => refreshWf());
    assert.equal(await one().getAttribute('aria-pressed'), 'true', 'poll retains layout');
    await page.reload();
    await one().waitFor();
    assert.equal(await one().getAttribute('aria-pressed'), 'true', 'reload retains layout');
    await two().click();
    assert((await bounds()).chart.x > (await bounds()).main.x, 'restore columns');
    if (process.env.CLAUNCH_SCRATCH) await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'v2-layout-columns.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 900 });
    await one().click();
    assert((await bounds()).chart.bottom <= (await bounds()).main.y);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile does not overflow');
    await two().click();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'responsive columns do not overflow');
    assert.equal(posts, 0, 'layout does not send state mutations');
    assert.deepEqual(errors, []);
    console.log('cflow2layout_browser: widths, vertical order, drafts, expansion, polling, reload, mobile passed');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(e => { console.error(e); process.exit(1); });
