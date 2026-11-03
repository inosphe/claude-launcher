/* The button rows of a request card and of its notification-panel row stay
   inside the card, at the widths the dashboard actually draws them
   (claunch-gnlui.2).

   A run's question carries up to five answers, and the card adds Hide and
   the Remind group (1h / 6h / 24h) beside them; the panel row adds Show card
   or Open. The row was one flex line that never wrapped, so at the panel's
   default 380px the last presses ran past the card's right edge and the
   answers were squeezed until their labels broke over two lines.

   Pinned here, measured in a real browser against the shipped stylesheet and
   app.js: in the corner card and in the panel row at the panel's default
   and minimum widths, every press lies inside its card, no press label
   breaks over a line, and the Remind group sits on a line of its own below
   every other press. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions: [], llm_configured: false };
    else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript"
      : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

// The question of the reported screenshot: an end decision with five
// answers a card can press and one that needs a reason (left to the session).
const press = (label) => ({ label, path: `/api/cflow2/answer/${label}`, body: { option: label } });
const ASK = {
  id: "ask-wrap-1", session: "s5986", label: "settle/end-decision/ask-pose",
  node: "settle/end-decision/ask-pose", workflow: "improv-worker-v2",
  prompt: "문제: 이 세션을 끝낼지 정한다. 선택지: end (권장), auto-release, hold, new-task, self-improve.",
  presses: ["end", "auto-release", "hold", "new-task", "self-improve"].map(press)
    .concat([{ label: "reframe", path: "/api/cflow2/answer/reframe", reasonRequired: true }]),
};

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const errors = [];
    let page;
    // A fresh browser state for each scenario: hidden and reminded cards
    // are remembered in the page's storage.
    const load = async () => {
      if (page) await page.context().close();
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      page = await ctx.newPage();
      page.on("pageerror", e => errors.push(String(e)));
      await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.waitForFunction(() => typeof applyRunGate === "function" && !!document.getElementById("notices"));
      // The request is fed by hand; the rail poll's feed, which would settle
      // it as gone (this fixture serves no rail), is held still.
      await page.evaluate(() => { window.runGateFeed = () => {}; });
    };
    const settle = () => page.evaluate(() => new Promise(done => {
      let n = 3; const step = () => (--n ? requestAnimationFrame(step) : done());
      requestAnimationFrame(step);
    }));

    // Every press of one actions row, against the box of the card or panel
    // row that holds it.
    const measure = (sel) => page.evaluate((sel) => {
      const actions = document.querySelector(sel);
      if (!actions) return null;
      const holder = actions.closest(".notice, .notice-row");
      const h = holder.getBoundingClientRect();
      const cs = getComputedStyle(holder);
      const inner = { left: h.left + parseFloat(cs.borderLeftWidth),
                      right: h.right - parseFloat(cs.borderRightWidth) };
      const presses = [...actions.querySelectorAll("button")]
        .filter(b => b.getClientRects().length > 0)
        .map(b => {
          const r = b.getBoundingClientRect();
          // The lines the label occupies, counted from its text boxes: a
          // press's own height says nothing once the row stretches it.
          const range = document.createRange();
          range.selectNodeContents(b);
          const lines = new Set([...range.getClientRects()].map(t => Math.round(t.top))).size;
          return { label: b.textContent.trim(), left: r.left, right: r.right, top: r.top,
                   bottom: r.bottom, lines, remind: !!b.closest(".gate-remind") };
        });
      const remind = actions.querySelector(".gate-remind");
      const rr = remind && remind.getBoundingClientRect();
      return { inner, presses, remind: rr && { top: rr.top, left: rr.left, right: rr.right } };
    }, sel);

    const check = async (where, sel, { remind = true } = {}) => {
      const m = await measure(sel);
      assert(m, `${where}: the actions row is drawn`);
      const answers = m.presses.filter(p => !p.remind);
      const waits = m.presses.filter(p => p.remind);
      assert.deepEqual(waits.map(p => p.label), remind ? ["1h", "6h", "24h"] : [],
        `${where}: the Remind waits are drawn where the card can be put aside`);
      for (const want of ["end", "auto-release", "hold", "new-task", "self-improve"]) {
        assert(answers.some(p => p.label === want), `${where}: the answer ${want} is drawn`);
      }
      for (const p of m.presses) {
        assert(p.right <= m.inner.right + 0.5,
          `${where}: ${p.label} ends inside the card (press right ${p.right}, card inner right ${m.inner.right})`);
        assert(p.left >= m.inner.left - 0.5,
          `${where}: ${p.label} starts inside the card (press left ${p.left}, card inner left ${m.inner.left})`);
        assert.equal(p.lines, 1, `${where}: the label of ${p.label} stays on one line`);
      }
      if (!remind) return;
      const lowest = Math.max(...answers.map(p => p.bottom));
      assert(m.remind.top >= lowest - 0.5,
        `${where}: Remind is on its own line below every other press (remind top ${m.remind.top}, presses bottom ${lowest})`);
    };

    // The corner card.
    await load();
    await page.evaluate(rec => applyRunGate([rec], []), ASK);
    await settle();
    await check("corner card", "#notices .run-gate .gate-actions");

    // The panel row of a request whose card is up (the reported screenshot):
    // the panel is open when the request arrives, so the row carries Remind.
    await load();
    await page.evaluate(() => noticePanelToggle(true));
    await page.evaluate(rec => applyRunGate([rec], []), ASK);
    await settle();
    for (const w of [380, 280]) {
      await page.evaluate(px => applyNoticeW(px), w);
      await settle();
      await check(`panel row at ${w}px`, "#notice-panel .notice-row .gate-actions");
    }

    // Opening the panel over a card puts the card aside: that row answers
    // with Show card instead of Remind, and still stays inside its box.
    await load();
    await page.evaluate(rec => applyRunGate([rec], []), ASK);
    await page.evaluate(() => noticePanelToggle(true));
    for (const w of [380, 280]) {
      await page.evaluate(px => applyNoticeW(px), w);
      await settle();
      await check(`hidden card's panel row at ${w}px`, "#notice-panel .notice-row .gate-actions",
        { remind: false });
    }
    assert.deepEqual(errors, [], "no page errors");
    console.log("noticewrap_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
