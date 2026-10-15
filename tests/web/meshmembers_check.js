/* The mesh roster's lifecycle filter must keep the ended records off the
   default view without losing them, and must keep the graph, the wiring list
   and the send form agreeing with the table about who is on the roster.

   Checked here: the pure predicates, which are where the partition is
   decided; the chip bar, which takes the session rail's multi-state
   selection (claunch-qzhea); the two sentences the wiring panel says about a
   roster the filter has shortened — the places a filtered list would
   otherwise state something false about the mesh; and the page's folding
   sections, whose closed form must not build the panel it stands for. */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(
  __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"
), "utf8");

// The state and its vocabulary live up by the message filter; the predicates
// live with the render. Two slices, joined.
const a1 = src.indexOf("const MESH_MEMBER_CATEGORIES =");
const stateTail = "let meshMemberFilter = MESH_MEMBER_DEFAULT.slice();";
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
  n.insertBefore = (child, ref) => {
    const at = n.kids.indexOf(ref);
    n.kids.splice(at < 0 ? n.kids.length : at, 0, child);
    return child;
  };
  n.querySelector = (sel) => all(n, (k) => k !== n && k.tag === sel)[0] || null;
  n.classList = { add: (...c) => c.forEach((x) => n.cls.add(x)) };
  n.dataset = {};
  Object.defineProperty(n, "firstChild", { get: () => n.kids[0] || null });
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
  "let meshFocus = null;\n" + src.slice(a1, b1) + "\n" + src.slice(a2, b2) +
  "\nexports.category = meshMemberCategory;" +
  "\nexports.shown = meshMemberShown;" +
  "\nexports.counts = meshMemberCounts;" +
  "\nexports.visible = meshVisibleInfo;" +
  "\nexports.word = meshMemberStateWord;" +
  "\nexports.bar = meshMemberFilterBar;" +
  "\nexports.param = meshMemberStateParam;" +
  "\nexports.single = meshMemberSingleWord;" +
  "\nexports.toggle = meshMemberToggle;" +
  "\nexports.preset = meshMemberPresetOf;" +
  "\nexports.label = meshMemberFilterLabel;" +
  "\nexports.fold = meshFold;" +
  "\nexports.foldOpen = meshFoldOpen;" +
  "\nexports.state = () => meshMemberFilter;" +
  "\nexports.setState = (f) => { meshMemberFilter = f; };" +
  "\nexports.focus = () => meshFocus;"
)(ctx, el, (force) => refreshed.push(force));

/* The daemon's own gds6 roster, one field wider: its shape is why this filter
   exists (8 of its 12 members are ended records). Plus every category the
   daemon names, the two a session can never be in, and a row from a daemon
   that predates the field. */
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
  { handle: "s530", session: "s530", reachability: "held", category: "hold" },
  { handle: "s531", session: "s531", reachability: "held", category: "delayed" },
  { handle: "s532", session: "s532", reachability: "exited", category: "failed" },
  { handle: "far1", session: "far1", reachability: "remote-connected", category: "remote" },
  { handle: "old1", session: "old1", reachability: "idle" },
];

// An unknown or absent category is read as `remote`, i.e. shown: of the two
// ways to be wrong about a member, hiding a working one is the costly one.
check("a row with no category is read as remote, not as dead",
  ctx.category({ handle: "x", reachability: "idle" }), "remote");
check("a daemon's own word is passed through — hold, delayed and failed too",
  ["running", "hold", "delayed", "failed", "killed", "paused", "archived",
   "missing", "remote"].map((c) => ctx.category({ category: c })),
  ["running", "hold", "delayed", "failed", "killed", "paused", "archived",
   "missing", "remote"]);

// The partition itself.
const kept = (filter) => members.filter((m) => ctx.shown(ctx.category(m), filter))
  .map((m) => m.handle);
check("the default leaves killed, failed and archived off",
  kept(ctx.state()),
  ["s517", "s520", "s524", "s557", "s514", "s530", "s531", "far1", "old1"]);
check("current is every record but the archived ones, as on the rail",
  kept("current").includes("s516") || !kept("current").includes("s507"), false);
check("killed is exited-without-the-pause-marker", kept(["killed"]), ["s507", "s508"]);
check("paused is its own list, not folded into killed", kept(["paused"]), ["s514"]);
check("archived is its own list", kept(["archived"]), ["s516"]);
check("hold, delayed and failed are lists of their own",
  [kept(["hold"]), kept(["delayed"]), kept(["failed"])], [["s530"], ["s531"], ["s532"]]);
check("a union keeps a member in any of its words",
  kept(["killed", "archived"]), ["s507", "s508", "s516"]);
check("all is everything", kept("all").length, members.length);
check("an empty selection falls back to the default, never to nobody",
  kept([]), kept(ctx.state()));
// Each member lands in exactly one partition, so the counts can be added and
// compared against the roster.
const words = ["running", "hold", "delayed", "failed", "paused", "killed",
               "archived", "missing", "remote"];
check("every member is in exactly one partition",
  words.reduce((n, w) => n + kept([w]).length, 0), members.length);

// What the daemon is asked for: the selection's words, comma-joined.
check("the default asks the daemon for its categories",
  ctx.param(ctx.state()), "running,hold,delayed,paused,remote");
check("everything is asked for as all", ctx.param("all"), "all");
check("the daemon's words are read as the presets", ctx.param("current"),
  "running,hold,delayed,failed,paused,killed,missing,remote");

// An older daemon refuses the comma-joined word; it is then asked the one
// word that covers the selection, and the page narrows what comes back.
check("a selection inside current falls back to current",
  ctx.single(["running", "remote", "hold"]), "current");
