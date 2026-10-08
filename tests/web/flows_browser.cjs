/* Exercise shipped pages with fixture APIs: no live session is controlled. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const sessions = [
  { name: 'running', status: 'busy' },
  { name: 'paused', status: 'exited', paused_at: '2026-10-07' },
  { name: 'killed', status: 'exited' },
  { name: 'archived', status: 'exited', archived_at: '2026-10-07' },
].map(s => ({ cwd: '/repo', harness: 'codex', ...s }));
const workflow = { description: '## Workflow purpose\n\n- **Readable** instructions', name: 'improv-worker', start: 'work', steps: [{ id: 'work', title: 'Implement changes', instructions: '## Task\n\nInspect the current task.\n\n- Check **evidence** and `paths`.\n\n```sh\ngit status\n```\n\n<script>window.injected = true</script>', next: 'end' }] };
const runs = [...sessions.map(s => s.name), 'default'].map(scope => ({
  scope, cwd: '/repo', context: '## Goal\n\n- Keep **Markdown** readable.', workflow: workflow.name, status: 'step', step_id: 'work',
  title: 'Implement changes', steps_completed: 2, sessions: scope === 'running' ? ['running'] : [],
  reports: [{ step: 'intake', summary: 'Scope confirmed' }],
}));
const forced = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname === '/api/batch') { res.statusCode = 404; return res.end('{}'); }
    if (url.pathname === '/api/cflow/goto') {
      let body = '';
      req.on('data', chunk => body += chunk);
      req.on('end', () => { forced.push(JSON.parse(body)); res.end('{}'); });
      return;
    }
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
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem('claunch_token:/', 'fixture-token'));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/flows`);
    const names = () => page.locator('#cflow-list .cflow-name').allTextContents();
    await page.waitForFunction(() => document.getElementById('flows-result-count').textContent === '2 of 5 runs');
    assert.deepEqual(await names(), ['improv-worker · running', 'improv-worker · paused']);
    assert.equal(await page.locator('.flows-settings').evaluate(e => e.open), false);
    // Unknown first: archived records have not been fetched by the rail yet.
    for (const state of ['unknown', 'paused', 'killed', 'archived', 'running']) {
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
    await page.waitForFunction(() => {
      const box = document.querySelector('#wf-view .wf-instructions').getBoundingClientRect();
      return box.top >= 0 && box.top < innerHeight;
    });
    assert.equal(await page.locator('#wf-view .wf-desc .md-h2').textContent(), 'Workflow purpose');
    assert.equal(await page.locator('#wf-view .wf-context li strong').textContent(), 'Markdown');
    assert.equal(await page.locator('#wf-view .wf-instructions-text .md-h2').textContent(), 'Task');
    assert.equal(await page.locator('#wf-view .wf-instructions-text pre code').textContent(), 'git status');
    assert.equal(await page.locator('#wf-view .wf-instructions-text script').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    const force = page.locator('#wf-view .wf-step-controls .force');
    assert.equal(await force.textContent(), "Force run to 'work'");
    assert.equal(await page.locator('#wf-view .wf-diagram .force').count(), 0);
    page.once('dialog', dialog => dialog.dismiss());
    await force.click();
    assert.equal(forced.length, 0, 'cancel leaves run unchanged');
    page.once('dialog', dialog => dialog.accept());
    await Promise.all([page.waitForResponse(r => r.url().endsWith('/api/cflow/goto')), force.click()]);
    assert.deepEqual(forced, [{ cwd: '/repo', scope: 'running', step: 'work' }]);
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
