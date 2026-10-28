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
    root: { id: 'root', type: 'seq', children: [...Array.from({length: 60}, (_, i) => `past${i}`), 'frame'], roles: [] },
    frame: { id: 'frame', type: 'seq', label: 'Frame', children: ['task'], roles: [''], force_target: true },
    task: { id: 'task', type: 'task', label: 'Implement', title: 'A wide chart label for layout testing', children: [], roles: [] },
  } },
  instances: [
    {node: 'root', instance: 'root@1', parent: null, frame: {idx: 60}},
    {node: 'frame', instance: 'frame@1', parent: 'root@1', frame: {idx: 0}, can_step_out: true, returns: {ok: {}}},
    {node: 'task', instance: 'task@1', parent: 'frame@1', await: 'report', frame: {}},
  ], journal: [],
};
for (let i = 0; i < 60; i++) detail.chart.nodes[`past${i}`] = {
  id: `past${i}`, type: 'task', label: `Earlier task ${i}`, children: [], roles: [],
};
let posts = 0;
const actions = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    if (req.method === 'POST' && /^\/api\/cflow2?\//.test(url.pathname)) {
      posts++;
      let text = '';
      req.on('data', chunk => { text += chunk; });
      req.on('end', () => actions.push({path: url.pathname, body: JSON.parse(text)}));
    }
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
    const stackDetail = view.locator('.cf2-stack-detail');
    const overview = view.locator('.cf2-stack-overview');
    assert.equal(await view.locator('.cf2-stack-path').count(), 1, 'serial path occupies one line');
    assert.equal(await view.locator('.cf2-stack-path').textContent(), 'root › Frame › Implement');
    assert(!(await stackDetail.evaluate(e => e.open)), 'callstack details default closed');
    assert.equal(await view.getByRole('button', {name: 'Step out', exact: true}).count(), 0);
    assert.equal(await view.locator('.cf2-stack-path').evaluate(e => e.getBoundingClientRect().height), 24);
    await overview.focus();
    await page.keyboard.press('Enter');
    assert(await stackDetail.evaluate(e => e.open), 'keyboard expands full callstack');
    assert.equal(await view.locator('.cf2-stack-frame').count(), 3, 'all live frames shown');
    assert.equal(await view.getByRole('button', {name: 'Step out', exact: true}).count(), 1,
      'root and tasks cannot be released');
    const heights = await view.locator('.cf2-stack-frame').evaluateAll(nodes =>
      nodes.map(n => n.getBoundingClientRect().height));
    assert(heights.every(h => h <= 40), `collapsed desktop frames fit one row: ${heights}`);
    const frameSummary = view.locator('.cf2-stack-frame[data-instance="frame@1"] summary');
    await frameSummary.focus();
    await page.keyboard.press('Enter');
    assert(await frameSummary.evaluate(n => n.parentElement.open), 'keyboard opens frame details');
    await page.keyboard.press('Enter');
    assert(!(await frameSummary.evaluate(n => n.parentElement.open)), 'keyboard closes frame details');
    await page.evaluate(() => {
      window.savedCflowRoot = document.querySelector('#wf-view .cf2');
      window.savedCflowTree = document.querySelector('#wf-view .cf2-tree');
      window.savedCflowTree.scrollTop = 140;
      document.querySelector('#wf-view .cf2-stack-frame details').open = true;
    });
    await page.evaluate(() => refreshWf());
    assert(await page.evaluate(() => savedCflowRoot === document.querySelector('#wf-view .cf2')),
      'unchanged poll retains DOM');
    detail.run.reason = 'updated status';
    await page.evaluate(() => refreshWf());
    assert(await page.evaluate(() => savedCflowTree === document.querySelector('#wf-view .cf2-tree')),
      'non-chart updates retain chart DOM');
    assert.equal(await view.locator('.cf2-tree').evaluate(e => e.scrollTop), 140, 'changed poll retains scroll');
    assert(await view.locator('.cf2-stack-frame details').first().evaluate(e => e.open), 'frame details remain open');
    assert(await stackDetail.evaluate(e => e.open), 'callstack details remain open after changed poll');
    await view.locator('.cf2-row[data-node="root"] .cf2-toggle').click();
    assert.equal(await view.locator('.cf2-row[data-node="task"]').count(), 0, 'collapsed active ancestor');
    await view.getByRole('button', {name: 'Current state', exact: true}).click();
    await page.waitForFunction(() => {
      const tree = document.querySelector('#wf-view .cf2-tree');
      const row = tree.querySelector('[data-node="task"]');
      if (!row) return false;
      const r = row.getBoundingClientRect(), t = tree.getBoundingClientRect();
      return r.top >= t.top && r.bottom <= t.bottom;
    });
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
    if (process.env.CLAUNCH_SCRATCH) await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'v2-callstack-mobile.png'), fullPage: true });
    assert(!(await stackDetail.evaluate(e => e.open)), 'reload starts with compact callstack');
    await overview.click();
    const dismiss = dialog => dialog.dismiss();
    page.on('dialog', dismiss);
    await view.getByRole('button', {name: 'Rollback', exact: true}).click();
    await view.getByRole('button', {name: 'Step out', exact: true}).click();
    assert.equal(posts, 0, 'cancelled rollback and step out send no mutations');
    page.off('dialog', dismiss);
    page.on('dialog', dialog => dialog.accept());
    await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/cflow2/run?')),
      view.getByRole('button', {name: 'Rollback', exact: true}).click(),
    ]);
    await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/cflow2/run?')),
      view.getByRole('button', {name: 'Step out', exact: true}).click(),
    ]);
    assert.equal(actions.length, 2);
    assert.equal(actions[0].path, '/api/cflow2/move');
    assert.equal(actions[0].body.node, 'frame');
    assert.equal(actions[1].path, '/api/cflow2/step-out');
    assert.equal(actions[1].body.instance, 'frame@1');
    assert.equal(actions[1].body.run, 'r-layout');
    assert.equal(actions[1].body.outcome, 'ok');
    await overview.click();
    detail.chart.nodes.frame.type = 'par';
    detail.chart.nodes.frame.children.push('other');
    detail.chart.nodes.other = {id: 'other', type: 'task', label: 'Review a very long parallel branch name for mobile layout', children: [], roles: []};
    detail.instances.push({node: 'other', instance: 'other@1', parent: 'frame@1', await: 'report', frame: {}});
    await page.evaluate(() => refreshWf());
    assert(!(await stackDetail.evaluate(e => e.open)), 'closed details remain closed after poll');
    assert.deepEqual(await view.locator('.cf2-stack-path').allTextContents(), [
      'root › Frame › Implement', 'root › Frame › Review a very long parallel branch name for mobile layout',
    ], 'parallel branches have independent call paths');
    const pathBounds = await view.locator('.cf2-stack-path').evaluateAll(nodes => nodes.map(e => {
      const rect = e.getBoundingClientRect();
      return {top: rect.top, bottom: rect.bottom, height: rect.height};
    }));
    assert(pathBounds[0].bottom <= pathBounds[1].top, 'parallel paths use separate lines');
    assert(pathBounds.every(b => b.height === 24), 'long branch stays on one line');
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'long paths do not overflow mobile');
    if (process.env.CLAUNCH_SCRATCH) await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'v2-callstack-parallel.png'), fullPage: true });
    await overview.focus();
    await page.keyboard.press('Enter');
    assert.equal(await view.locator('.cf2-stack-frame:visible').count(), 4, 'parallel details include all frames');
    assert.equal(posts, 2, 'callstack disclosures do not mutate run');
    await view.locator('.cf2-row[data-node="task"]').first().click();
    const paneOut = view.locator('.cf2-pane .cf2-pane-stepout');
    await paneOut.waitFor();
    assert.deepEqual(await paneOut.locator('.cf2-press').evaluateAll(ns => ns.map(n => n.dataset.instance)),
      ['frame@1'], 'task pane offers its enclosing releasable frames only');
    await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/cflow2/run?')),
      paneOut.getByRole('button', {name: 'Step out', exact: true}).click(),
    ]);
    assert.equal(actions.length, 3);
    assert.equal(actions[2].path, '/api/cflow2/step-out');
    assert.equal(actions[2].body.instance, 'frame@1');
    assert.equal(actions[2].body.outcome, 'ok');
    assert.equal(actions[2].body.reason, 'step out from node pane');
    // the mobile pane covers the chart, so the row gets the click directly
    await view.locator('.cf2-row[data-node="root"]').first().dispatchEvent('click');
    await page.waitForFunction(() => document.querySelector('#wf-view .cf2-row[data-node="root"].selected'));
    assert.equal(await view.locator('.cf2-pane .cf2-pane-stepout').count(), 0, 'root pane has no step out');
    assert.deepEqual(errors, []);
    console.log(`cflow2layout_browser: compact rows ${heights}, keyboard details, layout, stable polling, scroll, current state, confirmation cancellation, rollback/stepout, node pane stepout passed`);
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(e => { console.error(e); process.exit(1); });
