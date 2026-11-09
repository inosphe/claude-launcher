/* The v2 run panel's head and its start-inputs table (claunch-jc99t).

   The panel used to give every input name a fixed 140px column (a long name
   ran into its value), drew a settable input twice -- its value, then its
   editor on a line of its own under it -- and stretched every text field
   across the whole panel; the head spread the presses, the name, the facts
   and the drift warning over unrelated lines. This harness pins the layout
   that replaced it, measured against the shipped app.js and style.css: one
   column of names as wide as the longest, each value or editor on its name's
   line, the settable inputs grouped first, and the presses level with the
   run's name. It also checks the editors still post what they posted. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');

const inputs = {
  goal: '', issue: 'claunch-0a5nk', start_at: 'full', scope: [], max_rounds: null,
  require_coding_approval: true, require_merge_approval: false, auto_merge: true,
  relation: 'stacked', reviewer_profile: '', simulation: 'off',
  retrospection_auto_record: true,
};
const settable = ['max_rounds', 'require_coding_approval', 'require_merge_approval',
  'auto_merge', 'reviewer_profile', 'simulation', 'retrospection_auto_record'];
const typeOf = { max_rounds: 'int', relation: 'enum', simulation: 'enum', start_at: 'enum', goal: 'text' };
const decls = Object.entries(inputs).map(([name, v]) => ({
  name,
  type: typeOf[name] || (typeof v === 'boolean' ? 'bool' : Array.isArray(v) ? 'list' : 'string'),
  choices: name === 'simulation' ? ['off', 'advisory', 'required']
    : name === 'relation' ? ['stacked', 'independent']
    : name === 'start_at' ? ['full', 'landing'] : undefined,
  required: name === 'simulation',
  set_by: settable.includes(name) ? ['user', 'agent'] : ['agent'],
}));
const detail = {
  cwd: '/repo', scope: 's6801', session: 's6801', sessions: ['s6801'],
  package_drift: { files: ['improv-worker-v2.yaml'], pinned_at: 1793950559 },
  run: { run: '20261106-090505-2a9f09', workflow: 'improv-worker-v2', round: 1, status: 'running',
         tasks: [], started: '2026-11-06T09:05:05+00:00', inputs },
  input_decls: decls,
  chart: { root: 'work', name: 'improv-worker-v2', meta: {},
           nodes: { work: { id: 'work', type: 'task', label: 'work', children: [], roles: [] } } },
  instances: [], visits: { work: 1 },
  journal: [{ event: 'entered', node: 'work', ts: '2026-11-06T09:05:06+00:00' }],
};
const posts = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    if (req.method === 'POST' && url.pathname === '/api/cflow2/input') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => { posts.push({ path: url.pathname, body: JSON.parse(body || '{}') }); res.end('{}'); });
      return undefined;
    }
    if (url.pathname === '/api/batch') { res.statusCode = 404; return res.end('{}'); }
    let data = {};
    if (url.pathname === '/api/daemon') data = { version: 'fixture', boot_id: 'runinputs' };
    if (url.pathname === '/api/sessions') data = { sessions: [] };
    if (url.pathname === '/api/observer') data = { sessions: [], enabled: false };
    if (url.pathname === '/api/cflow/run') data = { status: 'idle', v2: true };
    if (url.pathname === '/api/cflow2/run') data = detail;
    return res.end(JSON.stringify(data));
  }
  const file = path.join(root, url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/static\//, ''));
  try {
    res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript'
      : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
  return undefined;
});

(async () => {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/wf/s6801|%2Frepo`);
    const view = page.locator('#wf-view');
    await view.locator('details.cf2-run-inputs').waitFor();
    await view.locator('details.cf2-run-inputs > summary').click();

    const measure = () => view.locator('.cf2').evaluate((cf2) => {
      const rect = (e) => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, right: r.right, bottom: r.bottom, cy: r.y + r.height / 2 }; };
      const head = cf2.querySelector('.cf2-head');
      const rows = [...cf2.querySelectorAll('.cf2-run-input')].map((r) => {
        const edit = r.querySelector('.cf2-run-input-edit');
        const field = edit && edit.querySelector('input[type=text], input[type=number], select, textarea');
        return {
          name: r.querySelector('.cf2-run-input-k').textContent,
          settable: r.classList.contains('settable'),
          k: rect(r.querySelector('.cf2-run-input-k')),
          cell: rect(r.querySelector('.cf2-run-input-cell')),
          value: r.querySelector('.cf2-run-input-value') ? r.querySelector('.cf2-run-input-value').textContent : null,
          edit: edit ? rect(edit) : null,
          field: field ? rect(field) : null,
          text: r.textContent,
        };
      });
      return {
        summary: cf2.querySelector('details.cf2-run-inputs > summary').textContent,
        groups: [...cf2.querySelectorAll('.cf2-run-input-group')].map((g) => g.firstChild.textContent),
        rows,
        h2: rect(cf2.querySelector('.cf2-title h2')),
        bar: cf2.querySelector('.cf2-title .cf2-bar') ? rect(cf2.querySelector('.cf2-title .cf2-bar')) : null,
        headHasDrift: !!(head && head.querySelector('.cf2-drift-note')),
        headHasMeta: !!(head && head.querySelector('.cf2-meta')),
        overflow: cf2.scrollWidth > cf2.clientWidth + 1,
      };
    });

    const wide = await measure();
    // ---- the head: one card, presses level with the name ------------------
    assert.ok(wide.headHasMeta && wide.headHasDrift, 'the facts and the drift note sit in the head card');
    assert.ok(wide.bar, 'the run presses sit on the title line');
    assert.ok(Math.abs(wide.bar.cy - wide.h2.cy) < 8,
      `the presses are level with the run name (${wide.bar.cy} vs ${wide.h2.cy})`);
    assert.ok(wide.bar.x > wide.h2.right, 'the presses are to the right of the name');

    // ---- the inputs: counts, groups, order --------------------------------
    assert.match(wide.summary, /inputs\s*12/, `the summary counts the inputs: ${wide.summary}`);
    assert.match(wide.summary, /7 editable/, 'the summary counts the settable inputs');
    assert.deepEqual(wide.groups, ['settable', 'fixed at start'], 'settable inputs are grouped ahead');
    assert.deepEqual(wide.rows.filter((r) => r.settable).map((r) => r.name), settable,
      'the settable group holds exactly the set_by: [user] inputs, in declaration order');
    assert.equal(wide.rows.findIndex((r) => !r.settable), settable.length,
      'every settable row comes before the first fixed one');

    // ---- one column of names, values on the same line ---------------------
    const cellX = wide.rows[0].cell.x;
    for (const r of wide.rows) {
      assert.ok(Math.abs(r.cell.x - cellX) < 1, `${r.name}: its value column lines up with the others`);
      assert.ok(r.k.right <= r.cell.x + 0.5, `${r.name}: the name ends before its value starts`);
      assert.ok(Math.abs(r.k.cy - r.cell.cy) < 8, `${r.name}: the value sits on its name's line`);
    }
    for (const r of wide.rows.filter((x) => x.settable)) {
      assert.equal(r.value, null, `${r.name}: a short settable value is shown once, in its editor`);
      assert.ok(r.edit, `${r.name}: has an editor`);
    }
    const byName = Object.fromEntries(wide.rows.map((r) => [r.name, r]));
    assert.ok(byName.reviewer_profile.edit.w <= 481, 'a text editor is capped in width');
    assert.match(byName.require_coding_approval.text, /true$/, 'a bool editor says its state');
    assert.match(byName.require_merge_approval.text, /false$/, 'a bool editor says its state');
    assert.doesNotMatch(byName.auto_merge.text, /\bon$/, 'the bare "on" label is gone');
    assert.equal(byName.issue.value, 'claunch-0a5nk', 'a fixed input shows its value');
    assert.equal(byName.goal.value, '—', 'an empty fixed input shows a dash');
    assert.equal(wide.overflow, false, 'no horizontal overflow at 1400px');

    // ---- the editors post what they posted --------------------------------
    const merge = view.locator('.cf2-run-input', { hasText: 'require_merge_approval' });
    page.once('dialog', (d) => d.dismiss());
    await merge.locator('input[type=checkbox]').click();
    await page.waitForTimeout(100);
    assert.equal(await merge.locator('input[type=checkbox]').isChecked(), false, 'a dismissed confirm reverts the box');
    assert.match(await merge.textContent(), /false$/, 'and its state word');
    assert.equal(posts.length, 0, 'a dismissed confirm posts nothing');
    page.once('dialog', (d) => d.accept());
    await merge.locator('input[type=checkbox]').click();
    await page.waitForFunction(() => true);
    await page.waitForTimeout(200);
    assert.deepEqual(posts.map((p) => [p.path, p.body.name, p.body.value]),
      [['/api/cflow2/input', 'require_merge_approval', true]], 'an accepted toggle posts the input');

    // ---- narrow: still inside the panel ----------------------------------
    for (const width of [560, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(100);
      const narrow = await measure();
      assert.equal(narrow.overflow, false, `${width}: no horizontal overflow`);
      for (const r of narrow.rows) {
        assert.ok(r.cell.right <= (await view.locator('.cf2').evaluate((e) => e.getBoundingClientRect().right)) + 1,
          `${width}: ${r.name} stays inside the panel`);
      }
    }

    assert.deepEqual(errors, [], `the page ran without errors: ${errors.join('; ')}`);
    console.log('cflow2runinputs_browser: ok');
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
