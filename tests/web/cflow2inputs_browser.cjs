/* The v2 start-inputs box in the create/spawn form: one line per input, two
   to a row while the form is wide, and each input's help folded behind its
   own "?". A workflow declares a paragraph of help per input, and spent
   inline it made this box the tallest part of the form (claunch-npfyd).

   Measured against the shipped app.js and style.css: this is geometry, so it
   needs a real browser (the same gate as the other *_browser.cjs harnesses). */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

/* The start inputs of improv-worker-v2, cut down to one of each shape: a wide
   textarea, a one-line string, a checkbox, an enum, and the combobox rows
   whose `options` list arrives from the daemon. */
const FIELDS = [
  { name: 'goal', type: 'text', label: 'Goal', required: true,
    help: 'This round\u2019s goal.' },
  { name: 'issue', type: 'string', label: 'Issue', default: '' },
  { name: 'force_pr', type: 'bool', label: 'PR only',
    help: 'Land only as a pull request.' },
  { name: 'force_no_pr', type: 'bool', label: 'No pull request',
    help: 'Only the existing local landing is used; the request explains why.' },
  { name: 'relation', type: 'enum', choices: ['stacked', 'independent'],
    default: 'stacked', label: 'Relation to parent',
    help: 'stacked works on the parent\u2019s stack branch; independent works on master.' },
  { name: 'reviewer_profile', type: 'string', label: 'Reviewer profile',
    placeholder: 'e.g. work', default: '',
    help: 'The profile of the child reviewer. Empty inherits this session\u2019s.',
    options: { from: 'profiles', empty: 'session' } },
  { name: 'reviewer_harness', type: 'string', label: 'Reviewer harness',
    placeholder: 'e.g. codex', default: '',
    help: 'The harness of the child reviewer. Empty uses the profile\u2019s own.',
    options: { from: 'harnesses', of: 'reviewer_profile' } },
  { name: 'session_routing', type: 'bool', default: false,
    label: 'Difficulty-based session routing',
    help: 'Each item is rated and easy ones are implemented by a forked child.' },
  { name: 'delegate_profile', type: 'string', label: 'Delegate child profile',
    placeholder: 'e.g. ds4', default: '',
    help: 'The profile of the delegated child. Empty uses the daemon setting.',
    options: { from: 'profiles', empty: 'delegate' } },
  { name: 'delegate_model', type: 'string', label: 'Delegate child model preset',
    placeholder: 'e.g. small', default: '',
    help: 'The model of the delegated child, as its profile\u2019s harness accepts it.',
    options: { from: 'models', of: 'delegate_profile' } },
];

