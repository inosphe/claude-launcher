// The link panel (claunch-xbdy7 follow-up): the header's ⇄ link button and
// the floating panel behind it, with the real markup, style.css and the
// panel's own code from app.js. The link and control-socket state it reads
// are plain globals here, set by each step. No live daemon.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(process.env.CLAUNCH_PLAYWRIGHT || 'playwright');
const { safeListen } = require('./safe_listen.cjs');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');

function between(src, from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

const button = between(html, '<button id="term-conn"', '</button>') + '</button>'
  + between(html, '<button id="term-force"', '</button>') + '</button>';
const mobile = between(html, '<button id="m-conn"', '</button>') + '</button>';
const panel = between(html, '<div id="conn-panel"', '\n</div>\n') + '\n</div>';
const logCode = between(app, '/* ---- the link log (claunch-xbdy7) ----', '/* ---- the connection the page keeps ----');
const panelCode = between(app, '/* ---- the link panel', '/* ---- queued deliveries ----');

// What the panel reads, as globals a step can change.
const world = `
var BASE = "/";
function $(id) { return document.getElementById(id); }
var calls = [];
var linkState = "live", linkName = "s7", linkTry = 0, linkQueue = [], sessionEnded = false;
var linkPingAt = null, linkEchoed = true, linkLastPong = Date.now() - 2000, attachedBoot = "boot-a";
var ws = { readyState: 1, linkVia: "channel", ch: 3, linkStartedAt: Date.now() - 5000,
           linkReadyAt: Date.now() - 4000, linkLastRx: Date.now() - 1000 };
var controlSock = { readyState: 1 }, controlTry = 0, controlTimer = null;
var controlOpenedAt = Date.now() - 60000, controlLastRx = Date.now();
var latencySamples = [42], latencyVia = null, latencySentAt = null;
var controlWaiting = new Map(), channelLinks = new Map([[3, {}]]);
var p2pStopped = false, p2pOff = null, p2pLink = null, p2pLastFail = null;
var daemonOnline = true, daemonBoot = "boot-a";
var CONTROL_BACKOFF = [1, 2, 3], LINK_BACKOFF = [1, 2, 3, 4], LINK_PING_MS = 10000;
function controlUp() { return !!controlSock && controlSock.readyState === 1; }
function reconnectNow(byUser) { calls.push(["reconnectNow", byUser]); }
function linkTakeDead(why) { calls.push(["linkTakeDead", why]); ws = null; }
async function relogin() { calls.push(["relogin"]); return true; }
function controlRecycle(why) { calls.push(["controlRecycle", why]); controlSock = null; }
function openControlSocket() { calls.push(["openControlSocket"]); controlSock = { readyState: 0 }; }
`;

const server = http.createServer((req, res) => {
  if (req.url === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/style.css">'
      + `<body><div id="mobile-top">${mobile}</div><div id="term-header">${button}</div>${panel}</body>`);
  }
  if (req.url !== '/style.css') { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', 'text/css');
  res.end(fs.readFileSync(path.join(root, 'style.css')));
});

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CLAUNCH_CHROMIUM ? { executablePath: process.env.CLAUNCH_CHROMIUM } : {}) });
  try {
    const context = await browser.newContext({
      viewport: { width: 1200, height: 800 },
      permissions: ['clipboard-read', 'clipboard-write'],
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(() => localStorage.clear());
    await page.addScriptTag({ content: world + logCode + panelCode });
    await page.evaluate(() => {
      linkLog('term.open', { session: 's7', via: 'channel', ch: 3 });
      linkLogFlush();
    });
    const chip = page.locator('#term-conn');
    const box = page.locator('#conn-panel');
    const render = () => page.evaluate(() => renderLinkPanel());

    // Closed until asked, and the chip already carries the grade.
    await render();
    assert.equal(await box.isVisible(), false, 'panel starts closed');
    assert.match(await chip.getAttribute('class'), /\bok\b/);

    await chip.click();
    assert.equal(await box.isVisible(), true, 'the chip opens the panel');
    assert.deepEqual(errors, [], 'no page errors after opening');
    assert.equal(await chip.getAttribute('aria-expanded'), 'true');
    const rows = await page.locator('#conn-panel-rows').innerText();
    assert.match(rows, /channel 3/, 'the terminal row names its channel');
    assert.match(rows, /42ms/, 'the control socket shows its last round trip');
    assert.match(rows, /boot-a/);
    assert.match(await page.locator('#conn-panel-log').innerText(), /term\.open/, 'the log tail is shown');

    // A terminal still attached to the daemon before a restart is a warning.
    await page.evaluate(() => { daemonBoot = 'boot-b'; });
    await render();
    assert.match(await page.locator('#conn-panel-grade').innerText(), /earlier daemon boot/);
    await page.evaluate(() => { daemonBoot = 'boot-a'; });

    // A terminal on a socket of its own is a warning; one given up is down.
    await page.evaluate(() => { ws.linkVia = 'socket'; });
    await render();
    assert.match(await chip.getAttribute('class'), /\bwarn\b/);
    assert.match(await page.locator('#conn-panel-grade').innerText(), /socket of its own/);
    await page.evaluate(() => { linkState = 'lost'; ws = null; });
    await render();
    assert.match(await chip.getAttribute('class'), /\bdown\b/);
    assert.match(await page.locator('#conn-panel-grade').innerText(), /given up/);

    // force reconnect, from the header or the panel: the state at the press
    // is logged first, then a fresh login, then both connections reopened.
    await page.evaluate(() => {
      linkState = 'live';
      controlSock = { readyState: 1 };
      ws = { readyState: 1, linkVia: 'socket', linkStartedAt: Date.now(), linkReadyAt: Date.now() };
    });
    await page.locator('#term-force').click();
    await page.waitForFunction(() => calls.length >= 4);
    assert.deepEqual(await page.evaluate(() => calls), [
      ['relogin'], ['controlRecycle', 'force reconnect'],
      ['linkTakeDead', 'force reconnect'], ['reconnectNow', true],
    ]);
    const snap = await page.evaluate(() => linkLogEntries().find((e) => e.k === 'force.snapshot'));
    assert.equal(snap['t.via'], 'socket', 'the snapshot holds what the page believed at the press');
    assert.equal(snap['c.state'], 'OPEN');
    assert.equal(snap.grade, 'warn');
    // With the control socket already gone it is opened, not recycled.
    await page.evaluate(() => { calls = []; controlSock = null; ws = null; linkState = 'lost'; });
    await page.locator('#conn-panel-reconnect').click();
    await page.waitForFunction(() => calls.length >= 3);
    assert.deepEqual(await page.evaluate(() => calls), [
      ['relogin'], ['openControlSocket'], ['reconnectNow', true],
    ]);

    // copy hands out the rows and the whole log as text.
    await page.locator('#conn-panel-copy').click();
    await page.waitForFunction(() => document.getElementById('conn-panel-copy').textContent === 'copied');
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    assert.match(copied, /\[terminal link\]/);
    assert.match(copied, /force\.done/, 'the log section carries the panel\'s own entries');

    // Dragged by its title bar.
    const before = await box.boundingBox();
    const head = await page.locator('#conn-panel-head .conn-panel-title').boundingBox();
    await page.mouse.move(head.x + 5, head.y + 5);
    await page.mouse.down();
    await page.mouse.move(head.x - 300, head.y + 200, { steps: 5 });
    await page.mouse.up();
    const after = await box.boundingBox();
    assert.ok(Math.abs(after.x - (before.x - 305)) <= 2 && Math.abs(after.y - (before.y + 195)) <= 2,
      `moved by the drag: ${JSON.stringify([before, after])}`);
    assert.ok(after.y + after.height <= 800, 'a dragged panel stays inside the window');

    if (process.env.CLAUNCH_TEST_DIR) {
      await page.screenshot({ path: path.join(process.env.CLAUNCH_TEST_DIR, 'linkpanel.png') });
    }

    // Escape closes it; the chip opens it again where it was dragged to.
    await page.keyboard.press('Escape');
    assert.equal(await box.isVisible(), false, 'Escape closes the panel');
    await chip.click();
    const again = await box.boundingBox();
    assert.ok(Math.abs(again.x - after.x) <= 1 && Math.abs(again.y - after.y) <= 1, 'reopens where it was left');
    await page.locator('#conn-panel-close').click();
    assert.equal(await box.isVisible(), false, '× closes the panel');

    // At phone width the panel stays inside the viewport.
    const phone = await context.newPage();
    await phone.setViewportSize({ width: 360, height: 640 });
    await phone.goto(`http://127.0.0.1:${server.address().port}/`);
    await phone.addScriptTag({ content: world + logCode + panelCode });
    await phone.locator('#m-conn').click();
    const small = await phone.locator('#conn-panel').boundingBox();
    assert.ok(small.x >= 0 && small.x + small.width <= 360, `fits at 360px: ${JSON.stringify(small)}`);
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth <= 360), true, 'no horizontal scroll');

    assert.deepEqual(errors, [], 'no page errors');
    console.log('browser link panel: grade chip, open/close, rows and log, force reconnect, copy, drag, phone width passed');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
