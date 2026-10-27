/* The mega thread page (#/megathreads, megathread-page.js, claunch-v5nhp).

   The daemon decides who a message reaches; the page only draws that decision
   and sends what the reader picked. What has to hold is that the ring places
   one node per member, that the composer sends a plain message until the
   reader ticks members (then exactly those threads), that each verdict maps
   to the rows and the sentence the page shows, and that the page is wired
   into the address bar and the rail. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("node:assert/strict");

const WEB = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(WEB, "megathread-page.js"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in megathread-page.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

const NAMES = ["ringLayout", "edgeWidth", "nodeTone", "routeBody", "optionRows",
  "shortTitle", "verdictLine", "deliveryLine", "ledgerLine", "isOpen", "statusLabel",
  "statusTone", "sessionLine", "matchesFilter", "pickerRows", "createHint",
  "pct", "logitText", "esc", "ring",
  // the create panel's model (claunch-vz96l)
  "sessionCategory", "metaLine", "briefLine", "sortRows", "hierarchyOrder",
  "keepsRow", "filterChoices",
  // the preview's threshold slider (claunch-wlk6g)
  "probLogit", "logitProb", "draftPicks"];
const ctx = {};
vm.createContext(ctx);
// ringLayout's defaults read the page's own stage constants; take them from
// the source rather than repeating their values here (a drift would otherwise
// pass this check while the ring drew somewhere else).
const CONST_NAMES = ["STAGE_W", "STAGE_H", "RING_RX", "RING_RY", "CATEGORIES", "SORTS",
  "CATEGORY_KO"];
const CONSTS = CONST_NAMES.map((name) => {
  const found = src.match(new RegExp(`const ${name} = ([^;]+);`));
  if (!found) throw new Error(`cannot locate const ${name} in megathread-page.js`);
  // A `const` in a vm script is scoped to the script, not to the context, so
  // the check below reads it off this explicit assignment rather than
  // pretending it became a global.
  return `const ${name} = ${found[1]};\nglobalThis.${name} = ${name};`;
});
vm.runInContext(CONSTS.concat(NAMES.map(slice)).join("\n"), ctx);
/* Values built inside the vm carry that realm's prototypes, which strict
   deepEqual refuses; compare their data, not their identity. */
const plain = (value) => JSON.parse(JSON.stringify(value));

/* ---- the ring ---------------------------------------------------------- */
assert.deepEqual(plain(ctx.ringLayout(0)), []);
const four = ctx.ringLayout(4, 280, 210, 190, 130);
assert.equal(four.length, 4);
assert.ok(Math.abs(four[0].x - 280) < 1e-9, "the first node sits at the top");
assert.ok(four[0].y < 210);
assert.ok(Math.abs(four[1].x - (280 + 190)) < 1e-9, "the second sits at the right");
assert.deepEqual(plain(four), plain(ctx.ringLayout(4, 280, 210, 190, 130)), "the layout is deterministic");
const many = ctx.ringLayout(12, 280, 210, 190, 130);
const seen = new Set(many.map((p) => `${p.x.toFixed(2)},${p.y.toFixed(2)}`));
assert.equal(seen.size, 12, "twelve members get twelve distinct places");

