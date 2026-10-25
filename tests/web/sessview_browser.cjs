/* The session's view, remembered per session, and the split pane under the
   HTML view (claunch-br7qh), in the whole app against a fixture daemon:
   picking HTML on s1, looking at s2 and following s1's rail link back lands
   on s1's conversation page; the split pane opened there sits between the
   conversation and the session line, and its bar resizes the two. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const {chromium} = require("playwright");
const {safeListen} = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sessions = ["s1", "s2"].map(name => ({name, status: "exited", exit_code: 0,
  cwd: "/repo", harness: "codex", profile: "codex"}));
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = {version: "fixture", boot_id: "test"};
    if (url.pathname === "/api/sessions") data = {sessions};
    if (url.pathname.endsWith("/capture")) data = {lines: []};
    if (url.pathname.endsWith("/transcript")) data = {records: [], more: false};
    if (url.pathname.endsWith("/threads")) data = {threads: []};
    res.end(JSON.stringify(data)); return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});
const box = (page, sel) => page.locator(sel).evaluate(n => {
  const r = n.getBoundingClientRect();
  return {top: r.top, bottom: r.bottom, height: r.height};
});
(async () => {
  await safeListen(server);
  const browser = await chromium.launch({headless: true});
  try {
    // Wide enough that the threads button stays on the toolbar row: the row
    // peels its controls into the overflow menu from the right as the column
    // narrows (toolbar.js), and at 1440 the threads button is in that menu --
    // hidden, so the click below could never land. What this harness holds is
    // the view memory and the split pane, not when a button leaves the row;
    // toolbar_browser.cjs measures that.
    const page = await browser.newPage({viewport: {width: 2600, height: 900}});
    const errors = []; page.on("pageerror", e => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture"));
    // The rail's default filter holds running, hold, delayed and paused, so an
    // ended session is drawn and then filtered out. Both fixture sessions are
    // exited; ask for the category they land in (killed) or the row is never
    // visible to click.
    await page.addInitScript(() => localStorage.setItem("claunch_session_filter:/",
      JSON.stringify(["running", "hold", "delayed", "paused", "killed"])));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/s/s1`);
    await page.locator('#session-list li.sess-card[data-name="s1"]').waitFor();

    // HTML on s1, then s2 by its rail row, then s1 by its rail row.
    await page.locator('#term-view label:has(input[value="html"])').click();
    await page.waitForFunction(() => location.hash.startsWith("#/log/s1"));
    await page.locator('#session-list li.sess-card[data-name="s2"]').click({position: {x: 30, y: 10}});
    await page.waitForFunction(() => location.hash.startsWith("#/s/s2"));
    // s2 has no choice of its own: its terminal.
    await page.waitForFunction(() =>
      document.querySelector('input[name="term-view"][value="cli"]').checked);
    await page.locator('#session-list li.sess-card[data-name="s1"]').click({position: {x: 30, y: 10}});
    await page.waitForFunction(() => location.hash.startsWith("#/log/s1"));
    await page.waitForFunction(() =>
      document.querySelector('input[name="term-view"][value="html"]').checked);
    assert.ok(!await page.locator('#term-header').evaluate(n => n.classList.contains("hidden")),
              "the session header stays up on the HTML view it was routed to");
    assert.ok(await page.locator('#log-view').evaluate(n => n.classList.contains("in-session")));

    // Back from there goes to s2, not through a #/s/s1 the reader never saw.
    await page.goBack();
    await page.waitForFunction(() => location.hash.startsWith("#/s/s2"));
    await page.goForward();
    await page.waitForFunction(() => location.hash.startsWith("#/log/s1"));
    // ...and is s1's HTML view again, though s2 was attached on the way.
    await page.waitForFunction(() => !document.getElementById("term-header").classList.contains("hidden")
      && document.getElementById("log-view").classList.contains("in-session"));

    // The threads pane, opened on the HTML view.
    await page.locator('#term-threadsbtn').click();
    await page.waitForFunction(() => !document.getElementById("term-wf").classList.contains("hidden"));
    const log = await box(page, "#log-view");
    const bar = await box(page, "#term-split");
    const pane = await box(page, "#term-wf");
    const composer = await box(page, "#term-composer");
    assert.ok(log.height > 80 && pane.height > 80, JSON.stringify({log, pane}));
    assert.ok(log.bottom <= bar.top + 1 && bar.bottom <= pane.top + 1 && pane.bottom <= composer.top + 1,
              "conversation, bar, pane, session line — top to bottom: " +
              JSON.stringify({log, bar, pane, composer}));
    const ratio = log.height / (log.height + pane.height);
    assert.ok(Math.abs(ratio - 0.6) < 0.05, `default share ${ratio}`);

    // Drag the bar down: the conversation grows, the choice is kept.
    const x = 700, y = bar.top + bar.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x, y + 120, {steps: 6});
    await page.mouse.up();
    const log2 = await box(page, "#log-view");
    assert.ok(log2.height > log.height + 60, JSON.stringify({before: log, after: log2}));
    const kept = await page.evaluate(() => JSON.parse(localStorage.getItem("claunch_sesslayout:/")).s1);
    assert.equal(kept.view, "html");
    assert.equal(kept.pane, "threads");
    assert.ok(kept.split && kept.ratio > 0.62, JSON.stringify(kept));

    // CLI keeps the same pane under the terminal; the choice flips to cli.
    await page.locator('#term-view label:has(input[value="cli"])').click();
    await page.waitForFunction(() => location.hash.startsWith("#/s/s1"));
    assert.ok(!await page.locator('#term-wf').evaluate(n => n.classList.contains("hidden")));
    // Written by route() on the hashchange, which lands after the address.
    await page.waitForFunction(() =>
      JSON.parse(localStorage.getItem("claunch_sesslayout:/")).s1.view === "cli");
    assert.deepEqual(errors, []);
    console.log("session view browser checks passed");
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(e => { console.error(e); process.exitCode = 1; server.close(); });
