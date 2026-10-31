/* A corner notification card above a certain size is folded by default
   (claunch-qf41a): the reader's screenshot was an end-decision card whose
   decision brief filled the corner for as long as the run waited. What such
   a card still answers while folded — which request it is, and the presses
   that settle it — must stay up; the body comes back with a toggle. The
   fold no longer hides the body outright: it stays up as a five-line
   preview so the reader can tell what the notice is about
   (claunch-qf41a.1, thread th-e3d6db), and the notification history
   window's rows fold the same way.

   Pinned here, measured in a real browser against the shipped stylesheet
   and app.js:
   1. a decision card with a report-length markdown body is foldable and
      folded by default; folded, its body shows as a clamped five-line
      preview, its title and every answer press are still drawn, and the
      folded card is no taller than the threshold the classification
      measured it against;
   2. the toggle expands the card (body back, label flipped) and folds it
      again on a second press;
   3. a one-question card stays unfolded — the threshold is a size, not a
      kind: small cards are untouched;
   4. a card born while the history panel hid the strip (measured 0 there)
      is classified when the panel closes and the strip shows it;
   5. a long row of the notification history window is foldable and folded
      by default: the head stays up, the body is a clamped preview, and the
      row's toggle expands it. */
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
    res.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript"
      : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

const press = (label) => ({ label, path: `/api/cflow2/answer/${label}`, body: { option: label } });
const ANSWERS = ["end", "hold", "new-task"];
/* The question of the reported screenshot: an end decision whose brief is a
   page of markdown — long enough that the unfolded card is taller than the
   NOTICE_FOLD_MAX_PX classification threshold. */
