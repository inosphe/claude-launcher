/* The create form is read once when it opens, and the dashboard poll does not
   reach into it while it is open (claunch-p3i0n).

   Measured on the live dashboard with the New session dialog open for 60 s:
   every 5 s tick rebuilt the three worktree pickers (~970 option nodes a
   tick, from refreshNewWorktree re-rendering the same answer) and re-read
   /api/workspaces and /api/projects for the form's rows; a session status or
   a mesh message counter moving also rebuilt the parent, mesh and workflow
   rows (refreshParentChoices, syncOnboardPickers). The user's rule: read on
   entry, then only on the edge of an input the operator changes.

   This drives the shipped page against a fixture daemon whose sessions and
   mesh counters move on every answer, so a signature guard alone would not
   keep the form still, and polls by hand (the page's own timers are off):
     1. New session dialog: twelve ticks (60 s at DASHBOARD_POLL_MS) keep the
        rail polled and make no form request and no record inside the form;
     2. changing the Directory row is the one read it causes (/api/git once);
     3. after the dialog closes, the first tick reads the registry again;
     4. back on home, the form is off screen (#new-view, hidden): twelve ticks
        keep home's registry fresh and rewrite nothing inside the form, and
        a workspace added meanwhile reaches its Directory row on the next
        opening (claunch-p3i0n.1; measured before the fix: 288 records in
        twelve ticks -- worktree, mesh, workflow, parent and resume rows);
     5. Spawn child dialog: the same twelve ticks, the same zero;
     6. the #/new page form: the same. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = process.env.STATIC_ROOT || path.join(__dirname, "../../src/claude_launcher/web/static");

let tick = 0;
const sessions = () => [
  { name: "p0", status: tick % 2 ? "busy" : "idle", cwd: "/repo", harness: "claude", model: "opus", conversation_id: "a" },
  { name: "c1", status: tick % 2 ? "idle" : "busy", cwd: "/repo/.claude/worktrees/c1", harness: "claude", model: "opus", parent: "p0" },
  { name: "q2", status: "idle", cwd: "/other", harness: "claude", model: "opus" },
];
const workspaces = [
  { name: "repo", path: "/repo", exists: true },
  { name: "other", path: "/other", exists: true },
];  // step 4 adds one, as 'claunch workspace add' in another window would
const git = (cwd) => ({
  repo: true, root_branch: "master", branches: ["master", "feature"],
  worktrees: ["c1", "w2"], paths: { c1: `${cwd}/.claude/worktrees/c1`, w2: `${cwd}/.claude/worktrees/w2` },
  worktree_branches: { c1: "c1-branch", w2: "w2-branch" },
});
const log = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    log.push(url.pathname + (url.pathname === "/api/git" ? `?cwd=${url.searchParams.get("cwd")}` : ""));
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/health") data = { ok: true, boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions: sessions(), llm_configured: true };
    else if (url.pathname === "/api/mesh") data = { meshes: [{ name: "team", members: [], messages: tick }] };
    else if (url.pathname === "/api/workspaces") data = { workspaces };
    else if (url.pathname === "/api/projects") data = { projects: [] };
    else if (url.pathname === "/api/git") data = git(url.searchParams.get("cwd") || "/repo");
    else if (url.pathname === "/api/roles") data = { default: "", roles: [{ name: "worker" }] };
    else if (url.pathname === "/api/cflow/workflows") data = { workflows: [{ name: "wf" }] };
    else if (url.pathname.endsWith("/capture")) data = { lines: [] };
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});
server.on("upgrade", (req, sock) => sock.destroy());

// The reads that feed the form's rows. None of them is the rail's.
const FORM_READS = /^\/api\/(workspaces|projects|git|roles|cflow\/workflows|prompt-packs|prompter|borrow-options|spawn-policy|mesh\/[^/]+\/roles)/;

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      window.setInterval = () => 0;
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('#session-list li.sess-card[data-name="q2"]').waitFor();
    // Every read the page started has been answered and painted: nothing in
    // flight, nothing queued for the next batch, and a moment for the paint.
    // An input's change starts reads of its own (the Directory row re-reads
    // that directory's checkouts and workflows); those are allowed, so a
    // window starts only after they have landed rather than counting them as
    // the poll's.
    const settle = async () => {
      await page.waitForFunction(() => httpInFlight === 0 && batchPending === null);
      await page.waitForTimeout(150);
      await page.waitForFunction(() => httpInFlight === 0 && batchPending === null);
    };

    // Twelve polls by hand with the fixture moving under each; the records
    // a MutationObserver sees inside the form, and the requests made.
    async function ticks(n = 12) {
      await page.evaluate(() => {
        window.__records = [];
        window.__mo && window.__mo.disconnect();
        window.__mo = new MutationObserver((rs) => {
          for (const r of rs) {
            const t = r.target.nodeType === 1 ? r.target : r.target.parentElement;
            window.__records.push(`${r.type} ${t && (t.name || t.id || t.className)}`);
          }
        });
        window.__mo.observe(document.getElementById("new-session"),
          { subtree: true, childList: true, attributes: true, characterData: true });
      });
      const from = log.length;
      for (let i = 0; i < n; i++) {
        tick++;
        await page.evaluate(() => pollTick());
        await settle();
      }
      const records = await page.evaluate(() => { window.__mo.disconnect(); return window.__records; });
      const reqs = log.slice(from);
      return { records, reqs, sessions: reqs.filter((p) => p === "/api/sessions").length,
               form: reqs.filter((p) => FORM_READS.test(p)) };
    }
    const quiet = (label, got) => {
      assert(got.sessions >= 12, `${label}: the rail kept polling (${got.sessions} /api/sessions)`);
      assert.deepEqual(got.form, [], `${label}: no form read while it is open`);
      assert.deepEqual(got.records, [], `${label}: nothing inside the form was rewritten`);
    };

    /* ---- 1. New session dialog ---------------------------------------- */
    await page.evaluate(() => openSessionModal({ tab: "new" }));
    await settle();
    assert.equal(await page.locator("#modal-overlay").isVisible(), true);
    assert(await page.locator("#new-session select[name=worktree_from] option").count() > 1,
      "the worktree pickers were filled on opening");
    quiet("new session dialog", await ticks());

    /* ---- 2. an input's change is the one read it causes ---------------- */
    let from = log.length;
    await page.evaluate(() => {
      const sel = document.querySelector("#new-session select[name=cwd]");
      sel.value = "/other";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await settle();
    assert.deepEqual(log.slice(from).filter((p) => p.startsWith("/api/git")), ["/api/git?cwd=/other"],
      "the Directory change reads that directory's checkouts once");
    quiet("after the Directory change", await ticks());

    /* ---- 3. closed: the next tick catches up --------------------------- */
    await page.evaluate(() => sessionModalClose());
    await page.evaluate(() => showView("home"));
    from = log.length;
    tick++;
    await page.evaluate(() => pollTick());
    await settle();
    assert(log.slice(from).includes("/api/workspaces"), "the registry is polled again once the form is shut");

    /* ---- 4. off screen on home: left alone, then caught up on opening -- */
    assert.equal(await page.locator("#new-session").isVisible(), false, "the form is off screen");
    workspaces.push({ name: "added", path: "/added", exists: true });
    const hidden = await ticks();
    assert(hidden.sessions >= 12, `home: the rail kept polling (${hidden.sessions} /api/sessions)`);
    assert(hidden.reqs.includes("/api/workspaces"), "home: the registry is still polled for home");
    assert.deepEqual(hidden.form.filter((p) => !/^\/api\/(workspaces|projects)$/.test(p)), [],
      "home: no read for the form's own rows (git, roles, workflows, packs)");
    assert.deepEqual(hidden.records, [], "home: nothing inside the hidden form was rewritten");
    assert.equal(await page.locator('#new-session select[name=cwd] option[value="/added"]').count(), 0,
      "the hidden Directory row still holds the list it was drawn with");
    await page.evaluate(() => openSessionModal({ tab: "new" }));
    await settle();
    assert.equal(await page.locator('#new-session select[name=cwd] option[value="/added"]').count(), 1,
      "opening rebuilds the Directory row from the registry it reads");
    await page.evaluate(() => sessionModalClose());

    /* ---- 5. Spawn child dialog ----------------------------------------- */
    await page.evaluate(() => openSpawnModal("p0"));
    await settle();
    assert.equal(await page.locator("#modal-overlay").isVisible(), true);
    quiet("spawn child dialog", await ticks());
    await page.evaluate(() => sessionModalClose());

    /* ---- 6. the #/new page form ---------------------------------------- */
    await page.evaluate(() => { location.hash = "#/new"; });
    await page.locator("#new-view #new-session").waitFor();
    await settle();
    quiet("#/new page form", await ticks());

    assert.deepEqual(errors, []);
    console.log("modal once ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
