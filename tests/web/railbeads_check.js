/* The rail row's beads line: what the board assigns to a session, as pills,
   plus what it filed on its own and does not hold (daemon/beads.created_of).

   The queues ride /api/beads/queues on their own clock and are painted over
   the rows that exist, the way the briefing line is. Five things must hold:

   1. the index is by session, known sessions only, a name on two boards
      keeps both queues, and `created` concatenates the same way;
   2. a row's line draws one pill per queued issue in the worker's order,
      tinted by status, the next one outlined, then one dashed pill per
      created-but-unheld issue, capped together with a "+n" that names the
      rest; each pill links to the issue and its click does not attach the
      row;
   3. hovering a pill fills the one card with the issue's id, status,
      priority, title, facts (including who created it, when that differs
      from the assignee) and the head of its description (headings dropped,
      cut at six lines), and leaving hides it;
   4. painting adds the line under the cwd line whenever either list is
      non-empty, replaces it on a repaint, and removes it when both are
      gone.

   Slice the real functions out of app.js and drive them against a stub
   DOM. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, children: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    style: {}, title: "", href: "", parent: null,
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      c.parent = this;
      const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
      return c;
    },
    remove() {
      if (!this.parent) return;
      const i = this.parent.children.indexOf(this);
      if (i >= 0) this.parent.children.splice(i, 1);
      this.parent = null;
    },
    replaceWith(c) {
      const i = this.parent.children.indexOf(this);
      c.parent = this.parent;
      this.parent.children[i] = c;
      this.parent = null;
    },
    get nextSibling() {
      if (!this.parent) return null;
      const i = this.parent.children.indexOf(this);
      return this.parent.children[i + 1] || null;
    },
    querySelector(sel) {
      const cls = sel.replace(/^\./, "");
      return this.all().slice(1).find((k) => k.classes.has(cls)) || null;
    },
    querySelectorAll(sel) {
      if (sel === "li[data-name]") return this.all().filter((k) => k.tag === "li" && k.dataset.name);
      const cls = sel.replace(/^\./, "");
      return this.all().slice(1).filter((k) => k.classes.has(cls));
    },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { for (const fn of this.handlers[k] || []) fn(ev || {}); },
    set innerHTML(v) { if (v === "") this.children = []; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get classList() {
      const self = this;
      return {
        add: (...cs) => cs.forEach((c) => self.classes.add(c)),
        remove: (...cs) => cs.forEach((c) => self.classes.delete(c)),
        contains: (c) => self.classes.has(c),
      };
    },
    all() {
      const out = [this];
      for (const k of this.children) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((n) => n.classes.has(cls)); },
  };
  return n;
}
const body = node("body");
const document = { createElement: (t) => node(t), body };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const stubs = `
let railBeads = new Map();
let railBeadsAt = 0;
let railBeadsBusy = false;
const RAIL_BEADS_EVERY = 5000;
const RAIL_BEADS_MAX = 4;
let beadPop = null;
let listEl = null;
function $(id) { return id === "session-list" ? listEl : null; }
function setList(l) { listEl = l; }
function setBeads(m) { railBeads = m; }
`;

const ctx = {};
new Function(
  "exports", "document", "el",
  stubs
  + slice("refreshRailBeads") + slice("railBeadsIndex") + slice("applyRailBeads")
  + slice("railBeadsLine") + slice("showBeadPop") + slice("hideBeadPop")
  + slice("beadsStripMeta") + slice("beadPopExcerpt")
  + `
Object.assign(exports, {
  index: railBeadsIndex, line: railBeadsLine, apply: applyRailBeads,
  excerpt: beadPopExcerpt, setList, setBeads, pop: () => beadPop,
});`)(ctx, document, el);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- 1. the index --------------------------------------------------------- */
const QUEUES = {
  boards: [
    { root: "/a", lanes: [
      { session: "s1", known: true, issues: [{ id: "a-1", status: "in_progress" }],
        created: [{ id: "a-2", status: "open" }], summary: { next: null } },
      { session: "lead", known: false, issues: [{ id: "a-9", status: "open" }], summary: {} },
    ] },
    { root: "/b", lanes: [
      { session: "s1", known: true, issues: [{ id: "b-1", status: "open" }],
        created: [{ id: "b-2", status: "open" }], summary: { next: "b-1" } },
      { session: "s2", known: true, issues: [], summary: {} },
    ] },
  ],
};
const idx = ctx.index(QUEUES);
check("known sessions only", [...idx.keys()], ["s1", "s2"]);
check("a name on two boards keeps both queues",
      idx.get("s1").issues.map((i) => i.id), ["a-1", "b-1"]);
