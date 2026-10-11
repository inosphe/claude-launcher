// The session line's bar (#term-input) lays out as one row: the buttons, the
// one-row field and the note share one height and one bottom edge, the empty
// field stays one row however narrow the bar is (its placeholder does not wrap
// it open), and the field never shows a scrollbar for content it fits
// (autogrowTermInput adds the border scrollHeight leaves out) (claunch-hmz62).
// At rest the field is one row; clicked, it opens upward over the terminal
// without changing the bar's height, and closes again on blur
// (claunch-hmz62.2).
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
        barH: box(form).height, barTop: box(form).top,
        field: { h: box(field).height, w: box(field).width, top: box(field).top, bottom: box(field).bottom,
                 overflow: field.scrollHeight > field.clientHeight },
        buttons: buttons.map(el => ({ id: el.id, h: box(el).height, bottom: box(el).bottom })),
        note: { h: box(note).height, w: box(note).width, bottom: box(note).bottom },
        bar: { scroll: form.scrollWidth, client: form.clientWidth },
        sendRight: box(document.getElementById('term-input-send')).right,
        viewport: window.innerWidth,
      };
    });

    // The bar on its own at the bottom of the page, with the long hold note
    // the screenshot in the issue showed.
    await page.evaluate(() => {
      const form = document.getElementById('term-input');
      document.body.appendChild(form);
      Object.assign(form.style, { position: 'fixed', left: '0', right: '0', bottom: '0' });
      form.classList.remove('hidden');
      const note = document.getElementById('term-input-note');
      note.textContent = 'this session has ended — a line sent now is queued and typed when the session is back';
      note.classList.remove('hidden');
      autogrowTermInput(document.getElementById('term-input-field'));
    });

    for (const width of [1280, 760, 520, 380]) {
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
      // nothing on the bar runs past its edge: the send button stays on screen
      assert.ok(m.bar.scroll <= m.bar.client, `${width}px: bar overflows ${m.bar.scroll} > ${m.bar.client}`);
      assert.ok(m.sendRight <= m.viewport, `${width}px: send button right ${m.sendRight} > ${m.viewport}`);
      // the note gives up width before the field drops under its minimum
      if (width >= 760) assert.ok(m.field.w >= 250, `${width}px: field squeezed to ${m.field.w}px`);
      for (const b of m.buttons) {
        assert.ok(Math.abs(b.h - row) < 1, `${width}px: ${b.id} is ${b.h}px, not ${row}px`);
        assert.ok(Math.abs(b.bottom - m.field.bottom) < 1, `${width}px: ${b.id} bottom ${b.bottom} vs field ${m.field.bottom}`);
      }
      if (width > 700) {
        assert.ok(Math.abs(m.note.h - row) < 1 && Math.abs(m.note.bottom - m.field.bottom) < 1,
          `${width}px: note ${JSON.stringify(m.note)} off the row`);
      } else {
        // a phone puts the note on its own row under the bar's controls
        assert.ok(m.note.bottom - m.note.h >= m.field.bottom - 1, `${width}px: note not under the field`);
        assert.ok(m.note.w >= m.bar.client - 30, `${width}px: note row only ${m.note.w}px wide`);
        assert.ok(m.field.w >= 40, `${width}px: field squeezed to ${m.field.w}px`);
      }
    }

    // Typed text, at rest: one line is one row without a scrollbar, and so
    // are three -- the field stays one row until it is clicked
    // (claunch-hmz62.2).
    await page.setViewportSize({ width: 1280, height: 600 });
    const type = text => page.evaluate(value => {
      const field = document.getElementById('term-input-field');
      field.value = value;
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }, text);
    await type('hello');
    const rest = await measure();
    const row = rest.buttons[0].h;
    assert.equal(rest.field.overflow, false, 'a one-line value overflows its field');
    await type('one\ntwo\nthree');
    const restThree = await measure();
    assert.ok(Math.abs(restThree.field.h - row) < 1, `three lines at rest opened the field to ${restThree.field.h}px`);
    assert.equal(restThree.barH, rest.barH, 'three lines at rest changed the bar height');
    assert.equal(await page.evaluate(() => getComputedStyle(document.getElementById('term-input-field')).overflowY),
      'hidden', 'a field at rest shows a scrollbar');

    // Clicked: the field opens upward to its content, over the terminal; the
    // bar keeps its height (the terminal above is not resized) and the
    // buttons stay on the field's bottom edge.
    await page.click('#term-input-field');
    const open = await measure();
    assert.ok(open.field.h > row + 30, `three lines did not open the field: ${open.field.h}px`);
    assert.equal(open.field.overflow, false, 'an opened three-line field overflows');
    assert.equal(open.barH, rest.barH, `opening the field changed the bar height ${rest.barH} -> ${open.barH}`);
    assert.ok(open.field.top < open.barTop, 'the opened field did not rise above the bar');
    for (const b of open.buttons) {
      assert.ok(Math.abs(b.bottom - open.field.bottom) < 1, `${b.id} left the field's bottom edge`);
    }
    // What is on screen where the field rose is the field, over a layer
    // standing in for xterm's (they stack up to z-index 10) inside the real
    // #terminal, which holds them (isolation) -- and a page-wide overlay
    // (#auth-overlay, z-index 10) still covers the field.
    const hits = await page.evaluate(top => {
      const form = document.getElementById('term-input');
      const terminal = document.getElementById('terminal');
      document.body.insertBefore(terminal, form);
      // absolute, not fixed: a fixed box is a stacking context of its own and
      // would hold the layer without the stylesheet's isolation
      Object.assign(terminal.style, { position: 'absolute', left: '0', right: '0', top: '0',
        height: `${form.getBoundingClientRect().top}px`, padding: '0' });
      const layer = document.createElement('div');
      Object.assign(layer.style, { position: 'absolute', inset: '0', zIndex: '10', background: '#123' });
      terminal.appendChild(layer);
      const field = document.getElementById('term-input-field');
      const r = field.getBoundingClientRect();
      const over = document.elementFromPoint(r.left + 20, top + 5) === field;
      const auth = document.getElementById('auth-overlay');
      auth.classList.remove('hidden');
      const mid = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      const covered = mid !== field && auth.contains(mid);
      auth.classList.add('hidden');
      layer.remove();
      return { over, covered };
    }, open.field.top);
    assert.equal(hits.over, true, 'the opened field is covered by the terminal');
    assert.equal(hits.covered, true, 'the auth overlay does not cover the opened field');

    // Clicked while empty: three lines of room to type in.
    await type('');
    const openEmpty = await measure();
    assert.ok(openEmpty.field.h > row + 30, `an empty clicked field stayed ${openEmpty.field.h}px`);
    assert.equal(openEmpty.barH, rest.barH, 'an empty opened field changed the bar height');

    // Many lines: capped at 30vh, the rest scrolls.
    await type(Array.from({ length: 40 }, (_, i) => `line ${i}`).join('\n'));
    const tall = await measure();
    assert.ok(tall.field.h <= 600 * 0.3 + 1, `the field passed its ceiling: ${tall.field.h}px`);

    // Blurred: back to one row, its margin cleared.
    await page.evaluate(() => document.getElementById('term-input-field').blur());
    const back = await measure();
    assert.ok(Math.abs(back.field.h - row) < 1, `blur left the field at ${back.field.h}px`);
    assert.equal(await page.evaluate(() => document.getElementById('term-input-field').style.marginTop), '');
    assert.equal(back.barH, rest.barH, 'blur changed the bar height');

    assert.deepEqual(errors, []);
    console.log('terminputlayout_browser: ok');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
