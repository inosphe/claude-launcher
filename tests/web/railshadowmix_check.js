/* Other daemons' sessions (remote-shadow rows, daemon/shadow.py) drawn in the
   one session list, among this daemon's rows.

   The rail used to keep them in a section of their own below the list, so a
   rail grouped by mesh showed a mesh's local members under its heading and
   its remote members somewhere else. Now one list draws both kinds of item:
   a local row (li[data-name], keyed by bare name, with its controls) and a
   shadow card (li.shadow-card, keyed by machine and name, with none).

   What has to hold:
   - under a mesh heading, every member the rail can show sits together, and
     the heading counts them all; a mesh only remote members are in still
     gets its heading;
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
     never matches one, and a group jump can land on one. */
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
    set textContent(v) { n.text = String(v); },
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
const $ = (id) => (id === "session-list" ? list : null);
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
const folded = new Set();
function isSessionGroupCollapsed(group, value) { return folded.has(group + "/" + value); }
function setSessionGroupCollapsed(group, value, shut) {
  if (shut) folded.add(group + "/" + value); else folded.delete(group + "/" + value);
}
function syncSessionGroupStickyOffsets() {}
function sessionGroupJumpOffset(l, heading, row) { exports.jumped = row; return 0; }
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
function decorateBriefingRow() {}
function openDetail() {}
function openSpawnModal() {}
function railCardKey() {}
function go() {}
`;

const names = [
  "byLineage", "sessionCategory", "sessionMatchesFilter", "sessionFilterCounts",
  "syncSessionGroupSearch", "syncSessionFilters",
  "sessionSearchText", "sessionMatchesSearch",
  "recordProject", "sessionInCurrentProject", "meshInCurrentProject", "railSessions",
  "sessMeshes", "sessionMeshGroup", "sessionWorkspaceGroup", "sessionWorkspaceLabel",
  "sessionGroupValue", "shadowMeshGroup", "shadowGroupValue",
  "regroupDepths", "sessionGroupRows", "remoteRailRows",
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
      case "fold": folded.add(value); break;
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

(async () => {
  pick("");
  ctx.set("shadows", SHADOWS);
  await ctx.refresh();

  /* ---- one list, mesh groups hold both kinds ------------------------- */
  check("every mesh with a member on the rail gets a heading, remote-only ones too",
        groups().map((g) => g.dataset.value),
        ["(no mesh)", "alpha", "bravo", "gamma", "zeta"]);
  check("a mesh group holds its local rows, then its remote cards",
        items(bodyOf(groupOf("alpha"))).map(who), ["s1", "s1@m2", "r3@m3", "r5@m2"]);
  check("and its heading counts all of them", countOf(groupOf("alpha")), "4");
  check("a mesh only remote sessions are in",
        [items(bodyOf(groupOf("gamma"))).map(who), countOf(groupOf("gamma"))],
        [["r4@m3"], "1"]);
  check("a shadow in two meshes is drawn once, under the first",
        items(list).filter((n) => who(n) === "r3@m3").length, 1);
  check("there is no second rail", walk(list).some((n) => n.classes.has("shadow-group")), false);

  /* ---- the kinds stay apart ------------------------------------------ */
  const s1Remote = shadowCard("s1@m2");
  check("a shadow card carries no name key", s1Remote.dataset.name, undefined);
  check("and no control",
        walk(s1Remote).filter((n) => ["button", "input", "textarea", "select", "a"]
          .includes(n.tag)).length, 0);
  check("the name-keyed rows are this daemon's alone",
        list.querySelectorAll("li[data-name]").map((n) => n.dataset.name).sort(),
        ["s1", "s2", "s3"]);
  check("the local s1 and the remote s1 are two items",
        [list.querySelectorAll("li[data-name]").filter((n) => n.dataset.name === "s1").length,
         !!s1Remote], [1, true]);
  check("sessionsCache never takes a shadow", ctx.cache(), ["s1", "s2", "s3"]);
  check("the ⊕ on a mesh heading stays one per heading",
        groupOf("alpha").querySelectorAll(".session-group-new").length, 1);

  /* ---- ownership -------------------------------------------------------- */
  pick("default");
  await ctx.refresh({ cached: true });
  check("a project shows only the shadows it owns through a mesh",
        items(list).filter((n) => !n.dataset.name).map(who).sort(),
        ["r3@m3", "r4@m3", "r5@m2", "s1@m2"]);
  check("and no heading for another project's mesh",
        groups().map((g) => g.dataset.value), ["(no mesh)", "alpha", "bravo", "gamma"]);
  pick("other");
  await ctx.refresh({ cached: true });
  check("the other project owns the rest",
        items(list).filter((n) => !n.dataset.name).map(who).sort(), ["r5@m2", "r9@m2"]);
  check("a shadow in both projects files under the picked project's mesh",
        items(bodyOf(groupOf("zeta"))).map(who), ["r5@m2", "r9@m2"]);
  check("with no heading for a mesh the project does not own",
        groupOf("alpha"), undefined);
  pick("");

  /* ---- ungrouped, and by workspace ------------------------------------ */
  ctx.set("groups", []);
  await ctx.refresh({ cached: true });
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
        groups().map((g) => g.dataset.value), ["@m2", "@m3", "C:/w"]);

  ctx.set("groups", ["mesh", "workspace"]);
  await ctx.refresh({ cached: true });
  const alphaInner = bodyOf(groupOf("alpha")).kids.map((g) => g.dataset.value);
  check("nested under a mesh, the machine is the shadow's workspace",
        alphaInner, ["@m2", "@m3", "C:/w"]);

  /* ---- filters and search --------------------------------------------- */
  ctx.set("groups", ["mesh"]);
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
  ctx.set("fold", "mesh/gamma");
  ctx.set("search", { q: "r4" });
  ctx.filters();
  check("a folded group opens for a shadow that matches",
        groupOf("gamma").classes.has("collapsed"), false);
  ctx.set("search", { q: "" });
  ctx.filters();
  check("and shuts again when the search clears",
        groupOf("gamma").classes.has("collapsed"), true);

  /* ---- the jump and the open card ------------------------------------- */
  const jump = groupOf("gamma").querySelector(".session-group-jump");
  jump.handlers.click({ stopPropagation() {} });
  check("the jump of a remote-only group lands on its first card",
        ctx.jumped && who(ctx.jumped), "r4@m3");
  check("and unfolds it on the way", groupOf("gamma").classes.has("collapsed"), false);

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

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railshadowmix_check: ok");
})().catch((err) => { console.error(err); process.exit(1); });
