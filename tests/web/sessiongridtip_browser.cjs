/* The grid's pinned card keeps the list row's first line on one line.

   A right-click on a grid cell pins #sg-tip, a copy of the list's row for that
   session. The copy takes the list's rules but not the rail's @container
   rule, so it gets the wide one-line layout, and at 320px every card pushed
   its buttons (+ pin eye pencil ⓘ, the checks refresh, the ▸) onto a second
   line and squeezed the name to a few pixels (claunch-pua72). This drives the
   shipped UI against a fixture daemon, pins a card with every one of those
   buttons, and asserts they sit on the name's line -- then narrows the card
   back to 320px to show the check sees the wrap it is there to catch. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const now = new Date().toISOString();
const checks = ["C", "M", "T"].map((name, i) => ({
  id: `c${i}`, name, question: `${name}?`, answer: i === 1 ? "no" : "yes", reported_at: now, source: "agent",
}));
const sessions = [
  { name: "s1", status: "idle", cwd: "/repo", harness: "claude", model: "opus" },
  {
    name: "s2", status: "idle", harness: "claude", model: "opus", profile: "sr:claude",
    borrow: "sr", cwd: "/repo/.claude/worktrees/s1-s2", branch: "s2-a-longer-feature-branch",
    issue: "claunch-abcde", status_checks: checks, last_output_at: now, last_input_at: now,
    context: { tokens: 200000, input: 2, cache_read: 199000, cache_write: 900, output: 98, model: "claude-opus-5-5", at: now, compact_window: 400000 },
    briefing: { one_line: "one line of what this session is doing", state: "working", goal: "g", now: "n" },
  },
];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: true };
    else if (url.pathname.endsWith("/capture")) data = { lines: [] };
    else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});
const BUTTONS = [".sess-plus", ".sess-pin", ".sess-observe", ".sess-note-edit", ".sess-info",
  ".sess-status-check-rowref", ".sess-brief-rowref", ".sess-brief-toggle"];
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", e => errors.push(String(e)));
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      localStorage.setItem("claunch_session_view:/", "grid");
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('#session-grid .sg-cell[data-name="s2"]').waitFor();
    await page.locator('#session-list li.sess-card[data-name="s2"] .sess-status-check-rowref').waitFor({ state: "attached" });
    // The card's buttons, each with the vertical centre of its box, and the
    // name group's; a button on the name's line shares its centre.
    const pinned = (width) => page.evaluate(({ width, buttons }) => {
      const cell = document.querySelector('#session-grid .sg-cell[data-name="s2"]');
      cell.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
      const tip = document.getElementById("sg-tip");
      if (width) tip.style.width = width;
      const mid = (el) => { const r = el.getBoundingClientRect(); return (r.top + r.bottom) / 2; };
      const card = tip.querySelector("li.sess-card");
      const head = card && card.querySelector(".rail-head");
      return {
        pinned: tip.classList.contains("pinned"),
        width: tip.getBoundingClientRect().width,
        head: head && mid(head),
        buttons: Object.fromEntries(buttons.map((sel) => {
          const el = card && card.querySelector(sel);
          return [sel, el ? mid(el) : null];
        })),
      };
    }, { width, buttons: BUTTONS });
    const offLine = (got) => Object.entries(got.buttons)
      .filter(([, y]) => y === null || Math.abs(y - got.head) > 4).map(([sel]) => sel);

    const got = await pinned(null);
    assert.ok(got.pinned, "a right-click pins the card");
    assert.ok(got.head !== null, "the pinned card holds the list's row");
    assert.deepEqual(offLine(got), [], `every button sits on the name's line at ${got.width}px`);

    // The same card at the old width wraps; the check above would see it.
    await page.keyboard.press("Escape");
    const narrow = await pinned("320px");
    assert.ok(offLine(narrow).length > 0, "at 320px the buttons leave the name's line");
    assert.deepEqual(errors, []);
    console.log("sessiongridtip_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
