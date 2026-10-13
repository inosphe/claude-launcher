// The session line's bar (#term-input) lays out as one row: the buttons, the
// one-row field and the note share one height and one bottom edge, the empty
// field stays one row however narrow the bar is (its placeholder does not wrap
// it open), and the field never shows a scrollbar for content it fits
// (autogrowTermInput adds the border scrollHeight leaves out) (claunch-hmz62).
// The whole composer grows in layout and stays expanded during internal
// focus transfers, leaving preset/prompter buttons usable (claunch-z0h4f).
// The field, presets and prompter row form one focus group (#term-composer):
// only the field opens it, so a press on a collapsed composer's tool does not
// move the tool before the release (claunch-1bhp2).
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
      if (name === 'app.js') content = content.toString().replace(/^boot\(\);$/m, '');
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
        row.append(button);
      }
      // the group as the page has it: presets, prompter row, then the bar
      host.append(document.getElementById('term-composer'));
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

    // An ended session says "queued" in the field itself -- placeholder,
    // dashed border, title -- and leaves the note hidden, so the field keeps
    // the bar's width; a live session gets the plain placeholder and title
    // back (claunch-tww0b).
    await page.setViewportSize({ width: 1280, height: 600 });
    const ended = await page.evaluate(() => {
      const field = document.getElementById('term-input-field');
      const note = document.getElementById('term-input-note');
      const baseTitle = field.title;
      sessionEnded = true;
      refreshTermInput();
      autogrowTermInput(field);
      const out = {
        noteHidden: note.classList.contains('hidden'),
        placeholder: field.placeholder, queued: field.classList.contains('queued'),
        border: getComputedStyle(field).borderTopStyle,
        title: field.title, w: field.getBoundingClientRect().width,
        h: field.getBoundingClientRect().height,
      };
      sessionEnded = false;
      refreshTermInput();
      out.livePlaceholder = field.placeholder;
      out.liveQueued = field.classList.contains('queued');
      out.liveTitleRestored = field.title === baseTitle;
      return out;
    });
    const endedRow = (await measure()).buttons[0].h;
    assert.ok(ended.noteHidden, 'an ended session shows a standing note beside the field');
    assert.match(ended.placeholder, /queued/, `ended placeholder: ${ended.placeholder}`);
    assert.ok(ended.queued && ended.border === 'dashed', `ended field not marked: ${JSON.stringify(ended)}`);
    assert.match(ended.title, /queued/, `ended title: ${ended.title}`);
    assert.ok(ended.w >= 250, `ended field squeezed to ${ended.w}px`);
    assert.ok(Math.abs(ended.h - endedRow) < 1, `ended field ${ended.h}px against button ${endedRow}px`);
    assert.match(ended.livePlaceholder, /^type for this session/, `live placeholder: ${ended.livePlaceholder}`);
    assert.ok(!ended.liveQueued && ended.liveTitleRestored, `live field keeps the ended marks: ${JSON.stringify(ended)}`);

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

    // A real click on a tool while the composer is collapsed: the press must
    // not open the composer under the pointer, or the button moves before the
    // release and the click lands on something else (claunch-1bhp2).
    for (const id of ['term-preset-buttons-test', 'term-prompter-row-test']) {
      await page.focus('#terminal');
      const before = await page.inputValue('#term-input-field');
      const at = await page.locator('#' + id).boundingBox();
      await page.mouse.move(at.x + at.width / 2, at.y + at.height / 2);
      await page.mouse.down();
      const pressed = await page.locator('#' + id).boundingBox();
      assert.equal(pressed.y, at.y, `${id} moved under the pointer on press`);
      await page.mouse.up();
      const after = await page.inputValue('#term-input-field');
      assert.equal(after.length, before.length + 'snippet'.length, `${id} click was lost`);
    }
    // Pressing the bare strip between buttons keeps an open composer open.
    await page.focus('#term-input-field');
    const openStrip = await measure();
    const strip = await page.locator('#term-preset-buttons').boundingBox();
    await page.mouse.click(strip.x + strip.width - 4, strip.y + strip.height / 2);
    assert.equal((await measure()).field.h, openStrip.field.h, 'pressing the preset strip collapsed the composer');
    await page.focus('#terminal');
    await page.evaluate(() => document.getElementById('term-input-field').blur());
    await page.evaluate(() => { document.getElementById('term-input-field').value = ''; });
    await type(Array.from({ length: 40 }, (_, i) => `line ${i}`).join('\n'));
    await page.focus('#terminal');

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

    // Use the real prompter renderer: More/Less must preserve the focused
    // toggle and the draft, while xterm follows the changing snippet row.
    await page.setViewportSize({ width: 1280, height: 600 });
    await page.evaluate(() => {
      currentName = 'composer-fixture'; currentPage = 'terminal'; sessionEnded = false;
      prompterExpanded = false;
      prompterState = { connected: true, prompts: Array.from({ length: 20 }, (_, i) => ({
        name: `Prompt ${i} with a long title`, body: `snippet ${i}`,
      })) };
      renderTermPrompterRow();
    });
    await page.focus('#term-input-field');
    await page.waitForFunction(() => term.rows === fitAddon.proposeDimensions().rows, null, { timeout: 5000 });
    const beforeToggle = await measure();
    const draft = await page.inputValue('#term-input-field');
    const toggle = page.locator('.term-prompter-toggle');
    await toggle.click();
    assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    assert.equal(await toggle.evaluate(el => el === document.activeElement), true, 'More lost focus');
    assert.equal((await measure()).field.h, beforeToggle.field.h, 'More collapsed the draft');
    assert.ok((await measure()).terminalH < beforeToggle.terminalH, 'More did not change terminal space');
    await page.waitForFunction(() => term.rows === fitAddon.proposeDimensions().rows, null, { timeout: 5000 });
    await toggle.click();
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    assert.equal(await toggle.evaluate(el => el === document.activeElement), true, 'Less lost focus');
    assert.equal((await measure()).field.h, beforeToggle.field.h, 'Less collapsed the draft');
    assert.equal((await measure()).terminalH, beforeToggle.terminalH, 'Less did not restore terminal space');
    assert.equal(await page.inputValue('#term-input-field'), draft, 'toggle changed the draft');
    await page.waitForFunction(() => term.rows === fitAddon.proposeDimensions().rows, null, { timeout: 5000 });
    await page.focus('#terminal');
    assert.ok(Math.abs((await measure()).field.h - row) < 1, 'leaving prompter did not collapse');

    assert.deepEqual(errors, []);
    console.log('terminputlayout_browser: ok');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