check("and both boards' follow-ups",
      idx.get("s1").created.map((i) => i.id), ["a-2", "b-2"]);
check("an empty queue is an empty lane, not a missing one", idx.get("s2").issues, []);
check("a lane with no follow-ups gets an empty list, not a missing one",
      idx.get("s2").created, []);

/* ---- 2. the line ----------------------------------------------------------- */
const LANE = {
  issues: [
    { id: "x-1", status: "in_progress", title: "now", priority: 1 },
    { id: "x-2", status: "open", title: "next", priority: 2 },
    { id: "x-3", status: "in_ready", title: "then", priority: 2 },
    { id: "x-4", status: "in_review", title: "waiting", priority: 3 },
    { id: "x-5", status: "blocked", title: "stuck", priority: 3 },
    { id: "x-6", status: "open", title: "later", priority: 4 },
  ],
  summary: { next: "x-2" },
};
const line = ctx.line("s1", LANE);
const pills = line.find("rail-bead").filter((p) => !p.classes.has("rail-bead-more"));
check("one pill per issue in queue order, capped at four",
      pills.map((p) => p.text), ["x-1", "x-2", "x-3", "x-4"]);
check("tinted by status", pills.map((p) => p.classes.has(p.text === "x-1" ? "in_progress" : "open") || [...p.classes].join(" ")),
      [true, true, "rail-bead in_ready", "rail-bead in_review"]);
check("the next one is outlined", pills.map((p) => p.classes.has("next")), [false, true, false, false]);
check("a pill links to the issue", pills[0].href, "#/beads/x-1");
check("and says what it is on hover", pills[0].title, "x-1 [in_progress] now");
const more = line.find("rail-bead-more")[0];
check("the rest is counted and named", [more.text, more.title], ["+2", "x-5 [blocked]\nx-6 [open]"]);
let stopped = 0;
pills[0].fire("click", { stopPropagation: () => stopped++ });
check("a pill's click does not attach the row", stopped, 1);
check("no queue draws no pill", ctx.line("s1", { issues: [], summary: {} }).children.length, 0);

const withCreated = ctx.line("s1", {
  issues: [{ id: "m-1", status: "open", title: "held" }],
  created: [{ id: "m-2", status: "open", title: "filed" }],
  summary: {},
});
const wcPills = withCreated.find("rail-bead").filter((p) => !p.classes.has("rail-bead-more"));
check("assigned work first, then a follow-up, marked apart",
      wcPills.map((p) => [p.text, p.classes.has("created")]),
      [["m-1", false], ["m-2", true]]);
check("a follow-up says it is unassigned to the session on hover",
      wcPills[1].title, "m-2 [open] (created, unassigned to s1) filed");
check("a lane with only follow-ups still draws pills",
      ctx.line("s1", { issues: [], created: [{ id: "n-1", status: "open" }], summary: {} })
        .find("rail-bead").map((p) => p.text),
      ["n-1"]);
const capLine = ctx.line("s1", {
  issues: [{ id: "p-1", status: "open" }, { id: "p-2", status: "open" }, { id: "p-3", status: "open" }],
  created: [{ id: "p-4", status: "open" }, { id: "p-5", status: "open" }, { id: "p-6", status: "open" }],
  summary: {},
});
check("assigned and follow-up pills share one cap, assigned first",
      capLine.find("rail-bead").filter((p) => !p.classes.has("rail-bead-more")).map((p) => p.text),
      ["p-1", "p-2", "p-3", "p-4"]);
check("the remainder names issues from both lists",
      capLine.find("rail-bead-more")[0].title, "p-5 [open]\np-6 [open]");

/* ---- 3. the hover card ---------------------------------------------------- */
pills[0].fire("mouseenter");
let pop = ctx.pop();
check("the card is shown beside the pill", pop.classes.has("hidden"), false);
check("it says which issue, in what state, at what priority",
      [pop.find("beads-id")[0].text, pop.find("beads-status")[0].text, pop.find("beads-pri")[0].text],
      ["x-1", "in_progress", "P1"]);
