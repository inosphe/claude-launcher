const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const {chromium} = require("playwright");
const {safeListen} = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sessions = [
  ...["s1", "s2"].map(name => ({name, status: "exited", exit_code: 0,
    cwd: "/repo", harness: "codex", profile: "codex"})),
  // A pi-durable session, ended and live: only these have a ◇ chart, and the
  // button must follow them onto their HTML view too (claunch-iaddm).
  ...[["p1", "exited"], ["p2", "running"]].map(([name, status]) => ({name, status,
    cwd: "/repo", harness: "pi", profile: "pi", kind: "pi-durable"})),
];
let sims = [{id: "sim-test", owner: "s1", title: "긴 제목의 simulation <test> 확인", state: "idle"}];
let runs = {s1: {scope: "s1", cwd: "/repo", workflow: "worker", status: "running", current: [], owed: []}};
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
    if (url.pathname.endsWith("/durable")) data = {events: [], live: false, total: 0, since: 0};
    if (url.pathname === "/api/sims") data = {sims};
    if (url.pathname === "/api/cflow2/rail") data = {rows: runs};
    res.end(JSON.stringify(data)); return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});
(async () => {
  await safeListen(server);
  const browser = await chromium.launch({headless: true});
  try {
    // Wide enough that the split-pane buttons stay on the row: the toolbar
    // peels its controls into the overflow menu from the right as the column
    // narrows (toolbar.js), and at 1440 the sims and chart buttons are in
    // that menu -- hidden, so the waits below could not see them. This
    // harness is about what the buttons claim, not about when they leave the
    // row, which toolbar_browser.cjs measures.
    const page = await browser.newPage({viewport: {width: 2600, height: 900}});
    const errors = []; page.on("pageerror", e => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture"));
    // The rail's default filter holds running, hold, delayed and paused, so an
    // ended session is drawn and then filtered out — and the row is what the
    // sims block hangs off. Ask for the category ended sessions land in.
    await page.addInitScript(() => localStorage.setItem("claunch_session_filter:/",
      JSON.stringify(["running", "hold", "delayed", "paused", "killed"])));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/s/s1`);
    await page.locator('#session-list [data-name="s1"] .sess-sims a').waitFor();
    await page.locator('#term-simsbtn.has-resource').waitFor();
    await page.locator('#term-splitbtn.has-resource').waitFor();
    assert.equal(await page.locator('#term-simsbtn').getAttribute('aria-pressed'), 'false');
    assert.equal(await page.locator('#session-list .sess-sims a').textContent(), '◬ 긴 제목의 simulation <test> 확인 · 대기');
    assert.ok(await page.locator('.sess-sims').evaluate(n => n.scrollWidth <= n.clientWidth + 1));
    sims = [{...sims[0], state: 'running', run_rounds: 2, max_rounds: 8, runner_session: 'sim-runner'}];
    await page.evaluate(() => refreshSessionSims());
    await page.locator('.sess-sim-running').waitFor();
    assert.equal(await page.locator('.sess-sim-status').textContent(), '실행 중');
    assert.equal(await page.locator('.sess-sim-progress').textContent(), '실행 세션 sim-runner · 2회 완료 / 최대 8회');
    assert.ok(await page.locator('.sess-sims').evaluate(n => n.scrollWidth <= n.clientWidth + 1));
    await page.evaluate(() => { location.hash = '#/s/s2'; });
    await page.waitForFunction(() => !document.getElementById('term-simsbtn').classList.contains('has-resource'));
    assert.ok(!await page.locator('#term-splitbtn').evaluate(n => n.classList.contains('has-resource')));
    sims = [{...sims[0], owner: 's2', state: 'stopped'}]; runs = {};
    await page.evaluate(async () => { await refreshSessionSims(); await refreshCflow2Rail(); });
    assert.equal(await page.locator('[data-name="s1"] .sess-sims').count(), 0);
    assert.equal(await page.locator('[data-name="s2"] .sess-sims a').count(), 1);
    assert.equal(await page.locator('.sess-sim-running').count(), 0);
    await page.locator('#term-simsbtn.has-resource').waitFor();
    await page.locator('[data-name="s2"] .sess-sims a').click();
    assert.ok(page.url().endsWith('#/sims/sim-test'));

    // ◇ chart follows the pi-durable session onto its HTML view (claunch-iaddm).
    // The button lives on the header, which syncTermHeader draws over the
    // session's terminal and over its own conversation page alike, so both
    // views must light it; only a pi-durable session has the chart at all.
    const chartHidden = () => page.locator("#term-chartbtn")
      .evaluate(n => n.classList.contains("hidden"));
    await page.evaluate(() => { location.hash = "#/s/s1"; });
    await page.waitForFunction(() => location.hash === "#/s/s1");
    assert.ok(await chartHidden(), "a plain session has no chart button");

    // An ended pi-durable session: the button on its terminal...
    await page.evaluate(() => { location.hash = "#/s/p1"; });
    await page.waitForFunction(() => location.hash === "#/s/p1" &&
      !document.getElementById("term-header").classList.contains("hidden"));
    assert.ok(!await chartHidden(), "an ended pi-durable session shows the chart button");
    // ...and still on its HTML view, where it used to vanish.
    await page.locator('#term-view label:has(input[value="html"])').click();
    await page.waitForFunction(() => location.hash.startsWith("#/log/p1") &&
      document.getElementById("log-view").classList.contains("in-session"));
    assert.ok(!await chartHidden(), "the chart button stays on the HTML view");
    // Pressing it opens the chart pane under the conversation it is read with.
    await page.locator("#term-chartbtn").click();
    await page.waitForFunction(() => !document.getElementById("term-wf").classList.contains("hidden"));
    assert.equal(await page.locator("#term-chartbtn").getAttribute("aria-pressed"), "true");
    await page.locator("#term-wf .dfl-scroll").waitFor();
    const log = await page.locator("#log-view").evaluate(n => n.getBoundingClientRect().bottom);
    const pane = await page.locator("#term-wf").evaluate(n => n.getBoundingClientRect().top);
    assert.ok(log <= pane + 1, "the chart pane sits under the conversation");
    // A second press closes it; the button stays where it was.
    await page.locator("#term-chartbtn").click();
    await page.waitForFunction(() => document.getElementById("term-wf").classList.contains("hidden"));
    assert.ok(!await chartHidden(), "closing the pane leaves the button");

    // A running pi-durable session behaves the same on its HTML view.
    await page.evaluate(() => { location.hash = "#/s/p2"; });
    await page.waitForFunction(() => location.hash === "#/s/p2" &&
      !document.getElementById("term-header").classList.contains("hidden"));
    await page.locator('#term-view label:has(input[value="html"])').click();
    await page.waitForFunction(() => location.hash.startsWith("#/log/p2") &&
      document.getElementById("log-view").classList.contains("in-session"));
    assert.ok(!await chartHidden(), "a running pi-durable session keeps it on the HTML view");

    // A plain session's HTML view does not get one.
    await page.evaluate(() => { location.hash = "#/s/s1"; });
    await page.waitForFunction(() => location.hash === "#/s/s1");
    await page.locator('#term-view label:has(input[value="html"])').click();
    await page.waitForFunction(() => location.hash.startsWith("#/log/s1") &&
      document.getElementById("log-view").classList.contains("in-session"));
    assert.ok(await chartHidden(), "a plain session's HTML view has no chart button");

    assert.deepEqual(errors, []);
    console.log('session resources browser checks passed');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(e => { console.error(e); process.exitCode = 1; server.close(); });
