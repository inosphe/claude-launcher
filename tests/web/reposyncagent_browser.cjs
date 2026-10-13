/* Workspaces Sync agent modes: actual controls, requests and narrow layout. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL } : {}) });
  try {
    const page = await browser.newPage();
    const errors = [];
    const posts = [];
    page.on('pageerror', e => errors.push(e.message));
    const plan = { repo: '/repo', workspace: 'example', remote: 'origin', branch: 'master',
      local: 'a'.repeat(40), upstream: 'b'.repeat(40), action: 'rebase', ahead: 2,
      behind: 1, affected: [], summary: 'Rebase and push' };
    const doc = { plan, repo: '/repo', machine: 'local', branches: ['master', 'work'],
      remote_comparisons: [{ remote: 'long-remote-name-'.repeat(12), branch: 'master', state: 'diverged', ahead: 2, behind: 1, fetched: false }],
      remotes: { origin: 'origin' }, peers: [], agent_parents: [{ name: 'parent', profile: 'coder' }] };
    const previewText = body => `Assignment: ${body.kind} ${body.branch}\nUse git rebase --rebase-merges.\n${'long-path/'.repeat(70)}`;
    await page.route('http://sync.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) {
        if (url.pathname.endsWith('/agent-preview')) {
          return route.fulfill({ json: { prompt: previewText(route.request().postDataJSON()), plan } });
        }
        if (url.pathname.endsWith('/sync')) {
          const body = route.request().postDataJSON();
          posts.push(body);
          return route.fulfill({ status: 202, json: { status: 'delegated', plan,
            agent: { session: 'sync-child', status: 'busy', prompt: 'Use git rebase --rebase-merges.\n' +
              (body.agent_mode === 'on_failure' ? 'Failure: conflict in example.txt' : 'Sync the repository') },
            operation_error: body.agent_mode === 'on_failure' ? 'conflict: example.txt' : '' } });
        }
        return route.fulfill({ json: url.pathname.endsWith('/status') ? doc : {} });
      }
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
      await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript'
        : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://sync.test/');
    await page.evaluate(doc => {
      workspacesCache = [{ name: 'example', path: '/repo', exists: true }];
      wsSyncSt('example').doc = doc;
      wsSyncOpen.add('example');
      wsOpen = true; wsSection = 'workspaces';
      showView('workspaces'); renderWorkspaces();
    }, doc);
    const mode = page.getByLabel('Sync execution mode');
    assert.equal(await mode.inputValue(), 'commands');
    await mode.selectOption('agent');
    assert.equal(await page.getByRole('button', { name: 'Delegate sync', exact: true }).isDisabled(), true);
    await page.getByLabel('Sync agent parent session').selectOption('parent');
    await page.getByRole('button', { name: 'Preview push prompt' }).click();
    await page.locator('.ws-sync-push pre').waitFor();
    assert.match(await page.locator('.ws-sync-push pre').innerText(), /Assignment: push master/);
    await page.locator('#ws-sync-push-example').selectOption('work');
    assert.equal(await page.locator('.ws-sync-push pre').count(), 0, 'branch change hides old preview');
    await page.getByRole('button', { name: 'Preview push prompt' }).click();
    await page.locator('.ws-sync-push pre').waitFor();
    assert.match(await page.locator('.ws-sync-push pre').innerText(), /Assignment: push work/);
    await page.getByRole('button', { name: 'Preview sync prompt' }).click();
    await page.waitForFunction(() => document.querySelector('.ws-sync-master pre')?.textContent.includes('Assignment: sync'));
    assert.equal(await page.locator('.ws-sync-agent button').count(), 0, 'shared settings contain no operation previews');
    assert.match(await page.locator('.ws-sync-push pre').innerText(), /Assignment: push work/, 'sync preview preserves push preview');
    assert.equal(await page.locator('.ws-sync-pull .ws-sync-operation-preview').count(), 0);
    await page.locator('.ws-sync-push input[type="checkbox"]').check();
    assert.equal(await page.locator('.ws-sync-push pre').count(), 0, 'force change hides stale push preview');
    assert.equal(await page.locator('.ws-sync-master pre').count(), 1, 'push inputs do not hide sync preview');
    await page.getByRole('button', { name: 'Preview push prompt' }).click();
    await page.locator('.ws-sync-push pre').waitFor();
    for (const width of [1440, 1024, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 1100 });
      const layout = await page.locator('.ws-sync').evaluate(node => {
        const box = node.getBoundingClientRect();
        return { overflow: node.scrollWidth > node.clientWidth + 1,
          outside: [...node.querySelectorAll('select')].some(n => {
            const r = n.getBoundingClientRect();
            return r.left < box.left - 1 || r.right > box.right + 1;
          }) };
      });
      assert.deepEqual(layout, { overflow: false, outside: false }, `viewport ${width}`);
      assert.equal(await page.locator('.ws-sync-remotes').evaluate(n => n.scrollWidth > n.clientWidth + 1), false, `remote rows viewport ${width}`);
      assert.match(await page.locator('.ws-sync-remotes').innerText(), /Diverged[\s\S]*ahead 2 · behind 1/);
    }
    await mode.selectOption('commands');
    assert.equal(await page.locator('.ws-sync-operation-preview button').count(), 0);
    await mode.selectOption('agent');
    if (process.env.CLAUNCH_SCRATCH) {
      await page.setViewportSize({ width: 1024, height: 1400 });
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'reposync-layout.png'), fullPage: true });
    }
    await page.getByRole('button', { name: 'Delegate sync', exact: true }).click();
    await page.getByRole('link', { name: 'Assigned to agent sync-child' }).waitFor();
    assert.equal(posts[0].agent_mode, 'agent');
    assert.equal(posts[0].agent_parent, 'parent');
    assert.equal(posts[0].local, plan.local);
    await mode.selectOption('on_failure');
    await page.getByRole('button', { name: 'Rebase & push', exact: true }).click();
    await page.getByText('conflict: example.txt', { exact: true }).waitFor();
    assert.equal(posts[1].agent_mode, 'on_failure');
    assert.equal(posts[1].agent_parent, 'parent');
    assert.equal(await page.locator('.ws-sync-result').innerText().then(t => t.includes('done:')), false);
    await page.locator('.ws-sync-result summary').click();
    assert.match(await page.locator('.ws-sync-result pre').innerText(), /--rebase-merges[\s\S]*Failure: conflict in example.txt/);
    assert.deepEqual(errors, []);
    if (process.env.CLAUNCH_SCRATCH) {
      await page.setViewportSize({ width: 1440, height: 1100 });
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'reposync-agent.png') });
    }
    console.log('reposyncagent_browser: ok (A/B requests, pending result, 5 widths)');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
