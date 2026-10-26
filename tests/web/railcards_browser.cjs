/* The rail's three card densities and its per-role colours, measured in a
   real browser.

   railcardmode_check holds what the code DECIDES and what the stylesheet
   DECLARES; this holds what the page actually draws — because the two can
   disagree without either being wrong on its own. A rule can hide a line and
   a more specific rule can put it back; a card can be given a class and still
   lay out at the same height; the child stack's spine is a pseudo-element
   whose geometry only exists once a browser has computed it.

   What it asserts, against the shipped app.js and style.css and a fixture
   daemon:

     * the three densities are visibly different heights, and each draws the
       lines it is supposed to draw and no others;
     * every role family wears a different colour, and a role's alias wears
       the colour of its family;
     * a child row is a card with a spine at its depth, siblings share the
       spine's offset, and a shallower child's spine sits further left;
     * picking a session expands its card in place (the card grows, the
       sub-lines appear, the briefing card is drawn) — the fold the rail used
       to have is gone, so this is the only way a card opens. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");

const root = path.join(__dirname, "../../src/claude_launcher/web/static");

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

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
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
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: true };
    else if (url.pathname.endsWith("/briefing")) {
      const name = decodeURIComponent(url.pathname.split("/")[3] || "");
      const s = sessions.find((x) => x.name === name) || {};
      return res.end(JSON.stringify({
        session: name, generated_at: new Date().toISOString(), cached: true,
        briefing: {
          "one-line-job-description": (s.briefing || {}).one_line || "",
          goal: (s.briefing || {}).one_line || "",
          now: "스타일을 쓰는 중", state: (s.briefing || {}).state || "working",
          progress: "3/5",
        },
        raw: null,
      }));
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
   and the child stack's spine. */
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
      role: role ? role.textContent : "",
      roleColor: rs ? rs.color : "",
      roleClass: role ? role.className : "",
      lines: {
        brief: shown(brief),
        cwd: shown(li.querySelector(".rail-cwd")),
        ctx: shown(li.querySelector(".rail-ctx-line")),
        seen: shown(li.querySelector(".rail-seen")),
        actions: shown(li.querySelector(".rail-actions")),
        /* The five action buttons, separately from the box: a compact card
           keeps the box (it holds the key chip and the two refreshes, which
           are not actions on the session) but none of these. */
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

    assert.ok(by("rev-a").h > 0 && by("rev-a").h < by("lead").h / 3,
      `a one-line card is far shorter (one-line ${by("rev-a").h}px, compact ${by("lead").h}px)`);

    assert.deepEqual(by("rev-a").lines,
      { brief: false, cwd: false, ctx: false, seen: false, actions: false,
        actionButtons: 0, rowRefreshes: 0,
        meta: false, card: false, checks: false },
      "a one-line card draws the name and the role, nothing else");
    assert.deepEqual(by("lead").lines,
      { brief: true, cwd: false, ctx: false, seen: false, actions: true,
        actionButtons: 0, rowRefreshes: 2,
        meta: true, card: false, checks: true },
      "a compact card draws the briefing one-line and the chips");
    /* No action buttons on the compact card -- measured by painted buttons,
       not by the box. The box stays because it also holds the key chip and
       the two refreshes, which say something about the row rather than
       offering an action on it. */
    assert.equal(by("lead").lines.actionButtons, 0,
      "a compact card draws no action buttons (spawn, pin, observe, note, ⓘ)");
    /* ...and the refreshes it DOES draw are painted, not just present: the
       round that hid them left every selector reading as written. Two of
       them belong to a card with reported checks (the briefing ⟳ and the
       checks ⟳); w1b has no checks and so has only the briefing one. */
    assert.equal(by("lead").lines.rowRefreshes, 2,
      "a compact card keeps the ⟳ for the briefing and for the chips");
    assert.equal(by("w1b").lines.rowRefreshes, 1,
      "with no reported checks there is no checks ⟳, only the briefing one");
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
    assert.ok(deep.spine, "a child row draws a spine");
    assert.equal(sibling.spine, deep.spine,
      "siblings at one depth share the spine's offset");
    assert.ok(parseInt(shallow.spine.split("@")[1], 10)
              < parseInt(deep.spine.split("@")[1], 10),
      `a shallower child's spine sits further left (${shallow.spine} vs ${deep.spine})`);
    assert.deepEqual([shallow.depth, deep.depth], ["1", "2"],
      "each child carries its depth for the spine");
    assert.equal(by("lead").spine, "", "a root row draws no spine");
    assert.equal(deep.tick, '"└"', "the tick still hangs the child off its parent");
    assert.notEqual(deep.background, by("lead").background,
      "a child row is drawn as a card on a face of its own");

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

    /* ---- picking a card expands it ----------------------------------- */
    /* The reader's only way to open a card now: this is what the removed
       fold left behind, so it has to work in the shipped page. */
    const compactBefore = by("w1").h;
    await page.locator('#session-list li.sess-card[data-name="w1"] .rail-name').click();
    await page.waitForFunction(() => {
      const li = document.querySelector('#session-list li[data-name="w1"]');
      return li && li.dataset.railMode === "expanded";
    });
    /* The card asks the daemon on sight and is drawn "summarising…" first;
       the summary is what this block is about, so wait for it. */
    await page.waitForFunction(() => {
      const li = document.querySelector('#session-list li[data-name="w1"]');
      return li && li.querySelector(".sess-brief .sess-brief-one");
    });
    rows = await page.evaluate(READ);
    const opened = rows.find((r) => r.name === "w1");
    assert.equal(opened.mode, "expanded", "picking a card expands it");
    assert.ok(opened.h > compactBefore,
      `and it grows (${compactBefore}px -> ${opened.h}px)`);
    assert.equal(opened.lines.cwd, true, "the expanded card draws its directory line");
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
      { one: 1, rows: 0, faq: 0, checkRows: 0 },
      "the expanded card draws the one-line summary and no detail rows");

    /* Its children stay one-line cards while their parent is expanded: the
       density is the card's own, not inherited down the tree. */
    assert.equal(rows.find((r) => r.name === "w1a").mode, "compact",
      "a child card keeps its own density when its parent opens");

    /* And a pinned card is expanded without being picked. */
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
    assert.equal(rows.find((r) => r.name === "rev-b").mode, "min",
      "and its unpinned neighbour stays a one-line card");

    assert.deepEqual(errors, []);
    console.log("railcards_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
