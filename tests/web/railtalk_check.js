// The session-card labels for what waits on the reader in the talk panes
// (claunch-wp90t): unanswered QA questions and thread replies this browser
// has not shown yet, one pill per kind on the rail row.
const assert = require("assert/strict");
const fs = require("fs");
const vm = require("vm");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
function node(tag) {
  const classes = new Set();
  return {
    tag, dataset: {}, children: [], handlers: {}, className: "", textContent: "",
    type: "", title: "",
    classList: {contains: c => classes.has(c), toggle: (c, on) => on ? classes.add(c) : classes.delete(c), add: c => classes.add(c)},
    appendChild(c) { this.children.push(c); c.parent = this; },
    replaceChildren() { this.children = []; },
    querySelector(sel) { return this.children.find(c => c.className === sel.slice(1)) || null; },
    querySelectorAll() { return []; },
    addEventListener(k, fn) { this.handlers[k] = fn; },
    replaceWith(other) {
      const i = this.parent.children.indexOf(this);
      this.parent.children[i] = other; other.parent = this.parent;
    },
    remove() { this.parent.children = this.parent.children.filter(c => c !== this); },
    setAttribute() {},
  };
}
const a = node("li"), b = node("li");
a.dataset.name = "a"; b.dataset.name = "b";
let hash = "";
const layouts = {};
const dom = { "session-list": {querySelectorAll: () => [a, b]} };
let qaRows = {}, threadRows = {}, fail = false, requests = 0;
const store = {};
const ctx = vm.createContext({
  document: {createElement: node},
  $: id => dom[id],
  location: {set hash(v) { hash = v; }, get hash() { return hash; }},
  currentPage: "terminal", currentName: "a",
  BASE: "test",
  // The real helper is a one-liner over createElement; the slice under test
  // starts below its definition in app.js.
  el: (tag, cls, text) => { const n = node(tag); n.className = cls; if (text != null) n.textContent = text; return n; },
  localStorage: {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }},
  sessLayoutFor: name => layouts[name] || {split: false, pane: null},
  setSessLayout(name, lay) { layouts[name] = lay; },
  talkShows: (pane, want) => pane === want || pane === "both",
  isTalkPane: () => false,
  splitKind: "",
  isOperatorSession: () => false,
  sessCflowRun: () => null,
  cflow2RailCache: {},
  sessionsCache: [],
  api: async (url) => {
    requests++;
    if (fail) throw Error("offline");
    if (url === "/api/qa/rail") return {ok: true, json: async () => ({sessions: qaRows})};
    if (url === "/api/threads/rail") return {ok: true, json: async () => ({sessions: threadRows})};
    throw Error("unexpected " + url);
  },
  threadNoticeSync() {}, qaNoticeSync() {}, syncSessionResourceButtons() {},
  threadNoticeClear() {},
});
const from = src.indexOf("let threadsRailCache =");
const to = src.indexOf("async function refreshCflow2Rail(");
assert.ok(from > 0 && to > from, "slice markers found");
vm.runInContext(src.slice(from, to), ctx);
const talkLine = li => li.children.find(c => c.className === "rail-talk") || null;
const run = (code) => vm.runInContext(code, ctx);
(async () => {
  // Nothing anywhere: no line at all.
  run("threadsRailCache = {}; qaRailCache = {};");
  await ctx.refreshThreadsRail(); await ctx.refreshQaRail();
  assert.equal(talkLine(a), null);
  assert.equal(talkLine(b), null);

  // One unanswered QA question on a: one pill, clicking it opens the qa pane.
  qaRows = {a: {unresolved: 1, total: 2, agent_seq: 5,
                waiting: [{id: "qa-1", title: "어떤 색?", status: "open", revision: 1,
                           answer_revision: null, updated_at: ""}]}};
  await ctx.refreshQaRail();
  let line = talkLine(a);
  assert.ok(line, "qa pill line drawn");
  assert.equal(line.children.length, 1);
  assert.equal(line.children[0].textContent, "? qa 1");
  assert.ok(line.children[0].title.includes("어떤 색?"));
  line.children[0].handlers.click({stopPropagation() {}});
  assert.equal(layouts.a.pane, "qa");
  assert.ok(hash.includes("/s/a"));
  assert.equal(talkLine(b), null, "b has nothing waiting");

  // A stale question re-ask is still one of the waiting.
  qaRows.a.waiting[0].status = "stale";
  await ctx.refreshQaRail();
  assert.equal(talkLine(a).children[0].textContent, "? qa 1");
  assert.ok(talkLine(a).children[0].title.includes("(재질문)"));

  // The threads pill counts only replies past this browser's seen mark:
  // the browser last saw seq 7 of a's threads; two replies arrived since.
  qaRows = {}; await ctx.refreshQaRail();
  assert.equal(talkLine(a), null);
  store["claunch_threadsseen:test"] = JSON.stringify({a: 7});
  threadRows = {a: {open: 2, agent_seq: 9,
                    replies: [{seq: 8, thread: "th-1", title: "배포", excerpt: "됐습니다", at: ""},
                              {seq: 9, thread: "th-2", title: "리뷰", excerpt: "", at: ""}]}};
  await ctx.refreshThreadsRail();
  line = talkLine(a);
  assert.ok(line, "threads pill line drawn");
  assert.equal(line.children.length, 1);
  assert.equal(line.children[0].textContent, "🗪 2");
  assert.ok(line.children[0].title.includes("「배포」"));

  // Opening the threads pane marks the replies seen: the pill drops on the
  // next refresh even though the rail rows did not change.
  run("markThreadsSeen('a'); applyTalkRows();");
  assert.equal(talkLine(a), null, "seen replies drop the pill");

  // A session this browser never marked starts caught up: open threads are
  // not news by themselves (the header button reads the same way).
  threadRows = {b: {open: 3, agent_seq: 4,
                    replies: [{seq: 4, thread: "th-3", title: "x", excerpt: "", at: ""}]}};
  await ctx.refreshThreadsRail();
  assert.equal(talkLine(b), null, "never-marked session has no threads pill");

  // Both kinds at once: two pills, QA first. b was marked seen at seq 4.
  qaRows = {b: {unresolved: 1, total: 1, agent_seq: 2,
                waiting: [{id: "qa-2", title: "q", status: "open", revision: 1,
                           answer_revision: null, updated_at: ""}]}};
  store["claunch_threadsseen:test"] = JSON.stringify({a: 9, b: 4});
  threadRows = {b: {open: 1, agent_seq: 5,
                    replies: [{seq: 5, thread: "th-4", title: "y", excerpt: "", at: ""}]}};
  await ctx.refreshQaRail(); await ctx.refreshThreadsRail();
  line = talkLine(b);
  assert.equal(line.children.length, 2);
  assert.equal(line.children[0].textContent, "? qa 1");
  assert.equal(line.children[1].textContent, "🗪 1");
  // The threads pill takes the pane column over an open qa pane (the qa
  // button's auto-open is the one that joins "both").
  layouts.b = {split: true, pane: "qa"};
  line.children[1].handlers.click({stopPropagation() {}});
  assert.equal(layouts.b.pane, "threads");

  // A stable pill node survives a refresh that changed nothing (no mutation
  // per poll, claunch-v87), and shrinks when the last question is answered.
  const stable = talkLine(b);
  await ctx.refreshThreadsRail(); await ctx.refreshQaRail();
  assert.equal(talkLine(b), stable);
  qaRows = {b: {unresolved: 0, total: 1, agent_seq: 6, waiting: []}};
  await ctx.refreshQaRail();
  line = talkLine(b);
  assert.equal(line.children.length, 1);
  assert.equal(line.children[0].textContent, "🗪 1");
  store["claunch_threadsseen:test"] = JSON.stringify({a: 9, b: 4});
  threadRows = {b: {open: 1, agent_seq: 5, replies: []}};
  await ctx.refreshThreadsRail();
  assert.equal(talkLine(b), null, "nothing waiting removes the line");

  fail = true; requests = 0;
  await ctx.refreshThreadsRail(); await ctx.refreshQaRail();
  assert.ok(requests > 0, "offline still counted");
  console.log("railtalk_check OK");
})().catch((e) => { console.error(e); process.exit(1); });
