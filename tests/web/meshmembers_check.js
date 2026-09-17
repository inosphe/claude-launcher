/* The mesh roster's lifecycle filter must keep the ended records off the
   default view without losing them, and must keep the graph, the wiring list
   and the send form agreeing with the table about who is on the roster.

   Two things are checked here and no others: the pure predicates, which are
   where the partition is decided, and the two sentences the wiring panel says
   about a roster the filter has shortened — those are the places a filtered
   list would otherwise state something false about the mesh. */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(
  __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"
), "utf8");

// The state and its vocabulary live up by the message filter; the predicates
// live with the render. Two slices, joined.
const a1 = src.indexOf("const MESH_MEMBER_FILTERS =");
const stateTail = 'let meshMemberFilter = "current";';
const b1 = src.indexOf(stateTail, a1) + stateTail.length;
const a2 = src.indexOf("function meshMemberCategory(");
const b2 = src.indexOf("function renderMesh(", a2);
const a3 = src.indexOf("function renderWiring(");
const b3 = src.indexOf("function renderMesh(", a3);
if (a1 < 0 || b1 <= a1 || a2 < 0 || b2 <= a2 || a3 < 0 || b3 <= a3) {
  throw new Error("cannot locate the mesh member filter");
}

function node(tag, cls, text) {
  const n = { tag, cls: new Set(String(cls || "").split(/\s+/).filter(Boolean)),
    text: text === undefined ? "" : String(text), kids: [], handlers: {} };
  n.appendChild = (child) => { n.kids.push(child); return child; };
  n.addEventListener = (event, handler) => { n.handlers[event] = handler; };
  n.fire = (event) => n.handlers[event] && n.handlers[event]();
  n.setAttribute = (name, value) => { n.attrs = Object.assign(n.attrs || {}, { [name]: value }); };
  return n;
}
const el = (tag, cls, text) => node(tag, cls, text);

