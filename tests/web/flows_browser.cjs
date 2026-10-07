/* Exercise shipped pages with fixture APIs: no live session is controlled. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const sessions = [
  { name: 'running', status: 'busy' },
  { name: 'paused', status: 'exited', paused_at: '2026-10-07' },
  { name: 'killed', status: 'exited' },
  { name: 'archived', status: 'exited', archived_at: '2026-10-07' },
].map(s => ({ cwd: '/repo', harness: 'codex', ...s }));
const workflow = { name: 'improv-worker', start: 'work', steps: [{ id: 'work', title: 'Implement changes', instructions: 'Inspect the current task.', next: 'end' }] };
const runs = [...sessions.map(s => s.name), 'default'].map(scope => ({
  scope, cwd: '/repo', workflow: workflow.name, status: 'step', step_id: 'work',
  title: 'Implement changes', steps_completed: 2, sessions: scope === 'running' ? ['running'] : [],
  reports: [{ step: 'intake', summary: 'Scope confirmed' }],
}));
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname === '/api/batch') { res.statusCode = 404; return res.end('{}'); }
    let data = {};
    if (url.pathname === '/api/daemon') data = { version: 'fixture', boot_id: 'test' };
    if (url.pathname === '/api/sessions') data = { sessions: sessions.filter(s => url.searchParams.get('state') === 'all' || !s.archived_at) };
    if (url.pathname === '/api/cflow') data = { runs };
    if (url.pathname === '/api/cflow/run') data = { ...runs[0], run: runs[0], workflow, reports: runs[0].reports, journal: [] };
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
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem('claunch_token:/', 'fixture-token'));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/flows`);
    const names = () => page.locator('#cflow-list .cflow-name').allTextContents();
    await page.waitForFunction(() => document.getElementById('flows-result-count').textContent === '2 of 5 runs');
    assert.deepEqual(await names(), ['improv-worker · running', 'improv-worker · paused']);
    assert.equal(await page.locator('.flows-settings').evaluate(e => e.open), false);
    for (const state of ['paused', 'killed', 'archived', 'running', 'unknown']) {
      await page.selectOption('#flows-session-filter', state);
      const expected = state === 'unknown' ? 'improv-worker' : `improv-worker · ${state}`;
      await page.waitForFunction(expected => {
        const names = [...document.querySelectorAll('#cflow-list .cflow-name')];
        return names.length === 1 && names[0].textContent === expected;
      }, expected);
      assert.deepEqual(await names(), [expected]);
    }
    await page.selectOption('#flows-session-filter', 'all');
    await page.waitForFunction(() => document.querySelectorAll('#cflow-list .cflow-name').length === 5);
    await page.fill('#flows-search', 'PAUSED');
    assert.deepEqual(await names(), ['improv-worker · paused']);
    await page.evaluate(() => refreshCflow());
    assert.deepEqual(await names(), ['improv-worker · paused'], 'poll preserves filters');
    await page.fill('#flows-search', 'no-match');
    assert.match(await page.locator('.cflow-empty').textContent(), /No runs match/);
    await page.fill('#flows-search', 'running');
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `flows overflow ${width}`);
    }
    await page.locator('#cflow-list .cflow-name').click();
    await page.locator('#wf-view .wf-current').waitFor();
    await page.locator('#wf-view .wf-current').click();
    await page.locator('#wf-view .wf-instructions').waitFor();
    assert.match(await page.locator('#wf-view .wf-instructions').textContent(), /Inspect the current task/);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'detail mobile overflow');
    if (process.env.CLAUNCH_SCRATCH) await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'flows-detail-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 900 });
    if (process.env.CLAUNCH_SCRATCH) await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'flows-detail-desktop.png'), fullPage: true });
    await page.locator('#wf-view .wf-back').click();
    await page.locator('#flows-session-filter').waitFor();
    if (process.env.CLAUNCH_SCRATCH) await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'flows-list-desktop.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('flows_browser: filters, search, polling, navigation, current step, desktop/mobile passed');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(e => { console.error(e); process.exit(1); });
