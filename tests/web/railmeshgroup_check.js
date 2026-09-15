/* A session that joined more than one mesh, on a rail grouped by mesh.

   The grouping files each session under exactly one heading —
   `sessionMeshGroup` takes the alphabetically first membership — so the other
   rooms it is in have no heading of their own to appear under. Before this
   harness they were also being crowded off the row: the pills are capped at
   two, and one of those two was spent repeating the mesh named in the heading
   directly above the row, which pushed the rooms the heading could not show
   into the `+N` count.

   What has to hold: the row knows which heading it landed under
   (`sessionGroupRows` carries it), the pills drop that mesh and spend both
   slots on the others, a session whose only room IS the heading draws no
   pills at all, the role beside the name is the one held in THAT mesh, and an
   ungrouped rail still shows everything it did before. */
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
const capLine = src.match(/^const RAIL_MESH_TAGS = .+$/m);
if (!capLine) throw new Error("cannot locate RAIL_MESH_TAGS in app.js");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "", parent: null,
    appendChild(c) { c.parent = this; this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener() {},
    setAttribute() {},
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, "");
      return walk(this).filter((k) => k.classes.has(cls));
    },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get innerHTML() { return ""; },
    set innerHTML(v) { this.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    toggle: () => {},
    contains: (c) => n.classes.has(c),
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const document = { createElement: (tag) => node(tag) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

const list = node("ul");
let served = { sessions: [] };
const api = async () => ({ ok: true, json: async () => served });

/* Everything refreshSessions leans on that this harness is not about. Taken
   from raillayout_check, which owns the row's layout; here the rows only have
   to be built so their rooms and their role can be read. */
const stubs = `
let sessionFilter = "all";
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, snapshotName = null;
let keptTerms = new Map();
let railRedrawPending = false;
// The fold is remembered in localStorage by the page; nothing here folds, so
// every group is open and no group state is written.
function isSessionGroupCollapsed() { return false; }
function setSessionGroupCollapsed() {}
function dropKept() {}
function railHeld() { return false; }
function forgetDeadSessions() {}
function refreshResumeChoices() {}
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
function $(id) { return list; }
function refreshParentChoices() {}
function ctxNoteOnRow() {}
function ctxRailLine() { return el("span", "rail-ctx-line unknown"); }
function railCwdLine() { return el("span", "rail-cwd"); }
function railSeenLine() { return el("span", "rail-seen"); }
function decorateBriefingRow() {}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache", "sessionGroupOrder",
  stubs + capLine[0] + "\n"
  + slice("sessionCategory") + slice("sessionMatchesFilter")
  + slice("byLineage") + slice("sessionMeshGroup")
  + slice("sessionWorkspaceGroup") + slice("sessionWorkspaceLabel")
  + slice("sessionGroupValue") + slice("sessionGroupRows")
  + slice("sessMeshes") + slice("railMeshTags")
  + slice("sessHandles") + slice("handleTag")
  + slice("profileHarnessLabel") + slice("railMetaText")
  + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  rows: sessionGroupRows,
  tags: railMeshTags,
  setMeshes: (ms) => { meshCache = ms; },
});`)(ctx, document, el, api, list, [], ["mesh"]);

/* ---- the fleet --------------------------------------------------------- */
const member = (session, role) => ({
  session, handle: session, role, roles: [role], local: true, machine: "",
});
served = { sessions: [
  // Three rooms: filed under `alpha`, the alphabetically first of them.
  { name: "wide", status: "idle", profile: "nc", parent: null, cwd: "C:/w" },
  // Two rooms, and a different role in each.
  { name: "twin", status: "idle", profile: "nc", parent: null, cwd: "C:/w" },
  // One room, which is the heading it sits under.
  { name: "solo", status: "idle", profile: "nc", parent: null, cwd: "C:/w" },
  // No room at all.
  { name: "loner", status: "idle", profile: "nc", parent: null, cwd: "C:/w" },
] };
ctx.setMeshes([
  { name: "alpha", members: [member("wide", "worker"), member("twin", "leader")] },
  { name: "bravo", members: [member("wide", "worker"), member("twin", "reviewer"),
                             member("solo", "worker")] },
  { name: "delta", members: [member("wide", "reviewer")] },
]);

(async () => {
  await ctx.refresh();

  const rowOf = (name) => walk(list).find(
    (n) => n.tag === "li" && n.dataset.name === name);
  const pills = (name) => rowOf(name).querySelectorAll(".rail-mesh")
    .map((t) => t.text);
  const roleOf = (name) => rowOf(name).querySelector(".mesh-role").text;

  check("every session still gets exactly one row",
        ["wide", "twin", "solo", "loner"].map((n) => !!rowOf(n)),
        [true, true, true, true]);

  /* The headings, so what follows is read against the rail the reader
     actually sees. A heading is drawn per BUCKET, and a bucket holds the
     sessions filed into it: `delta` gets none, because its only member
     (`wide`) was filed under `alpha`. That is what the pills below are for —
     with no heading of its own, `delta` is named on the row or nowhere. */
  const headings = list.kids
    .filter((n) => n.classes.has("session-group-heading")).map((n) => n.text);
  check("a mesh whose members were all filed elsewhere gets no heading",
        headings, ["mesh · (no mesh)", "mesh · alpha", "mesh · bravo"]);

  /* The point of the change. `wide` is filed under `alpha`, so both of its
     pill slots go to `bravo` and `delta`; before, `alpha` took one of them
     and `delta` fell into a `+1`. */
  check("the row drops the mesh its own heading already names",
        pills("wide"), ["bravo", "delta"]);
  check("a session whose only room is its heading draws no pills",
        pills("solo"), []);
  check("and grows no pill box either",
        rowOf("solo").querySelectorAll(".rail-meshes").length, 0);
  check("a session in no mesh is unchanged", pills("loner"), []);

  /* A session holds one role per mesh, and the one worth drawing beside the
     name is the one it holds in the mesh whose heading the row sits under.
     `twin` is a leader in `alpha` and a reviewer in `bravo`. */
  check("the role is the one held in the heading's mesh",
        roleOf("twin"), "leader");
  check("a session with no membership keeps the packaged default",
        roleOf("loner"), "free-role");

  /* ---- ungrouped ------------------------------------------------------- */
  check("with no heading to defer to, the pills name the first rooms again",
        ctx.tags("wide").map((t) => t.text), ["alpha", "bravo", "+1"]);
  check("and the rooms past the cap are still counted",
        ctx.tags("wide").map((t) => [t.text, t.more || false]),
        [["alpha", false], ["bravo", false], ["+1", true]]);

  /* The marker rides the row objects, so a nested grouping carries the mesh
     down through the level below it, and the bucket of sessions that joined
     nothing carries none: `(no mesh)` is not a mesh to suppress. */
  const sessions = ctx.rows(
    served.sessions.map((s) => [s, 0]), ["mesh", "workspace"])
    .filter((r) => r.type === "session")
    .map((r) => [r.session.name, r.meshGroup]);
  check("the mesh a row landed under is carried through a nested group",
        sessions,
        [["loner", null], ["wide", "alpha"], ["twin", "alpha"],
         ["solo", "bravo"]]);

  const flat = ctx.rows(served.sessions.map((s) => [s, 0]), ["workspace"])
    .filter((r) => r.type === "session").map((r) => r.meshGroup);
  check("a rail grouped by something else marks no row with a mesh",
        flat, [null, null, null, null]);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railmeshgroup_check: ok");
})();
