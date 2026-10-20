/* The fill default button beside the Project row (claunch-rqzey) on the real
   form: it is enabled only while Directory or Mesh differs from the picked
   project's default, and a press puts those rows back.

   claunch-3b3vt: the button also has to keep working after the form has been
   opened and closed once. The Project row is the rail's pick (PROJECT_KEY),
   but closing the modal puts the values from before the box was opened back,
   and the row is rebuilt only when the project list moves — so a second
   opening found the row EMPTY. projectDefaultsDrift then had no project to
   compare against and the button stayed off however far the Directory and
   Mesh had drifted. This pins the reopen: the rail's project is taken again
   on every opening read (pickProjectWhenEmpty), so the drift is found and
   the press fills it.

   The registry values are this machine's own (project claunch -> workspace
   claunch at its real path, mesh claunch), because that is where the report
   came from: the project states both defaults and both pickers offer them, so
   neither "the project has no defaults" nor "the picker cannot offer the
   value" is what went wrong. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

const CLAUNCH_DIR = '/home/yusanghyun/works/claunch';
const GDS5_DIR = '/home/yusanghyun/works/gds5';
const PROJECTS = [
  { name: 'default', default_workspace: null, default_cwd: null, is_default: true },
  { name: 'claunch', default_workspace: 'claunch', default_cwd: CLAUNCH_DIR,
    default_mesh: 'claunch', is_default: false },
];
const WORKSPACES = [
  { name: 'claunch', path: CLAUNCH_DIR, exists: true },
  { name: 'gds5', path: GDS5_DIR, exists: true },
];
const MESHES = [{ name: 'claunch', members: [] }];

/* One answer per path, for both the lone read and the batched one. */
function answer(full) {
  const pathname = String(full).split('?')[0];
  if (pathname === '/api/projects') return { projects: PROJECTS };
  if (pathname === '/api/workspaces') return { workspaces: WORKSPACES };
  if (pathname === '/api/mesh') return { meshes: MESHES };
  if (pathname.includes('/roles')) {
    return { default: 'free-role', roles: [{ name: 'free-role' }, { name: 'worker' }] };
  }
  if (pathname.includes('/workflows')) return { workflows: [{ name: 'improv-worker-v2' }] };
  return {};
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(() => { window.setInterval = () => 0; });
    await page.route('http://filldefault.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/batch') {
        const body = JSON.parse(route.request().postData() || '{}');
        const answers = {};
        for (const p of body.paths || []) answers[p] = answer(p);
        return route.fulfill({ json: { answers, errors: {} } });
      }
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: answer(url.pathname) });
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
      await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript'
        : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://filldefault.test/');
    // The rail remembers a project, as it does on a browser that has used
    // this daemon before; the form's Project row follows it.
    await page.evaluate(() => localStorage.setItem(`claunch_project:${BASE}`, 'claunch'));
    await page.reload();
    await page.waitForTimeout(300);
    await page.evaluate(async () => {
      const f = $('new-session');
      f.profile.add(new Option('ds4', 'ds4'));
      f.harness.add(new Option('claude', 'claude'));
      f.workflow.add(new Option('none', ''));
      f.role.add(new Option('none', ''));
      globalThis.SysPerfPage?.stopBadge();
      await refreshMeshList();
    });

    const state = () => page.evaluate(() => {
      const f = $('new-session');
      const btn = $('new-project-defaults');
      return { project: f.project.value, cwd: f.cwd.value, mesh: f.mesh.value,
        disabled: btn.disabled, drift: btn.classList.contains('drift'),
        title: btn.title,
        rows: projectDefaultsDrift(f).map(d => [d.label, d.want]) };
    });
    const open = (opts) => page.evaluate(async (opts) => {
      if (sessionModal) sessionModalClose({ route: false });
      await openSessionModal(opts);
    }, opts);

    // 1. First opening: the rail's project is the row's answer, and the
    //    Directory row is still empty, so the button offers to fill it.
    await open({ tab: 'new' });
    await page.waitForTimeout(100);
    const first = await state();
    assert.equal(first.project, 'claunch');
    assert.equal(first.disabled, false, 'a directory off the default turns the button on');
    assert.equal(first.drift, true);
    assert.match(first.title, /^set Directory and Mesh to project claunch.s default$/);
    assert.deepEqual(first.rows, [['Directory', CLAUNCH_DIR], ['Mesh', 'claunch']]);

    // 2. Close and open again — the regression (claunch-3b3vt): the row used
    //    to come back empty, "no project picked", with the button dead.
    await page.evaluate(() => sessionModalClose({ route: false }));
    await open({ tab: 'new' });
    await page.waitForTimeout(100);
    const reopened = await state();
    assert.equal(reopened.project, 'claunch', 'the reopened form keeps the rail project');
    assert.equal(reopened.disabled, false, '...and its button still compares');
    assert.equal(reopened.title, "set Directory and Mesh to project claunch's default");

    // 3. A press puts both rows on the project's defaults and the button goes
    //    off — the same work each row's own change does, with no change event
    //    of its own.
    await page.locator('#new-project-defaults').click();
    await page.waitForTimeout(100);
    const filled = await state();
    assert.deepEqual([filled.cwd, filled.mesh, filled.disabled, filled.drift],
      [CLAUNCH_DIR, 'claunch', true, false]);
    assert.equal(filled.title, "Directory and Mesh already hold project claunch's defaults");

    // 4. A hand pick off the default turns it on and names that row only.
    await page.evaluate(() => {
      const f = $('new-session');
      f.cwd.value = '/home/yusanghyun/works/gds5';
      f.cwd.dispatchEvent(new Event('change', { bubbles: true }));
    });
    const drifted = await state();
    assert.deepEqual([drifted.disabled, drifted.title],
      [false, "set Directory to project claunch's default"]);
    await page.locator('#new-project-defaults').click();
    await page.waitForTimeout(100);
    assert.deepEqual(await state().then(s => [s.cwd, s.disabled]), [CLAUNCH_DIR, true]);

    // 5. A child's form is not this row's: it reads "(the parent's)", inherits
    //    the directory, and offers nothing to fill.
    await page.evaluate((cwd) => {
      sessionsCache = [{ name: 'w1', status: 'idle', harness: 'claude', profile: 'ds4',
        cwd, project: 'claunch' }];
    }, CLAUNCH_DIR);
    await open({ tab: 'spawn', parent: 'w1' });
    await page.waitForTimeout(100);
    const child = await state();
    assert.equal(child.project, '', 'a child names no project of its own');
    assert.equal(child.cwd, '');
    assert.equal(child.disabled, true, 'nothing to fill on a child');
    assert.equal(child.title, 'no project picked');
    assert.deepEqual(errors, []);
    console.log('filldefault_browser: ok (reopen keeps the rail project, drift fills both rows, child offers nothing)');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
