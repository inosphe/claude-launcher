/* The rail's three card densities and its per-role colours, measured in a
   real browser.

   railcardmode_check holds what the code DECIDES and what the stylesheet
   DECLARES; this holds what the page actually draws — because the two can
   disagree without either being wrong on its own. A rule can hide a line and
   a more specific rule can put it back; a card can be given a class and still
   lay out at the same height; a child card's depth is a computed shade
   that only exists once a browser has resolved the cascade.

   What it asserts, against the shipped app.js and style.css and a fixture
   daemon:

     * the three densities are visibly different heights, and each draws the
       lines it is supposed to draw and no others;
     * every role family wears a different colour, and a role's alias wears
       the colour of its family;
     * a child row is a card indented to its depth with no guide line down
       its edge, siblings share the offset, and a shallower child sits
       further left;
     * picking a session expands its card in place (the card grows, the
       sub-lines appear, the briefing card is drawn) — the fold the rail used
       to have is gone, so this is the only way a card opens. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");

const root = process.env.STATIC_ROOT || path.join(__dirname, "../../src/claude_launcher/web/static");

/* The chips a compact card draws: `lead` is compact, so its two checks cover
   the density the request named ("collapsed form"). Answers differ so a
   chip's text is checkable, not just its box. `w1` deliberately reports none,
   so the "no checks, no chips line" case is in the same fixture. */
const fixtureChecks = [
  { id: "tests", name: "Tests", question: "Did the targeted tests pass?",
    answer: "yes", reported_at: "2026-10-26T03:00:00Z", source: "agent" },
  { id: "tmpl", name: "Template", question: "Was the template followed?",
    answer: "no", reported_at: "2026-10-26T03:00:00Z", source: "agent" },
];

const sessions = [
  { name: "lead", status: "busy", role: "leader", profile: "nc", cwd: "/repo",
    parent: null, keep_alive: true, status_checks: fixtureChecks,
    tps: { counted: true, tps: 80, at: new Date().toISOString() },
    created_at: "2026-10-25T12:00:00Z",
    rail_commits: { commits: [{sha: "a123456789", short: "a123456", subject: "Card layout"}], more: 0 },
    context: { tokens: 177000, input: 2, cache_read: 176000, cache_write: 1090,
      output: 210, model: "claude-opus-4-5", at: new Date().toISOString(),
      compact_window: 200000 },
    briefing: { one_line: "착지를 관리하고 자식을 배분한다", state: "working" } },
  { name: "w1", status: "busy", role: "worker", profile: "nc",
    cwd: "/repo/.claude/worktrees/w1", parent: "lead",
    briefing: { one_line: "카드 밀도 분기를 구현한다", state: "working" } },
  { name: "w1a", status: "idle", role: "worker", profile: "nc",
    cwd: "/repo/.claude/worktrees/w1a", parent: "w1",
    fault: { active: true, kind: "quota", harness: "claude", code: 429,
             label: "account suspended", message: "the account is over quota",
             last_seen: "2026-10-25T15:00:00Z", until: "2026-10-26T15:00:00Z" },
    briefing: { one_line: "스타일을 작성한다", state: "done" } },
  { name: "w1b", status: "idle", role: "worker", profile: "nc",
    cwd: "/repo/.claude/worktrees/w1b", parent: "w1",
    briefing: { one_line: "테스트를 더한다", state: "idle" } },
  { name: "rev-a", status: "idle", role: "reviewer", profile: "ds4", cwd: "/repo",
    parent: null, briefing: { one_line: "축 PR을 심사한다", state: "idle" } },
  { name: "rev-b", status: "idle", role: "review", profile: "ds4", cwd: "/repo",
    parent: null, briefing: { one_line: "다른 PR을 심사한다", state: "idle" } },
  { name: "one-1", status: "idle", role: "oneshot", profile: "ds4", cwd: "/repo",
    parent: null, briefing: { one_line: "리포트 한 편을 쓴다", state: "working" } },
  { name: "spec", status: "busy", role: "specialist", profile: "nc", cwd: "/repo",
    parent: null, briefing: { one_line: "자원 하나를 직렬로 운영한다", state: "blocked" } },
  { name: "free1", status: "idle", profile: "ds4", cwd: "/repo", parent: null,
    briefing: { one_line: "지시를 기다린다", state: "idle" } },
];

const refreshRequests = [];
const briefingRequests = [];
let workflowRows = {};
let briefingDelay = 0;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.endsWith("/briefing")) briefingRequests.push(url.pathname + url.search);
  if (url.searchParams.get("refresh") === "1" || url.pathname.endsWith("/status-checks/refresh")) {
    refreshRequests.push({ path: url.pathname + url.search, method: req.method });
  }
  if (url.pathname === "/boot.html") {
    res.setHeader("Content-Type", "text/html");
    return res.end(`<!doctype html><script>
      localStorage.setItem("claunch_token:/", "fixture-token");
      location.replace("/");
    </script>`);
  }
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "card" };
    else if (url.pathname === "/api/cflow2/rail") data = { rows: workflowRows };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: true };
    else if (url.pathname === "/api/profiles/nc/keys") data = {
      pooled: true, keys: [{ id: "fixture-key", label: "rotation pool", current: true }],
    };
    else if (url.pathname.endsWith("/status-checks/refresh")) data = { delivered: true };
    else if (url.pathname.endsWith("/briefing")) {
      const name = decodeURIComponent(url.pathname.split("/")[3] || "");
      const s = sessions.find((x) => x.name === name) || {};
      return setTimeout(() => res.end(JSON.stringify({
        session: name, generated_at: new Date().toISOString(), cached: true,
        briefing: {
          "one-line-job-description": (s.briefing || {}).one_line || "",
          goal: (s.briefing || {}).one_line || "",
          now: "스타일을 쓰는 중", state: (s.briefing || {}).state || "working",
          progress: "3/5",
        },
        raw: null,
      })), briefingDelay);
    }
    else if (url.pathname.endsWith("/capture")) data = { lines: [] };
    else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html"
    : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript"
      : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