/* ---- the drawing --------------------------------------------------------- */
const members = [
  { session: "s1", thread: "th-a", thread_title: "log format", status: "idle" },
  { session: "s2", thread: "th-b", thread_title: "deploy", status: "exited" },
];
const scored = { "th-a": { logit: 3.0 }, "th-b": { logit: -1.0 } };
const svg = ctx.ring(members, scored, 2.197);
assert.equal((svg.match(/class="mt-node /g) || []).length, 2, "one node per member");
assert.equal((svg.match(/class="mt-edge /g) || []).length, 2, "one spoke per member");
assert.ok(svg.includes(">you<"), "the user sits in the middle");
assert.ok(svg.includes("g class=\"mt-node hit\"") || svg.includes('class="mt-node hit"'),
  "the member over the threshold is marked");
assert.ok(svg.includes('class="mt-node cold gone"'), "an exited session is dimmed");
assert.ok(svg.includes("log format") && svg.includes("deploy"), "thread titles label the nodes");
const empty = ctx.ring([], {}, 2.197);
assert.ok(empty.includes("멤버가 없습니다"), "an empty ring says so");

/* ---- the spoke weight and tone ----------------------------------------- */
assert.equal(ctx.edgeWidth(null), 1);
assert.equal(ctx.edgeWidth(0), 1);
assert.ok(ctx.edgeWidth(2) > ctx.edgeWidth(1));
assert.equal(ctx.edgeWidth(2), ctx.edgeWidth(-2), "a strong score either way makes a wide spoke");
assert.equal(ctx.edgeWidth(100), 9, "the spoke is clamped");
assert.equal(ctx.nodeTone(3, 2.197), "hit");
assert.equal(ctx.nodeTone(2.197, 2.197), "hit", "the threshold itself is a hit");
assert.equal(ctx.nodeTone(1, 2.197), "warm");
assert.equal(ctx.nodeTone(-1, 2.197), "cold");
assert.equal(ctx.nodeTone(undefined, 2.197), "cold");

/* ---- what the composer sends ------------------------------------------- */
assert.deepEqual(plain(ctx.routeBody("hello", [])), { text: "hello" });
assert.equal("choose" in ctx.routeBody("hello", []), false, "no tick means a plain route");
assert.deepEqual(plain(ctx.routeBody("hello", ["th-a", "th-b"])),
  { text: "hello", choose: ["th-a", "th-b"] });
assert.deepEqual(plain(ctx.routeBody(null, null)), { text: "" });

/* ---- the rows and sentences each verdict shows ------------------------- */
const choose = {
  verdict: "choose", threshold: 2.197,
  options: [{ thread: "th-a", session: "s1", p_mine: 0.97, logit: 3.4 },
            { thread: "th-b", session: "s2", p_mine: 0.93, logit: 2.6 }],
};
const rows = plain(ctx.optionRows(choose));
assert.deepEqual(rows.map((r) => r.thread), ["th-a", "th-b"]);
assert.equal(rows[0].session, "s1");
assert.ok(rows[0].label.includes("s1"));
assert.deepEqual(plain(ctx.optionRows({ verdict: "auto", options: [] })), []);
assert.deepEqual(plain(ctx.optionRows({ verdict: "error" })), []);
assert.deepEqual(plain(ctx.optionRows(null)), []);
assert.equal(ctx.optionRows({ verdict: "none", options: [1, 2, 3] }).length, 3);

assert.ok(ctx.verdictLine({ verdict: "auto", options: [{ session: "s1" }] }).includes("s1"));
assert.ok(ctx.verdictLine(choose).includes("2"));
assert.ok(ctx.verdictLine({ verdict: "none", threshold: 2.197 }).includes("2.20"));
assert.ok(ctx.verdictLine({ verdict: "error" }).length > 0);
assert.equal(ctx.verdictLine(null), "");

assert.equal(ctx.deliveryLine([]), "");
assert.equal(ctx.deliveryLine([{ session: "s1", delivered: true }]), "s1: 전달됨");
assert.ok(ctx.deliveryLine([{ session: "s1", delivered: true, hold: "draft" }]).includes("draft"));
assert.ok(ctx.deliveryLine([{ session: "s1", delivered: false }]).includes("대기"));
assert.ok(ctx.deliveryLine([{ thread: "th-a", error: "boom" }]).includes("boom"));

// A message that went out but whose log row did not is reported as such, so
// the empty history does not read as "nothing was sent".
assert.equal(ctx.ledgerLine({}), "");
assert.equal(ctx.ledgerLine(null), "");
assert.ok(ctx.ledgerLine({ ledger_error: "disk full" }).includes("disk full"));

// A closed cluster keeps its button, labelled by the transition it offers.
assert.equal(ctx.statusLabel({ status: "open" }), "닫기");
assert.equal(ctx.statusLabel({ status: "closed" }), "다시 열기");
assert.equal(ctx.isOpen({ status: "closed" }), false);
assert.equal(ctx.isOpen({ status: "open" }), true);

/* ---- the member picker -------------------------------------------------- */
// The rail poll carries status, issue, branch and progress; it does NOT carry
// `task` or a filled `role` (api.py RAIL_FIELDS), so the picker must read the
// fields the daemon really sends. tests/test_megathread_api.py::test_t19 holds
// this list to that constant.
const roster = [
  { name: "s2", status: "exited", issue: "claunch-abc" },
  { name: "s1", status: "idle", role: "worker", branch: "s1-log-format" },
  { name: "s3", status: "starting", issue: "claunch-xyz", branch: "s3-deploy", note: "deploy watch" },
];
assert.equal(ctx.statusTone("idle"), "live");
assert.equal(ctx.statusTone("busy"), "live");
assert.equal(ctx.statusTone("starting"), "live", "a booting session is not gone");
assert.equal(ctx.statusTone("exited"), "gone");
assert.equal(ctx.statusTone(""), "");
assert.equal(ctx.sessionLine(roster[1]), "worker · s1-log-format");
assert.equal(ctx.sessionLine(roster[0]), "claunch-abc", "an issue stands in for a missing branch");
assert.equal(ctx.sessionLine({ progress: { text: "step 3/7" } }), "step 3/7",
  "what the session publishes about itself comes first");
assert.equal(ctx.sessionLine({}), "");

assert.equal(ctx.matchesFilter(roster[1], ""), true);
assert.equal(ctx.matchesFilter(roster[1], "LOG"), true, "the filter folds case and reads the branch");
assert.equal(ctx.matchesFilter(roster[1], "  worker "), true, "spaces around the query are dropped");
assert.equal(ctx.matchesFilter(roster[2], "deploy"), true, "a note or branch match finds the session");
assert.equal(ctx.matchesFilter(roster[2], "claunch-xyz"), true);
assert.equal(ctx.matchesFilter(roster[0], "s1"), false);

const picked = plain(ctx.pickerRows(roster, { picked: ["s2"] }));
assert.deepEqual(picked.map((r) => r.name), ["s1", "s2", "s3"],
  "no readings on any row: the name breaks the tie");
assert.deepEqual(picked.map((r) => r.checked), [false, true, false], "a tick survives the next repaint");
assert.equal(picked[0].line, "worker · s1-log-format");
assert.equal(picked[1].tone, "gone");
assert.equal(picked[2].tone, "live");
assert.deepEqual(plain(ctx.pickerRows(roster, { query: "deploy" })).map((r) => r.name), ["s3"]);
assert.deepEqual(plain(ctx.pickerRows(roster, { query: "nothing here" })), []);
assert.deepEqual(plain(ctx.pickerRows(null, {})), []);

/* ---- the lifecycle partition (claunch-vz96l) ---------------------------
   Seven categories, by the rule daemon/session.py::session_category uses:
   archived first, then an exited record's marker (hold/delayed, failed,
   paused, killed), and running for everything still up. */
assert.equal(ctx.sessionCategory({ status: "idle" }), "running");
assert.equal(ctx.sessionCategory({ status: "starting" }), "running");
assert.equal(ctx.sessionCategory({ status: "exited" }), "killed");
assert.equal(ctx.sessionCategory({ status: "exited", exited_at: "x" }), "killed");
assert.equal(ctx.sessionCategory({ status: "exited", paused_at: "x" }), "paused");
assert.equal(ctx.sessionCategory({ status: "exited", held_at: "x" }), "hold");
assert.equal(ctx.sessionCategory({ status: "exited", held_at: "x", delayed_at: "y" }), "delayed");
assert.equal(ctx.sessionCategory({ status: "exited", failed_at: "x" }), "failed");
assert.equal(ctx.sessionCategory({ status: "exited", archived_at: "x" }), "archived",
  "an archived record is archived however it ended");
assert.equal(ctx.sessionCategory({ status: "idle", archived_at: "x" }), "archived");
assert.deepEqual(Array.from(ctx.CATEGORIES),
  ["running", "hold", "paused", "killed", "delayed", "failed", "archived"],
  "the filter offers the same seven the partition defines");

/* ---- ordering ---------------------------------------------------------
   The two axes the session list offers, each both ways. A row with no
   reading sorts last in the newest-first order rather than heading it. */
const timed = [
  { name: "b", created_at: "2026-01-02T00:00:00Z", last_activity_at: "2026-01-05T00:00:00Z" },
  { name: "a", created_at: "2026-01-01T00:00:00Z", last_activity_at: "2026-01-09T00:00:00Z" },
  { name: "c", created_at: "2026-01-03T00:00:00Z" },
];
assert.deepEqual(plain(ctx.sortRows(timed, "activity-desc")).map((r) => r.name), ["a", "b", "c"]);
assert.deepEqual(plain(ctx.sortRows(timed, "activity-asc")).map((r) => r.name), ["b", "a", "c"]);
assert.deepEqual(plain(ctx.sortRows(timed, "created-asc")).map((r) => r.name), ["a", "b", "c"]);
assert.deepEqual(plain(ctx.sortRows(timed, "created-desc")).map((r) => r.name), ["c", "b", "a"]);
assert.deepEqual(plain(ctx.sortRows(timed, "nonsense")).map((r) => r.name), ["a", "b", "c"],
  "an unknown key falls back to the default order, not to whatever the daemon sent");

/* ---- the parent/child tree -------------------------------------------- */
const tree = [
  { name: "kid", parent: "root" },
  { name: "root" },
  { name: "other" },
];
const ordered = plain(ctx.hierarchyOrder(tree));
assert.deepEqual(ordered.map((e) => e.row.name), ["root", "kid", "other"]);
assert.deepEqual(ordered.map((e) => e.depth), [0, 1, 0]);
const orphan = plain(ctx.hierarchyOrder([{ name: "x", parent: "gone" }]));
assert.deepEqual(orphan.map((e) => [e.row.name, e.depth]), [["x", 0]],
  "a parent that is not in the list leaves the row a root");
const cycle = plain(ctx.hierarchyOrder([{ name: "a", parent: "b" }, { name: "b", parent: "a" }]));
assert.equal(cycle.length, 2, "a cycle cannot make a row disappear");

/* ---- the filters -------------------------------------------------------
   Project, mesh and lifecycle are separate axes, and an empty one keeps
   everything. The mesh filter has to tell "in no mesh" apart from "any". */
const fleet = [
  { name: "s1", project: "p", status: "idle", last_activity_at: "2026-01-02T00:00:00Z" },
  { name: "s2", project: "q", status: "exited", paused_at: "x", last_activity_at: "2026-01-03T00:00:00Z" },
  { name: "s3", status: "exited", archived_at: "x" },
];
const meshOf = { s1: { mesh: "m1", role: "worker" }, s2: { mesh: "m2", role: "reviewer" } };
assert.deepEqual(plain(ctx.pickerRows(fleet, { project: "p" })).map((r) => r.name), ["s1"]);
assert.deepEqual(plain(ctx.pickerRows(fleet, { mesh: "m1", meshOf })).map((r) => r.name), ["s1"]);
assert.deepEqual(plain(ctx.pickerRows(fleet, { mesh: "-", meshOf })).map((r) => r.name), ["s3"],
  "'메시 없음' is the sessions in no mesh");
assert.deepEqual(plain(ctx.pickerRows(fleet, { statuses: new Set(["archived"]) })).map((r) => r.name), ["s3"]);
assert.deepEqual(plain(ctx.pickerRows(fleet, { statuses: new Set(["paused"]) })).map((r) => r.name), ["s2"]);
assert.deepEqual(plain(ctx.pickerRows(fleet, { statuses: new Set() })).map((r) => r.name),
  ["s2", "s1", "s3"], "an empty status set keeps everything (default order: newest activity)");
assert.deepEqual(plain(ctx.pickerRows(fleet, { sort: "created-asc", hierarchy: true }))
  .map((r) => r.name), ["s1", "s2", "s3"],
  "no row has a created_at, so the name breaks every tie");

/* ---- what a row says about the session ---------------------------------
   role comes from the mesh roster (the rail's own `role` is empty on every
   live row), cflow from /api/cflow2/rail, and the briefing digest from the
   rail. Each is optional: a daemon that does not answer one still lists
   the session. */
const rich = {
  name: "s9", project: "claunch", issue: "claunch-abc", branch: "s9-work",
  // The shape briefing.digest() puts on the rail: `one_line`, `state`, and
  // goal/now/progress only when filled (claunch-g07ra.1).
  briefing: { one_line: "정렬 UI를 만든다", state: "working",
    now: "데몬 실시간 상태는 busy이다", goal: "피커를 정렬한다" },
};
const meta = ctx.metaLine(rich, { s9: { mesh: "claunch", role: "worker" } },
  { s9: { workflow: "improv-worker-v2", status: "running", current: ["work"] } });
assert.ok(meta.includes("worker"), "the roster's role is on the row");
assert.ok(meta.includes("claunch"), "the project and the mesh are on the row");
assert.ok(meta.includes("improv-worker-v2") && meta.includes("work"), "the cflow run is on the row");
assert.equal(ctx.metaLine({ name: "s9" }, {}, {}), "", "no joins, no line — not a row of separators");
assert.equal(ctx.briefLine(rich), "정렬 UI를 만든다", "the one-line summary, not `now`");
assert.equal(ctx.briefLine({ briefing: { one_line: "", state: "working", now: "지금 하는 일" } }),
  "지금 하는 일", "an empty one-liner falls back to now");
assert.equal(ctx.briefLine({ briefing: "a string digest" }), "a string digest");
assert.equal(ctx.briefLine({}), "");

const choices = plain(ctx.filterChoices(
  [{ project: "b" }, { project: "a" }, { project: "a" }, {}],
  { s1: { mesh: "m2" }, s2: { mesh: "m1" } }));
assert.deepEqual(choices.projects, ["a", "b"]);
assert.deepEqual(choices.meshes, ["m1", "m2"]);

// The line above the button says why it is off instead of staying silent.
assert.equal(ctx.createHint("", 2), "제목을 입력하세요");
assert.ok(ctx.createHint("cluster", 0).includes("빈"), "no member is warned about, not blocked");
assert.equal(ctx.createHint("cluster", 2), "2명으로 만듭니다");

/* ---- number and text formatting ---------------------------------------- */
assert.equal(ctx.pct(0.97), "97.0%");
assert.equal(ctx.pct(0.995), "100%");
assert.equal(ctx.pct("x"), "–");
assert.equal(ctx.logitText(2.944), "+2.94");
assert.equal(ctx.logitText(-0.8), "-0.80");
assert.equal(ctx.logitText(null), "–");
assert.equal(ctx.esc('<b>&"x"'), "&lt;b&gt;&amp;&quot;x&quot;");

/* ---- wiring: address, rail, page container, script ---------------------- */
const app = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
assert.ok(html.includes('data-page="megathreads"'), "the rail has the entry");
assert.ok(html.includes('id="megathreads-view"'), "the page container exists");
assert.ok(html.includes('src="static/megathread-page.js"'), "the page module is loaded");
assert.ok(/megathreads:\s*"megathreads-view"/.test(app), "VIEWS maps the page");
assert.ok(app.includes('case "megathreads"'), "the router opens it");
assert.ok(app.includes("globalThis.MegaThreadPage?.stop()"), "leaving the page stops its poll");
assert.ok(/if \(parts\[0\] === "megathreads"\)/.test(app), "the address names the page");

/* ---- the preview's threshold (claunch-wlk6g) ---------------------------- */
// The slider speaks p(mine) and the daemon scores in log-odds; 90% is ln 9,
// the daemon's default bar, so the slider opens where the daemon judged.
assert.ok(Math.abs(ctx.probLogit(0.9) - Math.log(9)) < 1e-9);
assert.ok(Math.abs(ctx.logitProb(Math.log(9)) - 0.9) < 1e-9);
assert.ok(Number.isFinite(ctx.probLogit(1)) && Number.isFinite(ctx.probLogit(0)), "certainty is clamped");
{
  const preview = { threshold: Math.log(9), members: [
    { session: "a", logit: 2.6 }, { session: "b", logit: 1.5 }, { session: "c", logit: -0.4 },
    { session: "d", logit: null, error: "jev: HTTP 500" },
  ] };
  assert.deepEqual(plain(ctx.draftPicks(preview, Math.log(9))), ["a"]);
  // Lowering the bar widens the pick from the same scores, and the
  // comparison is inclusive like the daemon's judge.
  assert.deepEqual(plain(ctx.draftPicks(preview, 1.5)), ["a", "b"]);
  assert.deepEqual(plain(ctx.draftPicks(preview, -5)), ["a", "b", "c"], "a failed call is never picked");
  assert.deepEqual(plain(ctx.draftPicks(null, 0)), []);
}
// The slider never asks the daemon: its handler re-ticks and repaints, and
// requestDraft (the one function that posts a preview) is not reachable
// from it.
{
  const panel = slice("createPanel");
  const handler = panel.slice(panel.indexOf('slider.addEventListener("input"'));
  const body = handler.slice(0, handler.indexOf("});") + 3);
  assert.ok(body.includes("applyDraftThreshold"), body);
  assert.ok(!body.includes("requestDraft") && !body.includes("apiSend"), body);
  assert.ok(!slice("applyDraftThreshold").includes("apiSend"));
}

const styles = fs.readFileSync(path.join(WEB, "style.css"), "utf8");
assert.ok(styles.includes("#megathreads-view"), "the page has styles");
assert.ok(/--rail-nav-rows:\s*\d+/.test(styles), "the rail's hover cap is a named count");
// The picker's parts have to exist in the stylesheet too, or the class names
// above name nothing a reader can see.
for (const cls of [".mt-new-filter", ".mt-new-count", ".mt-new-session-row.on", ".mt-new-dot.live",
  // the create panel (claunch-vz96l): the dialog, its two rails and the
  // controls that are new with it
  ".mt-create-overlay", ".mt-create-card", ".mt-create-list", ".mt-create-controls",
  ".mt-create-status.on", ".mt-create-draft", ".mt-create-retry", ".mt-new-flag",
  ".mt-new-meta", ".mt-new-brief",
  // the preview's threshold slider (claunch-wlk6g)
  ".mt-create-threshold", ".mt-create-threshold-input", ".mt-create-threshold-value"]) {
  assert.ok(styles.includes(cls), `the picker draws ${cls}`);
}

console.log("megathread_check ok");
