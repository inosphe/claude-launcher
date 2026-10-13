/* Descendants stay on the rail across project and mesh filters
   (claunch-mz09h), run against the real helpers in app.js.

   A session a person can see keeps its children and grandchildren in sight
   wherever they are filed. Two filters used to hide them: the project pick
   (railSessions) and the mesh tab (sessionGroupRows buckets one row per
   mesh). What has to hold: the project pick adds every descendant of a
   picked-project session and nothing else; a mesh tab adds the descendants
   of its own rows, under their parent and marked `kin`; a session's own
   bucket is unchanged; and no project pick means no additions. */
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
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

const member = (session) =>
  ({ local: true, session, handle: session, role: "worker", roles: ["worker"] });
const rooms = [
  { name: "alpha", members: ["lead", "solo"].map(member) },
  { name: "beta", members: ["kid", "grandkid"].map(member) },
];

const ctx = {};
new Function("exports", "meshCache",
  [
    "let currentProject = '';",
    "let sessionsCache = [];",
    slice("byLineage"), slice("regroupDepths"), slice("bucketWithDescendants"),
    slice("sessionGroupRows"), slice("sessionGroupValue"),
    slice("sessionMeshGroup"), slice("sessionWorkspaceGroup"), slice("sessMeshes"),
    slice("recordProject"), slice("sessionInCurrentProject"),
    slice("lineageDescendants"), slice("railSessions"),
    slice("sessionShownForLineage"),
    "exports.pick = (p) => { currentProject = p; };",
    "exports.byLineage = byLineage;",
    "exports.sessionGroupRows = sessionGroupRows;",
    "exports.lineageDescendants = lineageDescendants;",
    "exports.railSessions = railSessions;",
    "exports.sessionShownForLineage = sessionShownForLineage;",
  ].join("\n"))(ctx, rooms);
const { byLineage, sessionGroupRows, lineageDescendants, railSessions,
        sessionShownForLineage } = ctx;

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const FLEET = [
  { name: "lead", project: "main" },
  { name: "kid", parent: "lead", project: "side" },
  { name: "grandkid", parent: "kid", project: "side" },
  { name: "solo", project: "main" },
  { name: "stranger", project: "side" },
];
const names = (list) => list.map((s) => s.name);

/* ---- the project pick ------------------------------------------------- */
check("descendants of the base, at every depth, parent first",
  names(lineageDescendants([FLEET[0]], FLEET)), ["kid", "grandkid"]);
check("a base with no children adds nothing",
  names(lineageDescendants([FLEET[3]], FLEET)), []);

ctx.pick("main");
check("the picked project keeps its rows and gains their descendants only",
  names(railSessions(FLEET)), ["lead", "solo", "kid", "grandkid"]);
check("a pulled-in descendant is marked, a picked-project row is not",
  [sessionShownForLineage(FLEET[1]), sessionShownForLineage(FLEET[0])],
  [true, false]);
ctx.pick("side");
check("the other project: its own rows, nothing pulled up from above",
  names(railSessions(FLEET)), ["kid", "grandkid", "stranger"]);
ctx.pick("");
check("no pick: every row, none marked",
  [names(railSessions(FLEET)), sessionShownForLineage(FLEET[1])],
  [names(FLEET), false]);

/* ---- the mesh tab ----------------------------------------------------- */
const shape = (rows) => rows.map((r) => r.type === "group"
  ? `# ${r.value}`
  : `${"  ".repeat(r.depth)}${r.session.name}${r.kin ? " *" : ""}`);
check("a mesh tab draws its rows' descendants from other meshes, marked",
  shape(sessionGroupRows(byLineage(FLEET), ["mesh"])),
  [
    "# (no mesh)", "stranger",
    "# alpha", "lead", "  kid *", "    grandkid *", "solo",
    "# beta", "kid", "  grandkid",
  ]);
check("an ungrouped rail adds nothing and marks nothing",
  shape(sessionGroupRows(byLineage(FLEET), [])),
  ["lead", "  kid", "    grandkid", "solo", "stranger"]);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("raillineagekin checks passed");
