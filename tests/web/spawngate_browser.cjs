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
   asker, filled with the request.

   claunch-oaxni: the Manual form offers both tabs whatever the scope, so a
   child request can be created top-level (and a top-level one under the
   asker); the link back says which tab made it.

   claunch-uzo40: Manual pauses the clock and leaves the request pending.
   A held request keeps its card, says the clock is paused, answers Approve /
   Open form / Reject / +5 min, and Open form brings the same form back. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

// 4:32 out. The card text is read seconds after the rows are built (browser
// launch, route setup, refresh), so the countdown asserts accept 4:00-4:39
// rather than the one 10-second band the clock starts in (claunch-zjq1j).
const soon = () => new Date(Date.now() + 272000).toISOString();
const SPEC = { profile: 'p', project: 'alpha', cwd: '/repo/alpha', task: 'Fix the login bug\nwith details',
  workflow: 'improv-worker-v2', worktree: '', issue: 'alpha-7' };
const spawnRec = () => ({ id: 'aaa111', kind: 'spawn', scope: 'root', session: 'op1', status: 'pending',
  reason: 'no running session does it', deadline: soon(), extensions: 0, spec: SPEC,
  basis: { operator_name: 'Alpha', user_text: 'fix login' }, requested_at: new Date().toISOString() });
const resumeRec = () => ({ id: 'bbb222', kind: 'resume', session: 'op1', status: 'pending', target: 'old1',
  reason: 'it did this work', deadline: soon(), extensions: 1, fallback: SPEC,
  basis: { operator_name: 'Alpha' }, requested_at: new Date().toISOString() });
// What the rail knows: both askers, so the Spawn child tab finds its parent
// after the rail is refreshed by a create.
const SESSIONS = [{ name: 'w1', status: 'idle', harness: 'claude', profile: 'p', cwd: '/repo/alpha' },
  { name: 'op1', status: 'idle', harness: 'claude', profile: 'p', cwd: '/repo/alpha' }];
const CHILD = { task: 'Write the parser tests\nall of them', role: 'worker', name: 'kid',
  workflow: 'improv-worker-v2', project: 'alpha', worktree: 'kid-wt', beads: false };
// The daemon's resolution of what the child spec leaves to its asker
// (claunch-mgope): profile and directory come from w1.
const CHILD_FACTS = { project: { value: 'alpha', inherited: false }, profile: { value: 'p', inherited: true },
  workspace: { value: 'alpha', inherited: true }, role: { value: 'worker', inherited: false },
  workflow: { value: 'improv-worker-v2', inherited: false } };
const childRec = () => ({ id: 'ccc333', kind: 'spawn', scope: 'child', session: 'w1', status: 'pending',
  reason: 'Write the parser tests', deadline: soon(), extensions: 0, spec: CHILD, facts: CHILD_FACTS,
  basis: {}, requested_at: new Date().toISOString() });
// A request the person took over by hand and whose form was closed without
// creating anything (claunch-uzo40): still pending, clock paused. Its `facts`
// carry the two empty rows the daemon now explains — an empty role is
// free-role and an empty workflow is no run, so the card does not draw a bare
// dash a person reads as "the daemon lost it" (claunch-8fvr5).
const HELD_FACTS = { project: { value: 'alpha', inherited: true }, profile: { value: 'p', inherited: true },
  workspace: { value: 'alpha', inherited: true }, worktree: { value: 'new (generated name)', inherited: false },
  role: { value: '', inherited: false, note: 'from the handle, else free-role' },
  workflow: { value: '', inherited: false, note: 'no run' } };
