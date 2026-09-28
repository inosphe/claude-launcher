/* Projects on the page: the tier above meshes and sessions, run against the
   real functions from app.js on a stub DOM.

   The daemon does the filing (a session's and a mesh's `project` field); what
   the page owns is the following, and each is pinned here because each fails
   silently:

   - The polls carry no filter; the page narrows. Both polls fetch every
     project and the rail draws the picked one out of the cache
     (railSessions, meshInCurrentProject), so a pick redraws at once with no
     request, and a tab of another project's session keeps a real record:
     its tab shows the session's state and a chip naming its project instead
     of the "unknown" dot a filtered poll left it with.
   - The pick is in the address (`?project=`), so a reload keeps it. The
     first build of the rail's <select> has no value to keep, and comparing
     that "" against the remembered pick used to drop it on every reload.
   - Opening a session while the rail is narrowed to another project moves
     the rail to the session's project; "All projects" is left alone.
   - The rail's selector stands above the search, and the create form's
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
     a name nothing is filed under. */
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
const railPick = html.indexOf('id="project-select"');
const railSearch = html.indexOf('id="session-search"');
check("the rail's project selector exists", railPick >= 0, true);
check("...and stands above the search it narrows",
      railPick >= 0 && railSearch >= 0 && railPick < railSearch, true);
const formStart = html.indexOf('<form id="new-session"');
const formProject = html.indexOf('<select name="project">', formStart);
const formCwd = html.indexOf('<select name="cwd">', formStart);
check("the create form asks the project", formProject >= 0, true);
check("...before the directory it answers", formProject < formCwd, true);
check("the rail selector has its own rule", /#project-select \{/.test(css), true);
check("...and the row it sits in", /\.project-row \{/.test(css), true);
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
form.form = { cwd: select("cwd") };
form.closest = () => form.form;
const rail = select("project-select");
const byId = { "project-select": rail };
const $ = (id) => byId[id] || null;
const document = {
  querySelector: (q) => (q === "#new-session select[name=project]" ? form : null),
};
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
  renderHome, renderWorkspaces, wsOpen, currentPage, BASE,
  spawnParent, syncSpawnMode, location, history,
};
const stateBlock = src.slice(
  src.indexOf("const PROJECT_KEY = "),
  src.indexOf("function recordProject("));
const code = [
  "let sessionsCache = [];",
  stateBlock,
  slice("recordProject"), slice("sessionInCurrentProject"),
  slice("meshInCurrentProject"), slice("railSessions"),
  slice("hashWithProject"), slice("syncProjectHash"),
  slice("setCurrentProject"), slice("syncRouteProject"),
  "let projectFollowPending = '';", slice("followSessionProject"),
  slice("sessionTabOtherProject"), slice("hashQuery"),
  slice("applyProjectDefaultCwd"), slice("projectSelectOptions"),
  slice("refreshProjects"),
  "return { setCurrentProject, applyProjectDefaultCwd, railSessions, " +
  "meshInCurrentProject, hashWithProject, syncRouteProject, followSessionProject, " +
  "sessionTabOtherProject, setSessions: (l) => { sessionsCache = l; }, " +
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
  check("a reload keeps the picked project on the freshly built selector",
        [fns.current(), rail.value, hash], ["hq", "hq", "#/s/x?project=hq"]);
  check("the rail offers every project behind 'all'",
        rail.options.map((o) => o.value), ["", "default", "hq", "solo"]);
  check("...with the default workspace on the label",
        rail.options.map((o) => o.text),
        ["All projects", "default", "hq — hq", "solo"]);
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
  check("a mesh is narrowed by the same rule",
        [fns.meshInCurrentProject({ project: "default" }),
         fns.meshInCurrentProject({ project: "hq" })], [true, false]);

  /* a tab of another project keeps its record and names the project */
  check("a tab of another project names it",
        fns.sessionTabOtherProject({ name: "d", project: "solo", status: "busy" }), "solo");
  check("...and one of the rail's project names nothing",
        fns.sessionTabOtherProject({ name: "b", status: "idle" }), "");
  check("...nor does a tab whose record is unknown", fns.sessionTabOtherProject(undefined), "");

  /* opening a session of another project moves the rail there */
  fns.followSessionProject("d");
  check("opening a session moves the rail to its project",
        [fns.current(), rail.value, hash], ["solo", "solo", "#/s/x?project=solo"]);
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
  check("a removed project is forgotten", [fns.current(), rail.value], ["", ""]);
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