function all(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const child of root.kids || []) all(child, pred, out);
  return out;
}
const byClass = (root, cls) => all(root, (n) => n.cls.has(cls));
let failures = 0;
function check(name, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error(`FAIL ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
    failures++;
  }
}

const refreshed = [];
const ctx = {};
new Function("exports", "el", "refreshMeshView",
  src.slice(a1, b1) + "\n" + src.slice(a2, b2) +
  "\nexports.category = meshMemberCategory;" +
  "\nexports.shown = meshMemberShown;" +
  "\nexports.counts = meshMemberCounts;" +
  "\nexports.visible = meshVisibleInfo;" +
  "\nexports.word = meshMemberStateWord;" +
  "\nexports.bar = meshMemberFilterBar;" +
  "\nexports.state = () => meshMemberFilter;"
)(ctx, el, (force) => refreshed.push(force));

/* The daemon's own gds6 roster, one field wider: its shape is why this filter
   exists (8 of its 12 members are ended records). Plus the two categories a
   session can never be in, and a row from a daemon that predates the field. */
const members = [
  { handle: "s517", session: "s517", reachability: "idle", category: "running" },
  { handle: "s520", session: "s520", reachability: "idle", category: "running" },
  { handle: "s524", session: "s524", reachability: "busy", category: "running" },
  { handle: "s557", session: "s557", reachability: "busy", category: "running" },
  { handle: "s507", session: "s507", reachability: "exited", category: "killed" },
  { handle: "s508", session: "s508", reachability: "exited", category: "killed" },
  { handle: "s514", session: "s514", reachability: "exited", category: "paused" },
  { handle: "s516", session: "s516", reachability: "exited", category: "archived" },
  { handle: "s528", session: "s528", reachability: "missing", category: "missing" },
  { handle: "far1", session: "far1", reachability: "remote-connected", category: "remote" },
  { handle: "old1", session: "old1", reachability: "idle" },
];

// An unknown or absent category is read as `remote`, i.e. shown: of the two
// ways to be wrong about a member, hiding a working one is the costly one.
check("a row with no category is read as remote, not as dead",
  ctx.category({ handle: "x", reachability: "idle" }), "remote");
check("a daemon's own word is passed through",
  ["running", "killed", "paused", "archived", "missing", "remote"]
    .map((c) => ctx.category({ category: c })),
  ["running", "killed", "paused", "archived", "missing", "remote"]);

// The partition itself.
const kept = (filter) => members.filter((m) => ctx.shown(ctx.category(m), filter))
  .map((m) => m.handle);
check("current is what has not ended, and keeps the unknowable",
  kept("current"), ["s517", "s520", "s524", "s557", "far1", "old1"]);
check("killed is exited-without-the-pause-marker",
  kept("killed"), ["s507", "s508"]);
check("paused is its own list, not folded into killed", kept("paused"), ["s514"]);
check("archived is its own list", kept("archived"), ["s516"]);
check("all is everything", kept("all").length, 11);
// Each member lands in exactly one partition, so the counts can be added and
// compared against the roster. `missing` is a partition of its own and is
// reachable only under `all` — a record that is gone is not a record that
// stopped, and the bar does not offer to hide it by naming it.
check("every member is in exactly one partition",
  new Set(["current", "killed", "paused", "archived", "missing"]
    .map((f) => kept(f)).flat()).size, members.length);
check("the counts add up to the roster",
  ["current", "killed", "paused", "archived", "missing"]
    .reduce((n, f) => n + kept(f).length, 0), members.length);
check("a missing record is shown only under all",
  [kept("current").includes("s528"), kept("missing").includes("s528")], [false, true]);

// Counts are over every member, never over the shown ones — a bar that
// counted its own output could not tell you what it is hiding.
check("counts come off the whole roster", ctx.counts(members),
  { current: 6, killed: 2, paused: 1, archived: 1, all: 11 });

// The filter reaches the links too: an edge is only as visible as both ends.
const info = {
  members,
  member_links: [
    { a: "s517", b: "s520", enabled: true },
    { a: "s517", b: "s507", enabled: true },
    { a: "far1", b: "s516", enabled: true },
  ],
};
const shownInfo = ctx.visible(info, "current");
check("a link to a hidden member goes with it", shownInfo.member_links,
  [{ a: "s517", b: "s520", enabled: true }]);
check("the panels can say how much is hidden",
  [shownInfo.members_total, shownInfo.members_hidden], [11, 5]);
check("all hides nothing", ctx.visible(info, "all").members_hidden, 0);
// The full roster is not mutated — the enrol form reads it to know who is
// already a member, and a filtered one would let a member be added twice.
check("the caller's list is left alone", info.members.length, 11);

// The row says which kind of ended it is, where the daemon knows.
check("a killed row says killed, not exited",
  ctx.word({ reachability: "exited", category: "killed" }), "killed");
check("a live row keeps the daemon's own word",
  ctx.word({ reachability: "busy", category: "running" }), "busy");
check("a paused row says paused",
  ctx.word({ reachability: "exited", category: "paused" }), "paused");

// The bar.
const bar = ctx.bar(members);
check("one tab per partition, with counts",
  byClass(bar, "seq-tab").map((n) => n.text),
  ["Current (6)", "Killed (2)", "Paused (1)", "Archived (1)", "All (11)"]);
check("the default is current and only current is on",
  byClass(bar, "seq-tab").map((n) => n.cls.has("on")),
  [true, false, false, false, false]);
check("the bar reuses the one tab style, adding no fourth",
  byClass(bar, "seq-tab").every((n) => n.cls.has("seq-tab")), true);
byClass(bar, "seq-tab")[3].fire("click");
check("picking archived sets the filter", ctx.state(), "archived");
check("picking a filter redraws at once", refreshed, [true]);
byClass(ctx.bar(members), "seq-tab")[3].fire("click");
check("re-picking the filter already on does nothing", refreshed, [true]);

/* The wiring panel, alone: its short-roster guard is the one place a filtered
   list would otherwise make a false claim about the mesh. Sliced on its own
   because the guard returns before it reaches anything else. */
const ctx2 = {};
new Function("exports", "el", "refreshMeshView", "meshFocus", "MeshError",
  src.slice(a3, b3) + "\nexports.render = renderWiring;"
)(ctx2, el, () => {}, null, Error);

const oneMember = [{ handle: "s517", session: "s517", reachability: "idle",
                     category: "running" }];
const shortNoFilter = ctx2.render({ members: oneMember, members_total: 1,
                                    members_hidden: 0 });
const shortFiltered = ctx2.render({ members: oneMember, members_total: 12,
                                    members_hidden: 11 });
const noteOf = (box) => byClass(box, "wf-note").map((n) => n.text).join(" ");
check("a one-member mesh still gets the enrol sentence",
  noteOf(shortNoFilter).includes("a mesh needs two members"), true);
check("a filtered roster is not reported as a mesh of one",
  noteOf(shortFiltered).includes("a mesh needs two members"), false);
check("the filtered guard names the filter and the real size",
  [noteOf(shortFiltered).includes("member filter"),
   noteOf(shortFiltered).includes("12")], [true, true]);

if (failures) process.exit(1);
console.log("meshmembers_check: ok");
