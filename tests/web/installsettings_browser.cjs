/* Real layout and bulk-install feedback for Settings > Install. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://install.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
      await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript'
        : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://install.test/');
    await page.evaluate(() => {
      const ids = ['common', 'beads', 'unreal-operator', 'role-leader', 'role-worker',
        'role-reviewer', 'role-specialist', 'role-analyst', 'role-operator', 'role-pm'];
      window.testDoc = {
        catalog: ids.map(id => ({ id, title: id === 'unreal-operator' ? 'Unreal — operator' : id })),
        workspaces: [{ name: 'example-workspace', path: 'F:/works/example-workspace',
          packs: ids.map((id, i) => ({ id, status: i === 1 ? 'edited' : 'missing' })) }],
        roles: ['worker', 'leader'], role_packs: { worker: ['role-worker'] }, default_packs: ['common'],
      };
      promptPacksDoc = window.testDoc;
      installOverview = { targets: [{ id: 'install:global', label: 'Global (your own Claude Code setup)',
        command: 'claunch install --global', summary: { create: 2, unchanged: 4 } }],
        global_workflows_dir: 'C:/Users/example/.claude-launcher/workflows', user_config_dir: 'C:/Users/example/.claude' };
      daemonPromptDoc = { instance: 'local', path: 'C:/Users/example/daemon-prompt.md', text: '', enabled: false };
      wsOpen = true; wsSection = 'install';
      showView('settings');
      renderWorkspaces();
      window.batchRequests = [];
      api = async (url, options) => {
        window.batchRequests.push([url, JSON.parse(options.body)]);
        await new Promise(resolve => { window.finishBatch = resolve; });
        return { ok: true, json: async () => ({ ...window.testDoc,
          batch: [{ id: 'common', error: 'permission denied' }] }) };
      };
    });
    for (const width of [1440, 1024, 768, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      const layout = await page.evaluate(() => {
        const root = document.getElementById('ws-view');
        const controls = [...root.querySelectorAll('.prompt-packs button, .prompt-pack-status, .prompt-pack-settings label')];
        return { overflow: root.scrollWidth > root.clientWidth + 1,
          clipped: controls.filter(n => n.scrollWidth > n.clientWidth + 1).map(n => n.textContent),
          wrapped: controls.filter(n => getComputedStyle(n).whiteSpace !== 'nowrap').map(n => n.textContent) };
      });
      assert.deepEqual(layout, { overflow: false, clipped: [], wrapped: [] }, `viewport ${width}`);
    }
    await page.getByRole('button', { name: 'Install all (9)', exact: true }).click();
    assert.equal(await page.getByRole('button', { name: 'Installing…', exact: true }).isDisabled(), true);
    assert.equal(await page.locator('.prompt-pack-item button:enabled').count(), 0);
    await page.evaluate(() => window.finishBatch());
    await page.waitForFunction(() => !promptPacksBusy);
    assert.match(await page.locator('.prompt-packs .wf-error').innerText(), /common: permission denied/);
    assert.deepEqual(await page.evaluate(() => window.batchRequests), [
      ['/api/prompt-packs/install-all', { workspace: 'example-workspace' }],
    ]);
    assert.deepEqual(errors, []);
    if (process.env.CLAUNCH_SCRATCH) {
      await page.setViewportSize({ width: 1440, height: 1100 });
      await page.evaluate(() => { document.getElementById('ws-view').scrollTop = 0; });
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'install-settings.png'), fullPage: true });
    }
    console.log('installsettings_browser: ok (4 widths, batch request, busy state, partial failure)');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
