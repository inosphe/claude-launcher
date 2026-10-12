/* Projects on the page: the tier above meshes and sessions, run against the
   real functions from app.js on a stub DOM.

   The daemon does the filing (a session's and a mesh's `project` field); what
   the page owns is the following, and each is pinned here because each fails
   silently:

   - The mesh list is not narrowed at all (claunch-6deos): a mesh may hold
     members of several projects, so the rail lists every mesh whatever
     project is picked.
   - The polls carry no filter; the page narrows. Both polls fetch every
     project and the rail draws the picked one out of the cache
     (railSessions), so a pick redraws at once with no
     request, and a tab of another project's session keeps a real record:
     its tab shows the session's state and a chip naming its project instead
     of the "unknown" dot a filtered poll left it with.
   - The pick is in the address (`?project=`), so a reload keeps it. The
     first build of the rail's picker has no value to keep, and comparing
     that "" against the remembered pick used to drop it on every reload.
   - The rail's picker is a strip of tabs (claunch-p6xb3): "All projects"
     and one per project, the open one marked; under it the picked
     project's header shares its line with the search.
   - Opening a session while the rail is narrowed to another project moves
     the rail to the session's project; "All projects" is left alone.
   - The rail's tabs stand above the search, and the create form's
     Project row stands above the Directory row it answers — in the markup,
     where a reorder done by moving HTML would drop it.
   - A pick fills the directory, and only a directory nobody chose. The
     create form's Directory row is filled from the picked project's default
     workspace when it still reads the daemon's own directory or the previous
     project's default; a directory the operator picked by hand is never
     overwritten, because that is exactly the "created in the wrong tree"
     mistake the row exists to prevent.
   - A remembered project that no longer exists falls back to "all". A
     project removed from another window must not leave the rail narrowed to
     a name nothing is filed under.
   - A pick fills the Mesh row by the directory's rule, from the project's
     default mesh. That setting has three states (projects.py): the field
     absent (undefined: never set), null (set to no mesh) and a name. The
     Projects card writes each one with its own body, and a project that
     never had one is sent nothing at creation, so it stays absent. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* ---- the markup --------------------------------------------------------- */
const railPick = html.indexOf('id="project-tabs"');
const railSearch = html.indexOf('id="session-search"');
check("the rail's project tabs exist", railPick >= 0, true);
check("...and stand above the search they narrow",
      railPick >= 0 && railSearch >= 0 && railPick < railSearch, true);
