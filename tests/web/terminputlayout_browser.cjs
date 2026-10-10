// The session line's bar (#term-input) lays out as one row: the buttons, the
// one-row field and the note share one height and one bottom edge, the empty
// field stays one row however narrow the bar is (its placeholder does not wrap
// it open), and the field never shows a scrollbar for content it fits
// (autogrowTermInput adds the border scrollHeight leaves out) (claunch-hmz62).
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 600 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => { window.setInterval = () => 0; });
    await page.route('http://bar.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
      const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/', '');
      let content = fs.readFileSync(path.join(root, name));
      if (name === 'app.js') content = content.toString().replace(/boot\(\);\s*$/, '');
      await route.fulfill({ body: content, contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://bar.test/');

    const measure = () => page.evaluate(() => {
      const form = document.getElementById('term-input');
      const box = el => el.getBoundingClientRect();
      const field = document.getElementById('term-input-field');
      const buttons = [...form.children].filter(el => el.classList.contains('term-btn')
        && getComputedStyle(el).display !== 'none');
      const note = document.getElementById('term-input-note');
      return {
        field: { h: box(field).height, w: box(field).width, bottom: box(field).bottom,
                 overflow: field.scrollHeight > field.clientHeight },
        buttons: buttons.map(el => ({ id: el.id, h: box(el).height, bottom: box(el).bottom })),
        note: { h: box(note).height, bottom: box(note).bottom },
      };
    });

    // The bar on its own at the bottom of the page, with the long hold note
    // the screenshot in the issue showed.
    await page.evaluate(() => {
      const form = document.getElementById('term-input');
      document.body.appendChild(form);
      Object.assign(form.style, { position: 'fixed', left: '0', right: '0', bottom: '0', zIndex: '999' });
      form.classList.remove('hidden');
      const note = document.getElementById('term-input-note');
      note.textContent = 'this session has ended — a line sent now is queued and typed when the session is back';
      note.classList.remove('hidden');
      autogrowTermInput(document.getElementById('term-input-field'));
    });

    for (const width of [1280, 760]) {
      await page.setViewportSize({ width, height: 600 });
      await page.evaluate(() => autogrowTermInput(document.getElementById('term-input-field')));
      // TERMINPUT_SHOT=<dir> keeps a picture of the bar at each width
      if (process.env.TERMINPUT_SHOT) {
        await page.locator('#term-input').screenshot({ path: path.join(process.env.TERMINPUT_SHOT, `terminput-${width}.png`) });
      }
      const m = await measure();
      const row = m.buttons[0].h;
      assert.ok(Math.abs(m.field.h - row) < 1, `${width}px: empty field ${m.field.h}px against button ${row}px`);
      assert.equal(m.field.overflow, false, `${width}px: the empty field overflows`);
      // the note gives up width before the field drops under its minimum
      assert.ok(m.field.w >= 250, `${width}px: field squeezed to ${m.field.w}px`);
      for (const b of m.buttons) {
        assert.ok(Math.abs(b.h - row) < 1, `${width}px: ${b.id} is ${b.h}px, not ${row}px`);
        assert.ok(Math.abs(b.bottom - m.field.bottom) < 1, `${width}px: ${b.id} bottom ${b.bottom} vs field ${m.field.bottom}`);
      }
      assert.ok(Math.abs(m.note.h - row) < 1 && Math.abs(m.note.bottom - m.field.bottom) < 1,
        `${width}px: note ${JSON.stringify(m.note)} off the row`);
    }

    // Typed text: one line stays one row without a scrollbar; three lines grow
    // the field and the buttons stay on its bottom edge.
    await page.setViewportSize({ width: 1280, height: 600 });
    const one = await page.evaluate(() => {
      const field = document.getElementById('term-input-field');
      field.value = 'hello';
      autogrowTermInput(field);
      return { h: field.getBoundingClientRect().height, overflow: field.scrollHeight > field.clientHeight };
    });
    assert.equal(one.overflow, false, 'a one-line value overflows its field');
    await page.evaluate(() => {
      const field = document.getElementById('term-input-field');
      field.value = 'one\ntwo\nthree';
      autogrowTermInput(field);
    });
    const three = await measure();
    assert.ok(three.field.h > one.h + 30, `three lines did not grow the field: ${three.field.h}`);
    assert.equal(three.field.overflow, false, 'a three-line value overflows its field');
    for (const b of three.buttons) {
      assert.ok(Math.abs(b.bottom - three.field.bottom) < 1, `${b.id} left the field's bottom edge`);
    }

    assert.deepEqual(errors, []);
    console.log('terminputlayout_browser: ok');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
