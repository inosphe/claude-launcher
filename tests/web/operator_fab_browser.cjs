/* Browser integration for the floating Operator (operator_fab.js,
   claunch-iw5i8.5): the dock holds the Operator's and Chat's segments; the
   badge counts the asks waiting across every operator (red while some are
   not shown yet); opened on a session's page the dialog selects the operator
   whose scope holds that session; asks are answered and messages sent from
   the dialog; each operator keeps its own draft; Alt+O opens and closes it;
   management stays on the Observer page, which hides the segment; on a phone
   the dialog takes the screen and its conversation is its one scroller.
   Above the conversation a session line names the operator, its session and
   that session's state and links to the session's page (claunch-iw5i8.13);
   the dialog is resized from its edges about its centre and the size is kept
   in localStorage (claunch-iw5i8.14).
   CLAUNCH_PLAYWRIGHT may name an installed playwright-core.
   Run: node tests/web/operator_fab_browser.cjs. No live daemon. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.CLAUNCH_PLAYWRIGHT || "playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sent = [];
const at = n => new Date(Date.UTC(2026, 9, 17, 9, 0, n)).toISOString();
const sessions = [
  { name: "s1", status: "idle", running: true, project: "alpha" },
  { name: "s2", status: "idle", running: true, project: "beta" },
];
const ops = {
  opA: { id: "opA", name: "alpha-ops", all: false, projects: ["alpha"], watch: true, session: null, seen: [],
         feed: [{ id: "p1", at: at(1), kind: "post", role: "bot", text: "alpha 보고", level: "info", refs: [] }] },
  opB: { id: "opB", name: "beta-ops", all: false, projects: ["beta"], watch: true, session: "s900", seen: [],
         feed: [{ id: "p2", at: at(2), kind: "post", role: "bot", text: "beta 보고\n\n- **s2**: `pytest` 대기", level: "info", refs: ["s2"] },
                { id: "a1", at: at(3), kind: "ask", role: "bot", type: "approve", text: "s2를 재시작할까요?", choices: [], level: "urgent", refs: ["s2"], answer: null }] },
};
function summary(o) {
  const open = o.feed.filter(e => e.kind === "ask" && !e.answer).map(e => e.id);
  return { id: o.id, name: o.name, all: o.all, projects: o.projects, profile: "p1:claude", watch: o.watch,
    operator: o.session ? { name: o.session, running: true, status: "idle" } : null,
    pending: open.length, unseen: open.filter(id => !o.seen.includes(id)).length };
}
function operatorsApi(req, res, body) {
  const url = req.url.split("?")[0], parts = url.split("/").slice(3).map(decodeURIComponent);
  const json = (value, status = 200) => { res.statusCode = status; return res.end(JSON.stringify(value)); };
  const data = body ? JSON.parse(body) : {};
  if (req.method !== "GET") sent.push({ method: req.method, url, body: data });
  if (!parts.length) return json({ operators: Object.values(ops).map(summary) });
  const o = ops[parts[0]], rest = parts.slice(1).join("/");
  if (!o) return json({ error: "no such operator" }, 404);
  if (!rest) return json({ ...summary(o), feed: o.feed, sessions: [], gates: [] });
  if (rest === "seen") { o.seen = o.feed.filter(e => e.kind === "ask" && !e.answer).map(e => e.id); return json(summary(o)); }
  if (rest === "message") {
    const entry = { id: `m${o.feed.length}`, at: new Date().toISOString(), kind: "user", role: "user", text: data.text };
    o.feed.push(entry);
    return json({ ...entry, nudge: o.session ? "sent" : "no-operator" });
  }
  const answer = rest.match(/^asks\/([^/]+)\/answer$/);
  if (answer) {
    const e = o.feed.find(x => x.id === answer[1]);
    e.answer = { decision: data.decision ?? null, text: data.text || null, at: new Date().toISOString() };
    return json({ ...e, nudge: "sent" });
  }
  return json({ error: "unhandled" }, 400);
}
const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    if (req.url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      const route = req.url.split("?")[0];
      if (route === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
      if (route.startsWith("/api/operators")) return operatorsApi(req, res, body);
      if (route === "/api/sessions") return res.end(JSON.stringify({ sessions, llm_configured: true }));
      // One session read on its own: the dialog asks for it when the app's
      // session list has not reached it yet.
      const one = route.match(/^\/api\/sessions\/([^/]+)$/);
      if (one) return res.end(JSON.stringify(sessions.find(x => x.name === decodeURIComponent(one[1])) || {}));
      if (route === "/api/observer") return res.end(JSON.stringify({ enabled: false, scope: "all", sessions: sessions.map(s => ({ ...s, meshes: [], summary: "", events: [] })) }));
      if (route === "/api/chat/rooms") return res.end('{"machine":"pcA","rooms":[]}');
      if (route === "/api/daemon") return res.end('{"version":"test","boot_id":"test"}');
      if (req.method === "GET") {
        if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(route)) return res.end("[]");
        return res.end("{}");
      }
      return res.end('{"ok":true}');
    }
    const file = path.join(root, (req.url === "/" || req.url.startsWith("/#") ? "index.html" : req.url.split("?")[0].replace("/static/", "")));
    try {
      res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
      res.end(fs.readFileSync(file));
    } catch { res.statusCode = 404; res.end(); }
  });
});
const lastSent = (method, suffix) => sent.filter(r => r.method === method && r.url.endsWith(suffix)).at(-1);

(async () => {
  await safeListen(server);
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CLAUNCH_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      // The dialog's 10 s and the badge's 15 s polls, at test speed.
      const interval = window.setInterval;
      window.setInterval = (fn, ms, ...args) => interval(fn, ms === 10000 || ms === 15000 ? 200 : ms, ...args);
    });
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(base + "#/");
    await page.waitForSelector("#fab-dock .operator-fab");

    // One dock, the Operator's segment and Chat's.
    const dock = await page.evaluate(() => ({
      docks: document.querySelectorAll(".fab-dock").length,
      parts: [...document.querySelectorAll("#fab-dock > button")].map(e => e.className.split(" ")[0]),
    }));
    assert.deepEqual(dock, { docks: 1, parts: ["operator-fab", "chat-fab"] });
    // The badge: beta-ops' ask, not shown yet, so the segment is red.
    await page.waitForFunction(() => document.getElementById("operator-fab-badge")?.textContent === "1");
    assert.equal(await page.locator(".operator-fab").evaluate(e => e.classList.contains("operator-fab-alert")), true);
    assert.equal(sent.filter(r => r.url.includes("/api/operator/") && r.method === "POST").length, 0, "nothing v1 is posted");

    // On s2's page (project beta) the dialog opens on beta-ops.
    await page.evaluate(() => { location.hash = "#/s/s2"; });
    await page.waitForFunction(() => location.hash === "#/s/s2");
    await page.click(".operator-fab");
    await page.waitForSelector(".opfab-modal[open] #opfab-view .op-entry");
    const selectedTab = () => page.locator('#opfab-view .op-tab[aria-selected="true"]').innerText();
    await page.waitForFunction(() => document.querySelector('#opfab-view .op-tab[aria-selected="true"]')?.textContent.includes("beta-ops"));
    assert.match(await selectedTab(), /beta-ops/);
    // Shown is seen: the badge keeps the open ask but is no longer red.
    await page.waitForFunction(() => !document.querySelector(".operator-fab").classList.contains("operator-fab-alert"));
    assert(lastSent("POST", "/api/operators/opB/seen"));
    assert.equal(await page.textContent("#operator-fab-badge"), "1");
    // The session line: outside the scroller, the operator, its session and
    // state, and the session's page.
    const sessionLine = () => page.evaluate(() => {
      const bar = document.querySelector("#opfab-view > .op-sessionbar");
      const link = bar?.querySelector("a.op-bar-open");
      return bar && { hidden: bar.hidden, who: bar.querySelector(".op-bar-who").textContent,
        session: bar.querySelector(".op-bar-session").textContent, href: link ? link.getAttribute("href") : null };
    });
    assert.deepEqual(await sessionLine(), { hidden: false, who: "beta-ops", session: "s900 · 실행 중 (idle)", href: "#/s/s900" });
    // Management is the Observer page's.
    assert.equal(await page.locator("#opfab-view .op-new").count(), 0);
    assert.equal(await page.locator("#opfab-view .op-actions").count(), 0);
    assert.equal(await page.locator("#opfab-view a.op-manage").getAttribute("href"), "#/observer");
    // The input row has the focus.
    assert.equal(await page.locator("#opfab-view .op-composer textarea").evaluate(e => document.activeElement === e), true);

    // The floating view renders bodies as markdown, like the Observer page.
    assert.deepEqual(await page.locator('#opfab-view .op-entry[data-entry="p2"] .op-text').evaluate(e => ({
      md: e.classList.contains("md"), li: e.querySelectorAll("li").length, code: e.querySelectorAll("code").length })),
      { md: true, li: 1, code: 1 });

    // Answer the ask.
    await page.locator('#opfab-view .op-entry[data-entry="a1"] button', { hasText: "승인" }).click();
    await page.waitForFunction(() => /사용자 답변 · 승인/.test(document.querySelector('#opfab-view .op-entry[data-entry="a1"]')?.textContent || ""));
    assert.deepEqual(lastSent("POST", "/api/operators/opB/asks/a1/answer").body, { decision: "approve" });
    await page.waitForFunction(() => document.getElementById("operator-fab-badge").hidden);

    // Send with Enter; the entry shows and the row empties.
    const text = "#opfab-view .op-composer textarea";
    await page.fill(text, "s2는 지금 무엇을 하고 있습니까?");
    await page.press(text, "Enter");
    await page.waitForFunction(() => [...document.querySelectorAll("#opfab-view .op-user")].some(e => e.textContent.includes("s2는 지금 무엇을")));
    assert.deepEqual(lastSent("POST", "/api/operators/opB/message").body, { text: "s2는 지금 무엇을 하고 있습니까?" });
    assert.equal(await page.inputValue(text), "");
    assert.match(await page.textContent("#opfab-view .op-composer-status"), /operator beta-ops: 메시지 전달됨/);

    if (process.env.CLAUNCH_SCREENSHOT_DESKTOP) await page.screenshot({ path: process.env.CLAUNCH_SCREENSHOT_DESKTOP });
    // Each operator keeps its own draft.
    await page.fill(text, "beta 초안");
    await page.locator("#opfab-view .op-tab", { hasText: "alpha-ops" }).click();
    await page.waitForFunction(() => document.querySelector('#opfab-view .op-tab[aria-selected="true"]')?.textContent.includes("alpha-ops"));
    assert.equal(await page.inputValue(text), "");
    // An operator with no session says so and has no link.
    assert.deepEqual(await sessionLine(), { hidden: false, who: "alpha-ops", session: "세션 없음", href: null });
    await page.locator("#opfab-view .op-tab", { hasText: "beta-ops" }).click();
    assert.equal(await page.inputValue(text), "beta 초안");
    await page.fill(text, "");

    // Alt+O closes and opens it; on s1's page (alpha) it moves to alpha-ops.
    await page.keyboard.press("Alt+o");
    await page.waitForFunction(() => !document.querySelector(".opfab-modal").open);
    await page.evaluate(() => { location.hash = "#/s/s1"; });
    await page.keyboard.press("Alt+o");
    await page.waitForFunction(() => document.querySelector(".opfab-modal").open
      && document.querySelector('#opfab-view .op-tab[aria-selected="true"]')?.textContent.includes("alpha-ops"));
    // A session link in the conversation closes the dialog on its way.
    await page.locator("#opfab-view .op-tab", { hasText: "beta-ops" }).click();
    await page.waitForSelector('#opfab-view .op-entry[data-entry="p2"] a.op-ref');
    await page.click('#opfab-view .op-entry[data-entry="p2"] a.op-ref');
    await page.waitForFunction(() => !document.querySelector(".opfab-modal").open);
    assert.equal(await page.evaluate(() => location.hash), "#/observer/session/s2");
    await page.evaluate(() => { location.hash = "#/"; });
    await page.waitForFunction(() => !document.body.classList.contains("observer-active"));
    // The session line's link opens the operator's session and closes the dialog.
    await page.click(".operator-fab");
    await page.locator("#opfab-view .op-tab", { hasText: "beta-ops" }).click();
    await page.waitForSelector("#opfab-view .op-sessionbar a.op-bar-open");
    await page.click("#opfab-view .op-sessionbar a.op-bar-open");
    await page.waitForFunction(() => !document.querySelector(".opfab-modal").open);
    assert.equal(await page.evaluate(() => location.hash), "#/s/s900");
    await page.evaluate(() => { location.hash = "#/"; });

    // The size: dragged from an edge it grows on both sides and stays centred,
    // the screen caps it, the next opening and a reload keep it, a double
    // click returns to the default, and a damaged value reads as the default.
    const box = () => page.evaluate(() => {
      const r = document.querySelector(".opfab-modal").getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height), cx: Math.round(r.left + r.width / 2), cy: Math.round(r.top + r.height / 2) };
    });
    const stored = () => page.evaluate(() => localStorage.getItem("claunch-fab-operator-size"));
    const drag = async (edge, dx, dy) => {
      const g = await page.locator(`.opfab-grip-${edge}`).boundingBox();
      const x = g.x + g.width / 2, y = g.y + g.height / 2;
      await page.mouse.move(x, y); await page.mouse.down();
      await page.mouse.move(x + dx / 2, y + dy / 2); await page.mouse.move(x + dx, y + dy);
      await page.mouse.up();
    };
    await page.click(".operator-fab");
    await page.waitForSelector(".opfab-modal[open]");
    assert.deepEqual(await box(), { w: 560, h: 720, cx: 640, cy: 450 });
    assert.equal(await stored(), null);
    await drag("e", 100, 0);
    assert.deepEqual(await box(), { w: 760, h: 720, cx: 640, cy: 450 });
    await drag("n", 0, -40);
    assert.deepEqual(await box(), { w: 760, h: 800, cx: 640, cy: 450 });
    assert.deepEqual(JSON.parse(await stored()), { w: 760, h: 800 });
    await drag("se", 2000, 2000);
    assert.deepEqual(await box(), { w: 1240, h: 852, cx: 640, cy: 450 }, "capped at the screen");
    await drag("nw", 2000, 2000);
    assert.deepEqual(await box(), { w: 360, h: 320, cx: 640, cy: 450 }, "held at the smallest size");
    await drag("w", -300, 0);
    assert.deepEqual(await box(), { w: 960, h: 320, cx: 640, cy: 450 });
    await page.keyboard.press("Escape");
    await page.click(".operator-fab");
    assert.deepEqual(await box(), { w: 960, h: 320, cx: 640, cy: 450 }, "kept for the next opening");
    await page.reload();
    await page.waitForSelector("#fab-dock .operator-fab");
    await page.click(".operator-fab");
    await page.waitForSelector(".opfab-modal[open]");
    assert.deepEqual(await box(), { w: 960, h: 320, cx: 640, cy: 450 }, "kept across a reload");
    await page.locator(".opfab-grip-e").dblclick();
    assert.deepEqual(await box(), { w: 560, h: 720, cx: 640, cy: 450 });
    assert.equal(await stored(), null);
    await page.evaluate(() => localStorage.setItem("claunch-fab-operator-size", "{not json"));
    await page.reload();
    await page.waitForSelector("#fab-dock .operator-fab");
    await page.click(".operator-fab");
    await page.waitForSelector(".opfab-modal[open]");
    assert.deepEqual(await box(), { w: 560, h: 720, cx: 640, cy: 450 }, "a damaged value reads as the default");
    // A stored size still applies on a phone only as the full screen.
    await page.evaluate(() => localStorage.setItem("claunch-fab-operator-size", JSON.stringify({ w: 900, h: 400 })));
    await page.keyboard.press("Escape");
    await page.reload();
    await page.waitForSelector("#fab-dock .operator-fab");

    // On a phone the dialog takes the screen; its conversation is the one
    // scroller and nothing runs sideways.
    for (let i = 0; i < 30; i++) ops.opB.feed.push({ id: `q${i}`, at: at(10 + i), kind: "post", role: "bot", text: `줄 ${i} `.repeat(12), level: "info", refs: [] });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.click(".operator-fab");
    await page.waitForSelector('#opfab-view .op-entry[data-entry="q29"]');
    const fit = await page.evaluate(() => {
      const dialog = document.querySelector(".opfab-modal").getBoundingClientRect();
      const area = document.querySelector("#opfab-view .op-area");
      const scrollers = [...document.querySelectorAll("#opfab-view *")].filter(e => {
        const cs = getComputedStyle(e);
        return (/auto|scroll/.test(cs.overflowY) && e.scrollHeight > e.clientHeight + 1) || (/auto|scroll/.test(cs.overflowX) && e.scrollWidth > e.clientWidth + 1);
      }).map(e => e.className || e.tagName.toLowerCase());
      const bar = document.querySelector("#opfab-view > .op-sessionbar").getBoundingClientRect();
      return { width: Math.round(dialog.width), height: Math.round(dialog.height), scrollers, atEnd: area.scrollHeight - area.scrollTop - area.clientHeight < 24,
        sideways: area.scrollWidth > area.clientWidth + 1, barShown: bar.height > 0 && bar.top >= 0 && bar.bottom <= area.getBoundingClientRect().top + 1,
        grips: [...document.querySelectorAll(".opfab-grip")].filter(g => g.getBoundingClientRect().width > 0).length };
    });
    assert.deepEqual(fit, { width: 390, height: 844, scrollers: ["op-area"], atEnd: true, sideways: false, barShown: true, grips: 0 });
    if (process.env.CLAUNCH_SCREENSHOT) await page.screenshot({ path: process.env.CLAUNCH_SCREENSHOT });
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".opfab-modal").open);

    // The Observer page has the operators on it; the segment steps aside.
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.evaluate(() => { location.hash = "#/observer"; });
    await page.waitForSelector("body.observer-active");
    assert.equal(await page.isVisible(".operator-fab"), false);
    assert.equal(await page.isVisible(".chat-fab"), true);

    assert.deepEqual(errors, []);
    console.log("PASS: one dock with both segments, markdown bodies, badge sum and alert, session-scoped default operator, seen on show, no management in the dialog, ask answer, Enter send, per-operator drafts, Alt+O toggle, link closes dialog, session line with link, centred resize kept in localStorage, phone full-screen with one scroller, hidden on Observer");
  } finally { await browser.close(); server.close(); server.closeAllConnections(); }
})().catch(error => { console.error(error); server.close(); server.closeAllConnections(); process.exitCode = 1; });
