// Real xterm geometry, soft wrapping and browser clipboard. No live daemon.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(process.env.CLAUNCH_PLAYWRIGHT || 'playwright');
const { safeListen } = require('./safe_listen.cjs');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const helpers = app.slice(app.indexOf('async function copyToBrowserClipboard('), app.indexOf('/* OSC 52'))
  + app.slice(app.indexOf('function terminalCommandAt('), app.indexOf('async function deleteHostClipboard('));
const server = http.createServer((req, res) => {
  if (req.url === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end('<link rel="stylesheet" href="/vendor/xterm.css"><link rel="stylesheet" href="/style.css">'
      + '<div id="terminal" style="width:700px;height:300px"></div><script src="/vendor/xterm.js"></script>');
  }
  const files = ['/vendor/xterm.css', '/vendor/xterm.js', '/style.css'];
  if (!files.includes(req.url)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', req.url.endsWith('.js') ? 'application/javascript' : 'text/css');
  res.end(fs.readFileSync(path.join(root, req.url)));
});
(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CLAUNCH_CHROMIUM ? { executablePath: process.env.CLAUNCH_CHROMIUM } : {}) });
  try {
    const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.addScriptTag({ content: helpers });
    await page.evaluate(() => {
      window.notify = () => {};
      window.t = new Terminal({ cols: 40, rows: 10 });
      t.open(document.getElementById('terminal'));
      window.sent = [];
      t.onData(data => sent.push(data));
      installTerminalCommandCopy(t);
      window.write = text => new Promise(resolve => t.write(text, resolve));
      window.realClipboard = navigator.clipboard;
    });
    const point = async (row, col = 0) => {
      const rect = await page.locator('.xterm-screen').boundingBox();
      return [rect.x + (col + 0.5) * rect.width / 40, rect.y + (row + 0.5) * rect.height / 10];
    };
    const hover = async (row, col = 0) => page.mouse.move(...await point(row, col));
    const clickAt = async (row, col = 0) => { await hover(row, col); await page.mouse.down(); await page.mouse.up(); };
    // The layer itself has no box; its underline rows are what shows.
    const link = page.locator('.term-command-link-row').first();
    const copied = () => page.waitForFunction(() => {
      const badge = document.querySelector('.term-command-copied');
      return !badge.hidden && badge.textContent === 'Copied';
    });
    const clipboard = () => page.evaluate(() => realClipboard.readText());
    assert.equal(await page.locator('button').count(), 0, 'no Copy command button');
    const command = '! echo "한국어 and two  spaces ' + 'x'.repeat(45) + '"';
    await page.evaluate(async cmd => {
      await realClipboard.writeText('unchanged');
      // ANSI color and mouse reporting are normal harness output.
      await write('\x1b[?1000h\x1b[32m' + cmd + '\x1b[0m\r\nordinary output');
    }, command);
    await hover(1); // second physical row of a single command
    assert.equal(await link.isVisible(), true, 'hovering the text underlines it');
    assert.equal(await page.locator('.term-command-link-row').count(), 2, 'one underline per wrapped row');
    const underline = await page.locator('.term-command-link-row').first()
      .evaluate(el => getComputedStyle(el).borderBottomStyle);
    assert.equal(underline, 'solid');
    assert.equal(await page.locator('.xterm-screen').evaluate(el => getComputedStyle(el).cursor), 'pointer',
      'link cursor over the command even while the PTY tracks the mouse');
    assert.equal(await clipboard(), 'unchanged', 'hover never copies');
    await hover(1, 39); // past the end of the command on its last row
    assert.equal(await link.isVisible(), false, 'off the text the underline goes away');
    assert.notEqual(await page.locator('.xterm-screen').evaluate(el => getComputedStyle(el).cursor), 'pointer');
    await clickAt(1, 3);
    await copied();
    assert.equal(await clipboard(), command);
    assert.deepEqual(await page.evaluate(() => sent), [], 'click on the command never sends PTY mouse/input bytes');
    // Mouse tracking off: a drag over the command is a normal selection.
    await page.evaluate(async () => { await write('\x1b[?1000l'); await realClipboard.writeText('unchanged'); });
    await hover(0, 2); await page.mouse.down(); await hover(0, 12); await page.mouse.up();
    assert.equal(await page.evaluate(() => t.hasSelection()), true, 'drag still selects text');
    assert.equal(await clipboard(), 'unchanged', 'a drag never copies');
    await clickAt(0, 4);
    await copied();
    assert.equal(await clipboard(), command, 'a plain click copies with tracking off');
    // A two-column character that cannot fit in the last column leaves a
    // null padding cell. Real spaces at this boundary must still survive.
    for (const prefix of ['x'.repeat(31), 'x'.repeat(30), 'x'.repeat(30) + ' ', 'x'.repeat(31) + ' ', 'x'.repeat(29) + '  ']) {
      const wide = '! echo "' + prefix + '한국어"';
      await page.evaluate(async cmd => { t.reset(); await realClipboard.writeText('unchanged'); await write(cmd); }, wide);
      assert.equal(await page.evaluate(() => terminalCommandAt(t, 0)), wide);
      await clickAt(1, 0);
      await page.waitForFunction(() => realClipboard.readText().then(text => text !== 'unchanged'));
      assert.equal(await clipboard(), wide, 'wide wrap and literal spaces preserved');
    }
    // Force the plain-HTTP fallback and check the real browser clipboard.
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true }));
    await page.evaluate(async () => { t.reset(); await write('! echo fallback'); });
    await clickAt(0, 2);
    await copied();
    assert.equal(await clipboard(), '! echo fallback');
    assert.equal(await page.locator('.xterm > textarea').count(), 0, 'fallback removes its temporary textarea');
    await hover(0, 2);
    assert.equal(await link.isVisible(), true);
    await page.evaluate(async () => { await write('\r\x1b[2Kordinary output'); });
    assert.equal(await link.isVisible(), false, 'repaint clears the link');
    await page.evaluate(async () => { await write('\r\x1b[2K! resize'); });
    assert.equal(await link.isVisible(), true, 'repaint re-measures under a still pointer');
    await page.evaluate(() => t.resize(50, 10));
    assert.equal(await link.isVisible(), false);
    await page.evaluate(() => t.dispose());
    assert.equal(await page.locator('.term-command-link').count(), 0);
    assert.equal(await page.locator('.term-command-copied').count(), 0);
    console.log('browser command copy: real xterm, hover underline/cursor, click vs drag, ANSI, Korean/wraps, mouse tracking, clipboard and HTTP fallback passed');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
