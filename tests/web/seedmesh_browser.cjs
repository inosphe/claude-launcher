/* A mesh the operator picked survives the seed (claunch-xlqwq).

   The report: "Spawn same setup" on a session in mesh gds6, then the "New
   session" tab, then mesh claunch picked by hand — and the session was
   created in gds6 (or, when the seed carried an empty mesh, in none).

   openSessionModal applies a seed twice: once as the box opens, and again
   after the daemon's option fills (refreshWorkspaces / refreshProjects /
   refreshWorkflowChoices / refreshRoles), because a value a picker rebuild
   dropped is put back by that second pass. The second pass ran over rows
   the operator had answered in between, and applySessionModalSeed left
   `pendingNewMesh` armed after writing the row, so the mesh row's own change
   handler spent it on the operator's pick and put the seed's value back.

   Driven here, one page per case so each starts from the app's own state:

   1. seed gds6, pick claunch before the fills land -> the row and the
      create request are claunch (the regression);
   2. no pick -> the seed still lands (gds6), so the second pass still does
      the job it exists for;
   3. seed mesh "", pick claunch -> claunch, not "no mesh";
   4. the same pick on the Spawn child tab -> the child request carries it.

   The fills are held open by the test (/api/projects answers only when the
   page releases it), so "the operator picked before the second pass" is a
   fact of the test rather than a race. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

const MESHES = [
  { name: 'claunch', parent_channel: false, members: [
    { session: 's1', handle: 's1', role: 'worker', local: true, roles: ['worker'] }] },
  { name: 'gds6', parent_channel: false, members: [
    { session: 'w1', handle: 'w1', role: 'worker', local: true, roles: ['worker'] }] },
];
const SESSIONS = [{ name: 'w1', status: 'idle', harness: 'claude', profile: 'p',
  cwd: '/repo/alpha' }];

function answer(pathname) {
  if (pathname === '/api/sessions') return { sessions: SESSIONS };
  if (pathname === '/api/projects') {
    return { projects: [{ name: 'alpha', default_cwd: '/repo/alpha', is_default: false }] };
  }
  if (pathname === '/api/mesh') return { meshes: MESHES, outgoing: [], relay: null };
  if (pathname.includes('/roles')) {
    return { default: 'worker', roles: [{ name: 'worker' }, { name: 'leader' }] };
  }
  return {};
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const opened = [];
  try {
    /* One page per case, and the project read (the last of the fills
       openSessionModal awaits) is held until `release()` — that is what
       keeps the operator's pick strictly before the second seed pass. */
    async function openForm(seed, releaseEarly) {
      const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
      const posts = [];
      let release;
      const held = new Promise((r) => { release = r; });
      page.on('pageerror', (e) => opened.push(e.message));
      await page.addInitScript(() => { window.setInterval = () => 0; });
      await page.route('http://seedmesh.test/**', async (route) => {
        const url = new URL(route.request().url());
        if (url.pathname === '/api/batch') {
          const body = JSON.parse(route.request().postData() || '{}');
          const answers = {};
          for (const p of body.paths || []) answers[p] = answer(String(p).split('?')[0]);
          return route.fulfill({ json: { answers, errors: {} } });
        }
        if (url.pathname.startsWith('/api/')) {
          if (route.request().method() === 'POST') {
            posts.push({ path: url.pathname, body: route.request().postDataJSON() });
            return route.fulfill({ json: { name: 'made1', status: 'running',
              mesh: { ok: true } } });
          }
          if (url.pathname === '/api/projects') {
            if (releaseEarly) release();
            await held;
            return route.fulfill({ json: { projects: [
              { name: 'alpha', default_cwd: '/repo/alpha', is_default: false }] } });
          }
          return route.fulfill({ json: answer(url.pathname) });
        }
        const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
        let content = fs.readFileSync(path.join(root, name));
        if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
        await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript'
          : name.endsWith('.css') ? 'text/css' : 'text/html' });
      });
      await page.goto('http://seedmesh.test/');
      // Reads go out one by one, or the project fill would ride the batch
      // endpoint and the gate above would never hold it.
      await page.evaluate(() => { window.batchable = () => false; });
      await page.evaluate(async () => {
        sessionsCache = [{ name: 'w1', status: 'idle', harness: 'claude', profile: 'p',
          cwd: '/repo/alpha' }];
        await refreshMeshList();
        await refreshSessions();
      });
      // Not awaited here: it resolves only once the gated project read
      // answers. The catch keeps a failed assertion from being masked by an
      // unhandled rejection when the browser closes with this still pending.
      const open = page.evaluate(async (seedMesh) => {
        const seed = { name: 'kid', task: 'Write the parser tests' };
        if (seedMesh !== undefined) seed.mesh = seedMesh;
        await openSessionModal({ tab: 'spawn', parent: 'w1', seed,
          modes: ['spawn', 'new'] });
      }, seed).catch(() => {});
      await page.waitForFunction(() => typeof sessionModal === 'object' && sessionModal);
      await page.waitForTimeout(80);
      return { page, posts, open, release: () => release() };
    }

    const row = (page) => page.evaluate(() => ({
      mesh: $('new-session').mesh.value,
      pending: pendingNewMesh,
      answered: [...(($('new-session')._answeredRows) || [])].map((c) => c.name),
      mode: sessionFormView().mode,
    }));
    const submit = async (page) => {
      await page.evaluate(() => {
        $('new-session').name.value = 'probe1';
        $('new-session').requestSubmit();
      });
      await page.waitForTimeout(600);
    };
    const create = (posts) => posts.find((p) => p.path === '/api/sessions'
      || p.path.endsWith('/children'));

    /* 1. The report: seed gds6, the operator answers the mesh row before the
          option fills land, and the create request carries that answer. */
    {
      const { page, posts, release } = await openForm('gds6');
      await page.click('#new-tab-new');
      await page.selectOption('#new-session select[name=mesh]', 'claunch');
      const before = await row(page);
      assert.equal(before.mesh, 'claunch');
      release();
      // The project option is the signal that the fills (and with them the
      // seed's second pass) have landed.
      await page.waitForFunction(() => [...$('new-session').project.options]
        .some((o) => o.value === 'alpha'));
      await page.waitForTimeout(250);
      const after = await row(page);
      assert.deepEqual([after.mesh, after.pending, after.mode], ['claunch', '', 'new'],
        'the seed\'s second pass leaves the answered row alone (was gds6)');
      assert.deepEqual(after.answered, ['mesh'], 'the pick is held as an answer');
      await submit(page);
      assert.equal(create(posts).path, '/api/sessions');
      assert.equal(create(posts).body.mesh, 'claunch',
        'the create request carries the chosen mesh (was gds6)');
      await page.close();
    }

    /* 2. The other half of the contract: with no answer on the row, the seed
          lands — the second pass still exists for what it was added for. */
    {
      const { page, posts, release } = await openForm('gds6');
      await page.click('#new-tab-new');
      release();
      await page.waitForFunction(() => [...$('new-session').project.options]
        .some((o) => o.value === 'alpha'));
      await page.waitForTimeout(250);
      const after = await row(page);
      assert.deepEqual([after.mesh, after.answered], ['gds6', []],
        'an unanswered row still takes the seed');
      await submit(page);
      assert.equal(create(posts).body.mesh, 'gds6');
      await page.close();
    }

    /* 3. A seed that names no mesh is the other arm of the report: the row
          went back to "(none)" and the session was created without one. */
    {
      const { page, posts, release } = await openForm('');
      await page.click('#new-tab-new');
      await page.selectOption('#new-session select[name=mesh]', 'claunch');
      release();
      await page.waitForFunction(() => [...$('new-session').project.options]
        .some((o) => o.value === 'alpha'));
      await page.waitForTimeout(250);
      assert.equal((await row(page)).mesh, 'claunch');
      await submit(page);
      assert.equal(create(posts).body.mesh, 'claunch',
        'an empty seeded mesh no longer erases the pick (was: no mesh at all)');
      await page.close();
    }

    /* 4. The same answer given on the Spawn child tab rides the child
          request, and the parent is what the request is addressed to. */
    {
      const { page, posts, release } = await openForm('gds6');
      const spawned = await row(page);
      assert.deepEqual([spawned.mode, spawned.mesh], ['spawn', 'gds6']);
      await page.selectOption('#new-session select[name=mesh]', 'claunch');
      release();
      await page.waitForFunction(() => [...$('new-session').project.options]
        .some((o) => o.value === 'alpha'));
      await page.waitForTimeout(250);
      assert.equal((await row(page)).mesh, 'claunch');
      await submit(page);
      assert.equal(create(posts).path, '/api/sessions/w1/children');
      assert.equal(create(posts).body.mesh, 'claunch');
      await page.close();
    }

    assert.deepEqual(opened, [], 'no page errors');
    console.log('seedmesh_browser: ok (a hand-picked mesh survives the seed\'s second '
      + 'pass, the seed still lands on an unanswered row, and the child request carries it)');
  } finally { await browser.close(); }
})().catch((e) => { console.error(e); process.exit(1); });
