/* A notification card is not put on screen until the dashboard's first
   frame is standing (claunch-zrf3n). The reader's report was a daemon page
   that showed four gate cards over a blank screen: boot()'s first pass
   polls the gates while the rail and the dashboard are still being drawn,
   so the cards arrived first.

   Pinned here, in a real browser against the shipped app.js:
   1. a card asked for while the page is still booting is not in the strip;
      the request it stands for is recorded all the same, so nothing is
      lost by waiting;
   2. once boot() has drawn the first frame, the held card is up;
   3. a card put away while it was held never comes up — the release skips
      it, and the other held card still appears.

   The fixture holds /api/daemon, which is the answer boot() waits on: while
   it is held the page is in exactly the loading window the report was
   about. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

let daemonHeld = true;
const daemonWaiters = [];
function holdDaemon() {
  daemonHeld = true;
  daemonWaiters.length = 0;
}
function releaseDaemon() {
  daemonHeld = false;
  while (daemonWaiters.length) daemonWaiters.pop()();
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    const answer = () => {
      res.setHeader("Content-Type", "application/json");
      if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
      let data = {};
      if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
      else if (url.pathname === "/api/sessions") data = { sessions: [], llm_configured: false };
      else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
      res.end(JSON.stringify(data));
    };
    if (url.pathname === "/api/daemon" && daemonHeld) { daemonWaiters.push(answer); return; }
    return answer();
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript"
      : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

const press = (label) => ({ label, path: `/api/cflow2/answer/${label}`, body: { option: label } });
const ANSWERS = ["end", "hold", "new-task"];
const ask = (id) => ({
  id, session: "s6261", label: "settle/end-decision/ask-pose",
  node: "settle/end-decision/ask-pose", workflow: "improv-worker-v2",
  prompt: `문제: ${id} — 이 세션을 끝낼지 정한다.`,
  presses: ANSWERS.map(press),
});

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const errors = [];
    let page;
    const load = async () => {
      holdDaemon();
      if (page) await page.context().close();
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      page = await ctx.newPage();
      page.on("pageerror", e => errors.push(String(e)));
      await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      // The script is evaluated even though boot() is waiting on the held
      // answer, so the strip and the gate feeds it is about are all there.
      await page.waitForFunction(() => typeof applyRunGate === "function" && !!document.getElementById("notices"));
      await page.evaluate(() => { window.runGateFeed = () => {}; });
    };
    const settle = () => page.evaluate(() => new Promise(done => {
      let n = 3; const step = () => (--n ? requestAnimationFrame(step) : done());
      requestAnimationFrame(step);
    }));
    const strip = () => page.evaluate(() => ({
      cards: [...document.querySelectorAll("#notices .run-gate")].map(n => n.textContent.trim().slice(0, 40)),
      booted: typeof booted === "boolean" ? booted : null,
      held: typeof noticeCardsHeld === "boolean" ? noticeCardsHeld : null,
    }));
    const release = async () => {
      releaseDaemon();
      // `booted` is a top-level `let`, so it is reachable by name but not on
      // window; the wait reads the binding the page itself checks.
      await page.waitForFunction(() => booted === true, null, { timeout: 10000 });
      await settle();
    };

    // 1 + 2. asked for while booting: not on screen, still recorded; up
    //        once the first frame stands.
    await load();
    let s = await strip();
    assert.equal(s.booted, false, "the fixture keeps the page in its boot window");
    await page.evaluate(rec => applyRunGate([rec], []), ask("ask-load-1"));
    await settle();
    s = await strip();
    assert.deepEqual(s.cards, [], `no card on the loading page (${JSON.stringify(s.cards)})`);
    assert.equal(s.held, true, "the card was held, not dropped");
    const recorded = await page.evaluate(id => {
      const key = RUN_GATE_PREFIX + id;
      const card = notices.get(key);
      return {
        known: !!card,
        connected: !!(card && card.node && card.node.isConnected),
        history: noticeHistory.some(e => e.gate && e.gate.rid === id),
      };
    }, "ask-load-1");
    assert.equal(recorded.known, true, "the request is recorded while its card waits");
    assert.equal(recorded.connected, false, "the held card is not on the page");
    assert.equal(recorded.history, true, "the request row is in the notification history");
    await release();
    s = await strip();
    assert.equal(s.held, false, "the release runs once the first frame is up");
    assert.equal(s.cards.length, 1, `the held card is up after the first frame (${JSON.stringify(s.cards)})`);

    // 3. put away while held: never comes up; the other held card does.
    await load();
    // One feed, two requests: applyRunGate reads its list as the whole
    // pending set, so two calls would drop the first card.
    await page.evaluate((recs) => applyRunGate(recs, []),
                       [ask("ask-load-a"), ask("ask-load-b")]);
    await settle();
    s = await strip();
    assert.deepEqual(s.cards, [], "both cards are held");
    await page.evaluate(() => dismissNotice(RUN_GATE_PREFIX + "ask-load-b"));
    await release();
    s = await strip();
    assert.equal(s.cards.length, 1, `one card survived the release (${JSON.stringify(s.cards)})`);
    const left = await page.evaluate(() => ({
      a: !!notices.get(RUN_GATE_PREFIX + "ask-load-a"),
      b: !!notices.get(RUN_GATE_PREFIX + "ask-load-b"),
    }));
    assert.equal(left.a, true, "the card nobody put away is up");
    assert.equal(left.b, false, "the card put away while held is not revived");

    assert.deepEqual(errors, [], `no page errors: ${errors.join(" | ")}`);
    console.log("noticeload_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