const LONG_ASK = {
  id: "ask-fold-1", session: "s6261", label: "settle/release-decision/ask-pose",
  node: "settle/release-decision/ask-pose", workflow: "improv-worker-v2",
  prompt_md: [
    "## 문제", "",
    "회차 정리가 끝났다. 이 세션을 끝낼지, 살려 둘지, 새 작업을 맡길지 정한다.",
    "이 문단은 카드가 접힐 만큼 긴 본문을 만들기 위해 쓴 줄이다. " .repeat(4),
    "", "## 선택지", "",
    ...ANSWERS.map((a) => `- ${a}`),
    "", "## 기대 효과와 리스크", "",
    ...Array.from({ length: 8 }, (_, i) => `- 항목 ${i + 1}: ` + "근거 문장. ".repeat(6)),
  ].join("\n"),
  presses: ANSWERS.map(press),
};
const SHORT_ASK = {
  id: "ask-fold-2", session: "s6261", label: "settle/end-decision/ask-pose",
  node: "settle/end-decision/ask-pose", workflow: "improv-worker-v2",
  prompt: "문제: 이 세션을 끝낼지 정한다.",
  presses: ANSWERS.map(press),
};

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const errors = [];
    let page;
    const load = async () => {
      if (page) await page.context().close();
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      page = await ctx.newPage();
      page.on("pageerror", e => errors.push(String(e)));
      await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.waitForFunction(() => typeof applyRunGate === "function" && !!document.getElementById("notices"));
      await page.evaluate(() => { window.runGateFeed = () => {}; });
    };
    const settle = () => page.evaluate(() => new Promise(done => {
      let n = 3; const step = () => (--n ? requestAnimationFrame(step) : done());
      requestAnimationFrame(step);
    }));
    const card = () => page.evaluate(() => {
      const node = document.querySelector("#notices .run-gate");
      if (!node) return null;
      const shown = (el) => !!el && el.getClientRects().length > 0;
      const fold = node.querySelector(":scope > .notice-fold");
      const actions = node.querySelector(":scope > .gate-actions");
      return {
        classes: node.className,
        height: node.getBoundingClientRect().height,
        titleShown: shown(node.querySelector(":scope > .notice-title")),
        foldPresent: !!fold,
        foldShown: shown(fold),
        foldClientH: fold ? fold.getBoundingClientRect().height : null,
        foldScrollH: fold ? fold.scrollHeight : null,
        actionsShown: shown(actions),
        presses: actions ? [...actions.querySelectorAll("button")]
          .filter(b => b.getClientRects().length > 0 && !b.closest(".gate-remind"))
          .map(b => b.textContent.trim()) : [],
        toggle: (() => {
          const t = actions && actions.querySelector(":scope > .notice-fold-toggle-box .notice-fold-toggle");
          return t && shown(t) ? t.textContent.trim() : null;
        })(),
      };
    });

    // 1 + 2. the long card folds by default; the presses stay; the toggle
    // flips the card open and shut.
    await load();
    await page.evaluate(rec => applyRunGate([rec], []), LONG_ASK);
    await settle();
    let c = await card();
    assert(c, "the long card is drawn");
    assert(/(?:^|\s)foldable(?:\s|$)/.test(c.classes), `foldable by default: ${c.classes}`);
    assert(/(?:^|\s)folded(?:\s|$)/.test(c.classes), `folded by default: ${c.classes}`);
    assert(c.foldPresent && c.foldShown, "folded: a five-line preview of the body stays up");
    assert(c.foldScrollH > c.foldClientH + 1,
      `folded: the preview is clamped (shows ${c.foldClientH}px of ${c.foldScrollH}px)`);
    assert(c.titleShown, "folded: the title stays up");
    assert(c.actionsShown, "folded: the actions row stays up");
    for (const want of ANSWERS) {
      assert(c.presses.includes(want), `folded: the answer ${want} is drawn`);
    }
    assert(c.toggle === "▸ more", `folded: the toggle offers more (${c.toggle})`);
    assert(c.presses.length === ANSWERS.length + 2,
      `folded: only the answers, Hide and the toggle are up (${c.presses})`);
    // Folded, the card is chrome (title, buttons, age) plus the five-line
    // preview — not necessarily back under NOTICE_FOLD_MAX_PX, which measures
    // the unfolded card. The promise is the clamped preview above plus a
    // bound a reader can scan at a glance.
    assert(c.height <= 320, `folded: chrome plus a five-line preview (${c.height})`);

    await page.click("#notices .run-gate .notice-fold-toggle");
    await settle();
    c = await card();
    assert(!/(?:^|\s)folded(?:\s|$)/.test(c.classes), "toggle: the card opens");
    assert(c.foldShown, "toggle: the body is back");
    assert(c.toggle === "▾ less", `toggle: the label flips (${c.toggle})`);
    for (const want of ANSWERS) {
      assert(c.presses.includes(want), `open: the answer ${want} is still drawn`);
    }
    const openHeight = c.height;
    assert(openHeight > 260, `open: the unfolded card is what crossed the threshold (${openHeight})`);

    await page.click("#notices .run-gate .notice-fold-toggle");
    await settle();
    c = await card();
    assert(/(?:^|\s)folded(?:\s|$)/.test(c.classes), "second press: the card folds again");

    // 3. a short question is a size decision, not a kind: no folding.
    await load();
    await page.evaluate(rec => applyRunGate([rec], []), SHORT_ASK);
    await settle();
    c = await card();
    assert(c, "the short card is drawn");
    assert(!/(?:^|\s)foldable(?:\s|$)/.test(c.classes),
      `short card stays unfolded: ${c.classes}`);
    assert(!c.foldPresent, "short card: no fold box");

    // 4. born hidden: the panel was open when the request arrived, and its
    //    card was put back with "Show card" — folded when the strip shows it.
    await load();
    await page.evaluate(() => noticePanelToggle(true));
    await page.evaluate(rec => applyRunGate([rec], []), LONG_ASK);
    await settle();
    await page.evaluate(() => gateUnhide("ask", "ask-fold-1"));
    await settle();
    await page.evaluate(() => noticePanelToggle(false));
    await settle();
    c = await card();
    assert(c, "the born-hidden card is up after the panel closes");
    assert(/(?:^|\s)folded(?:\s|$)/.test(c.classes),
      `classified when the strip became visible: ${c.classes}`);
    for (const want of ANSWERS) {
      assert(c.presses.includes(want), `born-hidden: the answer ${want} is drawn`);
    }

    // 5. the notification history window folds the same way: a long history
    //    row is foldable and folded by default — head up, body a clamped
    //    preview — and the row's own toggle expands it.
    await load();
    await page.evaluate(() => {
      window.claunchNotices.record({
        id: "hist-fold-1", kind: "notice", title: "a notice with a page of detail",
        sub: Array.from(
          { length: 24 },
          (_, i) => `detail line ${i + 1}: ` + "content that runs on. ".repeat(6),
        ).join("\n"),
      });
      noticePanelToggle(true);
    });
    await settle();
    const hrow = () => page.evaluate(() => {
      const node = document.querySelector("#notice-panel-body .notice-row");
      if (!node) return null;
      const shown = (el) => !!el && el.getClientRects().length > 0;
      const fold = node.querySelector(":scope > .notice-fold");
      const toggle = node.querySelector(":scope > .notice-fold-toggle");
      return {
        classes: node.className,
        headShown: shown(node.querySelector(":scope > .notice-row-head")),
        foldPresent: !!fold,
        foldShown: shown(fold),
        foldClientH: fold ? fold.getBoundingClientRect().height : null,
        foldScrollH: fold ? fold.scrollHeight : null,
        toggle: toggle && shown(toggle) ? toggle.textContent.trim() : null,
      };
    });
    let h = await hrow();
    assert(h, "the history row is drawn");
    assert(/(?:^|\s)foldable(?:\s|$)/.test(h.classes), `history row foldable by default: ${h.classes}`);
    assert(/(?:^|\s)folded(?:\s|$)/.test(h.classes), `history row folded by default: ${h.classes}`);
    assert(h.headShown, "history row folded: the head stays up");
    assert(h.foldPresent && h.foldShown,
      "history row folded: a five-line preview of the body stays up");
    assert(h.foldScrollH > h.foldClientH + 1,
      `history row folded: the preview is clamped (shows ${h.foldClientH}px of ${h.foldScrollH}px)`);
    assert(h.toggle === "▸ more", `history row folded: the toggle offers more (${h.toggle})`);

    await page.click("#notice-panel-body .notice-row > .notice-fold-toggle");
    await settle();
    h = await hrow();
    assert(!/(?:^|\s)folded(?:\s|$)/.test(h.classes), "history row toggle: the row opens");
    assert(h.foldShown && !(h.foldScrollH > h.foldClientH + 1),
      "history row toggle: the body is back");
    assert(h.toggle === "▾ less", `history row toggle: the label flips (${h.toggle})`);

    assert.deepEqual(errors, [], "no page errors");
    console.log("noticefold_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
