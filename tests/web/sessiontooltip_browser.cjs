/* A session named on a card or a row tells the reader nothing but its name —
   the ask of someone who met "open s6110" on a gate card and could not say
   which session that was (claunch-q4qn0). The hover tooltip on a session name
   now adds what the session was opened to do (its opening task) and the
   briefing one-liner, read off the session poll — no extra fetch, and a name
   the poll has no record for keeps its plain title.

   Pinned here, measured in a real browser against the shipped app.js:
   1. a session with an opening task and a briefing one-liner: the
      gateSessionLink tooltip keeps the surface's own first line and adds
      both, on their own lines;
   2. a session with a task and no briefing: the tooltip has the task line
      and no briefing line;
   3. a session the poll has no record for: the tooltip is the plain
      "open <name>" — nothing is appended;
   4. a long task is clipped, not dropped — the first line survives whole or
      cut with an ellipsis, and the tooltip stays one attribute string. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const TASK_TEXT = "특정 크기 이상의 notification card를 기본으로 접어서 표시하고 토글을 만든다";
const BRIEF_TEXT = "알림 카드 접힘 회차를 마감하고 착지시켰다";
const SESSIONS = [
  { name: "s6110", status: "idle", running: true, project: "claunch",
    task: TASK_TEXT + "\n둘째 줄은 툴팁에 안 들어간다", role: "worker",
    briefing: { one_line: BRIEF_TEXT, state: "ready" } },
  { name: "s6200", status: "idle", running: true, project: "claunch",
    task: "opening task만 있는 세션", role: "worker" },
];

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") {
      data = { sessions: SESSIONS, llm_configured: false };
    } else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
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

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const errors = [];
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    page.on("pageerror", e => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.waitForFunction(() =>
      typeof gateSessionLink === "function" &&
      typeof sessionsCache !== "undefined" &&
      sessionsCache.some((s) => s.name === "s6110"));
    await page.evaluate(() => { window.runGateFeed = () => {}; });

    const tooltip = (name) => page.evaluate((n) => {
      const node = gateSessionLink(n);
      return node.title;
    }, name);

    // 1. task + briefing: both lines ride the tooltip, base line first.
    let t = await tooltip("s6110");
    assert(t.startsWith("open s6110"), `the surface's own line leads (${JSON.stringify(t)})`);
    assert(t.includes("task: " + TASK_TEXT), `the opening task is in the tooltip (${JSON.stringify(t)})`);
    assert(t.includes("briefing: " + BRIEF_TEXT), `the briefing one-liner is in the tooltip (${JSON.stringify(t)})`);
    assert(!t.includes("둘째 줄"), "the task's second line stays out — the first line is the tooltip's");
    assert(t.indexOf("task: ") < t.indexOf("briefing: "),
      "task precedes briefing, matching the rail card's own order");

    // 2. task without briefing: the task line, no briefing line.
    t = await tooltip("s6200");
    assert(t.includes("task: opening task만 있는 세션"), `task-only session shows its task (${JSON.stringify(t)})`);
    assert(!t.includes("briefing:"), "no briefing line is invented for a session without one");

    // 3. unknown session: the plain title, nothing appended.
    t = await tooltip("s9999");
    assert(t === "open s9999", `an unknown session keeps the plain tooltip (${JSON.stringify(t)})`);

    // 4. a long first line is clipped with an ellipsis, and the result is
    //    still a single attribute string.
    t = await page.evaluate(() => {
      sessionsCache.push({
        name: "s6300", status: "idle", running: true, project: "claunch",
        task: "한".repeat(500), role: "worker",
      });
      return gateSessionLink("s6300").title;
    });
    assert(t.includes("…"), `a 500-character task is clipped (${t.length} chars)`);
    assert(t.split("\n").length === 3, `clipped tooltip keeps base + blank + task line shape (${t.split("\n").length} lines)`);

    assert.deepEqual(errors, [], `no page errors: ${errors.join("; ")}`);
    console.log("sessiontooltip_browser: all assertions passed");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
