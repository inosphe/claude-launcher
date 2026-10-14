/* A poll that changes nothing changes nothing on the page (claunch-v87,
   claunch-0stm2).

   The dashboard repainted by assignment and by teardown: every tick wrote
   each rail row's title, aria-pressed and text again, and most ticks tore the
   whole rail down and built it back. Each of those is a mutation record, and
   a browser extension that watches the page (1Password walks the document on
   every batch) paid for all of them -- ~1 s of main thread every few seconds,
   which was the input lag. Measured on the live dashboard (389 rail rows, 30 s
   window): 34,519 records before, about 1,200 after.

   The in-place rail (rowSignature, kept rows) still wrote on every poll: the
   seen/tps/context lines were replaced whatever they showed, and a row's
   tooltip went back to its base text before the context note was added again
   -- two writes with a different value in between, which no same-value guard
   can drop. Those lines are now rebuilt only when what they show moves.

   This drives the shipped page against a fixture daemon and counts the
   records a MutationObserver sees:
     1. quietdom.js: a same-value write (title, class, classList, text, aria,
        disabled, setAttribute) is no record; a changed value still is;
     2. an idle poll leaves the rail without a single record;
     3. one session changing state rebuilds that row only, swapped into its
        own slot -- every other row is the same node and does not move, the
        row that held the keyboard still holds it, and the next poll is
        idle again;
     4. a session leaving the middle of the rail removes that row only;
     5. the session pickers keep their option nodes when a status moves;
     6. the details panel redrawn from the same answer adds no record. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
let sessions = [
  { name: "p0", status: "idle", cwd: "/repo", harness: "claude", model: "opus", conversation_id: "a" },
  { name: "c1", status: "busy", cwd: "/repo", harness: "claude", model: "opus", parent: "p0", conversation_id: "b" },
  { name: "c2", status: "idle", cwd: "/repo", harness: "claude", model: "opus", parent: "p0" },
  // A tps reading, as the daemon answers it: `age_s` grows by the poll
  // interval on every answer even when nothing was metered.
  { name: "q3", status: "idle", cwd: "/other", harness: "claude", model: "opus",
    tps: { ts: new Date(Date.now() - 7200e3).toISOString(), status: 200, counted: true,
           idle: true, age_s: 7200, max_age_s: 600, window: 0 } },
  { name: "q4", status: "idle", cwd: "/other", harness: "claude", model: "opus" },
];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    const meta = url.pathname.match(/^\/api\/sessions\/([^/]+)\/meta$/);
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: true };
    else if (meta) data = { session: sessions.find((s) => s.name === decodeURIComponent(meta[1])) || {} };
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

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      // The page's own timers stay off: each step below polls by hand, so a
      // record seen in a window is one that step made.
      window.setInterval = () => 0;
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('#session-list li.sess-card[data-name="q4"]').waitFor();

    // Records under `selector` while `fn` runs (and its microtasks settle).
    const recordsDuring = (selector, fn) => page.evaluate(async ([selector, fn]) => {
      const target = document.querySelector(selector);
      const seen = [];
      const mo = new MutationObserver((rs) => {
        for (const r of rs) {
          const t = r.target.nodeType === 1 ? r.target : r.target.parentElement;
          seen.push(`${r.type}${r.attributeName ? ":" + r.attributeName : ""} ${t && t.className}`);
        }
      });
      mo.observe(target, { subtree: true, childList: true, attributes: true, characterData: true });
      await (0, eval)(fn)();
      await new Promise((r) => setTimeout(r, 120));
      mo.takeRecords().forEach(() => {});
      mo.disconnect();
      return seen;
    }, [selector, fn.toString()]);

    /* ---- 1. same-value writes are no records -------------------------- */
    const quiet = await page.evaluate(() => {
      const box = document.createElement("div");
      document.body.appendChild(box);
      const b = document.createElement("button");
      b.className = "x on";
      b.title = "t";
      b.textContent = "label";
      b.setAttribute("aria-pressed", "true");
      b.disabled = true;
      box.appendChild(b);
      const records = [];
      const mo = new MutationObserver((rs) => records.push(...rs));
      mo.observe(box, { subtree: true, childList: true, attributes: true, characterData: true });
      b.className = "x on";
      b.title = "t";
      b.textContent = "label";
      b.setAttribute("aria-pressed", "true");
      b.ariaPressed = "true";
      b.disabled = true;
      b.classList.add("on");
      b.classList.remove("absent");
      b.classList.toggle("on", true);
      const same = mo.takeRecords().length + records.length;
      b.title = "u";
      b.textContent = "other";
      b.classList.toggle("on", false);
      const changed = mo.takeRecords().length;
      mo.disconnect();
      box.remove();
      return { installed: !!Element.prototype.__quietDom, same, changed,
               title: b.title, text: b.textContent, on: b.classList.contains("on") };
    });
    assert.equal(quiet.installed, true, "quietdom.js is loaded ahead of the page's scripts");
    assert.equal(quiet.same, 0, "same-value writes make no mutation record");
    assert.equal(quiet.changed, 3, "a write that changes the value still records it");
    assert.deepEqual([quiet.title, quiet.text, quiet.on], ["u", "other", false],
      "the writes that change something still land");

    /* ---- 2. an idle poll leaves the rail alone ------------------------- */
    await page.evaluate(() => refreshSessions());
    const idle = await recordsDuring("#session-list", () => refreshSessions());
    assert.deepEqual(idle, [], "an idle poll makes no record on the rail");
    // The tps reading's raw age moving on is still an idle poll: it rebuilt
    // every row that had a reading (claunch-0stm2).
    sessions = sessions.map((s) => s.tps ? { ...s, tps: { ...s.tps, age_s: s.tps.age_s + 5 } } : s);
    const aged = await recordsDuring("#session-list", () => refreshSessions());
    assert.deepEqual(aged, [], "a tps reading that only aged makes no record on the rail");

    /* ---- 3. a changed session touches its own row only ---------------- */
    const snapshot = () => page.evaluate(() => {
      window.__rows = new Map([...document.querySelectorAll("#session-list li.sess-card")]
        .map((li) => [li.dataset.name, li]));
    });
    const compare = () => page.evaluate(() => {
      const now = [...document.querySelectorAll("#session-list li.sess-card")];
      return {
        order: now.map((li) => li.dataset.name),
        same: now.filter((li) => __rows.get(li.dataset.name) === li).map((li) => li.dataset.name),
        focused: document.activeElement && document.activeElement.dataset
          ? document.activeElement.dataset.name : null,
        dots: Object.fromEntries(now.map((li) => [li.dataset.name, li.querySelector(".dot").className])),
        plus: Object.fromEntries(now.map((li) => [li.dataset.name, !!li.querySelector(".sess-plus")])),
      };
    });
    await snapshot();
    await page.evaluate(() => document.querySelector('#session-list li.sess-card[data-name="q3"]').focus());
    // busy -> idle: the in-place redraw keys a row on its whole record
    // (rowSignature), so the changed row is built again -- and only that
    // one; every other row is the node it was.
    sessions = sessions.map((s) => s.name === "c1" ? { ...s, status: "idle" } : s);
    const dotOnly = await recordsDuring("#session-list", () => refreshSessions());
    let after = await compare();
    assert.deepEqual(after.order, ["p0", "c1", "c2", "q3", "q4"], "the rail keeps its order");
    assert.deepEqual(after.same, ["p0", "c2", "q3", "q4"], "a status change rebuilds that row only");
    assert.ok(after.dots.c1.split(" ").includes("idle"), "the changed row shows its new state");
    assert.equal(after.focused, "q3", "the row holding the keyboard keeps it");
    // The new row takes the old one's slot in one record (not an insert
    // ahead of it that moves every row below), plus what its own late
    // decorations add.
    assert.ok(dotOnly.length > 0 && dotOnly.length <= 6,
      `one rebuilt row is a handful of records, not the rail (${dotOnly.length}: ${dotOnly.join(", ")})`);
    await snapshot();
    // Exiting changes what the row draws (no + to spawn from), so that row,
    // and only that row, is rebuilt.
    sessions = sessions.map((s) => s.name === "q4" ? { ...s, status: "exited", exit_code: 0 } : s);
    const swap = await recordsDuring("#session-list", () => refreshSessions());
    after = await compare();
    assert.deepEqual(after.same, ["p0", "c1", "c2", "q3"], "only the exited row is a new node");
    // And the poll after a change is idle again: nothing left over to settle.
    const settled = await recordsDuring("#session-list", () => refreshSessions());
    assert.deepEqual(settled, [], "the poll after a change makes no record on the rail");
    assert.equal(after.plus.q4, false, "the rebuilt row draws its new state");
    assert.equal(after.focused, "q3", "the row holding the keyboard keeps it");
    assert.ok(swap.length > 0 && swap.length <= 6,
      `one rebuilt row is a handful of records, not the rail (${swap.length}: ${swap.join(", ")})`);

    /* ---- 4. a row leaving the middle removes that row only ------------ */
    await page.evaluate(() => {
      window.__rows = new Map([...document.querySelectorAll("#session-list li.sess-card")]
        .map((li) => [li.dataset.name, li]));
    });
    sessions = sessions.filter((s) => s.name !== "c2");
    await page.evaluate(() => refreshSessions());
    const gone = await page.evaluate(() => {
      const now = [...document.querySelectorAll("#session-list li.sess-card")];
      return { order: now.map((li) => li.dataset.name),
               same: now.every((li) => __rows.get(li.dataset.name) === li) };
    });
    assert.deepEqual(gone.order, ["p0", "c1", "q3", "q4"]);
    assert.equal(gone.same, true, "the rows that stayed are the same nodes");

    /* ---- 5. the session pickers keep their options --------------------- */
    const picker = await page.evaluate(() => {
      const select = document.querySelector("#new-session select[name=resume]");
      // Filled as an opening fills it: the poll no longer builds the rows of
      // a form that is off screen (claunch-p3i0n.1).
      refreshResumeChoices();
      const before = new Map([...select.options].map((o) => [o.value, o]));
      sessionsCache = sessionsCache.map((s) => s.name === "p0" ? { ...s, status: "busy" } : s);
      refreshResumeChoices();
      const now = [...select.options];
      return {
        labels: now.map((o) => o.text),
        same: now.every((o) => before.get(o.value) === o),
      };
    });
    assert.ok(picker.labels.includes("p0 — busy"), "the moved status is shown");
    assert.equal(picker.same, true, "every option node is kept");

    /* ---- 6. the details panel redrawn from the same answer ------------ */
    await page.evaluate(async () => { openDetail("q4"); await refreshSession(); });
    await page.locator("#sess-view .sess-head").waitFor();
    await page.evaluate(() => refreshSession());
    const panel = await recordsDuring("#sess-view", () => refreshSession());
    assert.deepEqual(panel, [], "a redraw from the same answer makes no record in the panel");

    assert.deepEqual(errors, []);
    console.log("quietpoll_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
