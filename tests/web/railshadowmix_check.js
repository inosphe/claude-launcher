/* Other daemons' sessions (remote-shadow rows, daemon/shadow.py) drawn in the
   one session list, among this daemon's rows.

   The rail used to keep them in a section of their own below the list, so a
   rail grouped by mesh showed a mesh's local members under its heading and
   its remote members somewhere else. Now one list draws both kinds of item:
   a local row (li[data-name], keyed by bare name, with its controls) and a
   shadow card (li.shadow-card, keyed by machine and name, with none).

   What has to hold:
   - under a mesh tab, every member the rail can show sits together, and
     the tab counts them all; a mesh only remote members are in still gets
     its tab;
   - a shadow card carries no data-name and no control, so nothing that acts
     on a local row by name (kill, pin, the keyboard card, the filters'
     record lookup) can be handed one -- and a remote `s1` stays apart from
     the local `s1`;
   - ownership: a remote session has no project here, the meshes it sits in
     do, so a picked project shows exactly the shadows it owns through a
     mesh, and files each under one of THAT project's meshes;
   - ungrouped, the shadows trail the local rows under `remote · <machine>`;
     grouped by workspace, they file under `@<machine>`;
   - the status filter and the search apply to them, a search by meaning
     never matches one, and a tab holding a matching one is marked;
   - a shadow whose card names a parent (the spawning session, on its own
     daemon) sits right under that parent's card, indented with the └ tick,
     when the parent is a shadow of the same machine in the same bucket; a
     card without `parent` (an older host, a host that did not answer), a
     parent on another machine, under another heading or hidden by the
     filter leaves it a root (claunch-7u8nm). */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .+$`, "m"));
  if (!m) throw new Error(`cannot locate ${name} in app.js`);
  return m[0] + "\n";
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- stub DOM ----------------------------------------------------------
   Selectors: the forms the rail asks with -- `.cls`, `tag.cls`,
   `tag[data-name]`, an optional `:not(.cls)`, and comma lists of those. */
function matches(n, sel) {
  return sel.split(",").some((one) => {
    const m = one.trim().match(
      /^([a-z]*)((?:\.[\w-]+)*)(\[data-name\])?(?::not\(\.([\w-]+)\))?$/);
    if (!m) throw new Error("stub cannot read selector " + one);
    const [, tag, cls, named, not] = m;
    if (tag && n.tag !== tag) return false;
    for (const c of cls.split(".").filter(Boolean)) if (!n.classes.has(c)) return false;
    if (named && !n.dataset.name) return false;
    if (not && n.classes.has(not)) return false;
    return true;
  });
}
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {},
    style: { setProperty() {}, getPropertyValue() { return ""; } },
    attrs: {}, title: "", type: "", handlers: {}, parent: null,
    appendChild(c) { c.parent = n; n.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener(name, fn) { n.handlers[name] = fn; },
    setAttribute(k, v) { n.attrs[k] = v; },
    querySelector(sel) { return n.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) { return walk(n).filter((k) => matches(k, sel)); },
    getBoundingClientRect() { return { top: 0, height: 20 }; },
    get textContent() { return n.text; },
    // Writing text replaces the children, as the DOM does.
    set textContent(v) { n.text = String(v); n.kids = []; },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return ""; },
    set innerHTML(v) { n.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => {
      const want = on === undefined ? !n.classes.has(c) : !!on;
      if (want) n.classes.add(c); else n.classes.delete(c);
      return want;
    },
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const document = {
  createElement: node,
  createDocumentFragment: () => node("fragment"),
  createTextNode: (text) => ({ textContent: text }),
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const list = node("ul");
const tabBar = node("div");
tabBar.classes.add("hidden");
const groupHeader = node("div");
groupHeader.classes.add("hidden");
const $ = (id) => (id === "session-list" ? list
  : id === "session-group-tabs" ? tabBar
  : id === "session-group-header" ? groupHeader : null);
let served = { sessions: [] };
const api = async () => ({ ok: true, json: async () => served });
const location = { hash: "" };

/* Everything refreshSessions leans on that this harness is not about. The
   row's own layout is raillayout_check's, the card's own content is
   shadowcard_check's; here the question is where each item lands. */
const stubs = `
let sessionFilter = "current";
const SESSION_FILTERS = ["current", "running-paused", "running", "killed", "paused", "archived"];
let sessionSearch = { q: "", hits: null, pending: false, error: "", index: null };
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, snapshotName = null;
let railRedrawPending = false, briefingLLM = true, ragConfigured = false;
let currentProject = "";
let shadowRows = [], shadowOpen = null;
let sessionGroupTab = {}, sessionGroupTabScrollReset = false;
function railHeld() { return false; }
function forgetDeadSessions() {}
function refreshResumeChoices() {}
function refreshParentChoices() {}
function renderHome() {}
function syncBulkActions() {}
function syncMobileBars() {}
function renderTermHandle() {}
function applyCflowBadges() {}
function applyGotoFlash() {}
function applyRailQuiet() {}
function applyBriefingCards() {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function ctxNoteOnRow() {}
function ctxRailLine() { return null; }
function railCwdLine() { return el("span", "rail-cwd"); }
function railSeenLine() { return el("span", "rail-seen"); }
function decorateNoteRow() {}
function decorateProgressRow() {}
function decorateBriefingRow() {}
function openDetail() {}
function openSpawnModal() {}
function railCardKey() {}
function go() {}
// The drag on the strip needs real pointer input: tabdragscroll_browser.cjs.
function enableTabDragScroll() {}
`;

const names = [
  "byLineage", "sessionCategory", "sessionMatchesFilter", "sessionFilterCounts",
  "sessionGroupActiveTab", "setSessionGroupTab", "sessionGroupTabRows",
  "scrollSessionListTop", "selectSessionGroupTab", "renderSessionGroupTabs",
  "renderSessionGroupHeader", "syncSessionGroupTabSearch", "sessionGroupRowVisible",
  "syncSessionFilters",
  "sessionSearchText", "sessionMatchesSearch",
  "recordProject", "sessionInCurrentProject", "railSessions",
  "sessMeshes", "sessionMeshGroup", "sessionWorkspaceGroup", "sessionWorkspaceLabel",
  "sessionGroupValue", "shadowMeshGroup", "shadowGroupValue",
  "regroupDepths", "sessionGroupRows", "remoteRailRows", "shadowLineage",
  "railMeshTags", "sessHandles", "handleTag", "profileHarnessLabel", "railMetaText",
  "refreshSessions",
  "shadowKey", "shadowHash", "shadowRow", "shadowMeshes", "railShadows",
  "shadowCategory", "shadowMatchesFilter", "shadowSearchText", "shadowMatchesSearch",
  "syncShadowActive", "shadowStatus", "shadowCflowGated", "shadowCflowText",
  "renderShadowCard",
];

const ctx = {};
new Function(
  "exports", "document", "el", "api", "$", "location", "meshCache", "sessionGroupOrder",
  stubs + constLine("RAIL_MESH_TAGS") + names.map(slice).join("\n") + `
Object.assign(exports, {
  refresh: refreshSessions,
  filters: () => syncSessionFilters(railSessions()),
  active: syncShadowActive,
  cache: () => sessionsCache.map((s) => s.name),
  set: (key, value) => {
    switch (key) {
      case "meshes": meshCache = value; break;
      case "groups": sessionGroupOrder = value; break;
      case "project": currentProject = value; break;
      case "shadows": shadowRows = value; break;
      case "filter": sessionFilter = value; break;
      case "search": sessionSearch = { q: "", hits: null, pending: false, error: "",
                                       index: null, ...value }; break;
      case "page": currentPage = value; break;
      case "open": shadowOpen = value; break;
    }
  },
});`)(ctx, document, el, api, $, location, [], ["mesh"]);

/* ---- the fleet --------------------------------------------------------- */
const local = (session, role) => ({
  session, handle: session, role, roles: [role], local: true, machine: "",
});
const remote = (session, machine) => ({
  session, handle: session, role: "worker", roles: ["worker"], local: false, machine,
});
served = { sessions: [
  { name: "s1", status: "idle", profile: "nc", parent: null, cwd: "C:/w" },
  { name: "s2", status: "exited", profile: "nc", parent: null, cwd: "C:/w" },
  { name: "s3", status: "idle", profile: "nc", parent: null, cwd: "C:/w" },
] };
const MESHES = [
  { name: "alpha", project: "default",
    members: [local("s1", "worker"), remote("s1", "m2"), remote("r3", "m3"),
              remote("r5", "m2")] },
  { name: "bravo", project: "default", members: [local("s2", "worker")] },
  // Only other daemons' sessions are in this one.
  { name: "gamma", project: "default", members: [remote("r3", "m3"), remote("r4", "m3")] },
  // Another project's mesh.
  { name: "zeta", project: "other", members: [remote("r9", "m2"), remote("r5", "m2")] },
];
const via = (mesh, project, handle) => ({ mesh, project, handle, role: "worker",
                                          roles: ["worker"], linked: true });
const card = (session, extra) => ({ session, handle: session, role: "worker",
                                    status: "idle", exited: false, ...extra });
const SHADOWS = [
  // The same bare name as the local s1, on another daemon.
  { machine: "m2", session: "s1", meshes: [via("alpha", "default", "s1")],
    card: card("s1", { note: "remote twin" }), error: null },
  { machine: "m3", session: "r3",
    meshes: [via("gamma", "default", "r3"), via("alpha", "default", "r3")],
    card: card("r3", { status: "exited", exited: true }), error: null },
  { machine: "m3", session: "r4", meshes: [via("gamma", "default", "r4")],
    card: card("r4"), error: null },
  // In a mesh of each project: owned by both, filed per project.
  { machine: "m2", session: "r5",
    meshes: [via("alpha", "default", "r5"), via("zeta", "other", "r5")],
    card: card("r5"), error: null },
  // Only in the other project's mesh.
  { machine: "m2", session: "r9", meshes: [via("zeta", "other", "r9")],
    card: card("r9"), error: null },
];
/* The page narrows meshCache to the picked project (refreshMeshes); this
   does the same. */
function pick(project) {
  ctx.set("project", project);
  ctx.set("meshes", MESHES.filter((m) => !project || m.project === project));
}

const groups = () => list.kids.filter((n) => n.classes.has("session-group"));
const groupOf = (value) => groups().find((g) => g.dataset.value === value);
const bodyOf = (g) => g.kids.find((k) => k.classes.has("session-group-body"));
const countOf = (g) => g.querySelector(".session-group-count").text;
const items = (root) => walk(root).filter((n) => n.tag === "li" &&
  (n.dataset.name || n.classes.has("shadow-card")));
const who = (n) => (n.dataset.name ? n.dataset.name
  : `${n.dataset.session}@${n.dataset.machine}`);
const shadowCard = (key) => walk(list).find((n) => n.classes.has("shadow-card")
  && `${n.dataset.session}@${n.dataset.machine}` === key);
const shown = () => items(list).filter((n) => !n.classes.has("session-filtered")).map(who);
/* The tab strip: one tab per group, the open one's rows in the list. */
const tabs = () => tabBar.kids.filter((n) => n.classes.has("session-group-tab"));
const tabValues = () => tabs().map((t) => t.dataset.value);
const tabOf = (value) => tabs().find((t) => t.dataset.value === value);
const tabCount = (value) => tabOf(value).querySelector(".session-group-count").text;
const openTab = () => tabs().filter((t) => t.classes.has("active")).map((t) => t.dataset.value);
async function open(value) {
  tabOf(value).handlers.click();
  await new Promise((r) => setTimeout(r, 0));
}
/* Every shadow the tabs hold, open or not. */
const tabShadows = () => tabs().flatMap((t) => t._groupRows)
  .filter((r) => r.type === "shadow").map((r) => `${r.shadow.session}@${r.shadow.machine}`);

(async () => {
  pick("");
  ctx.set("shadows", SHADOWS);
  await ctx.refresh();

  /* ---- one list, mesh tabs hold both kinds --------------------------- */
  check("every mesh with a member on the rail gets a tab, remote-only ones too",
        tabValues(), ["(no mesh)", "alpha", "bravo", "gamma", "zeta"]);
  check("the first tab is open, and only its rows are drawn",
        [openTab(), items(list).map(who)], [["(no mesh)"], ["s3"]]);
  await open("alpha");
  check("a mesh tab holds its local rows, then its remote cards",
        items(list).map(who), ["s1", "s1@m2", "r3@m3", "r5@m2"]);
  check("and the tab counts all of them", tabCount("alpha"), "4");
  check("no heading is drawn in the list: the tab names the group",
        list.querySelectorAll(".session-group-heading").length, 0);
  check("a mesh only remote sessions are in",
        [tabOf("gamma")._groupRows.map((r) => r.shadow
          ? `${r.shadow.session}@${r.shadow.machine}` : r.session.name),
         tabCount("gamma")], [["r4@m3"], "1"]);
  check("a shadow in two meshes is drawn once, under the first",
        tabShadows().filter((k) => k === "r3@m3").length, 1);
  check("there is no second rail", walk(list).some((n) => n.classes.has("shadow-group")), false);

  /* ---- the kinds stay apart ------------------------------------------ */
  const s1Remote = shadowCard("s1@m2");
  check("a shadow card carries no name key", s1Remote.dataset.name, undefined);
  check("and no control",
        walk(s1Remote).filter((n) => ["button", "input", "textarea", "select", "a"]
          .includes(n.tag)).length, 0);
  check("the name-keyed rows are this daemon's alone",
        list.querySelectorAll("li[data-name]").map((n) => n.dataset.name), ["s1"]);
  check("the local s1 and the remote s1 are two items",
        [list.querySelectorAll("li[data-name]").filter((n) => n.dataset.name === "s1").length,
         !!s1Remote], [1, true]);
  check("sessionsCache never takes a shadow", ctx.cache(), ["s1", "s2", "s3"]);
  check("the ⊕ sits on the open mesh's header row, on no tab",
        [tabs().map((t) => t.querySelectorAll(".session-group-new").length),
         groupHeader.dataset.value, groupHeader.querySelectorAll(".session-group-new").length],
        [[0, 0, 0, 0, 0], "alpha", 1]);

  /* ---- ownership -------------------------------------------------------- */
  pick("default");
  await ctx.refresh({ cached: true });
  check("a project shows only the shadows it owns through a mesh",
        tabShadows().sort(), ["r3@m3", "r4@m3", "r5@m2", "s1@m2"]);
  check("and no tab for another project's mesh",
        tabValues(), ["(no mesh)", "alpha", "bravo", "gamma"]);
  pick("other");
  await ctx.refresh({ cached: true });
  check("the other project owns the rest", tabShadows().sort(), ["r5@m2", "r9@m2"]);
  await open("zeta");
  check("a shadow in both projects files under the picked project's mesh",
        items(list).map(who), ["r5@m2", "r9@m2"]);
  check("with no tab for a mesh the project does not own", tabOf("alpha"), undefined);
  pick("");

  /* ---- ungrouped, and by workspace ------------------------------------ */
  ctx.set("groups", []);
  await ctx.refresh({ cached: true });
  check("ungrouped, there is no tab strip",
        [tabBar.classes.has("hidden"), tabs().length], [true, 0]);
  check("ungrouped, local rows stay loose at the top",
        list.kids.filter((n) => n.dataset.name).map((n) => n.dataset.name),
        ["s1", "s2", "s3"]);
  check("and the shadows trail them, one group per daemon",
        groups().map((g) => [g.dataset.group, g.dataset.value,
                             g.querySelector(".session-group-name").text]),
        [["remote", "m2", "remote · m2"], ["remote", "m3", "remote · m3"]]);
  check("each holding that daemon's sessions",
        items(bodyOf(groupOf("m2"))).map(who), ["s1@m2", "r5@m2", "r9@m2"]);
  check("with no ⊕: a remote group is not a mesh to create in",
        groupOf("m2").querySelectorAll(".session-group-new").length, 0);

  ctx.set("groups", ["workspace"]);
  await ctx.refresh({ cached: true });
  check("by workspace, a shadow files under its machine",
        tabValues(), ["@m2", "@m3", "C:/w"]);

  /* ---- filters and search --------------------------------------------- */
  // Ungrouped, so every item is in the list at once.
  ctx.set("groups", []);
  await ctx.refresh({ cached: true });
  ctx.set("filter", "running");
  ctx.filters();
  check("running: the exited shadow is hidden with the exited local row",
        shown().sort(), ["r4@m3", "r5@m2", "r9@m2", "s1", "s1@m2", "s3"]);
  ctx.set("filter", "killed");
  ctx.filters();
  check("killed: only what has ended", shown().sort(), ["r3@m3", "s2"]);
  ctx.set("filter", "archived");
  ctx.filters();
  check("archived: a shadow is never an archived record", shown(), []);
  ctx.set("filter", ["killed", "archived"]);
  ctx.filters();
  check("multi-select includes exited shadows with killed local records", shown().sort(), ["r3@m3", "s2"]);
  ctx.set("filter", ["running", "paused"]);
  ctx.filters();
  check("multi-select includes running shadows", shown().sort(), ["r4@m3", "r5@m2", "r9@m2", "s1", "s1@m2", "s3"]);
  ctx.set("filter", []);
  ctx.filters();
  check("empty selection hides local and remote rows", shown(), []);
  ctx.set("filter", "current");
  ctx.set("search", { q: "twin" });
  ctx.filters();
  check("the search reads a shadow's own card", shown(), ["s1@m2"]);
  ctx.set("search", { q: "m3" });
  ctx.filters();
  check("...and its machine", shown().sort(), ["r3@m3", "r4@m3"]);
  ctx.set("search", { q: "anything", hits: new Map([["s1", 0.9]]) });
  ctx.filters();
  check("a hit by meaning is the local s1's, never the remote one's", shown(), ["s1"]);
  ctx.set("groups", ["mesh"]);
  await ctx.refresh({ cached: true });
  check("semantic hits count only the local session even with a remote namesake",
        [tabCount("alpha"), tabCount("bravo"), tabCount("gamma")], ["1", "0", "0"]);
  ctx.set("search", { q: "r4" });
  ctx.filters();
  check("search updates counts in closed tabs without rebuilding",
        [tabCount("alpha"), tabCount("gamma")], ["0", "1"]);
  check("a tab whose group holds a matching shadow is marked",
        tabs().filter((t) => t.classes.has("search-match")).map((t) => t.dataset.value),
        ["gamma"]);
  ctx.set("search", { q: "" });
  ctx.filters();
  check("and unmarked when the search clears",
        tabs().filter((t) => t.classes.has("search-match")).length, 0);

  /* ---- the tab press and the open card -------------------------------- */
  await open("alpha");
  ctx.set("filter", "running");
  ctx.filters();
  check("filter-only refresh keeps tab and header counts aligned with visible rows",
        [tabCount("alpha"), groupHeader.querySelector(".session-group-count").text,
         shown().length, tabCount("bravo")], ["3", "3 sessions", 3, "0"]);
  ctx.set("filter", "current");
  ctx.filters();
  list.scrollTop = 120;
  await open("alpha");
  check("pressing the open tab takes the list back to its top", list.scrollTop, 0);
  list.scrollTop = 120;
  await open("gamma");
  check("pressing another tab opens it, from the top",
        [openTab(), items(list).map(who), list.scrollTop], [["gamma"], ["r4@m3"], 0]);
  await open("alpha");

  ctx.set("page", "shadow");
  ctx.set("open", { machine: "m2", name: "s1" });
  ctx.active();
  check("the open shadow's card is the active one, not the local s1",
        walk(list).filter((n) => n.classes.has("active")).map(who), ["s1@m2"]);
  // A poll that rebuilds the rail keeps that mark.
  ctx.set("shadows", SHADOWS.slice(0, 4));
  await ctx.refresh({ cached: true });
  check("a rebuild keeps it", walk(list).filter((n) => n.classes.has("active")).map(who),
        ["s1@m2"]);

  /* ---- lineage among the shadows (claunch-7u8nm) ---------------------- */
  ctx.set("page", "home");
  ctx.set("open", null);
  ctx.set("filter", "current");
  ctx.set("search", { q: "" });
  // Arrival order puts children before their parents; the rail must not.
  const LINEAGE = [
    { machine: "m4", session: "k2", meshes: [via("alpha", "default", "k2")],
      card: card("k2", { parent: "k1" }), error: null },
    { machine: "m4", session: "k0", meshes: [via("alpha", "default", "k0")],
      card: card("k0"), error: null },
    // No `parent` at all: an older host's card.
    { machine: "m4", session: "k3", meshes: [via("alpha", "default", "k3")],
      card: card("k3"), error: null },
    { machine: "m4", session: "k1", meshes: [via("alpha", "default", "k1")],
      card: card("k1", { parent: "k0", status: "exited", exited: true }), error: null },
    // Same parent name, other machine: m5's k0 is not m4's k0.
    { machine: "m5", session: "q1", meshes: [via("alpha", "default", "q1")],
      card: card("q1", { parent: "k0" }), error: null },
    // Its parent is filed under another heading (bravo).
    { machine: "m4", session: "k4", meshes: [via("gamma", "default", "k4")],
      card: card("k4", { parent: "k0" }), error: null },
    // The host did not answer: no card, so no parent.
    { machine: "m4", session: "k5", meshes: [via("alpha", "default", "k5")],
      card: null, error: "daemon did not answer" },
    // A card naming itself.
    { machine: "m4", session: "k6", meshes: [via("alpha", "default", "k6")],
      card: card("k6", { parent: "k6" }), error: null },
  ];
  ctx.set("groups", ["mesh"]);
  ctx.set("shadows", LINEAGE);
  await ctx.refresh({ cached: true });
  await open("alpha");
  const alphaCards = () => items(list).filter((n) => !n.dataset.name);
  const depthOf = (n) => (n.classes.has("shadow-child")
    ? walk(n).filter((k) => k.classes.has("shadow-tick")).length && n.style.paddingLeft
    : 0);
  check("a spawned shadow follows its parent, a grandchild follows the child",
        alphaCards().map((n) => [who(n), depthOf(n)]),
        [["k0@m4", 0], ["k1@m4", "24px"], ["k2@m4", "34px"], ["k3@m4", 0],
         ["q1@m5", 0], ["k5@m4", 0], ["k6@m4", 0]]);
  check("the child card names its parent in its title",
        shadowCard("k2@m4").title.endsWith(" · spawned by k1"), true);
  check("a root card's title names no parent",
        shadowCard("k0@m4").title.includes("spawned by"), false);
  check("the local rows keep their place ahead of the shadows",
        items(list).filter((n) => n.dataset.name).map(who), ["s1"]);
  await open("gamma");
  check("a parent under another heading leaves the child a root there",
        items(list).map((n) => [who(n), depthOf(n)]), [["k4@m4", 0]]);
  await open("alpha");

  ctx.set("groups", []);
  await ctx.refresh({ cached: true });
  check("ungrouped, the machine group orders by lineage too",
        items(bodyOf(groupOf("m4"))).map((n) => [who(n), depthOf(n)]),
        [["k0@m4", 0], ["k1@m4", "24px"], ["k2@m4", "34px"], ["k4@m4", "24px"],
         ["k3@m4", 0], ["k5@m4", 0], ["k6@m4", 0]]);

  // The filter hides the exited k1: its child is not drawn under a card the
  // reader cannot see, and becomes a root where it arrived (first). A
  // changed card makes this poll a rebuild.
  ctx.set("groups", ["mesh"]);
  ctx.set("filter", "running");
  ctx.set("shadows", LINEAGE.map((r) => (r.session === "k3"
    ? { ...r, card: card("k3", { note: "rebuild" }) } : r)));
  await ctx.refresh({ cached: true });
  check("a parent hidden by the filter leaves its child a root",
        alphaCards().filter((n) => !n.classes.has("session-filtered"))
          .map((n) => [who(n), depthOf(n)]),
        [["k2@m4", 0], ["k0@m4", 0], ["k3@m4", 0], ["q1@m5", 0], ["k5@m4", 0],
         ["k6@m4", 0]]);
  ctx.set("filter", "current");

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railshadowmix_check: ok");
})().catch((err) => { console.error(err); process.exit(1); });