check("the select they replaced is gone", html.includes('id="project-select"'), false);
const formStart = html.indexOf('<form id="new-session"');
const formProject = html.indexOf('<select name="project">', formStart);
const formCwd = html.indexOf('<select name="cwd">', formStart);
check("the create form asks the project", formProject >= 0, true);
check("...before the directory it answers", formProject < formCwd, true);
check("the tabs have their own rule, open and not",
      [/\.project-tabs \{/.test(css), /\.project-tab\.active \{/.test(css)], [true, true]);
check("a tab of another project has its own look",
      [/\.session-tab\.other-project \{/.test(css), /\.session-tab-project \{/.test(css)],
      [true, true]);

/* ---- the polls carry every project -------------------------------------- */
check("the session poll asks for every project",
      slice("refreshSessions").includes(
        "`/api/sessions?view=rail&state=${encodeURIComponent(state)}`"), true);
check("...and so does the mesh poll",
      slice("refreshMeshList").includes('api("/api/mesh?view=rail")'), true);
check("neither poll spells a project filter any more",
      [/project=/.test(slice("refreshSessions")),
       /project=/.test(slice("refreshMeshList"))], [false, false]);
check("the session poll can redraw from the cache alone",
      /const cached = !!\(options && options\.cached\);\s*if \(!cached\) \{/.test(
        slice("refreshSessions")), true);
check("...and the rail draws the narrowed list",
      /byLineage\(railList, visibleSessions\)/.test(slice("refreshSessions")), true);
check("the mesh poll keeps its last answer for the same redraw",
      /options && options\.cached \? meshPollData : null/.test(slice("refreshMeshList")),
      true);
check("the router hands the address's project over first",
      /syncRouteProject\(\);/.test(slice("route")), true);
check("...and a session opened from anywhere follows its project",
      /followSessionProject\(r\.name\)/.test(slice("route")), true);
check("a mesh created from the page is filed where the rail is looking",
      /currentProject \? \{ name, project: currentProject \} : \{ name \}/.test(src), true);
check("the create form sends the project on both shapes",
      /if \(f\.project && f\.project\.value\) body\.project = f\.project\.value;/.test(src),
      true);

/* That assignment is only half the journey. sessionFormPayload filters the
   body against the mode's allowlist on the line before the POST, so a name
   missing from `payload` is dropped between the two: the row still shows the
   pick, the Directory row still takes the project's default workspace, and
   the session is filed under `default` with no error anywhere
   (claunch-jvxvq). The real pair is run here because the drop is silent in
   exactly the way a text match over the source cannot see. */
const formCfg = new Function(
  slice("sessionFormConfig") + slice("sessionFormPayload") +
  "return { sessionFormConfig, sessionFormPayload };")();
for (const mode of ["new", "spawn"]) {
  check(`...and the ${mode} payload still carries it to the daemon`,
        formCfg.sessionFormPayload(mode, { name: "s1", project: "hq" }),
        { name: "s1", project: "hq" });
  check(`...over a row the ${mode} form lets the operator answer`,
        formCfg.sessionFormConfig(mode).editable.includes("project"), true);
}

/* ---- the functions, against a stub DOM ---------------------------------- */
function Option(text, value) {
  return { text, value, selected: false };
}
function select(id) {
  const s = {
    id, options: [], _value: "", listeners: {},
    appendChild(o) { s.options.push(o); return o; },
    set innerHTML(v) { s.options = []; },
    get innerHTML() { return ""; },
    get value() { return s._value; },
    set value(v) {
      // a real <select> drops an assignment it has no option for
      s._value = s.options.some((o) => o.value === v) ? v : (s.options[0] ? s.options[0].value : "");
    },
    addEventListener(ev, fn) { s.listeners[ev] = fn; },
  };
  return s;
}
const form = select("form-project");
let meshChanges = 0;
const meshRow = select("mesh");
meshRow.dispatchEvent = (ev) => { if (ev.type === "change") meshChanges++; };
form.form = { cwd: select("cwd"), mesh: meshRow };
form.closest = () => form.form;
/* The tab strip and the header renderProjectTabs fills. */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, attrs: {}, handlers: {},
    title: "", tabIndex: undefined, scrolled: 0,
    appendChild(c) { n.kids.push(c); return c; },
    setAttribute(k, v) { n.attrs[k] = v; },
    addEventListener(ev, fn) { n.handlers[ev] = fn; },
    scrollIntoView() { n.scrolled++; },
    querySelector(sel) { return n.parts[sel] || null; },
    parts: {},
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); n.kids = []; },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  };
  return n;
}
const tabBar = node("div");
const header = node("div");
header.parts[".project-header-name"] = node("span");
header.parts[".project-header-detail"] = node("span");
const tabs = () => tabBar.kids;
const openTab = () => tabs().filter((t) => t.classes.has("active")).map((t) => t.dataset.project);
const byId = { "project-tabs": tabBar, "project-header": header };
const $ = (id) => byId[id] || null;
const document = {
  querySelector: (q) => (q === "#new-session select[name=project]" ? form : null),
  createElement: node,
};
let scrolledTop = 0;
const scrollSessionListTop = () => { scrolledTop++; };
let served = { projects: [] };
let apiCalls = [];
const api = async (url) => {
  apiCalls.push(url);
  return { ok: true, json: async () => served };
};
/* A reload on a terminal whose address names a project, in a browser that
   remembers a different one: the address is what the page opens on. */
let hash = "#/s/x?project=hq";
const location = { get hash() { return hash; } };
const history = {
  state: { routed: true },
  replaceState(st, _title, url) { history.state = st; if (url !== undefined) hash = url; },
};
const memory = { "claunch_project:/t": "solo" };
const localStorage = {
  getItem: (k) => (k in memory ? memory[k] : null),
  setItem: (k, v) => { memory[k] = String(v); },
};
let sessionsPolls = 0, meshPolls = 0, cachedRedraws = 0;
const refreshSessions = (o) => { if (o && o.cached) cachedRedraws++; else sessionsPolls++; };
const refreshMeshList = (o) => { if (o && o.cached) cachedRedraws++; else meshPolls++; };
const renderHome = () => {};
const renderWorkspaces = () => {};
let wsOpen = false;
let currentPage = "rail";
const BASE = "/t";
/* Rebuilding the form's picker drops the inherit entry a spawn puts at the
   top of it, so refreshProjects asks the spawn arm back. Here the form is
   never a spawn — what that arm does with the row is spawnform_check's, and
   these two only have to exist. */
let spawnResyncs = 0;
const spawnParent = () => null;
const syncSpawnMode = () => { spawnResyncs++; };

const ctx = {
  Option, document, $, api, localStorage, refreshSessions, refreshMeshList,
  scrollSessionListTop,
  renderHome, renderWorkspaces, wsOpen, currentPage, BASE,
  spawnParent, syncSpawnMode, location, history,
};
const stateBlock = src.slice(
  src.indexOf("const PROJECT_KEY = "),
  src.indexOf("function recordProject("));
const code = [
  "let sessionsCache = [];",
  "let meshCache = [];",
  stateBlock,
  slice("recordProject"), slice("sessionInCurrentProject"),
  slice("railSessions"), slice("sessMeshes"),
  slice("hashWithProject"), slice("syncProjectHash"),
  slice("setCurrentProject"), slice("syncRouteProject"),
  "let projectFollowPending = '';", slice("followSessionProject"),
  slice("sessionTabOtherProject"), slice("hashQuery"),
  slice("applyProjectDefaultMesh"),
  slice("applyProjectDefaultCwd"),
  // The drag on the strip needs real pointer input: tabdragscroll_browser.cjs.
  "function enableTabDragScroll() {}", slice("renderProjectTabs"),
  slice("projectSelectOptions"), slice("refreshProjects"),
  "return { setCurrentProject, applyProjectDefaultCwd, railSessions, " +
  "hashWithProject, syncRouteProject, followSessionProject, " +
  "sessionTabOtherProject, setSessions: (l) => { sessionsCache = l; }, " +
  "setMeshes: (l) => { meshCache = l; }, " +
  "pending: () => projectFollowPending, " +
  "projectSelectOptions, refreshProjects, current: () => currentProject, " +
  "cache: () => projectsCache };",
].join("\n");
const fns = new Function(...Object.keys(ctx), code)(...Object.values(ctx));

(async () => {
  /* the address outranks the browser's memory */
  check("the pick is read off the address first", fns.current(), "hq");

  /* the registry lands in both pickers */
  served = { projects: [
    { name: "default", default_workspace: null, default_cwd: null, is_default: true },
    { name: "hq", default_workspace: "hq", default_cwd: "D:\\hq", is_default: false },
    { name: "solo", default_workspace: null, default_cwd: null, is_default: false },
  ] };
  form.form.cwd.appendChild(Option("(daemon cwd)", ""));
  form.form.cwd.appendChild(Option("hq — D:\\hq", "D:\\hq"));
  form.form.cwd.appendChild(Option("other", "E:\\other"));
  form.form.cwd.value = "";
  await fns.refreshProjects();
  check("a reload keeps the picked project on the freshly built tabs",
        [fns.current(), openTab(), hash], ["hq", ["hq"], "#/s/x?project=hq"]);
  check("the rail offers every project behind 'all', a tab each",
        tabs().map((t) => t.dataset.project), ["", "default", "hq", "solo"]);
  check("...labelled by name, the default workspace in the tooltip",
        [tabs().map((t) => t.text), tabs()[2].title],
        [["All projects", "default", "hq", "solo"], "hq — hq — click to scroll to the top"]);
  check("...every one a tab to a reader, and saying which is open",
        tabs().map((t) => [t.attrs.role, t.attrs["aria-selected"], t.tabIndex]),
        [["tab", "false", 0], ["tab", "false", 0], ["tab", "true", 0], ["tab", "false", 0]]);
  check("the open tab is kept in view in a strip wider than the rail", tabs()[2].scrolled, 1);
  check("the header names the picked project and its default workspace",
        [header.parts[".project-header-name"].text, header.parts[".project-header-detail"].text],
        ["hq", "hq"]);

  /* the tabs pick */
  cachedRedraws = 0;
  tabs()[3].handlers.click();
  check("pressing another tab picks its project and redraws from the cache",
        [fns.current(), openTab(), hash, cachedRedraws], ["solo", ["solo"], "#/s/x?project=solo", 2]);
  check("...and the header follows, saying a project with no default so",
        [header.parts[".project-header-name"].text, header.parts[".project-header-detail"].text],
        ["solo", "no default workspace"]);
  cachedRedraws = 0;
  tabs()[3].handlers.click();
  check("pressing the open tab takes the list to its top and picks nothing",
        [scrolledTop, cachedRedraws, fns.current()], [1, 0, "solo"]);
  let prevented = 0;
  tabs()[0].handlers.keydown({ key: "Enter", preventDefault() { prevented++; } });
  check("Enter on a tab presses it", [fns.current(), prevented], ["", 1]);
  check("'All projects' heads the line with the count of projects",
        [header.parts[".project-header-name"].text, header.parts[".project-header-detail"].text],
        ["All projects", "3 projects"]);
  check("the form offers every project and no 'all'",
        form.options.map((o) => o.value), ["default", "hq", "solo"]);

  /* picking on the rail redraws both lists from the cache */
  fns.setCurrentProject("");
  check("'all' is the address with no project", hash, "#/s/x");
  cachedRedraws = 0;
  form.value = "default";
  form._lastProject = "default";
  fns.setCurrentProject("hq");
  check("the pick is written into the address", hash, "#/s/x?project=hq");
  check("...remembered", memory["claunch_project:/t"], "hq");
  check("...and both lists redraw from the cache, with no request",
        [cachedRedraws, sessionsPolls, meshPolls], [2, 0, 0]);
  check("the form follows the rail", form.value, "hq");
  check("...and the directory took the project's default", form.form.cwd.value, "D:\\hq");

  /* a hand-picked directory is never overwritten by a project pick */
  form.form.cwd.value = "E:\\other";
  form.value = "solo";
  fns.applyProjectDefaultCwd(form);
  check("a chosen directory survives a project change", form.form.cwd.value, "E:\\other");

  /* ...but the previous project's default is let go */
  form.form.cwd.value = "D:\\hq";
  form._lastProject = "hq";
  form.value = "solo";
  fns.applyProjectDefaultCwd(form);
  check("the previous default is released when the new project has none",
        form.form.cwd.value, "");

  /* the Mesh row follows the project's default mesh, by the same rule */
  served = { projects: [
    { name: "default", default_workspace: null, default_cwd: null, is_default: true },
    { name: "hq", default_workspace: "hq", default_cwd: "D:\\hq", is_default: false,
      default_mesh: "room" },
    { name: "solo", default_workspace: null, default_cwd: null, is_default: false,
      default_mesh: null },
  ] };
  await fns.refreshProjects();
  meshRow.appendChild(Option("(none)", ""));
  meshRow.appendChild(Option("room", "room"));
  meshRow.appendChild(Option("other", "other"));
  meshRow.value = "";
  form.form.cwd.value = "";
  form._lastProject = "default";
  form.value = "hq";
  meshChanges = 0;
  fns.applyProjectDefaultCwd(form);
  check("a pick fills an empty Mesh row with the project's default mesh",
        [meshRow.value, meshChanges], ["room", 1]);
  form.value = "solo";
  fns.applyProjectDefaultCwd(form);
  check("...and a project set to none lets the previous default go",
        [meshRow.value, meshChanges], ["", 2]);
  form.value = "default";
  fns.applyProjectDefaultCwd(form);
  check("an unset project leaves an empty row alone", [meshRow.value, meshChanges], ["", 2]);
  meshRow.value = "other";
  form.value = "hq";
  fns.applyProjectDefaultCwd(form);
  check("a mesh picked by hand survives a project change", meshRow.value, "other");
  form.value = "default";
  fns.applyProjectDefaultCwd(form);

  /* the three states on the Projects card */
  const meshFns = new Function(
    src.slice(src.indexOf("const PROJECT_MESH_UNSET = "),
              src.indexOf("function projectMeshChoice(")) +
    slice("projectMeshChoice") + slice("projectMeshValue") + slice("projectMeshBody") +
    "return { projectMeshChoice, projectMeshValue, projectMeshBody, " +
    "UNSET: PROJECT_MESH_UNSET, NONE: PROJECT_MESH_NONE };")();
  check("a row with no default_mesh key reads as unset",
        meshFns.projectMeshChoice({ name: "p" }), meshFns.UNSET);
  check("...null as none, a name as itself",
        [meshFns.projectMeshChoice({ default_mesh: null }),
         meshFns.projectMeshChoice({ default_mesh: "room" })], [meshFns.NONE, "room"]);
  check("the picker values map back to undefined / null / a name",
        [meshFns.projectMeshValue(meshFns.UNSET) === undefined,
         meshFns.projectMeshValue(meshFns.NONE), meshFns.projectMeshValue("room")],
        [true, null, "room"]);
  check("each state is written with its own body",
        [meshFns.projectMeshBody(undefined), meshFns.projectMeshBody(null),
         meshFns.projectMeshBody("room")],
        [{ default_mesh_unset: true }, { default_mesh: null }, { default_mesh: "room" }]);
  check("a new project left unset sends no default_mesh",
        /if \(mesh !== undefined\) body\.default_mesh = mesh;/.test(slice("projectAdd")),
        true);

  /* the rail narrows the cache it holds; "default" is the unnamed project */
  fns.setSessions([
    { name: "a", project: "hq", status: "busy" },
    { name: "b", status: "idle" },
    { name: "c", project: "default", status: "idle" },
    { name: "d", project: "solo", status: "busy" },
  ]);
  check("the rail draws the picked project only",
        fns.railSessions().map((s) => s.name), ["a"]);
  fns.setCurrentProject("default");
  check("...and a record naming no project is the default one",
        fns.railSessions().map((s) => s.name), ["b", "c"]);
  check("the mesh list is never narrowed to the picked project",
        [src.includes("function meshInCurrentProject("),
         /meshCache = \(data\.meshes \|\| \[\]\)\.filter/.test(src)], [false, false]);

  /* A shared mesh never widens the selected project. */
  fns.setMeshes([
    { name: "room", members: [
      { local: true, session: "b", handle: "b" },
      { local: true, session: "d", handle: "d" },
    ] },
    { name: "elsewhere", members: [{ local: true, session: "a", handle: "a" }] },
  ]);
  check("a shared mesh keeps only the selected project members",
        fns.railSessions().map((s) => s.name), ["b", "c"]);
  fns.setCurrentProject("");
  check("...and 'all projects' draws everything as before",
        fns.railSessions().map((s) => s.name), ["a", "b", "c", "d"]);
  fns.setCurrentProject("solo");
  check("...from either side of the mesh",
        fns.railSessions().map((s) => s.name), ["d"]);
  fns.setMeshes([]);
  check("...and with no mesh the grouped rail is the project's own",
        fns.railSessions().map((s) => s.name), ["d"]);
  fns.setCurrentProject("default");

  /* a tab of another project keeps its record and names the project */
  check("a tab of another project names it",
        fns.sessionTabOtherProject({ name: "d", project: "solo", status: "busy" }), "solo");
  check("...and one of the rail's project names nothing",
        fns.sessionTabOtherProject({ name: "b", status: "idle" }), "");
  check("...nor does a tab whose record is unknown", fns.sessionTabOtherProject(undefined), "");

  /* opening a session of another project moves the rail there */
  fns.followSessionProject("d");
  check("opening a session moves the rail to its project",
        [fns.current(), openTab(), hash], ["solo", ["solo"], "#/s/x?project=solo"]);
  fns.followSessionProject("a");
  check("...every time the project differs", fns.current(), "hq");
  fns.setCurrentProject("");
  fns.followSessionProject("d");
  check("\"All projects\" is left alone", fns.current(), "");
  check("...and shows every tab as its own",
        fns.sessionTabOtherProject({ name: "d", project: "solo" }), "");
  fns.setCurrentProject("hq");
  fns.followSessionProject("zz");
  check("a session not polled yet waits for the poll that brings it",
        [fns.current(), fns.pending()], ["hq", "zz"]);

  /* the router: an address with a project obeys it, one without is given it */
  hash = "#/s/d?project=solo";
  fns.syncRouteProject();
  check("an address naming a project picks it", fns.current(), "solo");
  hash = "#/mesh/m1";
  fns.syncRouteProject();
  check("an address naming none is given the pick back", hash, "#/mesh/m1?project=solo");
  check("...beside the detail rail's own parameter",
        fns.hashWithProject("#/s/a?detail=a", "hq"), "#/s/a?detail=a&project=hq");
  check("...and 'all' takes it out again",
        fns.hashWithProject("#/s/a?detail=a&project=hq", ""), "#/s/a?detail=a");

  /* a remembered project that vanished falls back to all */
  served = { projects: served.projects.filter((p) => p.name !== "solo") };
  await fns.refreshProjects();
  check("a removed project is forgotten", [fns.current(), openTab()], ["", [""]]);
  check("...and its tab is gone", tabs().map((t) => t.dataset.project), ["", "default", "hq"]);
  check("...in memory too", memory["claunch_project:/t"], "");
  check("...and in the address", hash, "#/mesh/m1");

  /* an older daemon without the route leaves the page as it was */
  apiCalls = [];
  const before = fns.cache().length;
  const failing = async () => ({ ok: false, status: 404, json: async () => ({}) });
  const older = new Function(...Object.keys(ctx), code)(
    ...Object.values({ ...ctx, api: failing }));
  await older.refreshProjects();
  check("a 404 from an older daemon changes nothing", older.cache().length, 0);
  check("...and the newer page kept its list", fns.cache().length, before);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("projects_check: all checks passed");
})();
