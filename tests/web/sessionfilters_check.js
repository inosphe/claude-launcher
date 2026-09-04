/* The rail's state filter: current working records, running sessions, killed
   records, paused records and archived records. Exercise the shipped helper
   and DOM state so a label can never claim one partition while the rows show
   another. */
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "app.js"), "utf8");
const html = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "index.html"), "utf8");
const css = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");

const a = src.indexOf("const SESSION_FILTER_KEY");
const b = src.indexOf("async function refreshSessions(", a);
if (a < 0 || b <= a) throw new Error("cannot locate session-filter helper");

function classes(initial = []) {
  const values = new Set(initial);
  return {
    values,
    api: { toggle(name, on) { if (on) values.add(name); else values.delete(name); } },
  };
}

const sessions = [
  { name: "run", status: "busy" },
  { name: "dead", status: "exited" },
  { name: "held", status: "exited", paused_at: "2026-09-02T00:00:00Z" },
  { name: "old", status: "exited", archived_at: "2026-08-28T00:00:00Z" },
];
const rows = sessions.map((session) => {
  const state = classes();
  return { dataset: { name: session.name }, classList: state.api, classes: state.values };
});
const list = { querySelectorAll: () => rows };
const buttons = {};
for (const filter of ["current", "running", "killed", "paused", "archived"]) {
  buttons[filter] = {
    textContent: "", children: [], attrs: {},
    append(...children) { this.children.push(...children); },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
}
const writes = [];
const localStorage = {
  getItem: () => "killed",
  setItem: (key, value) => writes.push([key, value]),
};
const document = {
  createTextNode: (text) => ({ textContent: text }),
  createElement: () => ({}),
};
const ctx = {};
new Function("exports", "BASE", "localStorage", "sessionsCache", "$", "document",
  "sessMeshes", "refreshSessions",
  src.slice(a, b) +
  "\nexports.category = sessionCategory;" +
  "\nexports.matches = sessionMatchesFilter;" +
  "\nexports.counts = sessionFilterCounts;" +
  "\nexports.sync = syncSessionFilters;" +
  "\nexports.set = setSessionFilter;" +
  "\nexports.setGroup = setSessionGroup;" +
  "\nexports.groupRows = sessionGroupRows;" +
  "\nexports.workspace = sessionWorkspaceGroup;" +
  "\nexports.current = () => sessionFilter;")(
    ctx, "/t/local/", localStorage, sessions,
    (id) => id === "session-list" ? list : buttons[id.replace("session-filter-", "")],
    document,
    (name) => name === "alpha" ? [{ mesh: "mesh-a" }] :
      name === "beta" ? [{ mesh: "mesh-b" }] : [],
    () => {}
  );

let failures = 0;
function check(what, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}` +
                  `\n  want ${JSON.stringify(want)}`);
    failures++;
  }
}

check("the four lifecycle categories are disjoint",
      sessions.map(ctx.category), ["running", "killed", "paused", "archived"]);
check("current combines running, killed and paused",
      sessions.map((s) => ctx.matches(s, "current")), [true, true, true, false]);
check("every count describes its exact partition", ctx.counts(sessions),
      { current: 3, running: 1, killed: 1, paused: 1, archived: 1 });

ctx.sync(sessions);
check("the remembered killed filter is selected",
      Object.values(buttons).map((b) => b.attrs["aria-pressed"]),
      ["false", "false", "true", "false", "false"]);
check("killed shows only killed rows — a paused record is not one",
      rows.map((row) => row.classes.has("session-filtered")), [true, false, true, true]);
check("counts are rendered beside every label",
      Object.values(buttons).map((b) => b.children[1].textContent),
      ["3", "1", "1", "1", "1"]);

ctx.set("paused");
check("paused shows only paused rows",
      rows.map((row) => row.classes.has("session-filtered")), [true, true, false, true]);
check("the paused filter asks the daemon for the paused partition",
      src.includes(': filter === "paused" ? "paused"'), true);

ctx.set("archived");
check("archived shows only archived rows",
      rows.map((row) => row.classes.has("session-filtered")), [true, true, true, false]);
check("a filter selection is remembered",
      writes.at(-1), ["claunch_session_filter:/t/local/", "archived"]);

ctx.set("current");
check("current restores the working fleet",
      rows.map((row) => row.classes.has("session-filtered")), [false, false, false, true]);

const grouped = ctx.groupRows([
  [{ name: "alpha", cwd: "F:/works/repo/.claude/worktrees/a" }, 0],
  [{ name: "beta", cwd: "F:/works/repo/.claude/worktrees/b" }, 0],
  [{ name: "solo", cwd: "F:/other" }, 0],
], ["mesh", "workspace"]);
check("workspace collapses worktrees of one repository",
      [ctx.workspace({ cwd: "F:/works/repo/.claude/worktrees/a" }),
       ctx.workspace({ cwd: "F:/works/repo/.claude/worktrees/b" })],
      ["F:/works/repo", "F:/works/repo"]);
check("first selected group is the outer nesting level",
      grouped.filter((row) => row.type === "group").map((row) =>
        `${row.level}:${row.group}:${row.value}`),
      ["0:mesh:(no mesh)", "1:workspace:F:/other", "0:mesh:mesh-a",
       "1:workspace:F:/works/repo", "0:mesh:mesh-b", "1:workspace:F:/works/repo"]);
ctx.setGroup("workspace", true);
ctx.setGroup("mesh", true);
check("group priority follows checkbox activation order",
      writes.slice(-2), [
        ["claunch_session_group_order:/t/local/", "[\"workspace\",\"mesh\"]"],
        ["claunch_session_group:/t/local/", "true"],
      ]);

check("the shipped page contains all five state controls",
      ["current", "running", "killed", "paused", "archived"].every((name) =>
        html.includes(`id="session-filter-${name}"`)), true);
check("archived sessions have a dedicated one-shot refresh control",
      html.includes('id="refresh-archived"') &&
      src.includes('refreshSessions({ state: "archived" })'), true);
check("filtered rows leave the layout",
      /#session-list\s*>\s*li\.session-filtered\s*\{[^}]*display:\s*none/.test(css), true);
check("the shipped page contains the mesh grouping checkbox",
      html.includes('id="session-group-mesh"') &&
      html.includes("Group by mesh"), true);
check("the shipped page contains the workspace grouping checkbox",
      html.includes('id="session-group-workspace"') &&
      html.includes("Group by workspace"), true);
check("mesh grouping persists and rebuilds the rail",
      src.includes("const SESSION_GROUP_KEY") &&
      src.includes("const SESSION_GROUP_ORDER_KEY") &&
      src.includes("sessionGroupByMesh") &&
      src.includes("sessionGroupByWorkspace") &&
      src.includes("sessionGroupRows") &&
      src.includes("session-group-heading") &&
      src.includes("setSessionGroupByMesh"), true);
check("mesh group headings have dedicated styling",
      /\.session-group-toggle\s*\{/.test(css) &&
      /#session-list \.session-group-heading\s*\{/.test(css), true);

const killStart = src.indexOf("async function killCurrentSession(");
const killEnd = src.indexOf("async function archiveExitedSession(", killStart);
const archiveStart = killEnd;
const archiveEnd = src.indexOf('$("term-archive")', archiveStart);
const bulkKillStart = src.indexOf('$("stop-all").addEventListener');
const bulkKillEnd = src.indexOf('$("resume-all").addEventListener', bulkKillStart);
const bulkArchiveStart = src.indexOf('$("archive-exited").addEventListener');
const bulkArchiveEnd = src.indexOf("for (const filter of SESSION_FILTERS)",
                                   bulkArchiveStart);
const actionBodies = {
  kill: src.slice(killStart, killEnd),
  archive: src.slice(archiveStart, archiveEnd),
  "bulk kill": src.slice(bulkKillStart, bulkKillEnd),
  "bulk archive": src.slice(bulkArchiveStart, bulkArchiveEnd),
};
check("the terminal kill button invokes the inspected kill handler",
      /\$\("term-kill"\)\.addEventListener\("click",\s*killCurrentSession\)/.test(src),
      true);
for (const [action, body] of Object.entries(actionBodies)) {
  check(`${action} preserves the selected session filter`,
        !body.includes("setSessionFilter(") &&
        !body.includes("sessionFilter ="), true);
  check(`${action} preserves the current route and terminal`,
        !body.includes("location.hash") &&
        !body.includes("currentName = null") &&
        !body.includes("detach()") &&
        !body.includes("go("), true);
}

if (failures) process.exit(1);
console.log("sessionfilters_check: ok");
