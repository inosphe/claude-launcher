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
const { safeListen } = require("./safe_listen.cjs");
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
/* What the tip's card draws, by density. The tip is a CLONE of the list's row
   (app.js sessionCardCopy), so it wears whatever density that row has — the
   default compact card — and the per-density rules reach it because
   sessionGridTipStyles re-scopes every `#session-list …` rule onto `#sg-tip`
   verbatim, `[data-rail-mode="compact"]` and all.
   A compact card hides the action buttons and keeps the controls that are not
   an action on the session (the key chip, and the two refreshes, style.css).
   So the questions this file asks are the density's, not a flat list of seven:
   what is painted shares the line it is on, and the two refreshes are painted
   on BOTH densities. Listing all seven as "on the buttons line" is what this
   file used to do; it went on passing through the round that hid two of them
   only because no round before that had hidden any (claunch-6ezw3.1). */
const BUTTONS = [".sess-plus", ".sess-pin", ".sess-observe", ".sess-note-edit", ".sess-info",
  ".sess-status-check-rowref", ".sess-brief-rowref"];
const ACTIONS = [".sess-plus", ".sess-pin", ".sess-observe", ".sess-note-edit", ".sess-info"];
const ALWAYS_PAINTED = [".sess-status-check-rowref", ".sess-brief-rowref"];
(async () => {
  await safeListen(server);
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
    // buttons line's; a button travels in the .rail-actions box one line
    // under the name line (claunch-ku033), so it shares the box's centre
    // whatever the width.
    const pinned = (width) => page.evaluate(({ width, buttons }) => {
      const cell = document.querySelector('#session-grid .sg-cell[data-name="s2"]');
      cell.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
      const tip = document.getElementById("sg-tip");
      if (width) tip.style.width = width;
      const mid = (el) => { const r = el.getBoundingClientRect(); return (r.top + r.bottom) / 2; };
      const card = tip.querySelector("li.sess-card");
      const actions = card && card.querySelector(".rail-actions");
      const name = card && card.querySelector(".rail-name");
      const box = (el) => { const r = el.getBoundingClientRect(); return { w: r.width, h: r.height, y: mid(el) }; };
      return {
        pinned: tip.classList.contains("pinned"),
        mode: card && card.dataset.railMode,
        width: tip.getBoundingClientRect().width,
        actions: actions && mid(actions),
        nameWidth: name && name.getBoundingClientRect().width,
        buttons: Object.fromEntries(buttons.map((sel) => {
          const el = card && card.querySelector(sel);
          return [sel, el ? box(el) : null];
        })),
      };
    }, { width, buttons: BUTTONS });
    /* A button that is not painted has no line to share, and two unpainted
       boxes agree at any tolerance -- the vacuous pass this file shares with
       railchildline_browser. Buttons are only compared once they have a box. */
    const offActions = (got) => Object.entries(got.buttons)
      .filter(([, b]) => b && b.w > 0 && b.h > 0)
      .filter(([, b]) => got.actions === null || Math.abs(b.y - got.actions) > 4)
      .map(([sel]) => sel);
    const painted = (got) => Object.entries(got.buttons)
      .filter(([, b]) => b && b.w > 0 && b.h > 0).map(([sel]) => sel);
    const unpainted = (got) => Object.entries(got.buttons)
      .filter(([, b]) => !b || b.w === 0 || b.h === 0).map(([sel]) => sel);

    const got = await pinned(null);
    assert.ok(got.pinned, "a right-click pins the card");
    assert.ok(got.actions !== null, "the pinned card holds the list's row");
    assert.deepEqual(offActions(got), [], `every painted button sits on the buttons line at ${got.width}px`);
    // The five actions are only drawn on the extended card; the tip is a copy
    // of a compact row, so at this density they are expected absent -- and
    // that expectation is pinned, so this file cannot quietly stop covering
    // them by having them disappear for another reason.
    assert.equal(got.mode, "compact", "the tip copies the list row's compact density");
    for (const sel of ACTIONS) {
      assert.ok(unpainted(got).includes(sel),
        `${sel} is the expanded card's, so the compact tip does not draw it`);
    }
    for (const sel of ALWAYS_PAINTED) {
      const b = got.buttons[sel];
      assert.ok(b && b.w > 0 && b.h > 0, `${sel} is painted on the ${got.mode} card (${b ? `${b.w}x${b.h}` : "absent"})`);
    }

    // The same card at the old width wraps; the buttons still share their
    // line, and the name keeps a readable width instead of stacking a
    // character per line.
    await page.keyboard.press("Escape");
    const narrow = await pinned("320px");
    assert.deepEqual(offActions(narrow), [], "at 320px the buttons still share the buttons line");
    for (const sel of ALWAYS_PAINTED) {
      const b = narrow.buttons[sel];
      assert.ok(b && b.w > 0 && b.h > 0, `${sel} survives the 320px wrap (${b ? `${b.w}x${b.h}` : "absent"})`);
    }
    assert.deepEqual(painted(narrow), painted(got), "narrowing does not take a control away");
    assert.ok(narrow.nameWidth >= 100,
      `at 320px the name keeps a readable width (${narrow.nameWidth}px)`);
    assert.deepEqual(errors, []);
    console.log("sessiongridtip_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
