/* The briefing on a session tab's hover tooltip, run against the real
   `briefingTabTooltip` and the real `renderSessionTabs` from app.js.

   The tab bar is a name and four controls; the briefing the rail row draws a
   line of and the card draws several is reachable there only by hovering. So
   what is pinned here is what a pointer that stops on a tab is told:

   - A session with no cached briefing keeps the plain title it always had.
     The tooltip is an addition, and a session the summariser has never run
     on must not gain a blank line or the word "briefing" with nothing under
     it.
   - A session with one reads the one-line first and then goal / now /
     progress, in the card's order and under the card's labels — a reader who
     has read one surface should not be learning a second vocabulary on the
     other.
   - The title is on the anchor AND on the tab, because a child's title wins
     over its parent's wherever the pointer lands, while the three buttons
     keep their own: a pointer resting on × is asking what × does.
   - A briefing composed while the bar is on screen repaints the title. The
     bar rebuilds only when its signature moves, so a briefing left out of
     that signature would arrive on the next pin or status change and not
     before — a tooltip that is silently one event stale.
   - The digest is read, never fetched: the tooltip must cost no request, so
     hovering can never reach the summariser.

   The fields come from the list poll (daemon/briefing.digest), which is
   where test_briefing.py pins the other half of this contract. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
require(path.join(root, "src", "claude_launcher", "web", "static", "session-tabs.js"));
const src = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "app.js"), "utf8");

/* The pin block whole — the storage, the helpers, the tab renderer and the
   tooltip beside it. Sliced rather than retyped so a rename in app.js fails
   here instead of leaving this harness testing a copy nobody ships. */
function pinsBlock() {
  const a = src.indexOf("const SESSION_PIN_KEY");
  const b = src.indexOf("/* ---- end rail pins", a);
  if (a < 0 || b <= a) throw new Error("cannot locate the rail pins block");
  return src.slice(a, b);
}

/* ---- stub DOM ---- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, attrs: {},
    title: "", type: "", href: "", disabled: false, handlers: {},
    appendChild(c) { n.kids.push(c); c.parentNode = n; return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    replaceChildren(...cs) { n.kids = []; cs.forEach((c) => n.appendChild(c)); },
    addEventListener(name, fn) { n.handlers[name] = fn; },
    setAttribute(name, value) { n.attrs[name] = value; },
    getAttribute(name) { return n.attrs[name]; },
    focus() {},
    contains(other) { return other === n || descendants(n).includes(other); },
    querySelectorAll(sel) {
      const want = sel.split(",").map((s) => s.trim());
      return descendants(n).filter((k) =>
        want.some((w) => (w.startsWith(".") ? k.classes.has(w.slice(1)) : k.tag === w)));
    },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
  };
  return n;
}
function descendants(n, out = []) {
  for (const k of n.kids) { out.push(k); descendants(k, out); }
  return out;
}
const document = { createElement: node };
const bar = node("div");
const elements = { "session-tabs": bar };
const $ = (id) => elements[id] || null;
const store = new Map();
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, v),
  removeItem: (k) => store.delete(k),
};
store.set("claunch_session_pins:/", JSON.stringify(["s1", "s2"]));
// Any request at all would mean the tooltip reaches the daemon; nothing in
// this harness is allowed to call one.
const calls = [];
const api = async (url) => { calls.push(url); throw new Error("no request expected"); };

const stubs = `
let sessionsCache = [], currentName = "s1", currentPage = "terminal";
function go() {}
async function modalInfo() {}
`;

const ctx = {};
new Function("exports", "document", "$", "localStorage", "BASE", "api",
  stubs + pinsBlock() + `
Object.assign(exports, {
  tooltip: briefingTabTooltip,
  render: renderSessionTabs,
  setCache: (rows) => { sessionsCache = rows; },
});`)(ctx, document, $, localStorage, "/", api);

/* ---- the composer on its own ---- */
assert.equal(ctx.tooltip(null), "", "no briefing adds nothing");
assert.equal(ctx.tooltip({ one_line: "", state: "" }), "",
  "an empty digest adds nothing");
assert.equal(
  ctx.tooltip({ one_line: "탭 툴팁을 단다", state: "working",
                goal: "g", now: "n", progress: "p" }),
  "briefing · working\n탭 툴팁을 단다\ngoal: g\nnow: n\nprogress: p",
  "one-line first, then the card's rows in the card's order");
