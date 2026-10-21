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
  "pct", "logitText", "esc", "ring"];
const ctx = {};
vm.createContext(ctx);
// ringLayout's defaults read the page's own stage constants; take them from
// the source rather than repeating their values here (a drift would otherwise
// pass this check while the ring drew somewhere else).
const CONSTS = ["STAGE_W", "STAGE_H", "RING_RX", "RING_RY"].map((name) => {
  const found = src.match(new RegExp(`const ${name} = ([^;]+);`));
  if (!found) throw new Error(`cannot locate const ${name} in megathread-page.js`);
  return `const ${name} = ${found[1]};`;
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
// The rail rows carry role, task and status; the picker has to show them,
// or a list of `dfv2-2026…` names is a list of identical names.
const roster = [
  { name: "s2", status: "exited", task: "deploy" },
  { name: "s1", status: "idle", role: "worker", task: "log format" },
  { name: "s3", status: "busy", issue: "claunch-abc" },
];
assert.equal(ctx.statusTone("idle"), "live");
assert.equal(ctx.statusTone("busy"), "live");
assert.equal(ctx.statusTone("exited"), "gone");
assert.equal(ctx.statusTone(""), "");
assert.equal(ctx.sessionLine(roster[1]), "worker · log format");
assert.equal(ctx.sessionLine(roster[2]), "claunch-abc", "an issue stands in for a missing task");
assert.equal(ctx.sessionLine({}), "");

assert.equal(ctx.matchesFilter(roster[1], ""), true);
assert.equal(ctx.matchesFilter(roster[1], "LOG"), true, "the filter folds case");
assert.equal(ctx.matchesFilter(roster[1], "  worker "), true, "spaces around the query are dropped");
assert.equal(ctx.matchesFilter(roster[0], "deploy"), true, "a task match finds the session");
assert.equal(ctx.matchesFilter(roster[2], "claunch-abc"), true);
assert.equal(ctx.matchesFilter(roster[0], "s1"), false);

const picked = plain(ctx.pickerRows(roster, "", ["s2"]));
assert.deepEqual(picked.map((r) => r.name), ["s1", "s2", "s3"], "rows are sorted by name");
assert.deepEqual(picked.map((r) => r.checked), [false, true, false], "a tick survives the next repaint");
assert.equal(picked[0].line, "worker · log format");
assert.equal(picked[1].tone, "gone");
assert.deepEqual(plain(ctx.pickerRows(roster, "deploy", [])).map((r) => r.name), ["s2"]);
assert.deepEqual(plain(ctx.pickerRows(roster, "nothing here", [])), []);
assert.deepEqual(plain(ctx.pickerRows(null, "", null)), []);

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

const styles = fs.readFileSync(path.join(WEB, "style.css"), "utf8");
assert.ok(styles.includes("#megathreads-view"), "the page has styles");
assert.ok(/--rail-nav-rows:\s*\d+/.test(styles), "the rail's hover cap is a named count");
// The picker's parts have to exist in the stylesheet too, or the class names
// above name nothing a reader can see.
for (const cls of [".mt-new-filter", ".mt-new-count", ".mt-new-session-row.on", ".mt-new-dot.live"]) {
  assert.ok(styles.includes(cls), `the picker draws ${cls}`);
}

console.log("megathread_check ok");
