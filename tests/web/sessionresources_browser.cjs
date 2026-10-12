const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const {chromium} = require("playwright");
const {safeListen} = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sessions = ["s1", "s2"].map(name => ({name, status: "exited", exit_code: 0,
  cwd: "/repo", harness: "codex", profile: "codex"}));
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
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const errors = []; page.on("pageerror", e => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture"));
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
    assert.deepEqual(errors, []);
    console.log('session resources browser checks passed');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(e => { console.error(e); process.exitCode = 1; server.close(); });
