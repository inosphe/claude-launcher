/* Projects on the page: the tier above meshes and sessions, run against the
   real functions from app.js on a stub DOM.

   The daemon does the filing (a session's and a mesh's `project` field, the
   `?project=` filter on both lists); what the page owns is four things, and
   each is pinned here because each fails silently:

   - One spelling of the filter. The session poll and the mesh poll must ask
     for the SAME project, or the rail shows one project's sessions beside
     another project's meshes and nothing says so. Both go through
     projectQuery, and the query is empty when the selector says "all".
   - The rail's selector stands above the search, and the create form's
     Project row stands above the Directory row it answers — in the markup,
     where a reorder done by moving HTML would drop it.
   - A pick fills the directory, and only a directory nobody chose. The
     create form's Directory row is filled from the picked project's default
     workspace when it still reads the daemon's own directory or the previous
     project's default; a directory the operator picked by hand is never
     overwritten, because that is exactly the "created in the wrong tree"
     mistake the row exists to prevent.
   - A remembered project that no longer exists falls back to "all". The
     choice lives in localStorage; a project removed from another window
     must not leave the rail asking for a name the daemon will answer with
     an empty list. */
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

/* ---- the polls agree on one filter -------------------------------------- */
/* Spelled inline in both polls, and identically: a helper would be undefined
   in the rail harnesses' sandboxes (they slice refreshSessions alone) and
   the poll would throw and draw nothing — which is how this was first
   found. The exact text is pinned so the two polls cannot drift apart. */
const FILTER = '${typeof currentProject === "string" && currentProject ? ' +
  '"&project=" + encodeURIComponent(currentProject) : ""}';
check("the session poll carries the filter",
      slice("refreshSessions").includes(
        "`/api/sessions?view=rail&state=${encodeURIComponent(state)}" + FILTER + "`"),
      true);
check("...and so does the mesh poll, spelled the same way",
      slice("refreshMeshList").includes("`/api/mesh?view=rail" + FILTER + "`"), true);
check("neither poll leans on a helper the rail harnesses do not define",
      [/projectQuery\(/.test(slice("refreshSessions")),
       /projectQuery\(/.test(slice("refreshMeshList"))], [false, false]);
check("a mesh created from the page is filed where the rail is looking",
      /currentProject \? \{ name, project: currentProject \} : \{ name \}/.test(src), true);
check("the create form sends the project on both shapes",
      /if \(f\.project && f\.project\.value\) body\.project = f\.project\.value;/.test(src),
      true);

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
const memory = {};
const localStorage = {
  getItem: (k) => (k in memory ? memory[k] : null),
  setItem: (k, v) => { memory[k] = String(v); },
};
let sessionsPolls = 0, meshPolls = 0;
const refreshSessions = () => { sessionsPolls++; };
const refreshMeshList = () => { meshPolls++; };
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
  spawnParent, syncSpawnMode,
};
const stateBlock = src.slice(
  src.indexOf("const PROJECT_KEY = "),
  src.indexOf("function projectQuery("));
const code = [
  stateBlock,
  slice("projectQuery"), slice("setCurrentProject"),
  slice("applyProjectDefaultCwd"), slice("projectSelectOptions"),
  slice("refreshProjects"),
  "return { projectQuery, setCurrentProject, applyProjectDefaultCwd, " +
  "projectSelectOptions, refreshProjects, current: () => currentProject, " +
  "cache: () => projectsCache };",
].join("\n");
const fns = new Function(...Object.keys(ctx), code)(...Object.values(ctx));

(async () => {
  /* the empty selector asks for nothing */
  check("no project = no filter", fns.projectQuery("&"), "");
  /* a page that never declared the project state asks for everything */
  const bare = new Function("return `" + FILTER + "`;")();
  check("...and so does a page without the state at all", bare, "");

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
  check("the rail offers every project behind 'all'",
        rail.options.map((o) => o.value), ["", "default", "hq", "solo"]);
  check("...with the default workspace on the label",
        rail.options.map((o) => o.text),
        ["All projects", "default", "hq — hq", "solo"]);
  check("the form offers every project and no 'all'",
        form.options.map((o) => o.value), ["default", "hq", "solo"]);

  /* picking on the rail narrows both polls with one spelling */
  fns.setCurrentProject("hq");
  check("the pick is the filter", fns.projectQuery("&"), "&project=hq");
  check("...as the inline spelling reads it",
        new Function("currentProject", "return `" + FILTER + "`;")("hq"), "&project=hq");
  check("...remembered", memory["claunch_project:/t"], "hq");
  check("...and both lists are asked again at once", [sessionsPolls, meshPolls], [1, 1]);
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

  /* a remembered project that vanished falls back to all */
  fns.setCurrentProject("solo");
  served = { projects: served.projects.filter((p) => p.name !== "solo") };
  await fns.refreshProjects();
  check("a removed project is forgotten", [fns.current(), rail.value], ["", ""]);
  check("...in memory too", memory["claunch_project:/t"], "");

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