/* What the page draws for every card: its density, how tall it came out,
   which of its lines are laid out at all, the role pill's computed colour,
   and whether a guide line is drawn down its edge. */
const READ = `(() => {
  const shown = (el) => !!el && getComputedStyle(el).display !== "none";
  return [...document.querySelectorAll("#session-list li[data-name]")].map((li) => {
    const role = li.querySelector(".mesh-role");
    const rs = role ? getComputedStyle(role) : null;
    const spine = getComputedStyle(li, "::after");
    const brief = li.querySelector(".rail-brief");
    return {
      name: li.dataset.name,
      mode: li.dataset.railMode,
      child: li.classList.contains("child"),
      depth: li.dataset.railDepth || "",
      h: Math.round(li.getBoundingClientRect().height),
      left: li.getBoundingClientRect().left,
      role: role ? role.textContent : "",
      roleColor: rs ? rs.color : "",
      roleClass: role ? role.className : "",
      lines: {
        brief: shown(brief),
        cwd: shown(li.querySelector(".rail-cwd")),
        ctx: shown(li.querySelector(".rail-ctx-line")),
        seen: shown(li.querySelector(".rail-seen")),
        actions: shown(li.querySelector(".rail-actions")),
        /* Measure session action buttons separately from their container. */
        actionButtons: [...li.querySelectorAll(
          ".sess-plus, .sess-pin, .sess-observe, .sess-note-edit, .sess-info")]
          .filter((b) => b.getBoundingClientRect().width > 0).length,
        rowRefreshes: [...li.querySelectorAll(
          ".sess-brief-rowref, .sess-status-check-rowref")]
          .filter((b) => b.getBoundingClientRect().width > 0).length,
        meta: shown(li.querySelector(".meta")),
        card: shown(li.querySelector(".sess-brief")),
        checks: shown(li.querySelector(".rail-status-checks")),
      },
      /* The chips are measured by their PAINTED box as well as by the
         selector: display:none leaves a 0x0 rect, and "the rule was not
         written" and "the row drew nothing" are different failures. */
      checkChips: [...li.querySelectorAll(".rail-status-check")].map((c) => {
        const b = c.getBoundingClientRect();
        return { text: c.textContent, h: Math.round(b.height), w: Math.round(b.width) };
      }),
      /* What the briefing card actually holds, by class: the rail's card is
         the summary alone (app.js renderBriefingCard, opts.rail), so a row or
         a faq item appearing here is the failure — not the absence of one. */
      cardParts: (() => {
        const c = li.querySelector(".sess-brief");
        if (!c) return null;
        return {
          one: c.querySelectorAll(".sess-brief-one").length,
          rows: c.querySelectorAll(".sess-brief-row").length,
          faq: c.querySelectorAll(".sess-brief-faq").length,
          checkRows: c.querySelectorAll(".sess-brief-check").length,
        };
      })(),
      briefText: brief ? brief.textContent.slice(0, 32) : "",
      /* The line that says why a session is silent. Measured by the PAINTED
         box, not by the selector: display:none leaves a 0x0 rect, and the
         regression this checks for was exactly that — the fault pill went
         from 17px to 0px on a compact card while every selector still read
         as written. */
      faultPill: (() => {
        const p = li.querySelector(".rail-quiet-pill.quiet-fault");
        if (!p) return null;
        const b = p.getBoundingClientRect();
        return { shown: shown(p), h: Math.round(b.height), w: Math.round(b.width) };
      })(),
      quietLine: shown(li.querySelector(".rail-quiet")),
      flash: li.classList.contains("goto-flash"),
      spine: spine.content === "none" || !spine.width ? "" : spine.width + "@" + spine.left,
      background: getComputedStyle(li).backgroundColor,
      shade: getComputedStyle(li).boxShadow,
      tick: getComputedStyle(li, "::before").content,
      toggle: !!li.querySelector(".sess-brief-toggle"),
    };
  });
})()`;

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 1200 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${server.address().port}/boot.html`);
    await page.locator('#session-list li.sess-card[data-name="lead"]').waitFor();

    let rows = await page.evaluate(READ);
    const by = (name) => rows.find((r) => r.name === name);

    assert.equal(rows.length, sessions.length, "the fixture fleet rendered");

    /* ---- the densities are three different cards --------------------- */
    assert.equal(by("lead").mode, "compact", "a worker card is compact");
    assert.equal(by("rev-a").mode, "min", "a reviewer card is one line");
    assert.equal(by("one-1").mode, "min", "a oneshot card is one line");

    assert.ok(by("rev-a").h > 0 && by("rev-a").h < by("lead").h / 2,
      `a one-line card is far shorter (one-line ${by("rev-a").h}px, compact ${by("lead").h}px)`);

    assert.deepEqual(by("rev-a").lines,
      { brief: false, cwd: false, ctx: false, seen: false, actions: false,
        actionButtons: 0, rowRefreshes: 0,
        meta: false, card: false, checks: false },
      "a one-line card draws the name and the role, nothing else");
    assert.deepEqual(by("lead").lines,
      { brief: true, cwd: false, ctx: false, seen: false, actions: false,
        actionButtons: 0, rowRefreshes: 0,
        meta: true, card: false, checks: true },
      "a compact card draws the briefing one-line and the chips");
    assert.equal(by("lead").lines.actionButtons, 0,
      "a compact card draws no session action buttons");
    const compactKey = page.locator('#session-list li[data-name="w1"] .sess-key');
    await compactKey.waitFor({ state: "attached" });
    assert.equal(await compactKey.isVisible(), false,
      "a compact card hides its rotation pool picker");
    assert.equal(by("lead").lines.rowRefreshes, 0,
      "a compact card has no briefing/check refresh buttons");
    assert.equal(by("w1b").lines.rowRefreshes, 0,
      "a compact card without checks also hides its refresh button");
    assert.ok(by("lead").briefText.length > 4,
      "the compact card's line is the briefing digest");
    assert.equal(by("lead").lines.card, false, "a compact card carries no briefing card");

    /* The chips are DRAWN on the compact card, not merely unhidden: the
       regression this pins is a rule that reads as written while the box is
       0x0, so the PAINTED box is what is measured. */
    assert.equal(by("lead").checkChips.length, 2,
      "the compact card carries one chip per reported check");
    for (const chip of by("lead").checkChips) {
      assert.ok(chip.h > 0 && chip.w > 0,
        `the chip "${chip.text}" is painted (${chip.w}x${chip.h})`);
    }
    /* The chip is `<icon> <name> · <age since reported>`; the age moves with
       the fixture's clock, so the icon and the name are what is pinned. */
    assert.deepEqual(by("lead").checkChips.map((c) => c.text.split(" · ")[0]),
      ["✓ Tests", "× Template"],
      "the compact card's chips are the reported answers, in order");
    /* A row with no checks draws no empty chips line. */
    assert.equal(by("w1b").lines.checks, false,
      "a session with no reported checks draws no chips line");

    /* The fold is gone: no card carries a briefing toggle. */
    assert.deepEqual(rows.filter((r) => r.toggle).map((r) => r.name), [],
      "no rail row carries a briefing toggle any more");

    /* ---- one colour per role family ---------------------------------- */
    const colour = (name) => by(name).roleColor;
    const families = {
      leader: colour("lead"), worker: colour("w1"), reviewer: colour("rev-a"),
      oneshot: colour("one-1"), specialist: colour("spec"), free: colour("free1"),
    };
    for (const [family, c] of Object.entries(families)) {
      assert.ok(c, `the ${family} role pill has a colour of its own (${c})`);
    }
    assert.equal(new Set(Object.values(families)).size, Object.keys(families).length,
      `no two families share a colour: ${JSON.stringify(families)}`);
    assert.equal(colour("rev-b"), families.reviewer,
      "a reviewer's alias wears its family's colour");
    for (const r of rows) {
      assert.ok(/mesh-role/.test(r.roleClass),
        `${r.name}: the role is still the shared .mesh-role pill (${r.roleClass})`);
    }

    /* ---- the child stack --------------------------------------------- */
    const deep = by("w1a");            // depth 2, under w1
    const sibling = by("w1b");
    const shallow = by("w1");
    /* claunch-zijwn.1: the depth shade marks the leading edge, so no guide
       line is drawn over it at any depth. */
    for (const r of [shallow, deep, sibling]) {
      assert.equal(r.spine, "", `${r.name}: a child row draws no guide line (${r.spine})`);
    }
    assert.ok(shallow.left < deep.left,
      `a shallower child sits further left (${shallow.left} vs ${deep.left})`);
    assert.deepEqual([shallow.depth, deep.depth], ["1", "2"],
      "each child carries its depth for the shade");
    assert.equal(by("lead").spine, "", "a root row draws no guide line");
    assert.equal(deep.tick, '"└"', "the tick still hangs the child off its parent");
    assert.equal(deep.left, sibling.left, "siblings align their entire cards");
    assert.equal(deep.background, by("lead").background,
      "ordinary parent and child cards share the rail background");

    /* ---- a deeper child sits deeper in the rail (claunch-zijwn) ------- */
    /* The child card carries an inset shade on its top and leading edges,
       and the shade grows with the depth, so a stack reads as stepping into
       the screen. Measured as the alpha of the shade's first layer: a root
       row paints none, a depth-1 child some, a depth-2 child more. */
    const shadeAlpha = (r) => {
      if (!/inset/.test(r.shade)) return 0;
      const m = /rgba\(0, 0, 0, ([\d.]+)\)/.exec(r.shade);
      return m ? Number(m[1]) : 0;
    };
    assert.equal(shadeAlpha(by("lead")), 0,
      `a root row draws no recess (${by("lead").shade})`);
    assert.ok(shadeAlpha(shallow) > 0,
      `a depth-1 child draws an inset shade (${shallow.shade})`);
    assert.ok(shadeAlpha(deep) > shadeAlpha(shallow),
      `a depth-2 child's shade is darker than depth 1's (${deep.shade} vs ${shallow.shade})`);
    assert.equal(sibling.shade, deep.shade, "siblings at one depth share the shade");

    /* ---- the line that says why a session is silent ------------------- */
    /* w1a is a compact child card with an active harness fault. The compact
       density is the default for every row the reader is not on, so a rule
       that hides the quiet line outright takes the fault `!` off the rail
       for most of the fleet — and docs/session-faults.md promises it there.
       Measured: this pill used to come out 0px tall on a compact card. */
    assert.equal(deep.mode, "compact", "the faulted fixture is a compact card");
    assert.ok(deep.faultPill, "a faulted session still draws its fault pill");
    assert.equal(deep.faultPill.shown, true, "and the pill is laid out");
    assert.ok(deep.faultPill.h > 8 && deep.faultPill.w > 8,
      `and it has a box, not a swallowed one (${deep.faultPill.w}x${deep.faultPill.h})`);
    assert.equal(deep.quietLine, true, "its quiet line is drawn with it");

    /* The decided flags are still allowed to go: keep-alive on a compact row
       is a setting that wants nothing from the reader. */
    assert.equal(by("lead").quietLine, false,
      "a compact row with no alerting flag draws no quiet line");

    /* ---- a child row that ⇱ just landed on still shows the mark ------- */
    /* The child card is a background and a rim on the row, declared after
       the flash rule at the same specificity — so without a rule of its own
       it paints over the mark on exactly the rows where the reader pressed
       `⇱`. Measured before the fix: the flashed child came out rgb(23,27,33)
       with a grey rim, the root's flash rgb(27,42,68) with a blue one. */
    /* Both a root row and a child row, flashed the same way, so the
       comparison is between two marks rather than against a face the root
       row happens not to have (a plain row is transparent). */
    const flashOf = async (name) => {
      await page.evaluate((n) => { window.revealSessionCard(n); }, name);
      await page.waitForFunction((n) => {
        const li = document.querySelector(`#session-list li[data-name="${n}"]`);
        return li && li.classList.contains("goto-flash");
      }, name);
      const row = (await page.evaluate(READ)).find((r) => r.name === name);
      return { row, bg: await page.evaluate((n) => {
        const li = document.querySelector(`#session-list li[data-name="${n}"]`);
        const cs = getComputedStyle(li);
        return { bg: cs.backgroundColor, ring: cs.boxShadow };
      }, name) };
    };
    const rootMark = await flashOf("lead");
    const childMark = await flashOf("w1a");
    assert.equal(childMark.row.flash, true, "the child row is the one marked");
    assert.equal(childMark.bg.bg, rootMark.bg.bg,
      `a flashed child wears the same face as a flashed root (${childMark.bg.bg} vs ${rootMark.bg.bg})`);
    assert.equal(childMark.bg.ring, rootMark.bg.ring,
      `...and the same ring (${childMark.bg.ring} vs ${rootMark.bg.ring})`);
    assert.notEqual(childMark.bg.bg, deep.background,
      `...which is not its ordinary child-card face (${deep.background})`);

    /* ---- ...and it still shows it on the row that is ALSO active ------ */
    /* The case above flashes a child that is not the current session, and
       that is not how the reader reaches the flash. `revealSessionCard` is
       called from `⇱ card` (`gotoSessionCard`, which reveals `currentName`
       by definition), from the boot landing, and from a band click — so the
       row being marked is, in the ordinary path, the one about to be or
       already `.active`. `li.child`, `li.child:hover` and `li.child.active`
       all tie `.goto-flash` at specificity 0,2,1, so only declaration order
       decides, and a flash rule placed before the tints loses to them.
       Measured on tree e76ed6e4 with the rule in that position: this exact
       row came out rgb(38,45,56) — the active tint — instead of the flash's
       rgb(27,42,68), while the assertion above still passed. The flash has
       to beat the tint: the tint says where the reader is, the flash says
       which row was just asked for. `w1b` is used because the assertions
       after this one read `w1` and `w1a`. */
    const activeChild = async (name) => {
      await page.evaluate((n) => {
        document.querySelectorAll("#session-list li").forEach((li) => {
          li.classList.toggle("active", li.dataset.name === n);
          li.classList.toggle("goto-flash", li.dataset.name === n);
        });
      }, name);
      return await page.evaluate((n) => {
        const li = document.querySelector(`#session-list li[data-name="${n}"]`);
        const cs = getComputedStyle(li);
        return { active: li.classList.contains("active"), flash: li.classList.contains("goto-flash"),
                 child: li.classList.contains("child"),
                 bg: cs.backgroundColor, ring: cs.boxShadow };
      }, name);
    };
    const both = await activeChild("w1b");
    assert.equal(both.child, true, "the row under test is a child");
    assert.equal(both.active, true, "...which is also the active row");
    assert.equal(both.flash, true, "...and carries the flash");
    assert.equal(both.bg, rootMark.bg.bg,
      `an active flashed child keeps the flash face, not the active tint (${both.bg} vs ${rootMark.bg.bg})`);
    assert.equal(both.ring, rootMark.bg.ring,
      `...and the flash ring (${both.ring} vs ${rootMark.bg.ring})`);

    /* ---- the row the reader is on is findable ------------------------- */
    /* What marks the session whose terminal is on screen. The request that
       produced this ("highlight that card more clearly") was about a mark
       the reader had to hunt for: a 2px edge in a muted blue on the card's
       transparent face, the same weight as the hover tint beside it. So what
       is measured is the DIFFERENCE between the row's two faces, not one
       colour: a face that paints nothing, or paints the hover tint, fails.
       The class is toggled by hand because the fixture boots with no session
       attached and therefore no active row to read. */
    const activeFace = async (name, on) => page.evaluate(([n, want]) => {
      const li = document.querySelector(`#session-list li[data-name="${n}"]`);
      li.classList.toggle("active", want);
      li.classList.remove("goto-flash");
      const cs = getComputedStyle(li);
      const label = li.querySelector(".rail-name");
      return { bg: cs.backgroundColor, ring: cs.boxShadow,
               name: label ? getComputedStyle(label).color : "" };
    }, [name, on]);
    const resting = await activeFace("w1", false);
    const here = await activeFace("w1", true);
    assert.notEqual(here.bg, resting.bg,
      `the active row paints a face of its own (${here.bg} vs ${resting.bg})`);
    assert.ok(/inset/.test(here.ring) && /rgb\(88, 166, 255\)/.test(here.ring),
      `...carrying the accent bar and the rim (${here.ring})`);
    assert.notEqual(here.name, resting.name,
      `...and a name at a brighter contrast (${here.name} vs ${resting.name})`);
    await activeFace("w1", false);

    /* ---- picking a card expands it ----------------------------------- */
    /* The reader's only way to open a card now: this is what the removed
       fold left behind, so it has to work in the shipped page. */
    const compactBefore = by("w1").h;
    await page.locator('#session-list li.sess-card[data-name="w1"] .rail-name').click();
    await page.waitForFunction(() => {
      const li = document.querySelector('#session-list li[data-name="w1"]');
      return li && li.dataset.railMode === "expanded";
    });
    const worker = page.locator('#session-list li[data-name="w1"]');
    assert.equal(await worker.locator(".sess-key").isVisible(), true,
      "opening the card reveals its rotation pool picker");
    await worker.locator(".sess-brief-refresh").waitFor();
    assert.deepEqual(briefingRequests, [], "opening a never-summarised card makes no briefing request");
    assert.equal(await worker.locator(".sess-brief-note").count(), 0, "row summary is already present");
    assert.equal(await worker.locator(".sess-brief-refresh").isEnabled(), true);
    await worker.locator(".sess-brief-refresh").click();
    await worker.locator(".sess-brief-age").waitFor();
    assert.deepEqual(briefingRequests, ["/api/sessions/w1/briefing?refresh=1"],
      "only the explicit refresh requests a summary");
    rows = await page.evaluate(READ);
    const opened = rows.find((r) => r.name === "w1");
    assert.equal(opened.mode, "expanded", "picking a card expands it");
    assert.ok(opened.h > compactBefore,
      `and it grows (${compactBefore}px -> ${opened.h}px)`);
    assert.equal(opened.lines.cwd, false, "directory stays behind the details reveal");
    assert.equal(opened.lines.ctx, true, "and the context gauge");
    assert.equal(opened.lines.seen, true, "and the attention line");
    assert.equal(opened.lines.actions, true, "and the buttons line — expanded is where they live");
    assert.equal(opened.lines.card, true, "and the briefing card");
    assert.equal(opened.lines.brief, true, "the one-line is still there too");
    assert.equal(opened.lines.checks, false,
      "and no chips line — w1 reports no checks, so it has none at either density");
    /* The card is the summary alone: the goal / now / progress rows and the
       faq are gone from the rail (the session's own body holds them), and the
       chips are the row's, not the card's — so nothing doubles. w1 has no
       checks in the fixture, so this reads the summary with nothing under it. */
    assert.deepEqual(opened.cardParts,
      { one: 0, rows: 0, faq: 0, checkRows: 0 },
      "the row summary appears once; the rail card adds no duplicate title or details");

    /* Open the fixture with checks and context, then measure and exercise
       the remaining controls. A hidden duplicate must have a 0x0 box, and
       the visible button must still reach its own API. */
    // The ancestor overlay may cover the name after the previous scroll;
    // the summary is another ordinary target of the row's navigation.
    await page.locator('#session-list li[data-name="lead"] .rail-brief').click();
    const lead = page.locator('#session-list li[data-name="lead"]');
    await lead.locator(".sess-brief-refresh").waitFor();
    assert.equal(await lead.locator(".sess-brief-note").count(), 0,
      "an existing row summary suppresses the empty summary notice");
    assert.equal(briefingRequests.length, 1, "opening a second uncached card adds no request");
    await page.evaluate(() => { document.getElementById("sidebar").style.width = "660px"; });
    const actionsAligned = async () => lead.evaluate(li => {
      const actions = li.querySelector(".rail-actions");
      const buttons = [...li.querySelectorAll(".sess-brief-refresh, .sess-brief-check-refresh")];
      const details = actions.querySelector(".rail-details-toggle").getBoundingClientRect();
      return buttons.length === 2 && buttons.every(e => {
        const b = e.getBoundingClientRect();
        return e.parentElement === actions && Math.abs(b.top - details.top) < 2;
      });
    });
    assert.ok(await actionsAligned(), "refreshes share the actions line with Details closed");
    assert.equal(await lead.locator(".sess-brief-head").count(), 0, "no empty footer line");
    assert.equal(await lead.locator(".sess-brief-check-refresh").isVisible(), true,
      "summary presence preserves both refresh controls");
    if (process.env.RAIL_SCREENSHOT_DIR) await lead.screenshot({
      path: path.join(process.env.RAIL_SCREENSHOT_DIR, "summary-empty.png"),
    });
    briefingDelay = 1500;
    await lead.locator(".sess-brief-refresh").click();
    await lead.locator(".sess-brief-note").filter({hasText: "summarising"}).waitFor();
    assert.ok(await actionsAligned(), "loading keeps controls in the actions line");
    if (process.env.RAIL_SCREENSHOT_DIR) await lead.screenshot({
      path: path.join(process.env.RAIL_SCREENSHOT_DIR, "summary-loading.png"),
    });
    assert.equal(await lead.locator(".sess-brief-refresh").evaluate(e => getComputedStyle(e).animationName), "none",
      "the button box does not rotate while loading");
    await lead.locator(".sess-brief-age").waitFor();
    briefingDelay = 0;
    const unified = await lead.evaluate((li) => {
      const visible = (e) => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0;
      const summary = li.querySelector(".rail-brief");
      const actions = li.querySelector(".rail-actions");
      const meta = li.querySelector(".rail-cwd");
      return {
        titles: li.querySelectorAll(".sess-brief-one").length,
        ctx: li.querySelectorAll(".sess-brief-ctx").length,
        gauge: visible(li.querySelector(".rail-ctx-line")),
        rowRefreshes: [...li.querySelectorAll(".sess-brief-rowref, .sess-status-check-rowref")].filter(visible).length,
        cardRefreshes: [...li.querySelectorAll(".sess-brief-refresh, .sess-brief-check-refresh")].filter(visible).length,
        summaryBeforeActions: summary.getBoundingClientRect().bottom <= actions.getBoundingClientRect().top,
        inset: [summary, actions, meta].map((e) => {
          const c = getComputedStyle(e);
          return parseFloat(c.paddingLeft) + parseFloat(c.borderLeftWidth);
        }),
      };
    });
    assert.deepEqual(unified, { titles: 0, ctx: 0, gauge: true,
      rowRefreshes: 0, cardRefreshes: 2, summaryBeforeActions: true,
      inset: [16, 16, 16] }, "expanded rail has one summary, one context reading and one pair of refreshes");
    refreshRequests.length = 0;
    await lead.locator(".sess-brief-refresh").click();
    await page.waitForFunction(() => !document.querySelector(
      '#session-list li[data-name="lead"] .sess-brief-refresh').disabled);
    await lead.locator(".sess-brief-check-refresh").click();
    assert.deepEqual(refreshRequests, [
      { path: "/api/sessions/lead/briefing?refresh=1", method: "GET" },
      { path: "/api/sessions/lead/status-checks/refresh", method: "POST" },
    ], "the two visible refresh controls retain their distinct functions");

    const closedHeight = await lead.evaluate(e => e.getBoundingClientRect().height);
    assert.equal(await lead.locator(".rail-commits").isVisible(), false);
    const details = lead.locator(".rail-details-toggle");
    await lead.focus();
    await page.keyboard.press("c");
    assert.equal(await details.getAttribute("aria-expanded"), "true", "c opens Details");
    await page.keyboard.press("Control+c");
    assert.equal(await details.getAttribute("aria-expanded"), "true", "copy does not toggle Details");
    await page.keyboard.press("c");
    assert.equal(await details.getAttribute("aria-expanded"), "false", "c closes Details");
    await details.focus();
    await page.keyboard.press("c");
    assert.equal(await details.getAttribute("aria-expanded"), "false", "a control keeps its own keys");
    await page.keyboard.press("Enter");
    assert.equal(await details.getAttribute("aria-expanded"), "true");
    assert.ok(await actionsAligned(), "refreshes stay in the actions line with Details open");
    assert.equal(await lead.locator(".rail-ctx-tokens").isVisible(), true);
    assert.equal(await lead.locator(".rail-commits").isVisible(), true);
    assert.equal(await lead.locator(".rail-tps-line").isVisible(), true, "TPS remains available in Details");
    await lead.evaluate(e => { e.dataset.rebuildProbe = "old"; });
    sessions[0].profile = "nc2";
    await page.evaluate(() => refreshSessions());
    assert.equal(await lead.getAttribute("data-rebuild-probe"), null,
      "the fixture changed the record enough to rebuild its row");
    assert.equal(await lead.locator(".rail-details-toggle").getAttribute("aria-expanded"), "true",
      "poll updates preserve the reveal");
    sessions[0].profile = "nc";
    await page.evaluate(() => refreshSessions());
    assert.ok(await lead.evaluate(e => e.getBoundingClientRect().height) > closedHeight,
      "diagnostics are omitted from the default card height");

    for (const width of [260, 440]) for (const theme of ["light", "dark"]) {
      await page.evaluate(({width, theme}) => {
        document.getElementById("sidebar").style.width = width + "px";
        window.claunchTheme.set(theme);
      }, {width, theme});
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      // Earlier navigation can leave the expanded root outside the rail's
      // viewport. Bring it into view before measuring content-visibility rows,
      // and wait for all measured sections after the theme/width transition.
      await lead.scrollIntoViewIfNeeded();
      await page.waitForFunction(() => {
        const li = document.querySelector('#session-list li[data-name="lead"]');
        return li && [".rail-brief", ".rail-ctx-line", ".rail-ctx-tokens", ".rail-seen", ".rail-life", ".sess-brief"]
          .every(selector => {
            const e = li.querySelector(selector), r = e && e.getBoundingClientRect();
            return r && r.width > 0 && r.height > 0;
          });
      });
      const footer = await lead.evaluate(li => {
        const head = li.querySelector(".rail-actions"), box = head.getBoundingClientRect();
        return {overflow: head.scrollWidth > head.clientWidth + 1,
          buttonsInside: [...head.querySelectorAll(".sess-brief-refresh, .sess-brief-check-refresh, .rail-details-toggle")].every(e => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.left >= box.left && r.right <= box.right + 1;
          })};
      });
      assert.deepEqual(footer, {overflow:false, buttonsInside:true},
        `${theme} ${width}px action controls fit with TPS data present`);
      await lead.locator(".sess-brief-refresh").click();
      await page.waitForFunction(() => !document.querySelector(
        '#session-list li[data-name="lead"] .sess-brief-refresh').disabled);
      await lead.locator(".sess-brief-check-refresh").click();
      const hierarchy = await page.evaluate(() => ["lead", "w1", "w1a", "w1b"].map(name => {
        const e = document.querySelector(`#session-list li[data-name="${name}"]`);
        const r = e.getBoundingClientRect();
        return {left: r.left, right: r.right};
      }));
      const step = width <= 420 ? 12 : 20;
      assert.equal(hierarchy[1].left - hierarchy[0].left, step, "first-level card inset");
      assert.equal(hierarchy[2].left - hierarchy[1].left, step, "second-level card inset");
      assert.equal(hierarchy[2].left, hierarchy[3].left, "sibling card edges align");
      assert.ok(hierarchy.every(r => Math.abs(r.right - hierarchy[0].right) < 1),
        "nested cards share the rail's right edge");
      if (process.env.RAIL_SCREENSHOT_DIR) {
        await page.locator("#sidebar").screenshot({path: path.join(
          process.env.RAIL_SCREENSHOT_DIR, `rail-${theme}-${width}.png`)});
      }
      const sections = await lead.evaluate(li => {
        const rowBg = getComputedStyle(li).backgroundColor;
        const names = [".rail-brief", ".rail-ctx-line", ".rail-ctx-tokens", ".rail-seen", ".rail-life", ".sess-brief"];
        return names.map(selector => {
          const e = li.querySelector(selector), c = getComputedStyle(e), box = e.getBoundingClientRect();
          const cells = [...e.querySelectorAll(".rail-seen-pair")].map(x => x.getBoundingClientRect());
          return {selector, display: c.display, connected: e.isConnected,
            mode: li.dataset.railMode, scroll: document.getElementById("session-list").scrollTop,
            cardTop: li.getBoundingClientRect().top, cardHeight: li.getBoundingClientRect().height,
            filled: c.backgroundColor !== "rgba(0, 0, 0, 0)" && c.backgroundColor !== rowBg,
            painted: box.width > 0 && box.height > 0,
            aligned: cells.every(x => Math.abs(x.width - cells[0].width) < 1),
            contained: cells.every(x => x.left >= box.left && x.right <= box.right + 1)};
        });
      });
      for (const section of sections) {
        assert.ok(!section.filled && section.painted && section.contained,
          `${theme} ${width}px internal content shares the card surface and stays visible and aligned: ${JSON.stringify(section)}`);
      }
    }

    await page.evaluate(() => { document.getElementById("sidebar").style.width = "260px"; });
    const narrowMetadata = await lead.evaluate(li => {
      const edge = li.getBoundingClientRect().right;
      return [...li.querySelectorAll(".rct-part, .rail-seen-pair")].map(e => {
        const r=e.getBoundingClientRect();
        return {text:e.textContent,painted:r.width>0 && r.height>0,contained:r.right<=edge};
      });
    });
    assert.ok(narrowMetadata.length>=5, "the narrow fixture includes tokens and lifetime metrics");
    for (const m of narrowMetadata) assert.ok(m.painted && m.contained,
      `260px metadata stays inside the card: ${m.text}`);
    await page.evaluate(() => { document.getElementById("sidebar").style.width = ""; });

    await lead.locator(".rail-details-toggle").click();
    assert.equal(await lead.locator(".rail-ctx-tokens").isVisible(), false);
    for (const width of [260, 440]) for (const theme of ["light", "dark"]) {
      await page.evaluate(({width, theme}) => {
        document.getElementById("sidebar").style.width = width + "px";
        window.claunchTheme.set(theme);
      }, {width, theme});
      await lead.scrollIntoViewIfNeeded();
      if (process.env.RAIL_SCREENSHOT_DIR) await page.locator("#sidebar").screenshot({
        path: path.join(process.env.RAIL_SCREENSHOT_DIR, `default-${theme}-${width}.png`),
      });
      const compact = page.locator('#session-list li[data-name="w1b"]');
      assert.ok((await compact.boundingBox()).height < 100,
        `${width}px compact card with a one-line job stays below 100px`);
    }

    /* Its children stay one-line cards while their parent is expanded: the
       density is the card's own, not inherited down the tree. */
    assert.equal(rows.find((r) => r.name === "w1a").mode, "compact",
      "a child card keeps its own density when its parent opens");

    /* Workflow state stays visible on compact and minimum cards too. */
    for (const status of ["running", "waiting", "done"]) {
      workflowRows = Object.fromEntries(["w1", "rev-a"].map(name => [name, {
        workflow: "fixture-worker-v2", scope: name + "@main", cwd: "/repo",
        status, gated: false, current: ["work"], round: 1,
      }]));
      await page.evaluate(() => refreshCflow2Rail());
      for (const width of [260, 440]) {
        await page.evaluate(width => { document.getElementById("sidebar").style.width = width + "px"; }, width);
        for (const name of ["w1", "rev-a"]) {
          const badge = page.locator(`#session-list li[data-name="${name}"] .sess-cflow2`);
          const box = await badge.boundingBox();
          assert.ok(box && box.width > 0 && box.height > 0,
            `${name} ${width}px collapsed workflow ${status} has a painted box`);
          assert.match(await badge.innerText(), new RegExp(status));
        }
      }
    }

    workflowRows = Object.fromEntries(["lead", "w1", "w1b"].map(name => [name, {
      workflow: "improv-worker-v2", scope: name + "@main", cwd: "/repo",
      status: "running", gated: false, current: ["work"], round: 1,
    }]));
    await page.evaluate(() => refreshCflow2Rail());
    for (const width of [260, 440]) for (const theme of ["light", "dark"]) {
      await page.evaluate(({width, theme}) => {
        document.getElementById("sidebar").style.width = width + "px";
        window.claunchTheme.set(theme);
      }, {width, theme});
      await lead.scrollIntoViewIfNeeded();
      if (process.env.RAIL_SCREENSHOT_DIR) await page.locator("#sidebar").screenshot({
        path: path.join(process.env.RAIL_SCREENSHOT_DIR, `workflow-${theme}-${width}.png`),
      });
      console.log(JSON.stringify({width, theme, cards: await page.evaluate(READ).then(
        rows => rows.filter(r => ["lead", "w1", "w1b"].includes(r.name)).map(r => ({name:r.name, height:r.h}))),
      }));
    }

    /* And a pinned card is expanded without being picked. */
    const beforeReload = briefingRequests.length;
    const reload = page.waitForEvent("load");
    await page.evaluate(() => {
      localStorage.setItem("claunch_session_pins:/", JSON.stringify(["rev-a"]));
      location.reload();
    });
    await reload;
    await page.locator('#session-list li.sess-card[data-name="rev-a"]').waitFor();
    await page.waitForFunction(() => {
      const li = document.querySelector('#session-list li[data-name="rev-a"]');
      return li && li.dataset.railMode === "expanded";
    });
    rows = await page.evaluate(READ);
    assert.equal(rows.find((r) => r.name === "rev-a").mode, "expanded",
      "a pinned one-line role is expanded by the pin alone");
    assert.equal(briefingRequests.length, beforeReload, "restoring pinned cards never requests a summary");
    assert.equal(rows.find((r) => r.name === "rev-b").mode, "min",
      "and its unpinned neighbour stays a one-line card");

    /* The pool response redraws rows after reload; focus the final row. */
    await page.locator('#session-list li[data-name="lead"] .sess-key')
      .waitFor({ state: "attached" });
    const runLink = page.locator('#session-list li[data-name="lead"] a.sess-cflow2');
    await runLink.scrollIntoViewIfNeeded();
    assert.match(await runLink.innerText(), /Run ↗/);
    assert.equal(await runLink.getAttribute("href"), "#/wf/" + encodeURIComponent("lead@main|/repo"));
    await runLink.focus();
    assert.equal(await runLink.evaluate(e => getComputedStyle(e).outlineStyle), "solid");
    if (process.env.RAIL_SCREENSHOT_DIR) await runLink.screenshot({
      path: path.join(process.env.RAIL_SCREENSHOT_DIR, "run-link-focus.png"),
    });
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => location.hash === "#/wf/" + encodeURIComponent("lead@main|/repo"));
    assert.deepEqual(errors, []);
    console.log("railcards_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
