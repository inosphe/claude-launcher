/* durable-flow.js (claunch-neeya): a pi-durable run session's event log as a
   flow diagram in its ⬒ run pane, updated in place.

   - model(): one column per phase in the order the loop entered it, nodes in
     time order; a result event completes the node of its ask instead of
     adding one; the node set for a prefix of the log is a prefix of the
     node set for the whole log, at the same places (append-stable).
   - mount(): polls only the tail (`since`), keeps ONE <svg> across polls,
     keeps every node element it drew (same object), appends the new ones,
     rewrites the state of the ones that changed, moves the pending node and
     drops it when the run ends; the poll stops once the session is gone. */
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

// -- app.js: the ⬒ run pane of a pi-durable session mounts the flow ----------
(async () => {
  const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
  const start = src.indexOf("async function refreshSplit(");
  let depth = 0, end = -1;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) { end = j + 1; break; } }
  }
  const pane = new El("div");
  const mounted = [];
  const asked = [];
  const sandbox = vm.createContext({
    encodeURIComponent, clearInterval: () => { sandbox.cleared = true; },
    $: () => pane, el: (t, c, x) => { const e = new El(t); e.className = c; e.textContent = x; return e; },
    api: async (url) => { asked.push(url); return { ok: true, json: async () => ({
      session: { name: "sim-abc123-run1", kind: url.includes("plain") ? undefined : "pi-durable" },
      cflow: { cwd: "/repo", scope: "x" } }) }; },
    DurableFlow: { mount: (host, name, opts) => { mounted.push({ host, name, opts }); return { unmount() { sandbox.unmounted = true; } }; } },
  });
  vm.runInContext(`var splitFor = "sim-abc123-run1", splitKind = "run", splitCwd = null, splitScope = null,
    splitPollTimer = 3, splitDurable = null; var globalThis = this; ${src.slice(start, end)}`, sandbox);
  await vm.runInContext("refreshSplit()", sandbox);
  assert.strictEqual(mounted.length, 1);
  assert.strictEqual(mounted[0].name, "sim-abc123-run1");
  assert.strictEqual(mounted[0].host, pane);
  assert.ok(sandbox.cleared, "the run poll stops; the flow keeps its own");
  assert.deepStrictEqual(asked, ["/api/sessions/sim-abc123-run1/meta"], "no workflow run is asked for");
  await vm.runInContext("refreshSplit()", sandbox);
  assert.strictEqual(mounted.length, 1, "mounted once");
  assert.strictEqual(asked.length, 1);
  // closeSplit unmounts it.
  assert.match(src, /if \(splitDurable\) \{ splitDurable\.unmount\(\); splitDurable = null; \}/);
  // Any other session is left to the workflow run.
  vm.runInContext(`splitFor = "plain"; splitCwd = null; splitDurable = null;`, sandbox);
  asked.length = 0;
  await vm.runInContext("refreshSplit()", sandbox).catch(() => {});
  assert.strictEqual(mounted.length, 1);
  assert.ok(asked.some((u) => u.startsWith("/api/cflow/run?")), asked.join(" "));
  console.log("durableflow_check: run pane ok");
})().catch((e) => { console.error(e); process.exit(1); });
