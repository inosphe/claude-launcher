/* The waiting-request cards, one kind at a time: every kind's card carries
   the same aside group — Hide, then the reminder waits 1h / 6h / 24h — and
   every kind's panel row carries those same waits.

   Why this file exists (claunch-gate-card-harness-one-callsite-4b6zr):
   `spawngate_browser.cjs` draws four cards, and all four come out of ONE
   builder — the spawn gate's. The aside group of the other eight kinds is
   drawn by their own builders, each reaching for `gateAsideActions` for
   itself, so a kind that stopped reaching for it left the whole suite green.
   Here every kind `NOTICE_SOURCES` marks `gate: true` is drawn from its own
   poll and counted.

   The fixture map at the bottom of this comment block is the list of kinds.
   The last assertion compares its keys with NOTICE_SOURCES, so a kind added
   there fails here until this file draws it and counts its aside group. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

const soon = () => new Date(Date.now() + 272000).toISOString();
const asked = () => new Date(Date.now() - 60000).toISOString();
const SPEC = { profile: 'p', project: 'alpha', cwd: '/repo/alpha', task: 'Fix the login bug',
  workflow: 'improv-worker-v2', worktree: '', issue: 'alpha-7', source: 'agent', parent: '' };

// One pending request per kind. Each is the smallest record its own poll and
// card builder read; the card is found by the sentence it prints, so a builder
// that drew nothing fails on the sentence rather than on a selector.
const RECORDS = {
  restart: () => ({ id: 'r1', session: 'op1', reason: 'apply the new build', status: 'pending',
    deadline: soon(), extensions: 0, requested_at: asked() }),
  goto: () => ({ id: 'g1', session: 'op1', target_session: 'w1', from: 'review', step: 'land',
    reason: 'the reviewer is done', status: 'pending', deadline: soon() }),
  reposync: () => ({ id: 'p1', session: 'op1', status: 'pending', deadline: soon(),
    plan: { workspace: 'alpha', repo: '/repo/alpha', branch: 'work', local: 'abcdef1',
      remote: 'origin', remote_key: null, upstream: null, summary: 'push 1 commit',
      action: 'push', affected: [] } }),
  spawn: () => ({ id: 'a1', kind: 'spawn', scope: 'root', session: 'op1', status: 'pending',
    reason: 'no running session does it', deadline: soon(), extensions: 0, spec: SPEC,
    basis: { operator_name: 'Alpha' }, requested_at: asked() }),
  resume: () => ({ id: 's1', session: 'op1', target: 'old1', reason: 'it did this work',
    status: 'pending', requested_at: asked() }),
  kill: () => ({ id: 'k1', session: 'op1', target: 'old1', reason: 'its work is reported',
    status: 'pending', requested_at: asked() }),
  meshjoin: () => ({ id: 'j1', mesh: 'other', handle: 'w9', machine: 'box2', session: 's9',
    role: 'worker', requested_at: asked() }),
  // The run gate is not an endpoint: its records are handed to applyRunGate by
  // the run polls, so this one is drawn by that call below.
  ask: () => ({ id: 'q1', session: 'op1', kind: 'choose', label: 'next step',
    prompt: 'which step next?', workflow: 'improv-worker-v2', version: 2,
    presses: [{ label: 'approve', path: '/api/cflow/answers/q1', cls: 'approve', body: {} },
      { label: 'decline', path: '/api/cflow/answers/q1', cls: 'clear', body: {} }] }),
  // op2, not op1: a nudge for a session whose run gate is already up is
  // folded into that card and never drawn as one of its own.
  nudge: () => ({ id: 'n1', session: 'op2', items: ['decide the gate'],
    message: 'the round is waiting', status: 'pending' }),
};

// The endpoint each kind's own poll reads, and the payload it answers with.
const FETCHED_BY = {
  restart: '/api/daemon/restart-request',
  goto: '/api/cflow/goto-requests',
  reposync: '/api/reposync/requests',
  spawn: '/api/spawn-requests',
  resume: '/api/resume-requests',
  kill: '/api/kill-requests',
  meshjoin: '/api/mesh-join-requests',
  nudge: '/api/flow-nudges',
};
const payloadFor = (kind) => (kind === 'restart'
  ? { request: RECORDS.restart() }
  : { requests: [RECORDS[kind]()] });

// The sentence that proves the card on screen is the kind under test.
const SAYS = {
  restart: 'daemon restart requested',
  goto: 'run move requested',
  reposync: 'repository sync wants to push',
  spawn: 'no running session does it',
  resume: 'session resume requested',
  kill: 'session kill requested',
  meshjoin: 'asks to join mesh other',
  ask: 'waits on your pick: next step',
  nudge: 'needs you: decide the gate',
};

// What the aside group is, in the order every builder draws it: Hide, then
// the three waits in NOTICE_REMIND_CHOICES' own order.
const ASIDE = ['Hide', '1h', '6h', '24h'];

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    const errors = [];
    let active = null;
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://gate.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) {
        const json = active && FETCHED_BY[active] === url.pathname ? payloadFor(active) : {};
        return route.fulfill({ json });
      }
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
      await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript'
        : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://gate.test/');
    // Reads go one by one, so the route above sees each path.
    await page.evaluate(() => { window.batchable = () => false; });

    const texts = (host, sel) => host.locator(sel)
      .evaluateAll(nodes => nodes.map(n => n.textContent.trim()));

    // Every gate poll run against an empty board: each reconciles its own keys
    // away, so the next kind starts from no card at all.
    const clearAll = async () => {
      active = null;
      await page.evaluate(async () => {
        for (const refresh of [refreshRestartGate, refreshGotoGate, refreshReposyncGate,
          refreshSpawnGate, refreshResumeGate, refreshKillGate, refreshMeshJoinGate,
          refreshNudgeGate]) await refresh();
        applyRunGate([], []);
      });
      await page.waitForFunction(() => document.querySelectorAll('.notice.gate').length === 0);
    };

    for (const kind of Object.keys(RECORDS)) {
      await clearAll();
      active = kind;
      await page.evaluate(async ({ kind, record }) => {
        if (kind === 'ask') return applyRunGate([record], []);
        const refresh = { restart: refreshRestartGate, goto: refreshGotoGate,
          reposync: refreshReposyncGate, spawn: refreshSpawnGate, resume: refreshResumeGate,
          kill: refreshKillGate, meshjoin: refreshMeshJoinGate, nudge: refreshNudgeGate };
        await refresh[kind]();
      }, { kind, record: RECORDS[kind]() });
      const card = page.locator('.notice.gate');
      await card.waitFor();
      assert.equal(await page.locator('.notice.gate').count(), 1, `${kind}: one card`);
      // A card above the fold threshold opens folded, and the reason sits in
      // the brief behind the toggle (claunch-qf41a).
      await page.evaluate(() => new Promise(done => {
        for (const t of document.querySelectorAll('.notice .notice-fold-toggle')) t.click();
        requestAnimationFrame(() => requestAnimationFrame(done));
      }));
      const text = await card.innerText();
      assert.ok(text.includes(SAYS[kind]),
        `${kind}: the card on screen is this kind — looked for ${JSON.stringify(SAYS[kind])} ` +
        `in ${JSON.stringify(text.slice(0, 300))}`);
      // The assertion this file was written for: this kind's own builder drew
      // the aside group. A builder that stops calling `gateAsideActions` — or
      // reaches for `gateHideButton` alone — leaves this empty or short here.
      assert.deepEqual(await texts(card, '.gate-actions > .gate-aside button'), ASIDE,
        `${kind}: the card carries Hide and the three reminder waits`);
      // The panel row answers for the same card, and the waits it offers are
      // the card's own, in the card's order (claunch-gnlui).
      const row = await page.evaluate(({ kind, rec }) => {
        const node = noticePanelGateRow(kind, rec);
        return [...node.querySelectorAll('.gate-remind button')].map(b => b.textContent.trim());
      }, { kind, rec: RECORDS[kind]() });
      assert.deepEqual(row, ASIDE.slice(1),
        `${kind}: the panel row offers the card's reminder waits`);
    }

    // A reminder pressed on a kind that is not the spawn gate: the card goes
    // and the wait is filed under that kind and that request. Drawn labels
    // alone would pass a build where the buttons were wired to nothing.
    await clearAll();
    active = 'resume';
    await page.evaluate(async () => refreshResumeGate());
    const resumeCard = page.locator('.notice.gate');
    await resumeCard.waitFor();
    await resumeCard.getByRole('button', { name: '1h', exact: true }).click();
    await resumeCard.waitFor({ state: 'detached' });
    assert.equal(await page.evaluate(() => (gateRemindIds.get('resume:s1') || {}).after), 3600000,
      'the reminder is filed under the kind that drew the card');
    await page.evaluate(() => gateRemindClear('resume', 's1'));
    await clearAll();

    // The list above is every gate kind NOTICE_SOURCES names: a kind added
    // there is uncovered here until it gets a record, a sentence and an aside
    // group — which is what makes this file a per-kind check rather than one
    // more check of the kind someone happened to test.
    const gateKinds = await page.evaluate(() => NOTICE_SOURCES.filter(s => s.gate).map(s => s.id));
    assert.deepEqual(Object.keys(RECORDS).sort(), gateKinds.slice().sort(),
      'every gate kind in NOTICE_SOURCES is drawn here');

    assert.deepEqual(errors, []);
    console.log(`gateaside_browser: ok (${Object.keys(RECORDS).length} gate kinds: card aside group, panel row waits)`);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