check("the default (paused included) falls back to all", ctx.single(ctx.state()), "all");

// Counts are over every member, never over the shown ones — a bar that
// counted its own output could not tell you what it is hiding.
const counts = ctx.counts(members);
check("counts come off the whole roster",
  [counts["running-paused"], counts.current, counts.all, counts.running,
   counts.killed, counts.hold, counts.failed],
  [9, 13, 14, 6, 2, 1, 1]);
check("a daemon's own counts are used as sent",
  ctx.counts([], { all: 458, running: 29, hold: 130, delayed: 6, killed: 292,
                   archived: 1 })["running-paused"], 165);

// The filter reaches the links too: an edge is only as visible as both ends.
const info = {
  members,
  member_links: [
    { a: "s517", b: "s520", enabled: true },
    { a: "s517", b: "s507", enabled: true },
    { a: "far1", b: "s516", enabled: true },
  ],
};
const shownInfo = ctx.visible(info, ctx.state());
check("a link to a hidden member goes with it", shownInfo.member_links,
  [{ a: "s517", b: "s520", enabled: true }]);
check("the panels can say how much is hidden",
  [shownInfo.members_total, shownInfo.members_hidden], [14, 5]);
check("all hides nothing", ctx.visible(info, "all").members_hidden, 0);
// The full roster is not mutated — the enrol form reads it to know who is
// already a member, and a filtered one would let a member be added twice.
check("the caller's list is left alone", info.members.length, 14);

// The row says which kind of ended it is, where the daemon knows.
check("a killed row says killed, not exited",
  ctx.word({ reachability: "exited", category: "killed" }), "killed");
check("a held row says hold, the rail's word",
  ctx.word({ reachability: "held", category: "hold" }), "hold");
check("a live row keeps the daemon's own word",
  ctx.word({ reachability: "busy", category: "running" }), "busy");

// Toggling, as on the rail.
check("a chip switches its categories on", ctx.toggle(ctx.state(), "killed")
  .includes("killed"), true);
check("running carries the remote members with it",
  ctx.toggle(ctx.state(), "running").some((c) => c === "running" || c === "remote"), false);
check("the last chip cannot be switched off",
  ctx.toggle(["killed"], "killed"), ["killed"]);
check("a preset replaces the selection", ctx.preset(ctx.toggle(["killed"], "all")), "all");
check("a hand-made selection is named by its chips",
  ctx.label(["killed", "archived"]), "Killed, Archived");

// The bar.
const tabs = (bar) => byClass(bar, "seq-tab");
const bar = ctx.bar(members);
check("presets then chips, with counts; missing has a chip only when it has rows",
  tabs(bar).map((n) => n.text),
  ["Running || Paused (9)", "Current (13)", "All (14)", "Running (6)", "Hold (1)",
   "Delayed (1)", "Failed (1)", "Paused (1)", "Killed (2)", "Archived (1)",
   "Missing (1)"]);
check("the default preset and its chips are on",
  tabs(bar).map((n) => n.cls.has("on")),
  [true, false, false, true, true, true, false, true, false, false, false]);
check("no missing chip while nobody is missing",
  tabs(ctx.bar(members.filter((m) => m.category !== "missing")))
    .some((n) => n.text.startsWith("Missing")), false);
tabs(bar)[8].fire("click");
check("pressing Killed adds it to the selection", ctx.state().includes("killed"), true);
check("pressing a chip redraws at once", refreshed, [true]);
tabs(ctx.bar(members))[0].fire("click");
check("the preset puts the default back", ctx.preset(ctx.state()), "running-paused");
tabs(ctx.bar(members))[0].fire("click");
check("re-pressing the preset already on does nothing", refreshed, [true, true]);

/* The page's sections fold. A closed one is its heading alone: the panel's
   builder is not called, so a closed wiring list costs nothing per poll. */
let built = 0;
const panel = () => {
  built++;
  const box = el("div", "mesh-wiring");
  const head = el("div", "mesh-wiring-head");
  head.appendChild(el("h3", null, "Connections"));
  box.appendChild(head);
  box.appendChild(el("p", "wf-note", "13530 rows"));
  return box;
};
check("wiring starts closed, members open", [ctx.foldOpen("wiring"),
  ctx.foldOpen("members")], [false, true]);
const closed = ctx.fold("wiring", "Connections", panel, { text: "3 connected pairs" });
check("a closed section does not build its panel", built, 0);
check("a closed section is its heading and a hint",
  [all(closed, (n) => n.tag === "h3").length, byClass(closed, "mesh-fold-hint")[0].text,
   closed.cls.has("closed")], [1, "3 connected pairs", true]);
const forced = ctx.fold("wiring", "Connections", panel, null, true);
check("a selection in the diagram opens the wiring panel", [built, forced.cls.has("open")],
  [1, true]);
const before = refreshed.length;
all(closed, (n) => n.tag === "h3")[0].fire("click");
check("the heading opens it and redraws", [ctx.foldOpen("wiring"), refreshed.length],
  [true, before + 1]);
const opened = ctx.fold("wiring", "Connections", panel);
check("an open section is the panel, its own heading the toggle",
  [built, byClass(opened, "mesh-fold-caret").length, byClass(opened, "wf-note")[0].text],
  [2, 1, "13530 rows"]);
const waiting = ctx.fold("peers", "Peer daemons", panel,
  { text: "1 join request waiting", attention: true });
check("a waiting join request is called out on a closed section",
  byClass(waiting, "attention").length, 1);

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
