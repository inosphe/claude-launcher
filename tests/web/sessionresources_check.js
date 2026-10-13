// Exercise the shipped ownership poll and DOM reconciliation across transfers,
// deletion, session switches and independent v1/v2 workflow snapshots.
const assert = require("assert/strict");
const fs = require("fs");
const vm = require("vm");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
function node() {
  const classes = new Set();
  return {
    dataset: {}, children: [], handlers: {}, className: "", textContent: "",
    classList: {contains: c => classes.has(c), toggle: (c, on) => on ? classes.add(c) : classes.delete(c)},
    appendChild(n) { this.children.push(n); n.parent = this; },
    replaceChildren() { this.children = []; },
    querySelector(sel) { return this.children.find(n => n.className === sel.slice(1)); },
    addEventListener(k, fn) { this.handlers[k] = fn; },
    remove() { this.parent.children = this.parent.children.filter(n => n !== this); },
  };
}
const a = node(), b = node(); a.dataset.name = "a"; b.dataset.name = "b";
const run = node(), sim = node(), chart = node(), qa = node();
const dom = {"session-list": {querySelectorAll: () => [a, b]}, "term-simsbtn": sim, "term-splitbtn": run,
             "term-chartbtn": chart, "term-qabtn": qa};
let rows = [], fail = false, requests = 0;
const ctx = vm.createContext({
  document: {createElement: node}, $: id => dom[id],
  currentName: "a", currentPage: "terminal", cflow2RailCache: {},
  sessionsCache: [{name: "a", kind: ""}, {name: "b", kind: "pi-durable"}],
  v1: null, sessCflowRun(name) { return ctx.v1?.scope === name ? ctx.v1 : null; },
  api: async () => { requests++; if (fail) throw Error("offline"); return {ok: true, json: async () => ({sims: rows})}; },
});
vm.runInContext(src.slice(src.indexOf("let sessionSimCache ="), src.indexOf("async function refreshCflow2Rail(")), ctx);
const has = n => n.classList.contains("has-resource");
(async () => {
  rows = [{id: "sim-1", owner: "a", title: "<unsafe>", state: "idle"},
          {id: "sim-2", owner: "a", title: "Second", state: "running", run_rounds: 2, max_rounds: 10, runner_session: "runner-1"},
          {id: "sim-3", owner: "", title: "Unowned", state: "stopped"}];
  await ctx.refreshSessionSims();
  assert.equal(a.children[0].children.length, 2);
  assert.equal(b.children.length, 0);
  assert.equal(a.children[0].children[0].textContent, "◬ <unsafe> · 대기");
  assert.equal(a.children[0].children[0].href, "#/sims/sim-1");
  const block = a.children[0].children[1];
  assert.equal(block.className, "sess-sim-running");
  assert.equal(block.children[1].textContent, "실행 중");
  assert.equal(block.children[2].textContent, "실행 세션 runner-1 · 2회 완료 / 최대 10회");
  let stopped = false;
  a.children[0].children[0].handlers.click({stopPropagation() { stopped = true; }});
  assert.ok(stopped);
  assert.ok(has(sim)); assert.ok(!has(run));
  // ◇ chart shows only on a pi-durable session (claunch-neeya.1).
  assert.ok(chart.classList.contains("hidden"), "a plain session has no chart button");
  ctx.currentName = "b"; ctx.syncSessionResourceButtons();
  assert.ok(!chart.classList.contains("hidden") && has(chart), "a pi-durable session shows it");
  ctx.currentName = "a"; ctx.syncSessionResourceButtons();
  const stable = a.children[0].children[0];
  await ctx.refreshSessionSims();
  assert.equal(a.children[0].children[0], stable, "unchanged polls preserve links");
  rows[1].run_rounds = 3;
  await ctx.refreshSessionSims();
  assert.ok(a.children[0].children[1].children[2].textContent.includes("3회 완료"));
  rows[1].state = "stopped";
  await ctx.refreshSessionSims();
  assert.equal(a.children[0].children[1].className, "");
  assert.equal(a.children[0].children[1].textContent, "◬ Second · 중지됨");
  rows = [{id: "sim-1", owner: "b", title: "Transferred", state: "stopped"}];
  await ctx.refreshSessionSims();
  assert.equal(a.children.length, 0); assert.equal(b.children.length, 1);
  assert.ok(!has(sim));
  ctx.currentName = "b"; ctx.syncSessionResourceButtons(); assert.ok(has(sim));
  ctx.v1 = {scope: "b"}; ctx.syncSessionResourceButtons(); assert.ok(has(run));
  ctx.v1 = null; ctx.cflow2RailCache.b = {status: "done"};
  ctx.syncSessionResourceButtons(); assert.ok(has(run));
  ctx.currentName = "a"; ctx.syncSessionResourceButtons(); assert.ok(!has(run)); assert.ok(!has(sim));
  ctx.currentName = "b"; fail = true; await ctx.refreshSessionSims();
  ctx.syncSessionResourceButtons(); assert.ok(has(sim), "failure retains last snapshot");
  fail = false; rows = []; delete ctx.cflow2RailCache.b; await ctx.refreshSessionSims();
  assert.equal(b.children.length, 0); assert.ok(!has(sim)); assert.ok(!has(run));
  rows = [{id: "sim-1", owner: "b", title: "Recreated", state: "idle"}];
  await ctx.refreshSessionSims(); b.replaceChildren(); ctx.applySimulationBadges();
  assert.equal(b.children.length, 1, "rail rebuild restores cached ownership");
  ctx.currentPage = "home"; ctx.syncSessionResourceButtons(); assert.ok(!has(sim));
  const before = requests;
  await Promise.all([ctx.refreshSessionSims(), ctx.refreshSessionSims()]);
  assert.equal(requests - before, 1, "overlapping polls share one request");
  // ? qa lights while a question of the session waits (claunch-l8vyi).
  ctx.currentPage = "terminal"; ctx.currentName = "a";
  vm.runInContext('qaRailCache = {a: {unresolved: 1, agent_seq: 4}, b: {unresolved: 0, agent_seq: 9}}', ctx);
  ctx.syncSessionResourceButtons(); assert.ok(has(qa), "an unanswered question lights ? qa");
  ctx.currentName = "b"; ctx.syncSessionResourceButtons(); assert.ok(!has(qa), "answered or closed only: dark");
  ctx.currentName = "ghost"; ctx.syncSessionResourceButtons(); assert.ok(!has(qa), "no questions: dark");
  console.log("session resources checks passed");
})().catch(e => { console.error(e); process.exitCode = 1; });
