/* The create form's Prompt packs row in a real browser: its layout is fixed
   (claunch-glntg). Ticking a chip, a role locking one and a longer summary
   must not move any chip, and the "The opening will include" block keeps its
   own place under the chips -- opening a pack's text below it moves neither.
   And no text is cut: every title, badge and summary pill shows whole, so
   nobody has to hover for a tooltip (claunch-uvm3w). Positions and widths
   are measured, not inferred from the DOM. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

const ROLES = ['analyst', 'free', 'leader', 'operator', 'pm', 'reviewer', 'specialist', 'worker'];
const OFFER = {
  workspace: { name: 'repo', path: 'F:/repo' },
  packs: [{ id: 'beads', title: 'Beads board (br · bv)' }, { id: 'common', title: '공통 지침' }],
  default_packs: ['common'],
  role_packs: { leader: ['beads', 'role-leader', 'role-specialist'], worker: ['role-worker'] },
  shipped: [
    { id: 'beads', title: 'Beads board' }, { id: 'common', title: 'Common' },
    { id: 'unreal-operator', title: 'Unreal — operator' },
    ...ROLES.map((r) => ({ id: `role-${r}`, title: `Role — ${r}` })),
  ],
  daemon_prompt: { id: '@daemon', title: 'Daemon prompt (default)' },
};

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = []; page.on('pageerror', (e) => errors.push(e.message));
    await page.addInitScript(() => { window.setInterval = () => 0; });
    await page.route('http://packs.test/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/boot\(\);\s*$/, '');
      await route.fulfill({ body: content,
        contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://packs.test/');
    await page.evaluate(async (offer) => {
      sessionsCache = [];
      for (const name of ['refreshWorkspaces', 'refreshNewWorktree', 'refreshWorkflowChoices', 'refreshRoles',
        'refreshSpawnPolicy', 'refreshSessionConnect', 'syncSpawnMode', 'syncOnboardPickers', 'refreshIssueChoices',
        'syncForkAvailability', 'refreshSessKids', 'refreshPromptPackOffer']) window[name] = () => {};
      refreshSessions = async () => {};
      sessMeshes = () => [{ mesh: 'team' }];
      api = async (url) => ({ ok: true, json: async () => (
        url.startsWith('/api/prompt-packs/text')
          ? { id: 'common', title: 'Common', body: '# Rules\n' + 'be kind\n'.repeat(30), source: 'workspace', status: 'current', lines: 31 }
          : {}) });
      const f = document.getElementById('new-session');
      f.profile.add(new Option('work', 'work')); f.harness.add(new Option('claude', 'claude'));
      f.mesh.add(new Option('team', 'team')); f.mesh.value = 'team';
      f.role.add(new Option('none', '')); f.role.add(new Option('leader', 'leader'));
      f.workflow.add(new Option('none', '')); f.cwd.add(new Option('repo', 'F:/repo'));
      showView('new');
      await openNewSession();
      newPackOffer = offer; newPackOfferFor = 'F:/repo';
      renderPromptPackRow();
    }, OFFER);
    assert.equal(await page.locator('#new-packs-row').isVisible(), true);

    // Every chip's box and the summary block's, keyed by the chip's input.
    const measure = () => page.evaluate(() => {
      const r = (n) => { const b = n.getBoundingClientRect(); return [b.x, b.y, b.width, b.height].map(Math.round); };
      const chips = {};
      for (const c of document.querySelectorAll('#new-packs-row .pack-chip')) {
        chips[c.querySelector('input').name] = r(c);
      }
      const summary = document.querySelector('#new-packs-row .new-packs-summary');
      const groups = document.querySelector('#new-packs-row .pack-groups');
      return { chips, summary: r(summary), groups: r(groups) };
    });
    // Every piece of chip and summary text, and whether it shows whole.
    const clipped = () => page.evaluate(() => {
      const out = [];
      const sel = '#new-packs-row .pack-chip-title, #new-packs-row .pack-chip-badge, ' +
        '#new-packs-row .pack-chip-lock, #new-packs-row .pack-chip-mini, #new-packs-row .pack-chip';
      for (const n of document.querySelectorAll(sel)) {
        if (n.scrollWidth > n.clientWidth + 1 || getComputedStyle(n).textOverflow === 'ellipsis') {
          out.push(`${n.className}: ${n.textContent.trim()} (${n.scrollWidth} > ${n.clientWidth})`);
        }
      }
      const row = document.querySelector('#new-packs-row').getBoundingClientRect();
      for (const c of document.querySelectorAll('#new-packs-row .pack-chip')) {
        if (c.getBoundingClientRect().right > row.right + 1) out.push(`outside the row: ${c.textContent.trim()}`);
      }
      return out;
    });
    const tick = (name) => page.evaluate((n) => {
      document.querySelector(`#new-packs-row input[name="${n}"]`).click();
    }, name);

    const base = await measure();
    // The summary is its own block under the chips, as wide as they are.
    assert.ok(base.summary[1] >= base.groups[1] + base.groups[3], 'summary under the chips');
    assert.equal(base.summary[0], base.groups[0]);
    assert.equal(base.summary[2], base.groups[2]);
    const general = ['daemon_prompt', 'prompt_pack_beads', 'prompt_pack_common', 'prompt_pack_unreal-operator'];
    assert.deepEqual(await clipped(), []);

    // Ticking and unticking: nothing moves, the summary keeps its box.
    await tick('prompt_pack_beads');
    await tick('prompt_pack_unreal-operator');
    let now = await measure();
    assert.deepEqual(now.chips, base.chips);
    assert.deepEqual(now.summary, base.summary);
    assert.match(await page.locator('#new-packs-row .new-packs-summary').innerText(), /beads \(ticked\)/);
    await tick('prompt_pack_beads');
    await tick('prompt_pack_unreal-operator');
    await tick('prompt_pack_common');
    await tick('daemon_prompt');
    now = await measure();
    assert.deepEqual(now.chips, base.chips);
    assert.deepEqual(now.summary, base.summary, 'an empty summary keeps its height');
    await tick('prompt_pack_common');
    await tick('daemon_prompt');

    // A role locks beads (a wider badge) and brings in its role chip: the
    // General chips keep their boxes.
    await page.evaluate(() => {
      const f = document.getElementById('new-session');
      f.role.value = 'leader';
      renderPromptPackRow();
    });
    now = await measure();
    for (const n of general) assert.deepEqual(now.chips[n], base.chips[n], n);
    assert.ok(now.chips['prompt_pack_role-leader'], 'the role chip is shown');
    assert.ok(now.chips['prompt_pack_role-specialist'], 'the role chip is shown');
    // Locked chips show whole too; the role is named in the summary.
    assert.deepEqual(await clipped(), []);
    assert.equal(await page.locator('#new-packs-row .pack-chip.locked .pack-chip-lock').first().innerText(), '🔒');
    assert.match(await page.locator('#new-packs-row .new-packs-summary').innerText(),
      /role-specialist \(role leader, claunch's copy\)/);
    // Every role pack unfolded: still nothing cut.
    await page.locator('#new-packs-row .pack-chip-more').click();
    assert.deepEqual(await clipped(), []);
    await page.locator('#new-packs-row .pack-chip-more').click();

    // Opening a pack's text: the chips and the summary stay put, the panel
    // goes under the summary.
    const before = await measure();
    await page.evaluate(() => togglePackPreview('common', false));
    await page.locator('#new-packs-row .pack-preview-body').waitFor();
    now = await measure();
    assert.deepEqual(now.summary, before.summary);
    for (const n of general) assert.deepEqual(now.chips[n].slice(0, 2), before.chips[n].slice(0, 2), n);
    const panel = await page.locator('#new-packs-row .pack-preview').boundingBox();
    assert.ok(panel.y >= now.summary[1] + now.summary[3], 'preview under the summary');

    // A narrow window: chips wrap to more lines, still nothing is cut.
    await page.setViewportSize({ width: 700, height: 900 });
    assert.deepEqual(await clipped(), []);

    assert.deepEqual(errors, []);
    console.log('promptpacks browser: chips stay put through ticks and role locks, no text is cut; the opening block stays put');
  } finally { await browser.close(); }
})().catch((e) => { console.error(e); process.exitCode = 1; });
