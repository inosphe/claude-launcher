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
const run = node(), sim = node();
const dom = {"session-list": {querySelectorAll: () => [a, b]}, "term-simsbtn": sim, "term-splitbtn": run};
let rows = [], fail = false, requests = 0;
const ctx = vm.createContext({
  document: {createElement: node}, $: id => dom[id],
  currentName: "a", currentPage: "terminal", cflow2RailCache: {},
  v1: null, sessCflowRun(name) { return ctx.v1?.scope === name ? ctx.v1 : null; },
  api: async () => { requests++; if (fail) throw Error("offline"); return {ok: true, json: async () => ({sims: rows})}; },
});
vm.runInContext(src.slice(src.indexOf("let sessionSimCache ="), src.indexOf("async function refreshCflow2Rail(")), ctx);
const has = n => n.classList.contains("has-resource");
(async () => {
  rows = [{id: "sim-1", owner: "a", title: "<unsafe>", state: "idle"},
          {id: "sim-2", owner: "a", title: "Second", state: "running"},
          {id: "sim-3", owner: "", title: "Unowned", state: "stopped"}];
  await ctx.refreshSessionSims();
  assert.equal(a.children[0].children.length, 2);
  assert.equal(b.children.length, 0);
  assert.equal(a.children[0].children[0].textContent, "◬ <unsafe> · 대기");
  assert.equal(a.children[0].children[0].href, "#/sims/sim-1");
  let stopped = false;
  a.children[0].children[0].handlers.click({stopPropagation() { stopped = true; }});
  assert.ok(stopped);
  assert.ok(has(sim)); assert.ok(!has(run));
  const stable = a.children[0].children[0];
  await ctx.refreshSessionSims();
  assert.equal(a.children[0].children[0], stable, "unchanged polls preserve links");
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
  console.log("session resources checks passed");
})().catch(e => { console.error(e); process.exitCode = 1; });
