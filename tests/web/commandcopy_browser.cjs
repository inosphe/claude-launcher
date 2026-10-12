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
    const hover = async row => {
      const rect = await page.locator('.xterm-screen').boundingBox();
      await page.mouse.move(rect.x + 15, rect.y + (row + 0.5) * rect.height / 10);
    };
    const button = page.locator('.term-command-copy');
    const command = '! echo "한국어 and two  spaces ' + 'x'.repeat(45) + '"';
    await page.evaluate(async cmd => {
      await realClipboard.writeText('unchanged');
      // ANSI color and mouse reporting are normal harness output.
      await write('\x1b[?1000h\x1b[32m' + cmd + '\x1b[0m\r\nordinary output');
    }, command);
    await hover(1); // second physical row of a single command
    assert.equal(await button.isVisible(), true);
    assert.equal(await page.evaluate(() => realClipboard.readText()), 'unchanged');
    await button.click();
    await page.waitForFunction(() => document.querySelector('.term-command-copy').textContent === 'Copied');
    assert.equal(await page.evaluate(() => realClipboard.readText()), command);
    assert.deepEqual(await page.evaluate(() => sent), [], 'button never sends PTY mouse/input bytes');
    // Force the plain-HTTP fallback and check the real browser clipboard.
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true }));
    await page.evaluate(async () => { t.reset(); await write('! echo fallback'); });
    await hover(0);
    await button.click();
    await page.waitForFunction(() => document.querySelector('.term-command-copy').textContent === 'Copied');
    assert.equal(await page.evaluate(() => realClipboard.readText()), '! echo fallback');
    assert.equal(await page.locator('.xterm > textarea').count(), 0, 'fallback removes its temporary textarea');
    await page.evaluate(async () => { await write('\r\x1b[2Kordinary output'); });
    assert.equal(await button.isVisible(), false, 'repaint clears hover');
    await hover(0); assert.equal(await button.isVisible(), false);
    await page.evaluate(async () => { await write('\r\x1b[2K! resize'); });
    await hover(0); assert.equal(await button.isVisible(), true);
    await page.evaluate(() => t.resize(50, 10));
    assert.equal(await button.isVisible(), false);
    await page.evaluate(() => t.dispose());
    assert.equal(await button.count(), 0);
    console.log('browser command copy: real xterm, ANSI, Korean/wraps, mouse tracking, clipboard and HTTP fallback passed');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
