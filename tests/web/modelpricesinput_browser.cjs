/* Typing into the Settings ▸ Model prices card while the page redraws, in a
   real browser (claunch-jmuvv).

   While a RAG index syncs, refreshRagStatus polls every RAG_POLL_MS (3 s) and
   used to redraw the whole Settings view each time. The add row's model id
   lived only in the DOM, so every poll emptied it; the rate cells had no id,
   so refocusSettingsField could not hand them focus back. The card looked
   like it kept resetting under the fingers.

   What is checked:
   - a full redraw keeps the add row's typed model id and picked provider,
     and gives the field focus back with the caret where it was;
   - a rate cell keeps focus and caret across a full redraw;
   - the RAG status poll redraws only the RAG card: the Model prices inputs
     are the same nodes after it, still focused;
   - Add model clears the remembered id once the row is added. */
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
    await page.addInitScript(() => {
      window.setInterval = () => 0;
      // The RAG poll re-arms itself with setTimeout; the check drives each
      // poll by hand instead.
      const realTimeout = window.setTimeout;
      window.setTimeout = (fn, ms, ...rest) => (ms >= 1000 ? 0 : realTimeout(fn, ms, ...rest));
    });
    await page.route('http://mpinput.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
      await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://mpinput.test/');
    await page.evaluate(() => {
      window.ragReads = 0;
      const syncing = () => ({
        configured: true,
        indexes: [{ name: 'sessions', syncing: true, pending: 5 + ragReads }],
        queue: { depth: 1, pending: ['sessions'] },
      });
      api = async (url) => {
        if (url === '/api/rag/status') {
          ragReads += 1;
          return { ok: true, status: 200, json: async () => syncing() };
        }
        if (url.startsWith('/api/rag/dbs')) {
          return { ok: true, status: 200, json: async () => ({ dbs: [] }) };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      };
      modelPrices = {
        currency: '$', any: '*', profiles: { work: 'anthropic' },
        prices: { '*': { 'accounts/fireworks/models/x': { input: 0.3, cache_read: 0.006, output: 1.2 } } },
      };
      modelPricesDraft = null;
      ragStatus = syncing();
      wsOpen = true;
      wsSection = '';
      showView('settings');
      renderWorkspaces();
    });

    // The add row: pick a provider, type part of an id, caret mid-word.
    await page.selectOption('#mp-add-provider', 'anthropic');
    await page.click('#mp-add-model');
    await page.keyboard.type('claude-opus');
    await page.evaluate(() => $('mp-add-model').setSelectionRange(6, 6));
    await page.evaluate(() => renderWorkspaces());
    assert.deepEqual(await page.evaluate(() => {
      const f = $('mp-add-model');
      return {
        value: f.value, provider: $('mp-add-provider').value,
        focused: document.activeElement === f, start: f.selectionStart, end: f.selectionEnd,
      };
    }), { value: 'claude-opus', provider: 'anthropic', focused: true, start: 6, end: 6 },
    'a full redraw keeps the typed id, the provider, focus and caret');
    // The next keystroke lands where the caret was.
    await page.keyboard.type('X');
    assert.equal(await page.evaluate(() => $('mp-add-model').value), 'claudeX-opus');

    // A rate cell across a full redraw.
    const rateId = await page.evaluate(() => modelPriceRateId('*', 'accounts/fireworks/models/x', 'output'));
    await page.evaluate((id) => {
      const f = $(id);
      f.focus();
      f.setSelectionRange(1, 1);
    }, rateId);
    await page.keyboard.type('5');
    await page.evaluate(() => renderWorkspaces());
    assert.deepEqual(await page.evaluate((id) => {
      const f = $(id);
      return { value: f.value, focused: document.activeElement === f, start: f.selectionStart };
    }, rateId), { value: '15.2', focused: true, start: 2 },
    'a rate cell keeps its draft value, focus and caret');

    // The RAG poll swaps its own card only.
    await page.click('#mp-add-model');
    await page.evaluate(() => {
      window.mpNode = $('mp-add-model');
      window.ragNode = document.querySelector('.rag-card');
    });
    const before = await page.evaluate(() => ragReads);
    await page.evaluate(() => refreshRagStatus());
    assert.deepEqual(await page.evaluate((n) => ({
      read: ragReads > n,
      same: $('mp-add-model') === mpNode,
      focused: document.activeElement === mpNode,
      ragSwapped: document.querySelector('.rag-card') !== ragNode && !!document.querySelector('.rag-card'),
      cards: document.querySelectorAll('.rag-card').length,
    }), before), { read: true, same: true, focused: true, ragSwapped: true, cards: 1 },
    'the RAG status poll redraws only the RAG card');

    // Adding the row forgets the typed id.
    await page.click('text=Add model');
    assert.deepEqual(await page.evaluate(() => ({
      value: $('mp-add-model').value,
      added: Object.keys((modelPricesDraft.prices.anthropic) || {}),
    })), { value: '', added: ['claudeX-opus'] });

    assert.deepEqual(errors, [], 'the page raised nothing');
    console.log('ok modelpricesinput_browser');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
