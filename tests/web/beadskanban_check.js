/* The Beads board as a kanban: lanes by status, cards nested by parenting.

   The page used to be a flat list, so the two things a board is read for --
   "what state is everything in" and "what hangs off what" -- were both absent
   from it. The lanes answer the first. The second is the harder half, because
   a family does not fit in a column: a child `in_progress` under a parent
   still `open` is drawn in a different lane from its parent, and no amount of
   indenting inside one lane can show that. So the drawing splits the claim in
   two -- a card is indented under the ancestors that are IN ITS OWN LANE, and
   a parent that is anywhere else is named on the card as a link instead.

   Five things must hold, and they are what this file checks:

   1. the forest is built only from `parent-child` edges, in `br`'s direction
      (`from` is the child), and survives what agents will actually write to a
      board: an edge to an issue that is not here, two parents on one issue,
      and a cycle;
   2. a lane's rows come out in forest order, and the indent counts only the
      ancestors present in that lane -- so the nesting a reader sees is
      nesting they can follow;
   3. a card whose parent is elsewhere says so and links up to it, and a card
      with children says how many;
   4. the lanes a board draws follow the status filter, and the hierarchy is
      built from the WHOLE board rather than from what the filter left, so a
      filtered-out parent is still named on its child rather than quietly
      promoting that child to a root;
   5. the tree layout is the same cards over every status at once.

   Slice the real functions out of app.js and drive them against a stub DOM. */
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
    tag, children: [], text: "", classes: new Set(), handlers: {},
    title: "", href: "", type: "",
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get classList() {
      const self = this;
      return {
        add: (...cs) => cs.forEach((c) => self.classes.add(c)),
        contains: (c) => self.classes.has(c),
        toggle: (c, on) => { on ? self.classes.add(c) : self.classes.delete(c); },
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
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const stubs = `
let beadsFocus = "";
let beadsFilter = "active";
let beadsSession = "";
let beadsPri = null;
let beadsLayout = "board";
function setView(o) {
  if (o.focus !== undefined) beadsFocus = o.focus;
  if (o.filter !== undefined) beadsFilter = o.filter;
  if (o.session !== undefined) beadsSession = o.session;
  if (o.pri !== undefined) beadsPri = o.pri;
  if (o.layout !== undefined) beadsLayout = o.layout;
}
`;

const ctx = {};
new Function(
  "exports", "document", "el",
  stubs
  + "const BEADS_STATUSES = " + JSON.stringify(["open", "in_ready", "in_progress", "in_review", "blocked", "closed"]) + ";\n"
  + "const BEADS_ACTIVE = new Set([\"open\", \"in_ready\", \"in_progress\", \"in_review\", \"blocked\"]);\n"
  + slice("beadsFilterIssues") + slice("beadsSortIssues") + slice("beadsStatusBadge")
  + slice("beadsPriBadge")
  + slice("beadsHierarchy") + slice("beadsLaneRows") + slice("beadsCard")
  + "const BEADS_BACKLOG = new Set([\"open\"]);\n"
  + "const BEADS_GROUPS = " + JSON.stringify([
      { key: "backlog", title: "backlog", note: "nobody has taken these up" },
      { key: "todo", title: "TODO", note: "taken up, in review, or finished" },
    ]) + ";\n"
  + slice("beadsLanes") + slice("beadsGroupOf") + slice("beadsLaneGroups")
  + slice("beadsBoardLabel") + slice("beadsBoardWhere")
  + slice("beadsGroupBlock") + slice("beadsLane") + slice("beadsBoardSection")
  + `
Object.assign(exports, {
  tree: beadsHierarchy, rows: beadsLaneRows, card: beadsCard,
  lanes: beadsLanes, lane: beadsLane, section: beadsBoardSection, setView,
});`)(ctx, document, el);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- the forest -------------------------------------------------------- */
/* An epic with two children, one of which has a child of its own. `from` is
   the child, which is the direction `br dep add <child> <parent>` stores. */
const FAMILY = [
  { id: "epic", status: "open", priority: 1, updated_at: "2026-01-01" },
  { id: "kid-a", status: "in_progress", priority: 1, updated_at: "2026-01-05",
    sessions: [{ name: "s9", via: ["assignee"], status: "busy" }] },
  { id: "kid-b", status: "open", priority: 2, updated_at: "2026-01-04" },
  { id: "grand", status: "open", priority: 1, updated_at: "2026-01-03" },
  { id: "lone", status: "open", priority: 4, updated_at: "2026-01-02" },
  { id: "triaged", status: "in_ready", priority: 2, updated_at: "2026-01-06" },
];
const EDGES = [
  { from: "kid-a", to: "epic", type: "parent-child" },
  { from: "kid-b", to: "epic", type: "parent-child" },
  { from: "grand", to: "kid-b", type: "parent-child" },
];

let t = ctx.tree(FAMILY, EDGES);
check("the child is the depending side, so `to` is the parent",
      [t.parent.get("kid-a"), t.parent.get("grand")], ["epic", "kid-b"]);
check("children hang off the parent", [...(t.kids.get("epic") || [])].sort(),
      ["kid-a", "kid-b"]);
check("a root has no parent", t.parent.has("epic"), false);
check("forest order is depth-first, each level in the list's own order",
      t.order, ["triaged", "epic", "kid-a", "kid-b", "grand", "lone"]);

check("`blocks` is a relation between peers and never nests",
      ctx.tree(FAMILY, [{ from: "kid-a", to: "epic", type: "blocks" }])
        .parent.has("kid-a"), false);
check("an edge to an issue that is not on this board is dropped",
      ctx.tree(FAMILY, [{ from: "kid-a", to: "elsewhere", type: "parent-child" }])
        .parent.has("kid-a"), false);
check("an issue cannot be its own parent",
      ctx.tree(FAMILY, [{ from: "epic", to: "epic", type: "parent-child" }])
        .parent.has("epic"), false);

/* Two parents on one child: the drawing must not shuffle between polls, so
   the choice is the lowest id and not whichever edge `br` listed first. */
const TWO = [{ from: "grand", to: "kid-b", type: "parent-child" },
             { from: "grand", to: "kid-a", type: "parent-child" }];
check("two parents: the lowest id wins, whichever order the edges arrive",
      [ctx.tree(FAMILY, TWO).parent.get("grand"),
       ctx.tree(FAMILY, [...TWO].reverse()).parent.get("grand")],
      ["kid-a", "kid-a"]);

/* A cycle is what hangs a naive walk. Every edge that would close one is
   dropped, which leaves those issues as roots -- visible, not lost. */
const CYCLE = ctx.tree(FAMILY, [
  { from: "kid-a", to: "kid-b", type: "parent-child" },
  { from: "kid-b", to: "kid-a", type: "parent-child" },
]);
check("a cycle nests nothing and loses nobody",
      [CYCLE.parent.has("kid-a"), CYCLE.parent.has("kid-b"),
       CYCLE.order.length], [false, false, 6]);

check("no edges at all is a flat forest of every issue",
      ctx.tree(FAMILY, []).order.length, 6);
check("a payload from a daemon that predates the edge read does not break it",
      ctx.tree(FAMILY, undefined).order.length, 6);

/* ---- a lane's rows ----------------------------------------------------- */
t = ctx.tree(FAMILY, EDGES);
let rows = ctx.rows(FAMILY.filter((i) => i.status === "open"), t);
check("the lane is in forest order, not list order",
      rows.map((r) => r.issue.id), ["epic", "kid-b", "grand", "lone"]);
check("indent counts the ancestors that are in THIS lane",
      rows.map((r) => r.indent), [0, 1, 2, 0]);
check("and the parent is marked present",
      rows.map((r) => r.parentHere), [false, true, true, false]);

/* kid-a is `in_progress`; its parent `epic` is `open`. Alone in its lane it
   must not be indented under a parent the reader cannot see beside it. */
rows = ctx.rows(FAMILY.filter((i) => i.status === "in_progress"), t);
check("a child whose parent is in another lane is not indented",
      [rows.length, rows[0].issue.id, rows[0].indent], [1, "kid-a", 0]);
check("but it still knows its parent, and says it is elsewhere",
      [rows[0].parent, rows[0].parentHere], ["epic", false]);
check("a card knows how many children hang off it, wherever they are",
      ctx.rows([FAMILY[0]], t)[0].kids, 2);

/* A grandchild whose own parent is filtered out but whose grandparent is in
   the lane: it steps in one level, under the ancestor that IS visible. */
rows = ctx.rows([FAMILY[0], FAMILY[3]], t);
check("a broken chain indents by what is visible, not by true depth",
      rows.map((r) => [r.issue.id, r.indent, r.parentHere]),
      [["epic", 0, false], ["grand", 1, false]]);

/* ---- a card ------------------------------------------------------------ */
let card = ctx.card({
  issue: { id: "kid-a", title: "the child", status: "in_progress", priority: 1,
           issue_type: "bug", labels: ["found"], assignee: "s9",
           sessions: [{ name: "s9", via: ["assignee"], status: "busy" }] },
  indent: 0, parent: "epic", parentHere: false, kids: 3,
});
check("the id links to the detail route", card.find("beads-id")[0].href,
      "#/beads/kid-a");
check("so does the title, which is the bigger target",
      card.find("beads-card-title")[0].href, "#/beads/kid-a");
check("priority is on the card and on its left edge",
      [card.find("beads-pri")[0].text, card.classes.has("pri1")], ["P1", true]);
check("and the badge itself is ranked, so its color can follow",
      card.find("beads-pri")[0].classes.has("p1"), true);
const up = card.find("beads-rel-up")[0];
check("a parent in another lane is named and links up",
      [up.text, up.href], ["↰ epic", "#/beads/epic"]);
check("and children are counted, since they may be in any lane",
      card.find("beads-rel-kids")[0].text, "3 children");
check("the badge row carries type, labels and assignee",
      card.find("beads-card-badges")[0].children.map((b) => b.text),
      ["bug", "#found", "→ s9"]);
const tag = card.find("beads-sess")[0];
check("the session tag still links to the terminal, with why it matched",
      [tag.href, tag.classes.has("busy"), tag.title],
      ["#/s/s9", true, "s9 (busy) — assignee"]);

card = ctx.card({ issue: { id: "epic", title: "t", status: "open", priority: 2 },
                  indent: 0, parent: "", parentHere: false, kids: 1 });
check("one child is not 1 children", card.find("beads-rel-kids")[0].text, "1 child");
check("a root card says nothing about a parent it does not have",
      card.find("beads-rel-up").length, 0);

card = ctx.card({ issue: { id: "x", title: "t", status: "open", priority: 2 },
                  indent: 0, parent: "epic", parentHere: true, kids: 0 });
check("a card whose parent is right above it spends no row saying so",
      card.find("beads-card-rel").length, 0);
check("and an issue with no type, labels or assignee grows no empty badge row",
      card.find("beads-card-badges").length, 0);

/* The board once drew nothing at all, and this is the check that would have
   caught it. `beadsCard` decided whether an optional row had anything in it
   by reading `.kids.length` off a node `el()` had just made -- a name this
   stub happened to use for its child array, and one the DOM does not have.
   In a browser `rel.kids` is `undefined`, the read threw on the first card,
   and every board rendered empty. The stub was the only reader that made it
   work, so 63 green checks said nothing about the page.

   The stub now calls that array by its DOM name, which is what actually
   holds the line: any reintroduction of a stub-only property throws right
   here. This check states the rule so the next reader knows why. */
for (const name of ["beadsCard", "beadsLane", "beadsBoardSection"]) {
  check(`${name} reads no property the DOM does not have`,
        (slice(name).match(/\.(kids|classes|handlers)/g) || []), []);
}

/* The indent is capped: a chain deeper than four would walk a card off the
   right edge of a 210px lane. */
check("indent is a class, and capped",
      [1, 2, 4, 7].map((n) => {
        const c = ctx.card({ issue: { id: "x", title: "t", status: "open" },
                             indent: n, parent: "p", parentHere: true, kids: 0 });
        return [...c.classes].filter((k) => k.startsWith("ind"))[0];
      }),
      ["ind1", "ind2", "ind4", "ind4"]);

/* ---- which lanes ------------------------------------------------------- */
check("active includes the triaged work",
      ctx.lanes("active"), ["open", "in_ready", "in_progress", "in_review", "blocked"]);
check("all adds closed", ctx.lanes("all"),
      ["open", "in_ready", "in_progress", "in_review", "blocked", "closed"]);
check("one status is a board of one lane, not four with three empty",
      ctx.lanes("blocked"), ["blocked"]);
check("in_ready is a board of one lane",
      ctx.lanes("in_ready"), ["in_ready"]);

const lane = ctx.lane("open", ctx.rows(FAMILY.filter((i) => i.status === "open"), t));
check("a lane is headed by its status and its count",
      [lane.find("beads-lane-name")[0].text, lane.find("beads-lane-count")[0].text],
      ["open", "4"]);
check("an empty lane still stands, and says it is empty",
      ctx.lane("blocked", []).find("beads-lane-empty").length, 1);

/* ---- the board section ------------------------------------------------- */
const BOARD = { root: "/repo", issues: FAMILY, deps: EDGES,
                sessions: [{ name: "s9", status: "busy" }] };

ctx.setView({ filter: "active", session: "", layout: "board", focus: "" });
let sec = ctx.section(BOARD);
check("the board draws one lane per active status",
      sec.find("beads-lane").map((l) => l.find("beads-lane-name")[0].text),
      ["open", "in_ready", "in_progress", "in_review", "blocked"]);
check("every issue lands in the lane its status names",
      sec.find("beads-lane").map((l) => l.find("beads-card").length),
      [4, 1, 1, 0, 0]);

/* The filter hides `epic`, but `kid-a` and `kid-b` are still its children --
   the hierarchy is a fact about the board, not about the view. */
ctx.setView({ filter: "in_progress" });
sec = ctx.section(BOARD);
check("a single-status filter draws that lane alone",
      sec.find("beads-lane").length, 1);
check("and a card whose parent the filter hid still names the parent",
      sec.find("beads-rel-up")[0].text, "↰ epic");

/* Only kid-a is tagged s9. The lanes are the board's shape and must not
   collapse to the one lane that happened to keep a card. */
ctx.setView({ filter: "active", session: "s9" });
sec = ctx.section(BOARD);
check("the session filter narrows the cards, not the lanes",
      [sec.find("beads-lane").length, sec.find("beads-card").length,
       sec.find("beads-id")[0].text], [5, 1, "kid-a"]);
check("and the one card left keeps its lane",
      sec.find("beads-lane").map((l) => l.find("beads-card").length),
      [0, 0, 1, 0, 0]);

/* Priority is the second axis over the same lanes: epic, kid-a and grand are
   the board's P1 work, and lone is its only P4. */
ctx.setView({ filter: "active", session: "", pri: 1 });
sec = ctx.section(BOARD);
check("the priority filter narrows the cards, not the lanes",
      [sec.find("beads-lane").length, sec.find("beads-card").length], [5, 3]);
ctx.setView({ pri: 0 });
check("a priority that leaves nothing names itself in the note",
      ctx.section(BOARD).find("wf-note")[1].text, "nothing active here at P0");
ctx.setView({ pri: null });

ctx.setView({ filter: "active", session: "" });
check("a board that could not be read says why instead of drawing lanes",
      [ctx.section({ root: "/repo", error: "br is not installed" })
         .find("beads-lane").length,
       ctx.section({ root: "/repo", error: "br is not installed" })
         .find("wf-warning")[0].text],
      [0, "br is not installed"]);
ctx.setView({ filter: "closed" });
check("a filter that leaves nothing says so rather than drawing an empty lane",
      [ctx.section(BOARD).find("beads-lane").length,
       ctx.section(BOARD).find("wf-note")[1].text],
      [0, "nothing closed here"]);

/* ---- the tree reading -------------------------------------------------- */
ctx.setView({ filter: "active", layout: "tree" });
sec = ctx.section(BOARD);
check("the tree layout has no lanes", sec.find("beads-lane").length, 0);
check("it is the whole visible board in forest order",
      sec.find("beads-card-title").length, 6);
check("and the nesting there is the real depth, since every status is present",
      sec.find("beads-card").map((c) =>
        [...c.classes].filter((k) => k.startsWith("ind"))[0] || "ind0"),
      ["ind0", "ind0", "ind1", "ind1", "ind2", "ind0"]);

/* A board from a daemon that predates the edge read: no `deps` key at all.
   The lanes must still draw -- flat, which is what a board with no edges
   looks like anyway. */
ctx.setView({ filter: "active", layout: "board" });
check("an older payload with no deps draws a flat board rather than nothing",
      ctx.section({ root: "/repo", issues: FAMILY, sessions: [] })
        .find("beads-card").length, 6);

if (failures) process.exit(1);
console.log("beadskanban_check ok");