/* What POST /api/cflow2/inputs/check would answer for those rows. */
const OPTIONS = {
  reviewer_profile: { from: 'profiles', profile: null,
    items: ['codex', 'devin', 'ds4', 'ds4-1m', 'glm-flash', 'glm-flash-1m', 'kimi', 'kimi-small', 'kimyori', 'nc', 'sr'] },
  reviewer_harness: { from: 'harnesses', profile: 'ds4',
    items: ['claude', 'agent', 'codex', 'devin', 'kimi', 'pi'] },
  delegate_profile: { from: 'profiles', profile: null,
    items: ['codex', 'devin', 'ds4', 'kimi', 'nc', 'sr'] },
  delegate_model: { from: 'models', profile: 'ds4',
    items: ['default', 'small', 'large', 'xlarge', 'subagent', 'haiku', 'sonnet', 'opus', 'fable'] },
};

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.addInitScript(() => { window.setInterval = () => 0; });
    await page.route('http://session.test/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
      await route.fulfill({
        body: content,
        contentType: name.endsWith('.js') ? 'text/javascript'
          : name.endsWith('.css') ? 'text/css' : 'text/html',
      });
    });
    await page.goto('http://session.test/');
    await page.evaluate(() => {
      sessionsCache = [{ name: 'lead', status: 'idle', harness: 'claude',
                         conversation_id: 'c1', cwd: 'F:/repo' }];
      for (const name of ['refreshWorkspaces', 'refreshNewWorktree', 'refreshWorkflowChoices',
        'refreshRoles', 'refreshSpawnPolicy', 'refreshSessionConnect', 'syncSpawnMode',
        'syncOnboardPickers', 'refreshIssueChoices', 'syncForkAvailability', 'refreshSessKids']) {
        window[name] = () => {};
      }
      refreshSessions = async () => {};
      sessMeshes = () => [];
      sessCflowRun = () => null;
      meshCache = [];
      spawnRecall = () => ({});
      const f = document.getElementById('new-session');
      f.profile.add(new Option('ds4', 'ds4'));
      f.harness.add(new Option('claude', 'claude'));
      f.mesh.add(new Option('none', ''));
      f.role.add(new Option('none', ''));
      f.workflow.add(new Option('none', ''));
      f.cwd.add(new Option('repo', 'F:/repo'));
      api = async () => ({ ok: true, json: async () => ({}) });
      showView('new');
      return openNewSession();
    });
    // Draw the box the way syncCflowInputs does, on the visible form.
    await page.evaluate(([fields, options]) => {
      const box = document.getElementById('new-inputs');
      box.innerHTML = '';
      box.classList.remove('hidden');
      cflowInputsForm(box, fields, [
        { assert: 'force_pr && force_no_pr', message: 'PR only and No pull request are exclusive' },
      ]);
      cflowInputsOptions(box, options);
    }, [FIELDS, OPTIONS]);

    const rowOf = (name) => page.locator(`#new-inputs .cf2-input[data-input="${name}"]`);

    /* One measurement pass at the current viewport. */
    async function measure() {
      return page.locator('#new-inputs').evaluate((box) => {
        const rect = (e) => {
          const r = e.getBoundingClientRect();
          return { x: r.x, y: r.y, w: r.width, h: r.height, right: r.right, bottom: r.bottom };
        };
        const row = (name) => box.querySelector(`.cf2-input[data-input="${name}"]`);
        const help = (name) => row(name).querySelector('.cf2-input-help');
        const note = (name) => row(name).querySelector('.cf2-input-options');
        return {
          columns: getComputedStyle(box).gridTemplateColumns.split(/\s+/).filter(Boolean).length,
          overflow: box.scrollWidth > box.clientWidth + 1,
          height: rect(box).h,
          issue: rect(row('issue')),
          forcePr: rect(row('force_pr')),
          profileRow: rect(row('reviewer_profile')),
          harnessRow: rect(row('reviewer_harness')),
          profileLabel: rect(row('reviewer_profile').querySelector('.cf2-input-label')),
          profileControl: rect(row('reviewer_profile').querySelector('.cf2-input-control')),
          checkbox: rect(row('force_pr').querySelector('input')),
          helpDisplay: getComputedStyle(help('reviewer_profile')).display,
          helpRect: rect(help('reviewer_profile')),
          note: (() => {
            const n = note('reviewer_profile');
            const r = rect(n);
            const cs = getComputedStyle(n);
            return { x: r.x, y: r.y, w: r.width, title: n.title, text: n.textContent,
                     nowrap: cs.whiteSpace, ellipsis: cs.textOverflow,
                     clipped: n.scrollWidth > n.clientWidth + 1 };
          })(),
          toggle: (() => {
            const b = row('reviewer_profile').querySelector('.cf2-input-help-toggle');
            const r = rect(b);
            return { exists: !!b, x: r.x, y: r.y, w: r.w, expanded: b.getAttribute('aria-expanded') };
          })(),
          toggleFree: !!row('issue').querySelector('.cf2-input-help-toggle'),
        };
      });
    }

    // ---- wide: two inputs to a row -------------------------------------
    await page.setViewportSize({ width: 1440, height: 900 });
    const wide = await measure();
    assert.equal(wide.columns, 2, 'a wide form lays the inputs out two to a row');
    assert.ok(Math.abs(wide.profileRow.y - wide.harnessRow.y) < 1,
      `neighbouring inputs share a row (${wide.profileRow.y} vs ${wide.harnessRow.y})`);
    assert.ok(wide.harnessRow.x > wide.profileRow.x + wide.profileRow.w / 2,
      'the second input is to the right of the first');
    assert.ok(Math.abs(wide.issue.y - wide.forcePr.y) < 1,
      'a one-line input and a checkbox share a row');
    assert.equal(wide.overflow, false, 'the box does not overflow its container');

    // ---- one line per input: the caption, then the control --------------
    assert.ok(Math.abs(wide.profileLabel.y + wide.profileLabel.h / 2
                       - (wide.profileControl.y + wide.profileControl.h / 2)) < 8,
      'a one-line input keeps its caption beside the control');
    assert.ok(wide.profileControl.x > wide.profileLabel.right,
      'the control starts after its caption');
    assert.ok(Math.abs(wide.checkbox.x - wide.forcePr.x) < 2,
      'the checkbox aligns with the left edge of its row');

    // ---- help is folded until asked for ---------------------------------
    assert.equal(wide.helpDisplay, 'none', 'help is folded on arrival');
    assert.equal(wide.toggle.expanded, 'false', 'the toggle says it is closed');
    assert.ok(wide.toggle.exists && wide.toggle.w >= 14,
      'a field with help carries a visible toggle');
    assert.equal(wide.toggleFree, false, 'a field without help has no toggle');

    await rowOf('reviewer_profile').locator('.cf2-input-help-toggle').click();
    const openedHelp = await page.locator('#new-inputs').evaluate((box) => {
      const row = box.querySelector('.cf2-input[data-input="reviewer_profile"]');
      const h = row.querySelector('.cf2-input-help');
      const b = row.querySelector('.cf2-input-help-toggle');
      return { display: getComputedStyle(h).display, expanded: b.getAttribute('aria-expanded'),
               text: h.textContent.includes('child reviewer') };
    });
    assert.equal(openedHelp.display, 'block', 'the toggle unfolds that row\u2019s help');
    assert.equal(openedHelp.expanded, 'true', 'the toggle records that it is open');
    assert.ok(openedHelp.text, 'the unfolded paragraph is the declared help');
    await rowOf('reviewer_profile').locator('.cf2-input-help-toggle').click();
    assert.equal((await measure()).helpDisplay, 'none', 'a second press folds it again');

    // ---- folding the help is what makes the box short -------------------
    const folded = (await measure()).height;
    await page.evaluate(() => {
      for (const row of document.querySelectorAll('#new-inputs .cf2-input')) {
        row.classList.add('help-open');
      }
    });
    const unfolded = (await measure()).height;
    assert.ok(unfolded - folded >= 80,
      `folded help saves height (${folded} vs ${unfolded})`);
    // The layout the box replaced stacked every input, help and all: ten
    // inputs measured ~1150px at this width. A block layout would cross this
    // bound again without failing anything else here.
    assert.ok(folded < 600, `the folded box stays short (${folded}px)`);
    await page.evaluate(() => {
      for (const row of document.querySelectorAll('#new-inputs .cf2-input')) {
        row.classList.remove('help-open');
      }
    });

    // ---- the `options` list is one line under its own control ----------
    assert.equal(wide.note.nowrap, 'nowrap', 'the options list is one line');
    assert.equal(wide.note.ellipsis, 'ellipsis', 'a longer list ends in an ellipsis');
    assert.ok(Math.abs(wide.note.x - wide.profileControl.x) < 2,
      'the list hangs under its control');
    assert.equal(wide.note.title, wide.note.text, 'the title carries the whole list');

    // ---- 768: still two rows, still inside the box ---------------------
    await page.setViewportSize({ width: 768, height: 900 });
    const mid = await measure();
    assert.equal(mid.columns, 2, 'a 768px form still pairs the inputs');
    assert.equal(mid.overflow, false, '768: no horizontal overflow');

    // ---- narrow: one input to a row, caption above the control ---------
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      const narrow = await measure();
      assert.equal(narrow.columns, 1, `${width}: one input to a row`);
      assert.equal(narrow.overflow, false, `${width}: no horizontal overflow`);
      assert.ok(narrow.forcePr.y > narrow.issue.bottom - 1,
        `${width}: the next input starts below the previous one`);
      assert.ok(narrow.profileControl.y >= narrow.profileLabel.bottom - 1,
        `${width}: the control sits below its caption`);
      assert.ok(Math.abs(narrow.checkbox.x - narrow.forcePr.x) < 2,
        `${width}: the checkbox aligns with the left edge of its row`);
      assert.equal(narrow.helpDisplay, 'none', `${width}: help stays folded`);
    }

    assert.deepEqual(errors, [], `the page ran without errors: ${errors.join('; ')}`);
    console.log('cflow2inputs_browser: ok');
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
