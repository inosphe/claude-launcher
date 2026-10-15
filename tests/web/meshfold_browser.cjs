/* The mesh page in a browser, on a fixture roster shaped like the claunch
   mesh (claunch-qzhea): mostly ended records, a few live ones.

   What a browser can say that the node slices cannot: the page asks the
   daemon for the selection it shows (comma-joined), the closed sections are
   there as headings and nothing else, a heading opens its section and the
   choice survives a reload, and a daemon too old for the comma-joined word
   still gets a page. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');

const kinds = [
  ['running', 4], ['hold', 3], ['delayed', 1], ['paused', 1], ['failed', 1],
  ['killed', 12], ['archived', 2],
];
const members = [];
for (const [category, n] of kinds) {
  for (let i = 0; i < n; i++) {
    const handle = `${category}${i}`;
    members.push({
      handle, session: handle, role: 'worker', category, local: true,
      reachability: category === 'running' ? 'idle'
        : category === 'hold' || category === 'delayed' ? 'held' : 'exited',
    });
  }
}
const links = [];
for (const a of members) for (const b of members) {
  if (a.handle < b.handle) links.push({ a: a.handle, b: b.handle, enabled: true });
}
const counts = { all: members.length, current: 0, remote: 0, missing: 0 };
for (const [category, n] of kinds) counts[category] = n;
counts.current = counts.running + counts.hold + counts.delayed;

const CURRENT = ['running', 'remote', 'hold', 'delayed'];
const inState = (category, word) => word === 'all' ? true
  : word === 'current' ? CURRENT.includes(category) : category === word;
let oldDaemon = false;
const asked = [];

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname === '/api/batch') { res.statusCode = 404; return res.end('{}'); }
    let data = {};
    if (url.pathname === '/api/daemon') data = { version: 'fixture', boot_id: 'meshfold' };
    if (url.pathname === '/api/sessions') data = { sessions: [] };
    if (url.pathname === '/api/observer') data = { sessions: [], enabled: false };
    if (url.pathname === '/api/mesh') data = { meshes: [{ name: 'claunch', members: members.length }] };
    const state = url.searchParams.get('state') || 'all';
    if (url.pathname === '/api/mesh/claunch' || url.pathname === '/api/mesh/claunch/owed') {
      asked.push(`${url.pathname.endsWith('owed') ? 'owed' : 'mesh'}:${state}`);
      if (oldDaemon && state.includes(',')) {
        res.statusCode = 400;
        return res.end(JSON.stringify({ error: `invalid member state: '${state}'` }));
      }
    }
    const words = state.split(',');
    const shown = members.filter((m) => words.some((w) => inState(m.category, w)));
    const keep = new Set(shown.map((m) => m.handle));
    if (url.pathname === '/api/mesh/claunch') {
      data = {
        name: 'claunch', members: shown, member_counts: counts,
        member_links: links.filter((e) => keep.has(e.a) && keep.has(e.b)),
        peers: [], links: [], requests: [], messages: 0, policy: {}, roles: {},
        relay: {},
      };
    }
    if (url.pathname === '/api/mesh/claunch/owed') {
      data = { members: [], owed: 0, member_counts: counts };
    }
    if (url.pathname === '/api/mesh/claunch/messages') {
      data = { messages: [], page: { counts: { all: 0, current: 0, archived: 0 } } };
    }
    return res.end(JSON.stringify(data));
  }
  const file = path.join(root, url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/static\//, ''));
  try {
    res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript'
      : file.endsWith('.css') ? 'text/css' : 'text/html');
    let body = fs.readFileSync(file);
    if (file.endsWith('app.js')) body = String(body).replace(/^boot\(\);$/m, '');
    res.end(body);
  } catch { res.statusCode = 404; res.end(); }
});

(async () => {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(() => openMesh('claunch'));
    const view = page.locator('#mesh-view');
    await view.locator('.mesh-members').waitFor();

    // The default selection is asked of the daemon, comma-joined.
    assert(asked.includes('mesh:running,hold,delayed,paused,remote'), asked.join(' '));
    const rows = () => view.locator('.mesh-members .mesh-member .mesh-handle').allTextContents();
    const shown = await rows();
    assert.equal(shown.length, 9, `running+hold+delayed+paused: ${shown}`);
    assert(!shown.some((h) => /^(killed|archived|failed)/.test(h)), shown.join(' '));
    assert.match(await view.locator('.mesh-member-filters').textContent(),
      /Running \|\| Paused \(9\).*Killed \(12\).*Archived \(2\)/);

    // Closed by default: headings only, with what they hold beside them.
    const closed = await view.locator('.mesh-fold.closed h3').allTextContents();
    for (const title of ['Connections', 'Peer daemons', 'Delivery policy', 'Roles']) {
      assert(closed.some((t) => t.includes(title)), `${title} closed: ${closed}`);
    }
    assert.equal(await view.locator('.mesh-wiring').count(), 0, 'a closed wiring list is not built');
    assert.match(await view.locator('[data-fold="wiring"] h3').textContent(), /36 connected pairs/);
    if (process.env.CLAUNCH_SCRATCH) {
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'meshfold-default.png'), fullPage: true });
    }

    // A chip adds its partition; the page asks again and shows it.
    await view.locator('.mesh-member-chip', { hasText: 'Killed' }).click();
    await page.waitForFunction(() => document.querySelectorAll('#mesh-view .mesh-members .mesh-member').length === 21);
    assert(asked.includes('mesh:running,hold,delayed,paused,killed,remote'), asked.join(' '));

    // A heading opens its section; the choice is remembered across a reload.
    await view.locator('[data-fold="wiring"] h3').click();
    await view.locator('.mesh-wiring').waitFor();
    await view.locator('[data-fold="members"] h3').first().click();
    await view.locator('[data-fold="members"].closed').waitFor();
    await page.reload();
    await page.evaluate(() => openMesh('claunch'));
    await view.locator('.mesh-wiring').waitFor();
    assert.equal(await view.locator('[data-fold="members"].closed').count(), 1, 'members stays folded');
    assert.match(await view.locator('[data-fold="members"] h3').textContent(), /21 shown of 24/,
      'the remembered filter came back with the page');

    // An older daemon refuses the comma-joined word: the page asks the word
    // that covers the selection and narrows the answer itself.
    oldDaemon = true;
    await view.locator('[data-fold="members"] h3').click();
    await page.evaluate(() => { localStorage.clear(); });
    await page.reload();
    await page.evaluate(() => openMesh('claunch'));
    await view.locator('.mesh-members .mesh-member').first().waitFor();
    assert(asked.includes('mesh:all'), asked.join(' '));
    assert.equal((await rows()).length, 9, 'the fallback still shows the selection only');

    // Phone width: no horizontal page scroll.
    await page.setViewportSize({ width: 390, height: 900 });
    const overflow = await page.evaluate(() => {
      const v = document.getElementById('mesh-view');
      return v.scrollWidth - v.clientWidth;
    });
    assert(overflow <= 1, `mesh view overflows by ${overflow}px at phone width`);
    if (process.env.CLAUNCH_SCRATCH) {
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'meshfold-phone.png'), fullPage: true });
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    server.close();
  }
  console.log('meshfold_browser: ok');
})().catch((err) => { console.error(err); process.exit(1); });
