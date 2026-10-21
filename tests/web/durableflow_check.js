/* durable-flow.js (claunch-neeya, claunch-neeya.1): a pi-durable session's
   event log as a flow diagram in its ◇ chart pane, updated in place.

   - model(): one column per phase in the order the loop entered it, nodes in
     time order; a result event completes the node of its ask instead of
     adding one; the node set for a prefix of the log is a prefix of the
     node set for the whole log, at the same places (append-stable).
   - mount(): polls only the tail (`since`), keeps ONE <svg> across polls,
     keeps every node element it drew (same object), appends the new ones,
     rewrites the state of the ones that changed, moves the pending node and
     drops it when the run ends; the poll stops once the session is gone.
   - sim2 (gds6, env SIM_RUN_DIR): questions grouped per stage into one
     counted node; a stage keeps its column; run_end ends the run.
   - app.js: ◇ chart opens the flow; ⬒ run is the workflow run again, for a
     pi-durable session too. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

class El {
  constructor(tag, ns) { this.tag = tag; this.ns = ns || null; this.children = []; this.attrs = {}; this._text = ""; this.className = ""; this.parent = null; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((k) => k !== c); c.parent = null; return c; }
  remove() { if (this.parent) this.parent.removeChild(this); }
  get firstChild() { return this.children[0] || null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(type, fn) { (this.handlers || (this.handlers = {}))[type] = fn; }
  click() {
    const fn = this.handlers && this.handlers.click;
    if (fn) fn({ preventDefault() {} });
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.writes = (this.writes || 0) + 1; }
  get scrollWidth() { return 0; }
  get clientWidth() { return 0; }
}
const doc = { createElement: (t) => new El(t), createElementNS: (ns, t) => new El(t, ns), hidden: false };
const ctx = vm.createContext({ console, Date, JSON, Math, Map, encodeURIComponent, isFinite, String, Number });
vm.runInContext(fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/durable-flow.js"), "utf8"), ctx);
const DF = ctx.DurableFlow;

// A real run's log (sim-998894-run1), trimmed of its pid/session columns.
const at = (s) => "2026-10-11T16:" + s + ".000Z";
const u = (t) => ({ input: 1, cacheRead: 0, output: 1, totalTokens: t, generations: 2 });
const LOG = [
  { at: at("34:16"), event: "session_start", reason: "startup" },
  { at: at("34:16"), event: "harness_open", b: { modelId: "accounts/fireworks/models/deepseek" }, c: { modelId: "x/glm" }, root: "/r" },
  { at: at("34:43"), event: "ask", role: "B", key: "formalize:1", usage: u(6061), stop: "stop" },
  { at: at("34:43"), event: "formalize", attempt: 1, ok: false, rejected: 1 },
  { at: at("34:59"), event: "ask", role: "B", key: "formalize:2", usage: u(7788), stop: "stop" },
  { at: at("34:59"), event: "formalize", attempt: 2, ok: true, rejected: 0 },
  { at: at("35:06"), event: "ask", role: "C", key: "judge:rules:a,rules:b,invariants:c:r0", usage: u(3153), stop: "stop" },
  { at: at("35:06"), event: "judge", key: "judge:rules:a,rules:b,invariants:c:r0", done: 3, retracted: [] },
  { at: at("35:06"), event: "round", round: 1, open: 2, ended: null },
  { at: at("35:16"), event: "ask", role: "C", key: "triage:r1:door/locked", usage: u(3170), stop: "stop" },
  { at: at("35:16"), event: "triage", tkey: "triage:r1:door/locked", done: 2 },
  { at: at("35:26"), event: "ask", role: "B", key: "gap:r1:door/locked:1", usage: u(9464), stop: "stop" },
  { at: at("35:26"), event: "gap_patch", base: "gap:r1:door/locked", attempt: 1, ok: true },
  { at: at("35:41"), event: "round", round: 2, open: 0, ended: { state: "idle", reason: "round 2 left nothing open" } },
  { at: at("36:15"), event: "ask", role: "B", key: "edit:unhandled|door|closed|close:1", usage: u(7174), stop: "stop" },
  { at: at("36:15"), event: "edits", attempt: 1, done: 3, errors: 0 },
  { at: at("36:15"), event: "done", reason: "round 2: round 2 left nothing open", failed: false },
  { at: at("36:15"), event: "session_shutdown", reason: "quit" },
];

// -- model ------------------------------------------------------------------
const m = DF.model(LOG, false);
assert.deepStrictEqual(Array.from(m.cols, (c) => c.title),
  ["시작", "형식화", "판정", "라운드 1", "라운드 2", "편집 제안", "끝"]);
const by = Object.fromEntries(m.nodes.map((n) => [n.id, n]));
assert.strictEqual(m.nodes.length, 10, m.nodes.map((n) => n.id).join(" "));
assert.strictEqual(by.start.sub, "B deepseek · C glm");
assert.strictEqual(by["ask:formalize:1"].status, "fail");
assert.match(by["ask:formalize:1"].sub, /거절 1/);
assert.strictEqual(by["ask:formalize:2"].status, "ok");
assert.strictEqual(by["ask:formalize:2"].row, 1);
assert.strictEqual(by["ask:judge:rules:a,rules:b,invariants:c:r0"].label, "판정 · 규칙 3개");
assert.match(by["ask:judge:rules:a,rules:b,invariants:c:r0"].sub, /확인 3/);
assert.strictEqual(by["ask:triage:r1:door/locked"].col, by["round:1"].col);
assert.strictEqual(by["ask:gap:r1:door/locked:1"].label, "빈칸 door/locked #1");
assert.match(by["ask:gap:r1:door/locked:1"].sub, /패치/);
assert.strictEqual(by["round:2"].sub, "열린 발견 0 · idle");
assert.strictEqual(by["ask:edit:unhandled|door|closed|close:1"].status, "ok");
assert.strictEqual(by.done.status, "ok");
assert.ok(by.done.tip.some((t) => /session_shutdown quit/.test(t)), "shutdown annotates the end, adds no node");
assert.strictEqual(m.tokens, 6061 + 7788 + 3153 + 3170 + 9464 + 7174);
assert.strictEqual(m.edges.length, m.nodes.length - 1);
assert.strictEqual(m.pending, null);

// Append-stable: every prefix's nodes sit where the whole log puts them.
for (let k = 1; k <= LOG.length; k++) {
  const part = DF.model(LOG.slice(0, k), true);
  for (const n of part.nodes) {
    assert.ok(by[n.id], `prefix ${k}: ${n.id} is not in the full model`);
    assert.deepStrictEqual([n.col, n.row], [by[n.id].col, by[n.id].row], `prefix ${k}: ${n.id} moved`);
  }
}
// Live and not ended: a pending node after the last one.
const live = DF.model(LOG.slice(0, 10), true);
assert.ok(live.pending && live.pending.col === live.nodes[live.nodes.length - 1].col);
assert.strictEqual(live.pending.row, live.nodes[live.nodes.length - 1].row + 1);
const withProgress = DF.model(LOG.slice(0, 10), true, { stage: "gaps", text: "round 1: door/locked (1/2)" });
assert.strictEqual(withProgress.pending.label, "진행 중 · gaps");
assert.strictEqual(withProgress.pending.sub, "round 1: door/locked (1/2)");
// A resumed run asks the same key again: a second node, not a collision.
const again = DF.model([...LOG.slice(0, 3), { event: "session_start", reason: "resume" },
                        { event: "ask", role: "B", key: "formalize:1", usage: u(1) }], true);
assert.deepStrictEqual(Array.from(again.nodes, (n) => n.id), ["start", "ask:formalize:1", "start#2", "ask:formalize:1#2"]);
assert.strictEqual(again.nodes[2].label, "재개");
// A failure and an unknown event both draw.
const bad = DF.model([LOG[0], { event: "ask", role: "B", key: "gap:r3:x:1", usage: u(1) },
                      { event: "gap_unreadable", base: "gap:r3:x", error: "no json" },
                      { event: "mystery" }, { event: "error", error: "boom\nstack" }], true);
assert.strictEqual(bad.nodes[1].status, "fail");
assert.strictEqual(bad.nodes[2].label, "mystery");
assert.strictEqual(bad.nodes[3].label, "오류");
assert.strictEqual(bad.pending, null, "an error ends the run");

// -- mount: tail polls and in-place updates ----------------------------------
const feeds = [
  { live: true, upto: 4, progress: { stage: "formalize", text: "reading" } },
  { live: true, upto: 9 },
  { live: true, upto: 9 },
  { live: false, upto: LOG.length },
];
const calls = [];
let step = 0;
async function api(url) {
  calls.push(url);
  const since = Number(new URL(url, "http://x").searchParams.get("since"));
  const f = feeds[Math.min(step++, feeds.length - 1)];
  return { ok: true, json: async () => ({ sim: "sim-998894", live: f.live, progress: f.progress || null,
    total: f.upto, since, events: LOG.slice(since, f.upto) }) };
}
let cleared = 0;
const timers = { setInterval: () => 7, clearInterval: () => { cleared++; } };

(async () => {
  const host = new El("div");
  const view = DF.mount(host, "sim-998894-run1", { doc, api, timers, interval: 1000 });
  await new Promise((r) => setTimeout(r, 0));
  const svg = view.view.svg;
  const nodesG = svg.children[2];
  assert.strictEqual(calls[0], "/api/sessions/sim-998894-run1/durable?since=0");
  assert.match(host.children[1].textContent, /진행: formalize — reading/);
  const firstNodes = nodesG.children.slice();
  const f1 = view.view.nodes.get("ask:formalize:1").g;
  assert.ok(view.view.nodes.get("live"), "a live run shows the pending node");
  assert.strictEqual(f1.getAttribute("class"), "dfl-node st-fail");

  await view.refresh();
  assert.strictEqual(calls[1], "/api/sessions/sim-998894-run1/durable?since=4", "asks only for the tail");
  assert.strictEqual(view.view.svg, svg, "the same <svg> is kept");
  assert.strictEqual(svg.parent.children.length, 1, "no second <svg>");
  for (const g of firstNodes) assert.ok(nodesG.children.includes(g), "a drawn node is never replaced");
  assert.strictEqual(view.view.nodes.get("ask:formalize:1").g, f1);
  assert.ok(view.view.nodes.get("round:1"), "the new nodes are appended");
  const w1 = svg.getAttribute("width");
  const title = view.view.nodes.get("ask:formalize:2").title;
  const writes = title.writes;

  await view.refresh();   // nothing new: nothing rewritten
  assert.strictEqual(calls[2], "/api/sessions/sim-998894-run1/durable?since=9");
  assert.strictEqual(title.writes, writes, "an unchanged node's text is not rewritten");

  await view.refresh();   // the run ends
  assert.ok(Number(svg.getAttribute("width")) > Number(w1), "the svg grows to the new columns");
  assert.strictEqual(view.view.nodes.get("live"), undefined, "the pending node goes when the run ends");
  assert.ok(!nodesG.children.some((g) => g.getAttribute("data-id") === "live"));
  assert.ok(!view.view.edges.has("live-edge"));
  assert.strictEqual(nodesG.children.length, 10);
  assert.strictEqual(view.view.edges.size, 9);
  assert.strictEqual(view.view.nodes.get("ask:formalize:1").g, f1);
  assert.strictEqual(cleared, 1, "the poll stops once the session is gone");
  assert.match(host.children[0].children[1].textContent, /sim-998894 · 세션 종료 · 사건 18 · 질문 6/);
  view.unmount();
  console.log("durableflow_check: ok");
})().catch((e) => { console.error(e); process.exit(1); });

// -- sim2: grouped stages ----------------------------------------------------
const a2 = (s) => "2026-10-13T04:" + s + ".000Z";
const SIM2 = [
  { at: a2("04:24"), role: "sim-run", event: "session_start", reason: "startup" },
  { at: a2("04:24"), role: "sim-run", event: "harness_open", file: "/runs/r/r.sqlite", pendingTasks: 0 },
  { at: a2("04:26"), role: "sim-run", event: "run_start", runDir: "/runs/r", rounds: 1, preset: "quick",
    b: { modelId: "a/glm-5p3-flash" }, c: { modelId: "a/glm-5p3-flash" } },
  { at: a2("05:57"), event: "ask", askRole: "B", key: "B:clash|4bf4698eb95f:0", usage: u(92220), stop: "stop" },
  { at: a2("06:10"), event: "ask", askRole: "B", key: "B:effects|aa:0", usage: u(1000), stop: "stop" },
  { at: a2("06:20"), event: "ask", askRole: "B", key: "B:clash|77:1", usage: u(8), stop: "length" },
  { at: a2("07:00"), event: "ask", askRole: "A", key: "A:1:p1:0:0", usage: u(10), stop: "stop" },
  { at: a2("07:01"), event: "ask", askRole: "B", key: "B:effects|bb:0", usage: u(2000), stop: "stop" },
  { at: a2("07:02"), event: "ask", askRole: "C", key: "C:r1:j", usage: u(30), stop: "stop" },
  { at: a2("07:03"), event: "ask", askRole: "C", key: "C:triage", usage: u(40), stop: "stop" },
  { at: a2("07:04"), event: "ask", askRole: "D", key: "D:cr-000001:0:0", usage: u(50), stop: "stop" },
  { at: a2("07:05"), event: "run_end", state: "done", error: null, tally: { B: { total: 2500000 }, A: { total: 10 } } },
  { at: a2("07:06"), event: "agent_end", simRunStarted: true },
  { at: a2("07:06"), event: "session_shutdown", reason: "quit" },
  { at: a2("07:06"), event: "harness_closed" },
];
const s2 = DF.model(SIM2, true);
const by2 = Object.fromEntries(s2.nodes.map((n) => [n.id, n]));
assert.deepStrictEqual(Array.from(s2.cols, (c) => c.title), ["시작", "모델 구성", "라운드 1", "분류", "편집 제안", "끝"]);
assert.strictEqual(by2.start.sub, "B glm-5p3-flash · C glm-5p3-flash", "run_start names the models");
assert.ok(by2.start.tip.some((t) => /run_start quick · 1 rounds/.test(t)));
assert.strictEqual(by2["grp:B:clash"].label, "충돌 ×2");
assert.strictEqual(by2["grp:B:clash"].status, "warn", "a cut-off answer marks its stage");
assert.strictEqual(by2["grp:B:effects"].label, "효과 ×2");
assert.strictEqual(by2["grp:B:effects"].sub, "B · 3.0k tok");
assert.strictEqual(by2["grp:B:effects"].col, by2["grp:B:clash"].col, "a stage keeps the column it opened");
assert.strictEqual(by2["grp:A:1:p1"].label, "플레이 p1 ×1");
assert.strictEqual(by2["grp:C:r1:j"].col, by2["grp:A:1:p1"].col);
assert.strictEqual(by2["grp:D:cr-000001"].label, "변경 cr-000001 ×1");
assert.strictEqual(by2.run_end.label, "끝");
assert.strictEqual(by2.run_end.sub, "done · 2.50M tok");
assert.strictEqual(s2.asks, 8);
assert.strictEqual(s2.pending, null, "run_end ends the run");
assert.strictEqual(s2.nodes.length, 8, s2.nodes.map((n) => n.id).join(" "));
assert.ok(s2.edges.every((e) => e.to.col >= e.from.col), "no edge runs backwards");
assert.ok(by2.run_end.tip.some((t) => /harness_closed/.test(t)), "closing notes annotate, add no node");
for (let k = 1; k <= SIM2.length; k++) {
  for (const n of DF.model(SIM2.slice(0, k), true).nodes) {
    assert.deepStrictEqual([n.col, n.row], [by2[n.id].col, by2[n.id].row], `sim2 prefix ${k}: ${n.id} moved`);
  }
}
const stopped = DF.model([...SIM2.slice(0, 4), { event: "run_end", state: "stopped", error: null, tally: {} }], true);
assert.deepStrictEqual([stopped.nodes.at(-1).label, stopped.nodes.at(-1).status], ["중단", "warn"]);
const broke = DF.model([...SIM2.slice(0, 4), { event: "run_end", state: "failed", error: "Harness is closed" }], true);
assert.deepStrictEqual([broke.nodes.at(-1).label, broke.nodes.at(-1).status], ["실패", "fail"]);
assert.deepStrictEqual(JSON.parse(JSON.stringify(DF.sim2Group("B:combat-ranged|x:0"))),
  { phase: "model", group: "B:combat-ranged", label: "기능 combat-ranged" });
console.log("durableflow_check: sim2 ok");

// -- parallel work: unit / ask_start (claunch-qk2nc) -------------------------
const ids = (mm) => Array.from(mm.edges, (e) => e.id).filter((x) => x !== "live-edge");
const plain = (x) => JSON.parse(JSON.stringify(x));   // out of the vm realm, for deepStrictEqual
// Every prefix keeps the places AND the edges the whole log gives.
function stable(log, name) {
  const full = DF.model(log, false);
  const at = Object.fromEntries(full.nodes.map((n) => [n.id, [n.col, n.row]]));
  const all = new Set(ids(full));
  for (let k = 1; k <= log.length; k++) {
    const part = DF.model(log.slice(0, k), true);
    for (const n of part.nodes) assert.deepStrictEqual([n.col, n.row], at[n.id], `${name} prefix ${k}: ${n.id} moved`);
    for (const e of ids(part)) assert.ok(all.has(e), `${name} prefix ${k}: edge ${e} is not in the full model`);
  }
}

// (a) A sequential claunch simulation keeps its chain: the edges are the
// consecutive pairs, and ask_start before each ask changes no node or edge.
assert.deepStrictEqual(ids(m), plain(m.nodes.slice(1).map((n, i) => m.nodes[i].id + ">" + n.id)));
const LOG_S = LOG.flatMap((e) => e.event === "ask" ? [{ at: e.at, event: "ask_start", key: e.key }, e] : [e]);
const ms = DF.model(LOG_S, false);
assert.deepStrictEqual(ms.nodes.map((n) => [n.id, n.col, n.row]), m.nodes.map((n) => [n.id, n.col, n.row]));
assert.deepStrictEqual(ids(ms), ids(m));
assert.deepStrictEqual([ms.starts, ms.peak, ms.running], [6, 1, 0], "one at a time");
stable(LOG, "sim"); stable(LOG_S, "sim+start");

// (b) An old sim2/rules2 log (askRole, no unit): the stages of a column ran
// side by side, so no edge joins two of them; each hangs off the column's entry.
const R2 = "|rules2|docu-x|docu-x@1|b0|h|t0";
const OLD = [
  { at: a2("20:58"), event: "session_start", reason: "startup" },
  { at: a2("21:01"), event: "run_start", preset: "game/items" },
  ...["items/_system", "07-grid-place/_feature", "05-open-state/_feature", "items/_system",
      "07-grid-place/01-incoming", "05-open-state/02-open"].map((d, i) =>
    ({ at: a2("22:" + String(10 + i)), event: "ask", askRole: "B", key: "B:" + d + R2, usage: u(100), stop: "stop" })),
];
const mo = DF.model(OLD, true);
const byo = Object.fromEntries(mo.nodes.map((n) => [n.id, n]));
assert.strictEqual(mo.cols.length, 2);
assert.strictEqual(byo["grp:B:items/_system"].label, "기능 items/_system ×2");
assert.strictEqual(mo.cols[1].nodes.length, 5, "one node per document");
assert.ok(mo.edges.every((e) => e.id === "live-edge" || e.from.col !== e.to.col), ids(mo).join(" "));
assert.deepStrictEqual(ids(mo), plain(mo.cols[1].nodes.map((id) => "start>" + id)), "every stage hangs off the start");
assert.strictEqual(mo.pending.col, 1);
assert.ok(s2.edges.every((e) => e.from.col !== e.to.col), "the sim2 run above has no same-column edge either");
stable(OLD, "old rules2"); stable(SIM2, "sim2");

// (c) A rules2 run that logs units: _system, then 13 feature documents that
// waited for it, then cards that waited for their feature. Three depth
// columns after the start; the only edges are the after edges.
const U = [
  { at: a2("30:00"), event: "session_start", reason: "startup" },
  { at: a2("30:01"), event: "run_start", preset: "game/items" },
  { at: a2("30:02"), event: "unit", unit: "items/_system", state: "start", label: "_system", phase: "시스템" },
  { at: a2("30:03"), event: "ask_start", key: "B:sys:1", askRole: "B", unit: "items/_system" },
  { at: a2("30:03"), event: "ask_start", key: "B:sys:2", askRole: "B", unit: "items/_system" },
  { at: a2("30:04"), event: "ask", askRole: "B", key: "B:sys:1", usage: u(1000), stop: "stop" },
  { at: a2("30:05"), event: "ask", askRole: "B", key: "B:sys:2", unit: "items/_system", usage: u(500), stop: "length" },
  { at: a2("30:06"), event: "unit", unit: "items/_system", state: "done" },
];
const FEAT = Array.from({ length: 13 }, (_, i) => String(i + 1).padStart(2, "0") + "-f");
for (const f of FEAT) {
  U.push({ at: a2("31:00"), event: "unit", unit: f, state: "start", label: f, phase: "기능", after: ["items/_system"] });
  U.push({ at: a2("31:01"), event: "ask_start", key: "B:" + f, askRole: "B", unit: f });
}
for (const f of FEAT) U.push({ at: a2("32:00"), event: "ask", askRole: "B", key: "B:" + f, unit: f, usage: u(10), stop: "stop" });
for (const f of FEAT.slice(0, 12)) U.push({ at: a2("32:01"), event: "unit", unit: f, state: "done" });
U.push({ at: a2("32:02"), event: "unit", unit: "13-f", state: "failed", note: "no json" });
U.push({ at: a2("33:00"), event: "unit", unit: "01-f/card-a", state: "start", label: "card-a", phase: "카드", after: ["01-f"] });
U.push({ at: a2("33:00"), event: "unit", unit: "02-f/card-b", state: "start", label: "card-b", phase: "카드", after: ["02-f", "ghost"] });
U.push({ at: a2("33:01"), event: "ask_start", key: "B:ca", askRole: "B", unit: "01-f/card-a" });
U.push({ at: a2("33:01"), event: "unit", unit: "02-f/card-b", state: "skipped" });
U.push({ at: a2("33:02"), event: "ask", askRole: "B", key: "B:noun", usage: u(1), stop: "stop" });   // no unit: drawn as before
const mu = DF.model(U, true);
const byu = Object.fromEntries(mu.nodes.map((n) => [n.id, n]));
assert.deepStrictEqual(Array.from(mu.cols, (c) => c.title), ["시작", "시스템", "기능", "카드", "모델 구성"]);
assert.deepStrictEqual(Array.from(mu.cols, (c) => c.nodes.length), [1, 1, 13, 2, 1]);
assert.strictEqual(mu.units, 16);
assert.deepStrictEqual([byu["unit:items/_system"].col, byu["unit:01-f"].col, byu["unit:01-f/card-a"].col], [1, 2, 3]);
const after = [...FEAT.map((f) => "unit:items/_system>unit:" + f), "unit:01-f>unit:01-f/card-a", "unit:02-f>unit:02-f/card-b"];
assert.deepStrictEqual(ids(mu).filter((e) => e.includes("unit:")).sort(), after.sort(), "unit edges are the after edges");
assert.ok(!mu.edges.some((e) => e.to.id === "grp:B:noun" || e.from.id === "start"), "the chain does not run across units");
assert.ok(mu.pending && mu.edges.some((e) => e.id === "live-edge"), "live: a pending node after the unit-less question");
const sys = byu["unit:items/_system"];
assert.deepStrictEqual([sys.label, sys.count, sys.tok, sys.status], ["_system", 2, 1500, "warn"], "a cut-off answer marks its unit");
assert.strictEqual(sys.sub, "물음 2 · 1.5k tok");
assert.deepStrictEqual([byu["unit:01-f"].status, byu["unit:13-f"].status, byu["unit:13-f"].sub], ["ok", "fail", "물음 1 · 10 tok · 실패"]);
assert.ok(byu["unit:13-f"].tip.includes("no json"));
assert.deepStrictEqual([byu["unit:01-f/card-a"].status, byu["unit:01-f/card-a"].sub], ["run", "물음 0 · 0 tok · 진행 1"]);
assert.deepStrictEqual(plain([byu["unit:02-f/card-b"].status, byu["unit:02-f/card-b"].after]), ["skip", ["02-f"]], "an unknown after is dropped");
assert.deepStrictEqual([mu.starts, mu.peak, mu.running, mu.asks], [16, 13, 1, 16]);
stable(U, "units");
// A unit drawn before its start event (an ask_start came first) stays at depth 0.
const early = DF.model([{ event: "ask_start", key: "k", unit: "u1" }, { event: "unit", unit: "u1", state: "start", after: ["zz"] }], true);
assert.deepStrictEqual([early.nodes[0].id, early.nodes[0].col, early.nodes[0].status], ["unit:u1", 0, "run"]);
// A question that threw logs its ask with an error and no usage: the pair
// closes, the node turns warn and its tip carries the error.
const thrown = DF.model([
  { event: "unit", unit: "d", state: "start", label: "doc" },
  { event: "ask_start", key: "B:d:1", askRole: "B", unit: "d" },
  { event: "ask", askRole: "B", key: "B:d:1", error: "cost cap reached\nstack", stop: "error" },
  { event: "unit", unit: "d", state: "done" },
  { event: "ask_start", key: "B:g|x", askRole: "B" },
  { event: "ask", askRole: "B", key: "B:g|x", error: "harness closed" },
  { event: "ask_start", key: "formalize:1" },
  { event: "ask", role: "B", key: "formalize:1", error: "aborted" },
], true);
const byt = Object.fromEntries(thrown.nodes.map((n) => [n.id, n]));
assert.deepStrictEqual([byt["unit:d"].status, byt["unit:d"].count, byt["unit:d"].running], ["warn", 1, 0]);
assert.ok(byt["unit:d"].tip.some((t) => / B:d:1: cost cap reached$/.test(t)), byt["unit:d"].tip.join("|"));
assert.deepStrictEqual([byt["grp:B:g"].status, byt["ask:formalize:1"].status], ["warn", "warn"]);
assert.ok(byt["grp:B:g"].tip.some((t) => /harness closed/.test(t)));
assert.ok(byt["ask:formalize:1"].tip.includes("aborted"));
assert.deepStrictEqual([thrown.running, thrown.peak], [0, 1]);
// An ask_start left without its ask is closed by its unit's end, and every
// one still open by the run's end (the process died before the ask).
const dead = [
  { event: "unit", unit: "a", state: "start" }, { event: "unit", unit: "b", state: "start" },
  { event: "ask_start", key: "ka", unit: "a" }, { event: "ask_start", key: "kb", unit: "b" },
  { event: "ask_start", key: "kb2", unit: "b" }, { event: "ask_start", key: "kn" },
];
const dm = (tail) => DF.model([...dead, ...tail], true);
assert.deepStrictEqual([dm([]).running, dm([]).peak], [4, 4]);
const da = dm([{ event: "unit", unit: "a", state: "interrupted" }]);
assert.deepStrictEqual([da.running, da.nodes[0].running, da.nodes[1].running, da.nodes[0].status], [3, 0, 2, "warn"]);
for (const end of ["run_end", "done", "error", "interrupted", "session_shutdown"]) {
  const e = dm([{ event: end, state: "failed" }]);
  assert.deepStrictEqual([e.running, e.nodes[1].running, e.peak], [0, 0, 4], end);
  assert.strictEqual(e.nodes[1].sub, "물음 0 · 0 tok", end);
}
// The ask that comes after its start was closed counts into the start's unit and changes nothing else.
const late = dm([{ event: "unit", unit: "b", state: "done" }, { event: "ask", key: "kb", usage: u(7) }]);
assert.deepStrictEqual([late.running, late.nodes[1].count, late.nodes[1].running], [2, 1, 0]);
stable([...dead, { event: "unit", unit: "a", state: "done" }, { event: "run_end", state: "done" }], "dead");
console.log("durableflow_check: units ok");

// (e) plan (claunch-ry446): the units a run means to do, drawn faint before
// they start. The live run gds6 rules-v2 20261020-105625: [01,02] -> _system -> [03,04,05].
const PL = [
  { unit: "items/03-c", label: "03-c", phase: "기능", after: ["items/_system"] },   // before its after
  { unit: "items/01-a", label: "01-a", phase: "문서" },
  { unit: "items/02-b", label: "02-b", phase: "문서" },
  { unit: "items/_system", label: "_system", phase: "시스템", after: ["items/01-a", "items/02-b"] },
  { unit: "items/04-d", label: "04-d", after: ["items/_system"] },
  { unit: "items/05-e", label: "05-e", after: ["items/_system", "ghost"] },
];
const P = [
  { at: a2("40:00"), event: "session_start", reason: "startup" },
  { at: a2("40:01"), event: "plan", units: PL },
  { at: a2("40:02"), event: "unit", unit: "items/01-a", state: "start", label: "01-a", phase: "문서" },
  { at: a2("40:02"), event: "ask_start", key: "B:b", askRole: "B", unit: "items/02-b" },   // a question wakes its unit
  { at: a2("40:03"), event: "ask", askRole: "B", key: "B:b", unit: "items/02-b", usage: u(5), stop: "stop" },
  { at: a2("40:04"), event: "unit", unit: "items/01-a", state: "done" },
  { at: a2("40:05"), event: "unit", unit: "items/02-b", state: "done" },
  { at: a2("40:06"), event: "unit", unit: "items/_system", state: "start", after: ["items/01-a", "items/02-b"] },
  { at: a2("40:07"), event: "unit", unit: "late", state: "start", label: "late" },   // not in the plan: drawn as before
];
const byOf = (mm) => Object.fromEntries(mm.nodes.map((n) => [n.id, n]));
// Right after the plan: six faint nodes at their plan depth, the plan's edges, done 0/6.
const p0 = DF.model(P.slice(0, 2), true), b0 = byOf(p0);
assert.deepStrictEqual(plain(["01-a", "02-b", "_system", "03-c", "04-d", "05-e"].map((x) => [b0["unit:items/" + x].col, b0["unit:items/" + x].status])),
  [[1, "plan"], [1, "plan"], [2, "plan"], [3, "plan"], [3, "plan"], [3, "plan"]]);
assert.deepStrictEqual(plain(p0.cols.map((c) => c.title)), ["시작", "문서", "시스템", "기능"]);
assert.deepStrictEqual(ids(p0).sort(), ["unit:items/01-a>unit:items/_system", "unit:items/02-b>unit:items/_system",
  "unit:items/_system>unit:items/03-c", "unit:items/_system>unit:items/04-d", "unit:items/_system>unit:items/05-e"]);
assert.ok(p0.edges.filter((e) => e.id !== "live-edge").every((e) => e.plan && !e.gone), "edges into a planned unit are faint");
assert.deepStrictEqual([b0["unit:items/03-c"].label, b0["unit:items/03-c"].sub], ["03-c", "대기"]);
assert.deepStrictEqual(plain(p0.plan), { total: 6, done: 0 });
assert.strictEqual(p0.units, 6);
// The run: the same nodes turn real, in place; an unplanned unit lands as before.
const pf = DF.model(P, true), bf = byOf(pf);
assert.deepStrictEqual(plain(["01-a", "02-b", "_system", "03-c"].map((x) => bf["unit:items/" + x].status)), ["ok", "ok", "run", "plan"]);
assert.deepStrictEqual([bf["unit:items/02-b"].count, bf["unit:items/02-b"].sub], [1, "물음 1 · 5 tok"]);
assert.ok(bf["unit:items/_system"].tip[0].includes(" 계획 items/_system"), "the plan stays in the tip");
assert.deepStrictEqual(plain([bf["unit:late"].col, bf["unit:late"].row, bf["unit:late"].status]), [1, 2, "run"]);
assert.deepStrictEqual(plain(pf.plan), { total: 6, done: 2 });
assert.ok(!pf.edges.find((e) => e.id === "unit:items/01-a>unit:items/_system").plan, "both ends real: a plain edge");
assert.ok(pf.edges.find((e) => e.id === "unit:items/_system>unit:items/04-d").plan);
stable(P, "plan");
// A later plan wins: a planned unit it leaves out is hidden in place (it never
// started), one it adds is placed, a started unit it leaves out stays.
const P2 = [...P.slice(0, 3), { at: a2("40:03"), event: "plan", units: [...PL.slice(0, 4), { unit: "items/06-f", after: ["items/_system"] }] }];
const p2 = DF.model(P2, true), b2 = byOf(p2);
assert.deepStrictEqual(plain(["04-d", "05-e", "06-f", "01-a"].map((x) => b2["unit:items/" + x].status)), ["gone", "gone", "plan", "run"]);
assert.deepStrictEqual(plain([b2["unit:items/06-f"].col, b2["unit:items/06-f"].row, b2["unit:items/06-f"].label]), [3, 3, "items/06-f"]);
assert.ok(p2.edges.find((e) => e.id === "unit:items/_system>unit:items/04-d").gone);
assert.deepStrictEqual(plain(p2.plan), { total: 5, done: 0 });
stable(P2, "replan");
// A third plan that names the dropped unit again brings it back; a hidden one that starts anyway is real.
const p3 = byOf(DF.model([...P2, { event: "plan", units: PL }, { event: "unit", unit: "items/06-f", state: "start" }], true));
assert.deepStrictEqual(plain(["04-d", "06-f"].map((x) => p3["unit:items/" + x].status)), ["plan", "run"]);
// A cycle or a malformed plan does not stop the drawing.
const cyc = DF.model([{ event: "plan", units: [{ unit: "x", after: ["y"] }, { unit: "y", after: ["x"] }, null, { label: "no id" }] }], true);
assert.deepStrictEqual(plain(cyc.nodes.map((n) => [n.id, n.col])), [["unit:x", 0], ["unit:y", 1]]);
assert.strictEqual(DF.model([{ event: "plan", units: "nope" }], true).nodes.length, 0);
// The view: faint and hidden by class, edges restyled when the unit starts.
{
  const host = new El("div"), vw = DF.makeView(doc, host);
  vw.draw(DF.model(P2.slice(0, 2), true));
  assert.match(vw.nodes.get("unit:items/04-d").g.getAttribute("class"), /st-plan/);
  const e = vw.edges.get("unit:items/01-a>unit:items/_system");
  assert.match(e.getAttribute("class"), /dfl-edge plan/);
  const g = vw.nodes.get("unit:items/04-d").g;
  vw.draw(p2);
  assert.strictEqual(vw.nodes.get("unit:items/04-d").g, g, "a hidden node keeps its element");
  assert.match(g.getAttribute("class"), /st-gone/);
  assert.match(vw.edges.get("unit:items/_system>unit:items/04-d").getAttribute("class"), /gone/);
  vw.draw(pf);
  assert.strictEqual(e.getAttribute("class"), "dfl-edge");
}
// The headline counts the plan's units done; without a plan it says nothing of units.
(async () => {
  const headline = async (log) => {
    const host = new El("div");
    const api = async () => ({ ok: true, json: async () => ({ live: true, total: log.length, since: 0, events: log }) });
    DF.mount(host, "plan-run", { doc, api, timers: { setInterval: () => 1, clearInterval() {} }, interval: 0 });
    await new Promise((r) => setTimeout(r, 0));
    return host.children[0].children[1].textContent;
  };
  assert.match(await headline(P), /단위 2\/6/);
  assert.doesNotMatch(await headline(U), /단위/);
  console.log("durableflow_check: plan headline ok");
})().catch((e) => { console.error(e); process.exit(1); });
console.log("durableflow_check: plan ok");

// (f) edges (claunch-nn4fx): only the transitive reduction of ``after`` is
// drawn, and no edge runs across a box. The live run gds6 rules-v2
// 20261020-195925 named the root in every card's ``after`` next to its
// feature, and one card also waited for a sibling feature.
// Every segment of every drawn edge stays out of every box's inside. A
// reduced edge stays in the model, hidden, so a unit that comes back keeps
// the edge ids already drawn.
const shown = (mm) => Array.from(mm.edges).filter((e) => e.id !== "live-edge" && !e.gone && !e.reduced).map((e) => e.id);
function crossings(mm) {
  const host = new El("div"), vw = DF.makeView(doc, host);
  vw.draw(mm);
  const boxes = mm.nodes.map((n) => {
    const t = /translate\(([-\d.]+),([-\d.]+)\)/.exec(vw.nodes.get(n.id).g.getAttribute("transform"));
    return { id: n.id, x: +t[1], y: +t[2], w: 168, h: 46 };
  });
  const out = [];
  for (const [id, path] of vw.edges) {
    if (/ gone/.test(path.getAttribute("class"))) continue;
    const pts = path.getAttribute("d").match(/-?[\d.]+,-?[\d.]+/g).map((s) => s.split(",").map(Number));
    for (let i = 1; i < pts.length; i++) {
      const [ax, ay] = pts[i - 1], [bx, by] = pts[i];
      const lo = [Math.min(ax, bx), Math.min(ay, by)], hi = [Math.max(ax, bx), Math.max(ay, by)];
      for (const b of boxes) {
        if (hi[0] > b.x && lo[0] < b.x + b.w && hi[1] > b.y && lo[1] < b.y + b.h) out.push(id + " x " + b.id);
      }
    }
  }
  return out;
}
{
  const R = "survival/_system", F = ["01-run", "02-stamina", "03-move-speed"].map((f) => f + "/_feature");
  const plan = [{ unit: R, label: "_system" }, ...F.map((f) => ({ unit: f, label: f, after: [R] })),
    { unit: "01-run/01-walk", after: [F[2], F[0], R] },                       // also waits for a sibling feature
    { unit: "01-run/02-dash", after: [F[0], R] },
    { unit: "02-stamina/01-drain", after: [F[1], R] }];
  const E = [{ at: a2("50:00"), event: "session_start", reason: "startup" }, { at: a2("50:01"), event: "plan", units: plan },
    { at: a2("50:02"), event: "unit", unit: R, state: "start", label: "_system", after: [] },
    { at: a2("50:03"), event: "unit", unit: R, state: "done" },
    { at: a2("50:04"), event: "unit", unit: "01-run/02-dash", state: "start", after: [F[0], R] }];
  const me = DF.model(E, true), be = byOf(me);
  assert.deepStrictEqual(shown(me).sort(), [
    "unit:" + F[0] + ">unit:01-run/01-walk", "unit:" + F[0] + ">unit:01-run/02-dash",
    "unit:" + F[1] + ">unit:02-stamina/01-drain", "unit:" + F[2] + ">unit:01-run/01-walk",
    ...F.map((f) => "unit:" + R + ">unit:" + f)].sort(), "root -> card edges implied through a feature are not drawn");
  assert.ok(me.edges.find((e) => e.id === "unit:" + R + ">unit:01-run/01-walk").reduced, "they stay in the model, hidden");
  assert.deepStrictEqual(plain(be["unit:01-run/01-walk"].after), [F[2], F[0], R], "the unit keeps its whole after list");
  assert.ok(be["unit:01-run/01-walk"].tip[0].endsWith(" ← " + [F[2], F[0], R].join(", ")), "and its tip lists it");
  assert.ok(be["unit:01-run/02-dash"].tip.some((t) => t.endsWith(" ← " + [F[0], R].join(", "))));
  assert.deepStrictEqual(crossings(me), []);
  stable(E, "reduced");
}
{
  // An edge the reduction keeps can still skip columns: d waits for a (depth
  // 0) and for x (depth 2 through z -> y). Drawn the old way it ran along d's
  // row, across y and x.
  const S = [["a"], ["z"], ["y", ["z"]], ["b", ["a"]], ["x", ["y"]], ["d", ["a", "x"]]]
    .map(([unit, after]) => ({ event: "unit", unit, state: "start", label: unit, after: after || [] }));
  const ms2 = DF.model(S, false), bs = byOf(ms2);
  assert.deepStrictEqual(plain(["a", "z", "y", "b", "x", "d"].map((x) => [bs["unit:" + x].col, bs["unit:" + x].row])),
    [[0, 0], [0, 1], [1, 0], [1, 1], [2, 0], [3, 0]]);
  assert.ok(shown(ms2).includes("unit:a>unit:d"), "a is not behind x: its edge stays");
  assert.deepStrictEqual(crossings(ms2), []);
  // A target below the first row turns in through the gap above it.
  const S2 = [S[0], S[1], S[2], S[3], S[4], { event: "unit", unit: "e", state: "start", after: ["x"] }, { event: "unit", unit: "d", state: "start", after: ["a", "x"] }];
  const m3 = DF.model(S2, false);
  assert.strictEqual(byOf(m3)["unit:d"].row, 1);
  assert.deepStrictEqual(crossings(m3), []);
}
{
  // Only a path through drawn units reduces an edge. C waits for B and A; a
  // replan drops B before it starts, so A > C is C's one visible edge. B
  // comes back on a third plan and A > C hides again under the same id.
  const pl = (units) => ({ at: a2("51:00"), event: "plan", units });
  const A = { unit: "A" }, B = { unit: "B", after: ["A"] }, C = { unit: "C", after: ["B", "A"] };
  const G = [pl([A, B, C]), { at: a2("51:01"), event: "unit", unit: "A", state: "start", after: [] }, pl([A, { unit: "C", after: ["A"] }])];
  const mg = DF.model(G, true);
  assert.strictEqual(byOf(mg)["unit:B"].status, "gone");
  assert.deepStrictEqual(shown(mg), ["unit:A>unit:C"], "B is hidden: A > C is drawn");
  assert.deepStrictEqual(ids(mg).sort(), ["unit:A>unit:B", "unit:A>unit:C", "unit:B>unit:C"]);
  stable(G, "gone");
  const G2 = [...G, pl([A, B, C])], mb = DF.model(G2, true);
  assert.strictEqual(byOf(mb)["unit:B"].status, "plan");
  assert.deepStrictEqual(shown(mb).sort(), ["unit:A>unit:B", "unit:B>unit:C"], "B is back: A > C hides again");
  assert.deepStrictEqual(ids(mb).sort(), ids(mg).sort(), "the same edge ids");
  stable(G2, "back");
  const host = new El("div"), vw = DF.makeView(doc, host);
  vw.draw(mg);
  const ac = vw.edges.get("unit:A>unit:C");
  assert.doesNotMatch(ac.getAttribute("class"), /gone/);
  vw.draw(mb);
  assert.strictEqual(vw.edges.get("unit:A>unit:C"), ac, "the drawn edge is kept");
  assert.match(ac.getAttribute("class"), /gone/);
}
console.log("durableflow_check: edges ok");

// (d) mount: the pending edge goes once a unit is the last node, and the
// headline counts the questions in flight.
(async () => {
  const feeds = [2, 5, U.length];
  let i = 0;
  const api = async (url) => {
    const since = Number(new URL(url, "http://x").searchParams.get("since"));
    const upto = feeds[Math.min(i++, feeds.length - 1)];
    return { ok: true, json: async () => ({ sim: "rules2", live: true, total: upto, since, events: U.slice(since, upto) }) };
  };
  const host = new El("div");
  const v = DF.mount(host, "rules2-run", { doc, api, timers: { setInterval: () => 1, clearInterval() {} }, interval: 1000 });
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(v.view.edges.has("live-edge"), "start -> pending");
  await v.refresh();
  assert.ok(!v.view.edges.has("live-edge"), "a unit is last: no pending edge");
  assert.match(host.children[0].children[1].textContent, /진행 중 2 · 최대 동시 2/);
  const sysG = v.view.nodes.get("unit:items/_system").g;
  await v.refresh();
  assert.strictEqual(v.view.nodes.get("unit:items/_system").g, sysG, "a unit node is kept");
  assert.strictEqual(v.view.edges.size, after.length + 1, "the after edges and the pending edge off the unit-less question");
  assert.match(host.children[0].children[1].textContent, /진행 중 1 · 최대 동시 13/);
  assert.strictEqual(sysG.getAttribute("class"), "dfl-node st-warn");
  v.unmount();
  console.log("durableflow_check: units mount ok");
})().catch((e) => { console.error(e); process.exit(1); });

// -- app.js: ◇ chart opens the flow; ⬒ run is the workflow run ---------------
(async () => {
  const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
  const html = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/index.html"), "utf8");
  const fn = (name) => {
    const start = src.indexOf(name);
    assert.ok(start >= 0, name);
    let depth = 0;
    for (let j = src.indexOf("{", start); j < src.length; j++) {
      if (src[j] === "{") depth++;
      else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
    }
    throw new Error(name);
  };
  assert.match(html, /<button id="term-chartbtn" class="term-btn split-btn hidden"[^>]*>◇ chart<\/button>/);
  assert.match(src, /\$\("term-chartbtn"\)\.addEventListener\("click", \(\) => toggleSplitPane\("chart"\)\)/);
  const pane = new El("div");
  const mounted = [];
  const asked = [];
  const sandbox = vm.createContext({
    encodeURIComponent, clearInterval: () => {}, setInterval: () => 9, visiblePoll: (f) => f,
    $: () => pane, el: (t, c, x) => { const e = new El(t); e.className = c; e.textContent = x; return e; },
    api: async (url) => { asked.push(url); return { ok: true, json: async () => ({
      session: { name: "sim-20261013-040416-simulation", kind: "pi-durable" },
      cflow: { cwd: "/repo", scope: "x" } }) }; },
    DurableFlow: { mount: (host, name, opts) => { mounted.push({ host, name, opts }); return { unmount() { sandbox.unmounted = true; } }; } },
    SimPane: { mount() {}, unmount() {} },
    renderSplit: () => { sandbox.rendered = true; },
    sessionsCache: [{ name: "sim-20261013-040416-simulation", kind: "pi-durable" }, { name: "s1", kind: "" }],
    loadSessLayouts: () => ({ a: { pane: "chart" }, b: { pane: "bogus" } }),
    DETAIL_SUBTAB_IDS: new Set(), clampSplitRatio: (r) => r || 0.5,
  });
  vm.runInContext(`var splitFor = null, splitKind = null, splitCwd = null, splitScope = null,
    splitPollTimer = null, splitDurable = null, splitSelectedStep = null, splitLastData = null, splitGen = 0, splitBody = null;
    var globalThis = this;
    ${fn("function isPiDurable(")} ${fn("function sessLayoutFor(")} ${fn("function isTalkPane(")}
    ${fn("function mountSplitFrame(")} var SPLIT_PANES = {}; function setSessLayout() {}
    ${fn("async function fetchCflow2Run(")} ${fn("function openSplit(")} ${fn("function closeSplit(")} ${fn("async function refreshSplit(")}`, sandbox);
  assert.strictEqual(vm.runInContext(`isPiDurable("sim-20261013-040416-simulation")`, sandbox), true);
  assert.strictEqual(vm.runInContext(`isPiDurable("s1")`, sandbox), false);
  assert.strictEqual(vm.runInContext(`sessLayoutFor("a").pane`, sandbox), "chart");
  assert.strictEqual(vm.runInContext(`sessLayoutFor("b").pane`, sandbox), "run");
  // The chart pane mounts the flow and asks nothing else.
  vm.runInContext(`openSplit("sim-20261013-040416-simulation", "chart")`, sandbox);
  assert.strictEqual(mounted.length, 1);
  assert.strictEqual(mounted[0].name, "sim-20261013-040416-simulation");
  assert.strictEqual(mounted[0].host, pane);
  assert.deepStrictEqual(asked, []);
  // Leaving it unmounts the flow.
  vm.runInContext(`closeSplit()`, sandbox);
  assert.ok(sandbox.unmounted);
  // The run pane of the same pi-durable session is the workflow run.
  vm.runInContext(`openSplit("sim-20261013-040416-simulation", "run")`, sandbox);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(mounted.length, 1, "⬒ run does not mount the chart");
  assert.ok(asked.some((x) => x.startsWith("/api/cflow/run?")), asked.join(" "));
  assert.ok(sandbox.rendered, "the workflow run is drawn");
  console.log("durableflow_check: chart pane ok");
})().catch((e) => { console.error(e); process.exit(1); });

// (g) sequence (claunch-qmwog): the same log on a time axis ----------------
const SAT = (s) => "2026-10-21T01:00:" + s + ".000Z";
const SLOG = [
  { at: SAT("00"), event: "session_start", reason: "startup" },
  { at: SAT("01"), event: "unit", unit: "a", state: "start", label: "A 문서", phase: "docs" },
  { at: SAT("01"), event: "unit", unit: "b", state: "start", label: "B 문서" },
  { at: SAT("02"), event: "ask_start", key: "a:1", unit: "a" },
  { at: SAT("02"), event: "ask_start", key: "b:1", unit: "b" },
  { at: SAT("05"), event: "ask", key: "a:1", unit: "a", usage: u(10), stop: "stop" },
  { at: SAT("07"), event: "ask", key: "b:1", unit: "b", usage: u(10), error: "boom" },
  { at: SAT("08"), event: "unit", unit: "a", state: "done" },
  { at: SAT("09"), event: "unit", unit: "b", state: "failed" },
  { at: SAT("10"), event: "ask", role: "B", key: "C:free" },
];
const SNOW = Date.parse(SAT("20"));
const SQ = DF.sequence(SLOG, true, SNOW);
{
  const ms = (s) => Date.parse(SAT(s));
  assert.deepStrictEqual(Array.from(SQ.lanes, (l) => l.id), ["u:a", "u:b", "a:C:free#1"]);
  const [a, b, free] = SQ.lanes;
  // A unit's interval is its own events; its ask hangs inside it.
  assert.deepStrictEqual([a.start, a.end, a.state], [ms("01"), ms("08"), "ok"]);
  assert.deepStrictEqual([b.end, b.state], [ms("09"), "fail"]);
  assert.deepStrictEqual(Array.from(a.asks, (x) => [x.start, x.end, x.status]),
    [[ms("02"), ms("05"), "ok"]]);
  assert.deepStrictEqual(Array.from(b.asks, (x) => x.status), ["fail"]);
  // A question outside any unit is a lane of its own, labelled as the tree labels it.
  assert.strictEqual(free.label, "C:free");
  assert.deepStrictEqual([free.start, free.end], [ms("10"), ms("10")]);
  // Two questions in flight at once: the parallelism is in the model.
  assert.strictEqual(SQ.peak, 2);
  assert.strictEqual(SQ.hidden, 0);
  // A sim2 run's questions group into one lane per stage, as the tree groups
  // them, with every question a mark inside it.
  const sim2 = DF.sequence([
    { at: SAT("01"), event: "ask_start", key: "B:effects|h1:0" },
    { at: SAT("02"), event: "ask_start", key: "B:effects|h2:0" },
    { at: SAT("03"), event: "ask", askRole: "B", key: "B:effects|h1:0", stop: "stop" },
    { at: SAT("04"), event: "ask", askRole: "B", key: "B:effects|h2:0", error: "cap" },
  ], false, SNOW);
  assert.deepStrictEqual(Array.from(sim2.lanes, (l) => [l.id, l.label, l.state]), [["g:B:effects", "효과", "fail"]]);
  assert.deepStrictEqual(Array.from(sim2.lanes[0].asks, (x) => [x.start, x.end, x.status]),
    [[Date.parse(SAT("01")), Date.parse(SAT("03")), "ok"],
     [Date.parse(SAT("02")), Date.parse(SAT("04")), "fail"]]);
  assert.strictEqual(sim2.peak, 2);
  // Live: the axis ends at now, not at the last event.
  assert.strictEqual(SQ.t1, SNOW);
  // A run that ended closes what stayed open at its end.
  const ended = DF.sequence([...SLOG, { at: SAT("12"), event: "run_end", state: "done" }], false, SNOW);
  assert.deepStrictEqual(Array.from(ended.lanes, (l) => l.end), [ms("08"), ms("09"), ms("10")]);
  assert.strictEqual(ended.t1, ms("12"));
  // Too many lanes: the newest are drawn, the rest are counted.
  const many = DF.sequence(Array.from({ length: DF.SEQ_MAX + 7 }, (_, i) => ({
    at: new Date(Date.parse(SAT("00")) + i * 1000).toISOString(),
    event: "ask", role: "B", key: "k" + i })), false, SNOW);
  assert.deepStrictEqual([many.lanes.length, many.hidden, many.total],
    [DF.SEQ_MAX, 7, DF.SEQ_MAX + 7]);
}

// The sequence view: a row a lane, the open one drawn out to the axis's end.
{
  const svq = DF.makeSequenceView(doc);
  svq.draw(SQ);
  assert.strictEqual(svq.svg.getAttribute("width"), "960");
  assert.strictEqual(svq.rows.size, 3);
  const a = svq.rows.get("u:a");
  assert.match(a.bar.getAttribute("class"), /dfl-seq-bar st-ok/);
  assert.ok(!/live/.test(a.bar.getAttribute("class")), "an ended unit is not live");
  assert.ok(Number(a.bar.getAttribute("x")) >= 210, "bars start right of the label column");
  assert.ok(Number(a.bar.getAttribute("width")) > 0);
  assert.strictEqual(a.asks.length, 1);
  assert.strictEqual(a.asks[0].getAttribute("class"), "dfl-seq-ask");
  assert.strictEqual(svq.rows.get("u:b").asks[0].getAttribute("class"), "dfl-seq-ask fail");
  // The marks sit under the unit's bar.
  assert.ok(Number(a.asks[0].getAttribute("y")) > Number(a.bar.getAttribute("y")));
  // The axis labels clock time.
  const axis = svq.svg.children[0];
  assert.ok(axis.children.some((n) => n.tag === "text" && /^\d\d:\d\d:\d\d$/.test(n.textContent)));
  // A unit still open runs to now, and is marked live; a lane the model no
  // longer has is dropped from the drawing.
  svq.draw(DF.sequence(SLOG.slice(0, 3), true, SNOW));
  assert.match(svq.rows.get("u:a").bar.getAttribute("class"), /st-run live/);
  assert.strictEqual(svq.rows.size, 2);
}

// Both views export: a standalone SVG (rules inline) and its PNG size.
{
  const tv = DF.makeView(doc, new El("div"));
  tv.draw(DF.model(LOG, false));
  const svq = DF.makeSequenceView(doc);
  svq.draw(SQ);
  for (const [name, v, mark] of [
    ["tree", tv, 'data-id="ask:formalize:1"'],
    ["sequence", svq, 'data-id="u:a"'],
  ]) {
    const src = DF.svgSource(v.svg);
    assert.match(src, /^<svg/);
    assert.match(src, /<style>/, `${name}: the rules travel with the file`);
    assert.match(src, /class="dfl-paper"/);
    assert.ok(src.includes(mark), `${name}: the drawing is in the file`);
    const size = DF.pngSize(v.svg, 2);
    assert.strictEqual(size.width, Number(v.svg.getAttribute("width")) * 2);
    assert.strictEqual(size.height, Number(v.svg.getAttribute("height")) * 2);
  }
  // The walker escapes text, so a report's prose cannot break the file.
  assert.strictEqual(DF.serializeNode({ tag: "text", attrs: {}, _text: "<a & b>" }),
    "<text>&lt;a &amp; b&gt;</text>");
  console.log("durableflow_check: export ok");
}

// The pane's switch: the same two elements, one shown at a time.
(async () => {
  const host = new El("div");
  const api = async () => ({ ok: true, json: async () => ({
    sim: "seq-run", live: true, total: SLOG.length, since: 0, events: SLOG }) });
  const mnt = DF.mount(host, "seq-run", {
    doc, api, timers: { setInterval: () => 1, clearInterval() {} }, interval: 0 });
  await new Promise((r) => setTimeout(r, 0));
  const tools = host.children[0].children[2];
  const scroller = host.children[2];
  assert.deepStrictEqual(Array.from(tools.children, (b) => b.textContent),
    ["트리", "시퀀스", "SVG", "PNG"]);
  assert.strictEqual(tools.children[0].getAttribute("aria-pressed"), "true");
  assert.strictEqual(scroller.children.length, 1);
  const treeSvg = mnt.view.svg;
  assert.strictEqual(scroller.children[0], treeSvg, "the tree is on screen first");
  // Switching shows the sequence view; the tree keeps its own element.
  tools.children[1].click();
  assert.strictEqual(mnt.mode, "sequence");
  assert.strictEqual(tools.children[1].getAttribute("aria-pressed"), "true");
  assert.strictEqual(scroller.children.length, 1);
  assert.strictEqual(scroller.children[0], mnt.sequence.svg);
  assert.match(scroller.className, /dfl-seq/);
  assert.strictEqual(mnt.view.svg, treeSvg, "the tree view is not rebuilt");
  assert.strictEqual(mnt.sequence.rows.size, 3, "the sequence drew the poll's lanes");
  // Back: the same tree element returns.
  tools.children[0].click();
  assert.strictEqual(mnt.mode, "tree");
  assert.strictEqual(scroller.children[0], treeSvg);
  assert.ok(!/dfl-seq/.test(scroller.className));
  mnt.unmount();
  console.log("durableflow_check: sequence ok");
})().catch((e) => { console.error(e); process.exit(1); });

// (h) the questions still in flight are marks with no end (review, 2026-10-21)
{
  const OPEN = [
    { at: SAT("00"), event: "unit", unit: "a", state: "start", label: "A" },
    { at: SAT("02"), event: "ask_start", key: "q1", unit: "a" },
    { at: SAT("30"), event: "unit", unit: "b", state: "start", label: "B" },
    { at: SAT("31"), event: "ask_start", key: "q2", unit: "b" },
  ];
  const open = DF.sequence(OPEN, true, Date.parse(SAT("40")));
  const [a, b] = open.lanes;
  assert.deepStrictEqual([a.asks.length, a.asks[0].end, a.asks[0].status], [1, null, "run"]);
  assert.deepStrictEqual([b.asks.length, b.asks[0].end], [1, null]);
  // The questions in flight are what the parallelism is: the model's count
  // and the timeline's peak are the same number here.
  assert.strictEqual(DF.model(OPEN, true).running, 2);
  assert.strictEqual(open.peak, 2);
  // A question outside any unit is a lane of its own, open until it answers.
  const free = DF.sequence([{ at: SAT("05"), event: "ask_start", key: "lonely" }], true, Date.parse(SAT("40")));
  assert.deepStrictEqual([free.lanes.length, free.lanes[0].label, free.lanes[0].end,
                          free.lanes[0].asks[0].end], [1, "lonely", null, null]);
  assert.strictEqual(free.peak, 1);
  // The answer closes the same mark (no second one), and the run's end
  // closes what is still open.
  const closed = DF.sequence([...OPEN, { at: SAT("35"), event: "ask", key: "q1", unit: "a" }], true, Date.parse(SAT("40")));
  assert.deepStrictEqual(Array.from(closed.lanes[0].asks, (x) => [x.start, x.end, x.status]),
    [[Date.parse(SAT("02")), Date.parse(SAT("35")), "ok"]]);
  assert.strictEqual(closed.peak, 2);
  const ended = DF.sequence([...OPEN, { at: SAT("36"), event: "run_end", state: "done" }], false, Date.parse(SAT("40")));
  assert.deepStrictEqual(Array.from(ended.lanes, (l) => l.asks[0].end),
    [Date.parse(SAT("36")), Date.parse(SAT("36"))]);
  // A sim2 start that carries no role still lands in its stage's lane once
  // the answer names it: one mark, not two, and no lane of its own left.
  const moved = DF.sequence([
    { at: SAT("01"), event: "ask_start", key: "B:effects|h1:0" },
    { at: SAT("03"), event: "ask", askRole: "B", key: "B:effects|h1:0", stop: "stop" },
  ], false, SNOW);
  assert.deepStrictEqual(Array.from(moved.lanes, (l) => l.id), ["g:B:effects"]);
  assert.deepStrictEqual(Array.from(moved.lanes[0].asks, (x) => [x.start, x.end, x.status]),
    [[Date.parse(SAT("01")), Date.parse(SAT("03")), "ok"]]);
  console.log("durableflow_check: open asks ok");
}

// (i) the export buttons: the whole way from the SVG to a saved file --------
(async () => {
  const seen = { urls: [], revoke: 0, draw: 0, toBlob: 0, download: "", shown: null, sizes: [] };
  const canvas = {
    width: 0, height: 0,
    getContext() {
      return { fillStyle: "", fillRect() {}, drawImage() { seen.draw++; } };
    },
    toBlob(cb, type) { seen.toBlob++; cb({ type, size: 1 }); },
  };
  const link = { href: "", download: "", click() { seen.download = this.download; }, remove() { seen.removed = true; } };
  ctx.Blob = class { constructor(parts, opts) { this.parts = parts; this.type = opts && opts.type; } };
  ctx.URL = {
    createObjectURL(b) { seen.urls.push(b); return "blob:test"; },
    revokeObjectURL() { seen.revoke++; },
  };
  ctx.Image = class {
    set src(v) { this._src = v; setTimeout(() => this.onload && this.onload(), 0); }
    get src() { return this._src; }
  };
  ctx.document = {
    createElement(tag) { return tag === "canvas" ? canvas : link; },
    body: { appendChild() {} },
  };
  // saveBlob releases its URL on a timer; the stub records that it asked and
  // never fires, so the test leaves no timer holding the stubs below.
  ctx.setTimeout = () => 1;
  const host = new El("div");
  const api = async () => ({ ok: true, json: async () => ({
    sim: "png-run", live: false, total: SLOG.length, since: 0, events: SLOG }) });
  const mnt = DF.mount(host, "png-run", {
    doc, api, timers: { setInterval: () => 1, clearInterval() {} }, interval: 0 });
  await new Promise((r) => setTimeout(r, 0));
  const tools = host.children[0].children[2];
  // SVG: the file carries the markup and the rules that paint it.
  tools.children[2].click();
  assert.strictEqual(seen.download, "durable-png-run-tree.svg");
  assert.match(seen.urls[0].parts[0], /^<svg/);
  assert.match(seen.urls[0].parts[0], /<style>/);
  // PNG: the same view drawn onto a canvas at twice its size, then saved.
  const tree = mnt.view.svg;
  const before = seen.urls.length;
  tools.children[3].click();
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(seen.urls.length > before, "the PNG blob is created");
  assert.strictEqual(seen.download, "durable-png-run-tree.png");
  assert.deepStrictEqual([canvas.width, canvas.height],
    [Number(tree.getAttribute("width")) * 2, Number(tree.getAttribute("height")) * 2]);
  assert.strictEqual(seen.draw, 1, "the image is drawn once");
  assert.strictEqual(seen.toBlob, 1, "and handed back as a PNG");
  assert.ok(seen.revoke >= 1, "the object URL is released");
  // The same buttons follow the view on screen.
  tools.children[1].click();
  tools.children[3].click();
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(seen.download, "durable-png-run-sequence.png");
  assert.deepStrictEqual([canvas.width, canvas.height],
    [Number(mnt.sequence.svg.getAttribute("width")) * 2,
     Number(mnt.sequence.svg.getAttribute("height")) * 2]);
  mnt.unmount();
  delete ctx.Blob; delete ctx.URL; delete ctx.Image; delete ctx.document;
  console.log("durableflow_check: export buttons ok");
})().catch((e) => { console.error(e); process.exit(1); });