assert.equal(ctx.tooltip({ goal: "g", state: "idle" }),
  "briefing · idle\ngoal: g",
  "a briefing with no one-line still says what it has");
assert.equal(ctx.tooltip({ one_line: "한 줄" }), "briefing\n한 줄",
  "an unstated state leaves the head without one");

/* ---- and on the bar ---- */
const tab = (name) => bar.kids.find((k) => k.dataset.name === name);
const anchor = (name) => tab(name).kids.find((k) => k.classes.has("session-tab-open"));
const button = (name, cls) => tab(name).kids.find((k) => k.classes.has(cls));

ctx.setCache([
  { name: "s1", status: "busy",
    briefing: { one_line: "한 줄", state: "working", goal: "g", now: "n" } },
  { name: "s2", status: "idle" },
]);
ctx.render();
assert.equal(anchor("s1").title,
  "s1 — pinned — busy\nbriefing · working\n한 줄\ngoal: g\nnow: n",
  "the briefing goes under the line that names the tab");
assert.equal(tab("s1").title, anchor("s1").title,
  "the tab carries the same tooltip as its anchor");
assert.equal(anchor("s2").title, "s2 — pinned — idle",
  "a session with no briefing keeps the plain title");
assert(!anchor("s2").title.includes("\n"), "and gains no blank line");
assert.equal(button("s1", "session-tab-close").title,
  "Close s1 tab — session keeps running",
  "the close button still says what pressing it does");
assert.equal(button("s1", "session-tab-pin").title, "Unpin s1");

// A briefing composed while the bar is on screen: the next poll repaints it.
ctx.setCache([
  { name: "s1", status: "busy",
    briefing: { one_line: "한 줄", state: "blocked", goal: "g", now: "n" } },
  { name: "s2", status: "idle", briefing: { one_line: "두 줄", state: "idle" } },
]);
ctx.render();
assert.equal(anchor("s2").title, "s2 — pinned — idle\nbriefing · idle\n두 줄",
  "a briefing that arrives on a poll reaches the tooltip");
assert(anchor("s1").title.includes("briefing · blocked"),
  "and so does a state that moved");
assert.deepEqual(calls, [], "the tooltip costs no request");

/* ---- a tab of another project than the rail shows ----
   The rail narrows to one project; the tab bar does not. Such a tab keeps
   the session's real state on its dot (the session is running, not gone)
   and says where it belongs with a chip and a line in its tooltip. The
   project helpers are the page's own, sliced from app.js. */
function fn(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
bar.kids = [];
bar._signature = undefined;
const proj = {};
new Function("exports", "document", "$", "localStorage", "BASE", "api",
  stubs + `let currentProject = "hq";\n` + fn("recordProject") + "\n" +
  fn("sessionInCurrentProject") + "\n" + pinsBlock() + `
Object.assign(exports, {
  render: renderSessionTabs,
  setCache: (rows) => { sessionsCache = rows; },
  pick: (p) => { currentProject = p; },
});`)(proj, document, $, localStorage, "/", api);
proj.setCache([
  { name: "s1", status: "busy", project: "hq" },
  { name: "s2", status: "busy", project: "solo" },
]);
proj.render();
const chip = (name) => anchor(name).kids.find((k) => k.classes.has("session-tab-project"));
assert(!tab("s1").classes.has("other-project"), "a tab of the rail's project is plain");
assert.equal(chip("s1"), undefined, "...and carries no project chip");
assert(tab("s2").classes.has("other-project"), "a tab of another project is marked");
assert.equal(chip("s2").textContent, "solo", "...with a chip naming its project");
const dot2 = anchor("s2").kids.find((k) => k.classes.has("dot"));
assert(dot2.classes.has("busy") && !dot2.classes.has("unknown"),
  "...and a dot drawn from its real state");
assert.equal(anchor("s2").title,
  "s2 — pinned — busy — project solo (the rail shows hq)",
  "...and a tooltip that says which project it is in");
proj.pick("");
proj.render();
assert(!tab("s2").classes.has("other-project"), "'All projects' marks no tab");
console.log("session tab briefing tooltip checks passed");