check("and its title", pop.find("rail-bead-pop-title")[0].text, "now");
check("a description-less issue draws no excerpt", pop.find("rail-bead-pop-desc").length, 0);
pills[0].fire("mouseleave");
check("leaving hides it", ctx.pop().classes.has("hidden"), true);

const rich = ctx.line("s1", { issues: [{
  id: "y-1", status: "open", title: "rich", priority: 2, assignee: "s1",
  issue_type: "bug", labels: ["session", "user"],
  description: "## 목표\nfix the thing\n\nand the other thing\n## 범위\nscope",
}], summary: {} });
rich.find("rail-bead")[0].fire("mouseenter");
pop = ctx.pop();
check("facts: type, who, labels",
      pop.find("rail-bead-pop-bits")[0].text, "bug  ·  assigned to s1  ·  #session  ·  #user");
check("the excerpt drops headings and blank lines",
      pop.find("rail-bead-pop-desc")[0].text, "fix the thing\nand the other thing\nscope");
check("the card is one element, refilled",
      ctx.pop() === pop, true);
check("a long description is cut with an ellipsis line",
      ctx.excerpt(Array.from({ length: 9 }, (_, i) => `line ${i}`).join("\n")).split("\n").length, 7);
check("and so is a wide one",
      ctx.excerpt("x".repeat(300) + "\n" + "y".repeat(300)).endsWith("…"), true);
/* The front matter is three lines at the top of a description and the excerpt
   keeps six, so an issue recording a workspace would have spent half its
   preview on YAML and pushed the goal out of the popover. */
check("the excerpt takes the front matter off before counting lines",
      ctx.excerpt("---\nworkspace: alpha\n---\n## 목표\nfix the thing\n"),
      "fix the thing");

const filedByOther = ctx.line("s1", { issues: [{
  id: "z-1", status: "open", title: "filed by another", assignee: "s2", created_by: "s1",
}], summary: {} });
filedByOther.find("rail-bead")[0].fire("mouseenter");
pop = ctx.pop();
check("facts note who created it when that differs from the assignee",
      pop.find("rail-bead-pop-bits")[0].text, "assignee s2  ·  created by s1");
const selfCreated = ctx.line("s1", { issues: [{
  id: "z-2", status: "open", title: "self", assignee: "s1", created_by: "s1",
}], summary: {} });
selfCreated.find("rail-bead")[0].fire("mouseenter");
pop = ctx.pop();
check("no redundant fact when the assignee created it too",
      pop.find("rail-bead-pop-bits")[0].text, "assigned to s1");

/* ---- 4. painting the rows --------------------------------------------------- */
function row(name) {
  const li = el("li");
  li.dataset.name = name;
  li.appendChild(el("span", "dot"));
  li.appendChild(el("div", "rail-cwd"));
  li.appendChild(el("div", "rail-seen"));
  return li;
}
const list = el("ul");
const r1 = list.appendChild(row("s1"));
const r2 = list.appendChild(row("s2"));
ctx.setList(list);
ctx.setBeads(new Map([["s1", LANE], ["s2", { issues: [], summary: {} }]]));
ctx.apply();
check("a queued session gets the line, under the cwd line",
      r1.children.map((k) => [...k.classes][0]), ["dot", "rail-cwd", "rail-beads", "rail-seen"]);
check("a session with no queue gets none", r2.find("rail-beads").length, 0);
ctx.setBeads(new Map([["s1", { issues: [{ id: "z", status: "open" }], summary: {} }], ["s2", LANE]]));
ctx.apply();
check("a repaint replaces the line in place and adds one where the queue appeared",
      [r1.children.map((k) => [...k.classes][0]), r1.find("rail-bead").map((p) => p.text),
       r2.find("rail-beads").length],
      [["dot", "rail-cwd", "rail-beads", "rail-seen"], ["z"], 1]);
ctx.setBeads(new Map([
  ["s1", { issues: [], created: [{ id: "f-1", status: "open" }], summary: {} }],
  ["s2", { issues: [], summary: {} }],
]));
ctx.apply();
check("a session with only follow-ups still gets the line",
      [r1.find("rail-beads").length, r1.find("rail-bead").map((p) => p.text)],
      [1, ["f-1"]]);
check("a session with neither still gets none", r2.find("rail-beads").length, 0);
ctx.setBeads(new Map());
ctx.apply();
check("a queue that is gone takes its line with it",
      [r1.find("rail-beads").length, r2.find("rail-beads").length], [0, 0]);

if (failures) process.exit(1);
console.log("railbeads_check ok");
