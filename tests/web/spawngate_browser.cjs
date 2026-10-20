/* The spawn-request card (claunch-iw5i8.15): an operator's ask for a new
   session, or for an exited one back, waits five minutes on a person.

   Pins: the card shows who asked on whose word, the reason, what would be
   made and the countdown; a spawn card answers Approve / Manual / Reject /
   +5 min, a resume card Resume / New session / Reject / +5 min; each press
   posts to its own route; Manual opens the New session tab (never a child)
   filled with the request's task, and the form remembers the request so
   its create can be linked back; the panel row offers the same answers.

   claunch-iw5i8.16: a top-level request says what no answer does (created
   as the asker's child instead); a child request names its parent, says it
   is created as asked, and its Manual opens the spawn child tab under the
   asker, filled with the request. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

const soon = () => new Date(Date.now() + 272000).toISOString();
const SPEC = { profile: 'p', project: 'alpha', cwd: '/repo/alpha', task: 'Fix the login bug\nwith details',
  workflow: 'improv-worker-v2', worktree: '', issue: 'alpha-7' };
const spawnRec = () => ({ id: 'aaa111', kind: 'spawn', scope: 'root', session: 'op1', status: 'pending',
  reason: 'no running session does it', deadline: soon(), extensions: 0, spec: SPEC,
  basis: { operator_name: 'Alpha', user_text: 'fix login' }, requested_at: new Date().toISOString() });
const resumeRec = () => ({ id: 'bbb222', kind: 'resume', session: 'op1', status: 'pending', target: 'old1',
  reason: 'it did this work', deadline: soon(), extensions: 1, fallback: SPEC,
  basis: { operator_name: 'Alpha' }, requested_at: new Date().toISOString() });
const CHILD = { task: 'Write the parser tests\nall of them', role: 'worker', name: 'kid',
  workflow: 'improv-worker-v2', project: 'alpha' };
// The daemon's resolution of what the child spec leaves to its asker
// (claunch-mgope): profile and directory come from w1.
const CHILD_FACTS = { project: { value: 'alpha', inherited: false }, profile: { value: 'p', inherited: true },
  workspace: { value: 'alpha', inherited: true }, role: { value: 'worker', inherited: false },
  workflow: { value: 'improv-worker-v2', inherited: false } };
const childRec = () => ({ id: 'ccc333', kind: 'spawn', scope: 'child', session: 'w1', status: 'pending',
  reason: 'Write the parser tests', deadline: soon(), extensions: 0, spec: CHILD, facts: CHILD_FACTS,
  basis: {}, requested_at: new Date().toISOString() });
// The card's labelled rows, as [label, value] pairs.
const factRows = card => card.locator('.gate-facts').evaluate(box => {
  const cells = [...box.children].map(n => n.textContent);
  const out = [];
  for (let i = 0; i < cells.length; i += 2) out.push([cells[i], cells[i + 1]]);
  return out;
});

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    const posts = [];
    let rows = [spawnRec(), resumeRec(), childRec()];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://gate.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) {
        if (url.pathname === '/api/spawn-requests') return route.fulfill({ json: { requests: rows } });
        const m = url.pathname.match(/^\/api\/spawn-requests\/(\w+)\/(\w+)$/);
        if (m && route.request().method() === 'POST') {
          posts.push([m[1], m[2]]);
          const rec = rows.find(r => r.id === m[1]);
          if (m[2] === 'extend') {
            rec.extensions += 1;
            return route.fulfill({ json: { ok: true, request: rec } });
          }
          rows = rows.filter(r => r.id !== m[1]);
          const status = { manual: 'manual', approve: 'approved', reject: 'rejected', spawn: 'spawned' }[m[2]];
          return route.fulfill({ json: { ok: true, request: { ...rec, status } } });
        }
        return route.fulfill({ json: {} });
      }
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
      await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript'
        : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://gate.test/');
    // Reads go one by one, so the route above sees each path (the batch
    // endpoint is not what is under test).
    await page.evaluate(() => { window.batchable = () => false; });
    await page.evaluate(() => refreshSpawnGate());

    const spawnCard = page.locator('.notice.spawn-gate', { hasText: 'new top-level session requested' });
    const childCard = page.locator('.notice.spawn-gate', { hasText: 'child session requested under' });
    const resumeCard = page.locator('.notice.spawn-gate', { hasText: 'session resume requested' });
    await spawnCard.waitFor();
    const spawnText = await spawnCard.innerText();
    assert.match(spawnText, /op1 \(operator Alpha\) on your message: “fix login”/);
    assert.match(spawnText, /reason: no running session does it/);
    assert.match(spawnText, /issue alpha-7 — Fix the login bug/);
    // No `facts` on the record (an older daemon): the spec's own values.
    assert.deepEqual(await factRows(spawnCard), [['project', 'alpha'], ['profile', 'p'],
      ['workspace', '/repo/alpha'], ['role', '—'], ['workflow', 'improv-worker-v2']]);
    assert.match(spawnText, /no answer in 4:3\d: no top-level session — created as op1's child instead/);
    const buttons = async card => (await card.locator('.gate-actions button').allInnerTexts()).map(t => t.trim());
    assert.deepEqual(await buttons(spawnCard), ['Approve', 'Manual', 'Reject', '+5 min', 'Hide']);

    const resumeText = await resumeCard.innerText();
    assert.match(resumeText, /old1/);
    assert.match(resumeText, /otherwise a new session: issue alpha-7/);
    assert.equal((await factRows(resumeCard))[0][1], 'alpha', 'the fallback spec fills the rows');
    assert.match(resumeText, /not resumed — a new session is asked for instead — in 4:3\d \(extended 1×\)/);
    assert.deepEqual(await buttons(resumeCard), ['Resume', 'New session', 'Reject', '+5 min', 'Hide']);
    const childText = await childCard.innerText();
    assert.match(childText, /child session requested under w1/);
    assert.match(childText, /name kid — Write the parser tests/);
    assert.deepEqual(await factRows(childCard), [['project', 'alpha'], ['profile', 'p (inherited from w1)'],
      ['workspace', 'alpha (inherited from w1)'], ['role', 'worker'], ['workflow', 'improv-worker-v2']]);
    assert.match(childText, /auto-approves in 4:3\d — created as asked/);
    assert.deepEqual(await buttons(childCard), ['Approve', 'Manual', 'Reject', '+5 min', 'Hide']);
    assert.equal(await page.evaluate(() => gatePending.spawn.length), 3, 'the bell counts all three');
    if (process.env.CLAUNCH_SCRATCH) {
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'spawngate-cards.png') });
    }

    // +5 min posts extend and the card stays, with the new count
    await resumeCard.getByRole('button', { name: '+5 min' }).click();
    await page.waitForFunction(() => [...document.querySelectorAll('.notice.spawn-gate .gate-clock')]
      .some(n => n.textContent.includes('extended 2×')));
    assert.deepEqual(posts.at(-1), ['bbb222', 'extend']);

    // the panel row offers the same answers
    const panelButtons = await page.evaluate(() => {
      const row = noticePanelGateRow('spawn', gatePending.spawn.find(r => r.kind === 'resume'));
      return [...row.querySelectorAll('.gate-actions button')].map(b => b.textContent);
    });
    assert.deepEqual(panelButtons.slice(0, 4), ['Resume', 'New session', 'Reject', '+5 min']);

    // New session on the resume card files the fallback; the card goes
    await resumeCard.getByRole('button', { name: 'New session' }).click();
    await resumeCard.waitFor({ state: 'detached' });
    assert.deepEqual(posts.at(-1), ['bbb222', 'spawn']);

    // Manual: the New session tab, filled with the request
    await page.evaluate(() => {
      const f = $('new-session');
      f.project.appendChild(new Option('alpha', 'alpha'));
      f.profile.appendChild(new Option('p', 'p'));
    });
    await spawnCard.getByRole('button', { name: 'Manual' }).click();
    await page.waitForFunction(() => typeof sessionModal === 'object' && sessionModal
      && sessionModal.spawnRequest === 'aaa111');
    assert.deepEqual(posts.at(-1), ['aaa111', 'manual']);
    const form = await page.evaluate(() => ({
      tab: sessionFormView().mode,
      task: $('new-session').task.value,
      parent: ($('new-session').parent || {}).value || '',
      project: $('new-session').project.value,
      profile: $('new-session').profile.value,
      overlay: !$('modal-overlay').classList.contains('hidden'),
    }));
    assert.deepEqual(form, { tab: 'new', task: SPEC.task, parent: '', project: 'alpha', profile: 'p', overlay: true });
    await spawnCard.waitFor({ state: 'detached' });
    if (process.env.CLAUNCH_SCRATCH) {
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'spawngate-manual.png') });
    }

    // Manual on a child request: the spawn child tab under the asker
    await page.evaluate(() => sessionModalClose({ route: false }));
    // the other requests' cards stayed up through the first Manual
    await page.evaluate(() => {
      sessionsCache = [{ name: 'w1', status: 'idle', harness: 'claude', profile: 'p', cwd: '/repo/alpha' }];
    });
    await childCard.getByRole('button', { name: 'Manual' }).click();
    await page.waitForFunction(() => typeof sessionModal === 'object' && sessionModal
      && sessionModal.spawnRequest === 'ccc333');
    assert.deepEqual(posts.at(-1), ['ccc333', 'manual']);
    const child = await page.evaluate(() => ({
      tab: sessionFormView().mode,
      task: $('new-session').task.value,
      name: $('new-session').name.value,
      parent: sessionFormView().parent || '',
    }));
    assert.deepEqual(child, { tab: 'spawn', task: CHILD.task, name: 'kid', parent: 'w1' });
    await childCard.waitFor({ state: 'detached' });
    if (process.env.CLAUNCH_SCRATCH) {
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'spawngate-child-manual.png') });
    }
    assert.deepEqual(errors, []);
    console.log('spawngate_browser: ok (root/child/resume cards, extend, panel row, manual forms)');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
