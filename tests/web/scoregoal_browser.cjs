const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => { window.setInterval = () => 0; });
    await page.route('http://score.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/boot\(\);\s*$/, '');
      await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://score.test/');
    await page.evaluate(() => {
      window.requests = [];
      window.defaultScoreGoal = false;
      api = async (url, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : null;
        if (options.method) requests.push({ url, body });
        if (url === '/api/score-goal/defaults') {
          if (options.method === 'PUT') defaultScoreGoal = body.enabled;
          return { ok: true, json: async () => ({ enabled: defaultScoreGoal }) };
        }
        if (url.endsWith('/score-goal')) return { ok: true, json: async () => ({ enabled: true, score: body.score, active: body.score < 10 }) };
        if (options.method === 'POST') return { ok: false, status: 400, json: async () => ({ error: 'test refusal' }) };
        return { ok: true, json: async () => ({}) };
      };
      sessionsCache = [{ name: 'rated', status: 'idle', score_goal: true, user_score: 0, harness: 'claude', cwd: 'F:/repo' },
        { name: 'other', status: 'idle' }];
      for (const name of ['refreshWorkspaces', 'refreshNewWorktree', 'refreshWorkflowChoices', 'refreshRoles',
        'refreshSpawnPolicy', 'refreshSessionConnect', 'syncSpawnMode', 'syncOnboardPickers', 'refreshIssueChoices',
        'syncForkAvailability', 'refreshSessKids']) window[name] = () => {};
      refreshSessions = async () => {};
      const form = $('new-session');
      form.profile.add(new Option('work', 'work')); form.harness.add(new Option('claude', 'claude'));
      form.cwd.add(new Option('repo', 'F:/repo'));
      showView('new');
      return openNewSession();
    });
    const option = page.locator('[name=score_goal]');
    assert.equal(await option.isChecked(), false);
    await option.check();
    await page.evaluate(() => $('new-session').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
    await page.waitForFunction(() => requests.some(r => r.url === '/api/sessions'));
    assert.equal(await page.evaluate(() => requests.find(r => r.url === '/api/sessions').body.score_goal), true);
    await page.evaluate(() => { $('ws-view').appendChild(scoreGoalSettingsCard()); showView('settings'); });
    await page.locator('.score-goal-settings input').check();
    await page.waitForFunction(() => defaultScoreGoal);
    await page.evaluate(() => openSpawnModal('rated'));
    await page.waitForFunction(() => $('new-session').score_goal.checked);
    await option.uncheck();
    await page.evaluate(() => $('new-session').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
    await page.waitForFunction(() => requests.some(r => r.url.includes('/children')));
    assert.equal(await page.evaluate(() => requests.find(r => r.url.includes('/children')).body.score_goal), false);
    await page.evaluate(() => {
      sessionModalClose({ route: false });
      currentName = 'rated'; sessionEnded = false;
      showView('terminal');
      $('term-input').classList.remove('hidden');
      renderScoreGoal();
    });
    assert.equal(await page.locator('#term-score-goal').isVisible(), true);
    await page.locator('#term-input-field').fill('preserve this draft');
    await page.locator('#term-score-value').fill('11');
    await page.locator('#term-score-save').click();
    assert.match(await page.locator('#term-score-note').innerText(), /0 to 10/);
    await page.locator('#term-score-value').fill('7.5');
    await page.locator('#term-score-value').press('Enter');
    await page.waitForFunction(() => sessionsCache[0].user_score === 7.5);
    assert.equal(await page.locator('#term-input-field').inputValue(), 'preserve this draft');
    await page.locator('#term-score-value').fill('10');
    await page.locator('#term-score-save').click();
    await page.waitForFunction(() => sessionsCache[0].user_score === 10);
    assert.match(await page.locator('#term-score-note').innerText(), /reminders off/);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.locator('#term-score-save').isVisible(), true);
    const bounds = await page.locator('#term-score-goal').boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390);
    await page.evaluate(() => { currentName = 'other'; renderScoreGoal(); });
    assert.equal(await page.locator('#term-score-goal').isVisible(), false);
    assert.deepEqual(errors, []);
    console.log('score goal: defaults, new/spawn payloads, rating, draft preservation, completion and mobile layout passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
