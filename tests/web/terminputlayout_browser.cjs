// The session line's bar (#term-input) lays out as one row: the buttons, the
// one-row field and the note share one height and one bottom edge, the empty
// field stays one row however narrow the bar is (its placeholder does not wrap
// it open), and the field never shows a scrollbar for content it fits
// (autogrowTermInput adds the border scrollHeight leaves out) (claunch-hmz62).
// The whole composer grows in layout and stays expanded during internal
// focus transfers, leaving preset/prompter buttons usable (claunch-z0h4f).
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
        terminalH: box(document.getElementById("terminal")).height,
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
      const host = document.createElement('div');
      document.body.appendChild(host);
      Object.assign(host.style, { position: 'fixed', inset: '0', display: 'flex', flexDirection: 'column' });
      const terminal = document.getElementById('terminal');
      terminal.classList.remove('hidden');
      Object.assign(terminal.style, { flex: '1', minHeight: '0' });
      host.append(terminal);
      for (const id of ['term-preset-buttons', 'term-prompter-row']) {
        const row = document.getElementById(id);
        row.classList.remove('hidden');
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'term-btn';
        button.textContent = id; button.id = id + '-test';
        button.addEventListener('click', () => insertPromptPreset('snippet'));
        row.append(button); host.append(row);
      }
      host.append(form);
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
    await page.evaluate(() => {
      railOpen = false;
      term = new Terminal({ fontSize });
      fitAddon = new FitAddon.FitAddon();
      term.loadAddon(fitAddon);
      term.open(document.getElementById('terminal'));
      localFit();
    });
    const restRows = await page.evaluate(() => term.rows);
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

    // The composer takes real space from the terminal; every tool remains
    // above the field and receives pointer hits at its visible position.
    await page.click('#term-input-field');
    await page.waitForFunction(rows => term.rows < rows, restRows, { timeout: 5000 });
    const open = await measure();
    assert.ok(open.field.h > row + 30, `three lines did not open the field: ${open.field.h}px`);
    assert.equal(open.field.overflow, false, 'an opened three-line field overflows');
    assert.ok(open.barH > rest.barH, 'opening did not grow the composer');
    assert.ok(open.terminalH < rest.terminalH, 'opening did not reduce the terminal height');
    assert.ok(open.field.top >= open.barTop, 'field extends outside the composer');
    for (const b of open.buttons) {
      assert.ok(Math.abs(b.bottom - open.field.bottom) < 1, `${b.id} left the field bottom`);
    }
    for (const id of ['term-preset-buttons-test', 'term-prompter-row-test', 'term-input-send']) {
      const hit = await page.locator('#' + id).evaluate(button => {
        const r = button.getBoundingClientRect();
        return button.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
      });
      assert.equal(hit, true, `${id} is covered`);
      await page.focus('#' + id);
      assert.equal((await measure()).barH, open.barH, `${id} focus collapsed the composer`);
    }
    for (const id of ['term-preset-buttons-test', 'term-prompter-row-test']) {
      const before = await page.inputValue('#term-input-field');
      await page.click('#' + id);
      const after = await page.inputValue('#term-input-field');
      assert.equal(after.length, before.length + 'snippet'.length);
      assert.ok(after.includes('snippet'), `${id} did not insert its text`);
      assert.ok((await measure()).field.h > row + 30, `${id} click collapsed the composer`);
    }
    // Tab within the input form also retains expansion, and a long draft
    // remains scrollable when its adjacent controls have the keyboard.
    await page.focus('#term-input-field');
    await page.keyboard.press('Tab');
    assert.ok((await measure()).field.h > row + 30, 'Tab collapsed the composer');
    await page.focus('#term-input-field');

    // Clicked while empty: three lines of room to type in.
    await type('');
    const openEmpty = await measure();
    assert.ok(openEmpty.field.h > row + 30, `an empty clicked field stayed ${openEmpty.field.h}px`);
    assert.ok(openEmpty.barH > rest.barH, 'an empty opened field did not grow the bar');

    // Many lines: capped at 30vh, the rest scrolls.
    await type(Array.from({ length: 40 }, (_, i) => `line ${i}`).join('\n'));
    const tall = await measure();
    assert.ok(tall.field.h <= 600 * 0.3 + 1, `the field passed its ceiling: ${tall.field.h}px`);

    await page.focus('#term-prompter-row-test');
    assert.equal(await page.locator('#term-input-field').evaluate(el => getComputedStyle(el).overflowY), 'auto');
    assert.equal((await measure()).field.h, tall.field.h, 'tool focus collapsed a long draft');

    // Focus outside the composer restores terminal space.
    await page.evaluate(() => {
      const terminal = document.getElementById('terminal');
      terminal.tabIndex = 0; terminal.focus();
    });
    await page.evaluate(() => document.getElementById('term-input-field').blur());
    const back = await measure();
    assert.ok(Math.abs(back.field.h - row) < 1, `blur left the field at ${back.field.h}px`);
    assert.equal(await page.evaluate(() => document.getElementById('term-input-field').style.marginTop), '');
    assert.equal(back.barH, rest.barH, 'leaving did not restore the bar height');
    assert.equal(back.terminalH, rest.terminalH, 'leaving did not restore terminal space');
    await page.waitForFunction(() => term.rows === fitAddon.proposeDimensions().rows, null, { timeout: 5000 });

    // A narrow viewport still leaves both snippet rows and send reachable.
    for (const width of [760, 520, 380]) {
      await page.setViewportSize({ width, height: 600 });
      await page.focus('#term-input-field');
      const narrow = await measure();
      assert.ok(narrow.field.h <= 181, `${width}px: draft exceeded 30vh`);
      assert.ok(narrow.bar.scroll <= narrow.bar.client, `${width}px: expanded bar overflows`);
      for (const id of ['term-preset-buttons-test', 'term-prompter-row-test', 'term-input-send']) {
        assert.equal(await page.locator('#' + id).evaluate(button => {
          const r = button.getBoundingClientRect();
          return button.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
        }), true, `${width}px: ${id} covered`);
        await page.focus('#' + id);
        assert.equal((await measure()).field.h, narrow.field.h, `${width}px: tool focus collapsed draft`);
      }
      await page.focus('#terminal');
      const closed = await measure();
      assert.ok(Math.abs(closed.field.h - closed.buttons[0].h) < 1, `${width}px: leaving did not collapse`);
    }

    assert.deepEqual(errors, []);
    console.log('terminputlayout_browser: ok');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
