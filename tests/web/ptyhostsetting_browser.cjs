/* The Settings card for daemon.pty_host in a real browser (claunch-xnnct.5.1).

   A click focuses the checkbox, and the save redraws the Settings view before
   it sends the PUT. That redraw hands the focused id to refocusSettingsField,
   which called setSelectionRange on whatever came back. A checkbox has that
   method and throws from it, so the save stopped there: no PUT, busy left set,
   the box disabled. ptyhostsetting_check.js runs on stub nodes, which do not
   throw, and passed throughout.

   What is checked: a click sends PUT {enabled: true}, the box comes back
   enabled and ticked, and the page raised nothing; a text field that had focus
   across a redraw keeps it with the caret at the end. */
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
    await page.route('http://ptyhost.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
      await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://ptyhost.test/');
    await page.evaluate(() => {
      window.puts = [];
      window.saved = false;
      const host = () => ({ configured: false, state: 'off', pid: null, sessions: 0 });
      api = async (url, options = {}) => {
        if (url === '/api/pty-host-setting') {
          if (options.method === 'PUT') {
            const body = JSON.parse(options.body);
            puts.push(body);
            saved = body.enabled;
          }
          return { ok: true, status: 200, json: async () => ({ enabled: saved, supported: true, host: host() }) };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      };
      ptyHostSetting.loaded = true;
      ptyHostSetting.host = host();
      wsOpen = true;
      wsSection = '';
      showView('settings');
      renderWorkspaces();
    });

    const box = page.locator('#pty-host-enabled');
    assert.equal(await box.isDisabled(), false, 'the box starts enabled');
    assert.equal(await box.isChecked(), false);
    await box.click();
    await page.waitForFunction(() => puts.length === 1 && !ptyHostSetting.busy, null, { timeout: 5000 });
    assert.deepEqual(await page.evaluate(() => puts), [{ enabled: true }]);
    assert.equal(await page.locator('#pty-host-enabled').isDisabled(), false, 'the box is enabled after the save');
    assert.equal(await page.locator('#pty-host-enabled').isChecked(), true, 'the box shows the saved value');
    assert.deepEqual(errors, [], 'the click raised nothing');

    // A field with a caret still gets focus and the caret back at the end.
    const caret = await page.evaluate(() => {
      const field = document.createElement('input');
      field.type = 'text';
      field.id = 'pty-host-probe-field';
      field.value = 'abcdef';
      document.body.appendChild(field);
      field.focus();
      field.setSelectionRange(1, 1);
      refocusSettingsField('pty-host-probe-field');
      return { focused: document.activeElement === field, start: field.selectionStart, end: field.selectionEnd };
    });
    assert.deepEqual(caret, { focused: true, start: 6, end: 6 });
    assert.deepEqual(errors, []);
    console.log('ok ptyhostsetting_browser');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