const heldRec = () => ({ id: 'ddd444', kind: 'spawn', scope: 'root', session: 'op2', status: 'pending',
  hold: 'manual', held_by: 'web', held_at: new Date().toISOString(), setup: 'custom',
  created_as: 'root', reason: 'waiting for the form', deadline: soon(), extensions: 0,
  spec: SPEC, facts: HELD_FACTS, basis: {}, requested_at: new Date().toISOString() });
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
    const bodies = [];
    const creates = [];
    let rows = [spawnRec(), resumeRec(), childRec(), heldRec()];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://gate.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) {
        if (url.pathname === '/api/spawn-requests') return route.fulfill({ json: { requests: rows } });
        // The form's own create (claunch-oaxni): the top-level endpoint
        // answers with the session, the spawn one wraps it.
        if (route.request().method() === 'POST' && (url.pathname === '/api/sessions'
            || /^\/api\/sessions\/\w+\/children$/.test(url.pathname))) {
          creates.push(url.pathname);
          const made = { name: url.pathname === '/api/sessions' ? 'solo' : 'kid1', status: 'running' };
          return route.fulfill({ json: url.pathname === '/api/sessions' ? made : { session: made } });
        }
        if (url.pathname === '/api/sessions') return route.fulfill({ json: { sessions: SESSIONS } });
        const m = url.pathname.match(/^\/api\/spawn-requests\/(\w+)\/(\w+)$/);
        if (m && route.request().method() === 'POST') {
          posts.push([m[1], m[2]]);
          if (m[2] === 'created') bodies.push([m[1], route.request().postDataJSON()]);
          const rec = rows.find(r => r.id === m[1]);
          if (m[2] === 'extend') {
            rec.extensions += 1;
            return route.fulfill({ json: { ok: true, request: rec } });
          }
          // Manual on an already-held request changes nothing: the request
          // stays pending and the form opens again (claunch-uzo40).
          if (m[2] === 'manual' && rec.hold === 'manual') {
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

    const spawnCard = page.locator('.notice.spawn-gate', { hasText: 'no running session does it' });
    const childCard = page.locator('.notice.spawn-gate', { hasText: 'child session requested under' });
    const resumeCard = page.locator('.notice.spawn-gate', { hasText: 'session resume requested' });
    const heldCard = page.locator('.notice.spawn-gate', { hasText: 'waiting for the form' });
    await spawnCard.waitFor();
    const spawnText = await spawnCard.innerText();
    assert.match(spawnText, /op1 \(operator Alpha\) on your message: “fix login”/);
    assert.match(spawnText, /reason: no running session does it/);
    assert.match(spawnText, /issue alpha-7 — Fix the login bug/);
    // No `facts` on the record (an older daemon): the spec's own values.
    // The directory and the checkout are two rows (claunch-8x6v2): the
    // workspace is where, the worktree is which checkout of it.
    assert.deepEqual(await factRows(spawnCard), [['project', 'alpha'], ['profile', 'p'],
      ['workspace', '/repo/alpha'], ['worktree', 'new (generated name)'],
      ['role', '—'], ['workflow', 'improv-worker-v2']]);
    assert.match(spawnText, /no answer in 4:[0-3]\d: no top-level session — created as op1's child instead/);
    // Two groups, two questions (claunch-odk2w.1). The answer buttons are the
    // card's own semantics, and each card asserts its own list exactly. The
    // aside group — Hide plus the reminder waits — is drawn for every kind by
    // one helper (`gateAsideActions`), so a whole-list assertion per card made
    // one new shared choice fail four times and named the wrong culprit: the
    // card was right and the list was stale. Its content is written once here
    // and every other card is required to draw that same group, which is the
    // invariant the helper exists for.
    const texts = async (card, sel) => card.locator(sel)
      .evaluateAll(nodes => nodes.map(n => n.textContent.trim()));
    const buttons = card => texts(card, '.gate-actions > button');
    const asideButtons = card => texts(card, '.gate-actions > .gate-aside button');
    assert.deepEqual(await buttons(spawnCard), ['Approve', 'Manual', 'Reject', '+5 min']);

    const resumeText = await resumeCard.innerText();
    assert.match(resumeText, /old1/);
    assert.match(resumeText, /otherwise a new session: issue alpha-7/);
    assert.equal((await factRows(resumeCard))[0][1], 'alpha', 'the fallback spec fills the rows');
    assert.match(resumeText, /not resumed — a new session is asked for instead — in 4:[0-3]\d \(extended 1×\)/);
    assert.deepEqual(await buttons(resumeCard), ['Resume', 'New session', 'Reject', '+5 min']);
    const childText = await childCard.innerText();
    assert.match(childText, /child session requested under w1/);
    assert.match(childText, /name kid — Write the parser tests/);
    assert.deepEqual(await factRows(childCard), [['project', 'alpha'], ['profile', 'p (inherited from w1)'],
      ['workspace', 'alpha (inherited from w1)'], ['worktree', 'kid-wt'],
      ['role', 'worker'], ['workflow', 'improv-worker-v2']]);
    assert.match(childText, /auto-approves in 4:[0-3]\d — created as asked/);
    assert.deepEqual(await buttons(childCard), ['Approve', 'Manual', 'Reject', '+5 min']);
    assert.equal(await page.evaluate(() => gatePending.spawn.length), 4, 'the bell counts all four');
    // A request taken over by hand stays on the card with its clock paused
    // and the form reachable again (claunch-uzo40).
    const heldText = await heldCard.innerText();
    assert.match(heldText, /taken by hand \(web\) — no clock: approve, reject, or \+5 min to start the countdown again/);
    assert.deepEqual(await buttons(heldCard), ['Approve', 'Open form', 'Reject', '+5 min']);
    // The aside group, written once: Hide, then the three reminder waits in
    // the order the card draws them. Every other card must draw this exact
    // group — a kind that lost the helper loses Hide and Remind together,
    // which is the gap `gateAsideActions` was introduced to close.
    assert.deepEqual(await asideButtons(spawnCard), ['Hide', '1h', '6h', '24h'],
      'the aside group is Hide and the reminder waits');
    for (const [card, name] of [[resumeCard, 'resume'], [childCard, 'child'], [heldCard, 'held']]) {
      assert.deepEqual(await asideButtons(card), await asideButtons(spawnCard),
        `the ${name} card draws the same aside group as the spawn card`);
    }
    // An empty row says which empty it is (claunch-8fvr5)
    const heldRows = Object.fromEntries(await factRows(heldCard));
    assert.equal(heldRows.role, '— (from the handle, else free-role)');
    assert.equal(heldRows.workflow, '— (no run)');
    if (process.env.CLAUNCH_SCRATCH) {
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'spawngate-cards.png') });
    }

    // +5 min posts extend and the card stays, with the new count
    await resumeCard.getByRole('button', { name: '+5 min' }).click();
    await page.waitForFunction(() => [...document.querySelectorAll('.notice.spawn-gate .gate-clock')]
      .some(n => n.textContent.includes('extended 2×')));
    assert.deepEqual(posts.at(-1), ['bbb222', 'extend']);

    // the panel row offers the same answers, and the card's reminder waits
    // (Hide is the card's own press: the row is already inside the panel)
    const panelRow = await page.evaluate(() => {
      const row = noticePanelGateRow('spawn', gatePending.spawn.find(r => r.kind === 'resume'));
      const texts = sel => [...row.querySelectorAll(sel)].map(b => b.textContent.trim());
      return { answers: texts('.gate-actions > button'), remind: texts('.gate-actions > .gate-remind button') };
    });
    assert.deepEqual(panelRow.answers, ['Resume', 'New session', 'Reject', '+5 min']);
    assert.deepEqual(panelRow.remind, (await asideButtons(resumeCard)).slice(1),
      'the panel row offers the card\'s reminder waits, in the same order');

    // New session on the resume card files the fallback; the card goes
    await resumeCard.getByRole('button', { name: 'New session' }).click();
    await resumeCard.waitFor({ state: 'detached' });
    assert.deepEqual(posts.at(-1), ['bbb222', 'spawn']);

    // Manual: the New session tab, filled with the request. What the browser
    // remembered is standing on the form first (the draft/recall a page load
    // restores); the request's own answers have to land over it
    // (claunch-8x6v2) — and a row the request did NOT name must not keep the
    // draft's value at all (claunch-130zs). `captureNewFormPristine` stands
    // in for the boot that this harness cuts: it is what the page ships,
    // before the draft goes on top.
    await page.evaluate(() => {
      captureNewFormPristine();
      const f = $('new-session');
      f.project.appendChild(new Option('alpha', 'alpha'));
      f.profile.appendChild(new Option('p', 'p'));
      f.project.appendChild(new Option('beta', 'beta'));
      f.profile.appendChild(new Option('draft-p', 'draft-p'));
      ensureOption(f.mesh, 'draft-mesh');
      ensureOption(f.role, 'draft-role');
      f.task.value = "the browser's last task";
      f.project.value = 'beta';
      f.profile.value = 'draft-p';
      f.mesh.value = 'draft-mesh';
      f.role.value = 'draft-role';
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
      mesh: $('new-session').mesh.value,
      role: $('new-session').role.value,
      worktree: $('new-session').worktree_mode.value,
      overlay: !$('modal-overlay').classList.contains('hidden'),
    }));
    assert.deepEqual(form, { tab: 'new', task: SPEC.task, parent: '', project: 'alpha',
      profile: 'p', mesh: '', role: '', worktree: 'new', overlay: true });
    // Both tabs are offered (claunch-oaxni): the top-level request can be
    // put under its asker from Spawn child, and back.
    const tabs = () => page.evaluate(() => ['new-tab-new', 'new-tab-spawn']
      .filter(id => !$(id).classList.contains('hidden')));
    assert.deepEqual(await tabs(), ['new-tab-new', 'new-tab-spawn']);
    await page.click('#new-tab-spawn');
    assert.deepEqual(await page.evaluate(() => [sessionFormView().mode, $('new-session').parent.value]),
      ['spawn', 'op1']);
    assert.equal(await page.evaluate(() => $('new-session').task.value), SPEC.task,
      'switching tabs keeps the request\'s answers');
    await page.click('#new-tab-new');
    assert.deepEqual(await page.evaluate(() => [sessionFormView().mode, $('new-session').parent.value]),
      ['new', '']);
    await spawnCard.waitFor({ state: 'detached' });
    if (process.env.CLAUNCH_SCRATCH) {
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'spawngate-manual.png') });
    }

    // Manual on a child request: the spawn child tab under the asker
    await page.evaluate(() => sessionModalClose({ route: false }));
    // the other requests' cards stayed up through the first Manual
    await page.evaluate(() => {
      sessionsCache = [{ name: 'w1', status: 'idle', harness: 'claude', profile: 'p', cwd: '/repo/alpha' }];
      const f = $('new-session');
      // the browser's remembered answers again, and the request has to win
      f.task.value = "the browser's last task";
      f.name.value = 'browser-kid';
      f.worktree_name.value = 'browser-wt';
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
      worktreeName: $('new-session').worktree_name.value,
      beads: $('new-session').beads.value,
    }));
    assert.deepEqual(child, { tab: 'spawn', task: CHILD.task, name: 'kid', parent: 'w1',
      worktreeName: 'kid-wt', beads: 'none' });
    // The child request made top-level instead (claunch-oaxni): the New
    // session tab is offered, drops the parent, keeps the request's answers,
    // and is told the asker's repository the way "Spawn same setup" is.
    assert.deepEqual(await tabs(), ['new-tab-new', 'new-tab-spawn']);
    assert.deepEqual(await page.evaluate(() => sessionFormView().newSeed), { cwd: '/repo/alpha' });
    await page.click('#new-tab-new');
    const asRoot = await page.evaluate(() => ({
      tab: sessionFormView().mode,
      parent: $('new-session').parent.value,
      task: $('new-session').task.value,
      name: $('new-session').name.value,
      request: sessionModal.spawnRequest,
      title: $('modal-title').textContent,
    }));
    assert.deepEqual(asRoot, { tab: 'new', parent: '', task: CHILD.task, name: 'kid',
      request: 'ccc333', title: 'New session' });
    await page.click('#new-tab-spawn');
    assert.deepEqual(await page.evaluate(() => [sessionFormView().mode, $('new-session').parent.value]),
      ['spawn', 'w1']);
    await childCard.waitFor({ state: 'detached' });
    if (process.env.CLAUNCH_SCRATCH) {
      await page.screenshot({ path: path.join(process.env.CLAUNCH_SCRATCH, 'spawngate-child-manual.png') });
    }
    // Open form on a held request: the form comes back with the same spec and
    // the request stays up (the mock keeps a held row pending).
    await page.evaluate(() => sessionModalClose({ route: false }));
    await heldCard.getByRole('button', { name: 'Open form' }).click();
    await page.waitForFunction(() => typeof sessionModal === 'object' && sessionModal
      && sessionModal.spawnRequest === 'ddd444');
    assert.deepEqual(posts.at(-1), ['ddd444', 'manual']);
    assert.equal(await page.evaluate(() => $('new-session').task.value), SPEC.task);
    assert.ok(await heldCard.isVisible(), 'the request stays on the card');
    await page.evaluate(() => sessionModalClose({ route: false }));

    // The create reports the tab it was made from (claunch-oaxni): the form
    // is submitted for real, and the link back names what the create did,
    // whatever scope the request asked for. Without it the daemon keeps the
    // scope's guess and tells a child's asker "as your child" for a
    // session that was made top-level.
    const made = async (rec, tab) => {
      await page.evaluate(async r => {
        sessionsCache = structuredClone(r.sessions);
        await spawnGateManual(r.rec);
      }, { rec, sessions: SESSIONS });
      await page.waitForFunction(id => typeof sessionModal === 'object' && sessionModal
        && sessionModal.spawnRequest === id, rec.id);
      await page.click(`#new-tab-${tab}`);
      const linked = bodies.length;
      await page.evaluate(() => $('new-session').requestSubmit());
      for (let i = 0; i < 50 && bodies.length === linked; i++) await page.waitForTimeout(50);
      assert.equal(await page.evaluate(() => $('create-error').classList.contains('hidden')
        ? '' : $('create-error').textContent), '', 'the create went out');
      return [creates.at(-1), bodies.at(-1)];
    };
    assert.deepEqual(await made(childRec(), 'new'),
      ['/api/sessions', ['ccc333', { session: 'solo', created_as: 'root' }]],
      'a child request made from New session is reported top-level');
    assert.deepEqual(await made(childRec(), 'spawn'),
      ['/api/sessions/w1/children', ['ccc333', { session: 'kid1', created_as: 'child' }]],
      'a child request made from Spawn child is reported as a child');
    assert.deepEqual(await made(spawnRec(), 'spawn'),
      ['/api/sessions/op1/children', ['aaa111', { session: 'kid1', created_as: 'child' }]],
      'a top-level request made from Spawn child is reported as the asker\'s child');
    assert.deepEqual(errors, []);
    console.log('spawngate_browser: ok (root/child/resume/held cards, extend, panel row, manual forms)');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
