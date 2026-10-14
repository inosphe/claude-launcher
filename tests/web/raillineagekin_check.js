/* Descendants and direct ancestors stay on the rail across project, mesh
   and search filters (claunch-mz09h, claunch-78p7b), run against the real
   helpers in app.js.

   A session a person can see keeps its children and grandchildren in sight
   wherever they are filed. Two filters used to hide them: the project pick
   (railSessions) and the mesh tab (sessionGroupRows buckets one row per
   mesh). What has to hold: the project pick adds every descendant of a
   picked-project session and nothing else; a mesh tab adds the descendants
   of its own rows, under their parent and marked `kin`; a session's own
   bucket is unchanged; and no project pick means no additions. The line
   upward is kept the same way: the project pick and a mesh tab add the
   direct ancestors of their rows (not the ancestors' other children), and
   the search keeps the descendants and ancestors of what it matches. */
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
    "let sessionSearch = { q: '', hits: null };",
    "let sessionFilter = ['running'];",
    slice("byLineage"), slice("regroupDepths"), slice("bucketWithDescendants"),
    slice("sessionGroupRows"), slice("sessionGroupValue"),
    slice("sessionMeshGroup"), slice("sessionWorkspaceGroup"), slice("sessMeshes"),
    slice("recordProject"), slice("sessionInCurrentProject"),
    slice("lineageDescendants"), slice("lineageAncestors"), slice("railSessions"),
    slice("sessionSearchText"), slice("sessionMatchesSearch"),
    slice("sessionSearchLineage"), slice("sessionSearchNote"),
    slice("sessionCategory"), slice("sessionMatchesFilter"),
    slice("sessionGroupRowVisible"), slice("sessionGroupRowsVisible"),
    slice("sessionShownForLineage"),
    "exports.pick = (p) => { currentProject = p; };",
    "exports.byLineage = byLineage;",
    "exports.sessionGroupRows = sessionGroupRows;",
    "exports.lineageDescendants = lineageDescendants;",
    "exports.railSessions = railSessions;",
    "exports.sessionShownForLineage = sessionShownForLineage;",
    "exports.lineageAncestors = lineageAncestors;",
    "exports.search = (q) => { sessionSearch = { q, hits: null }; };",
    "exports.sessionSearchLineage = sessionSearchLineage;",
    "exports.sessionSearchNote = sessionSearchNote;",
    "exports.sessionGroupRowsVisible = sessionGroupRowsVisible;",
  ].join("\n"))(ctx, rooms);
const { byLineage, sessionGroupRows, lineageDescendants, railSessions,
        sessionShownForLineage, lineageAncestors, sessionSearchLineage,
        sessionSearchNote, sessionGroupRowsVisible } = ctx;

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
check("the other project: its own rows, with the ancestor above them",
  names(railSessions(FLEET)), ["kid", "grandkid", "stranger", "lead"]);
check("...the ancestor is marked as shown for its lineage",
  [sessionShownForLineage(FLEET[0]), sessionShownForLineage(FLEET[1])],
  [true, false]);
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
    "# beta", "lead *", "  kid", "    grandkid",
  ]);
check("an ungrouped rail adds nothing and marks nothing",
  shape(sessionGroupRows(byLineage(FLEET), [])),
  ["lead", "  kid", "    grandkid", "solo", "stranger"]);

/* ---- the ancestors ---------------------------------------------------- */
const TREE = [
  { name: "root" },
  { name: "mid", parent: "root" },
  { name: "leaf", parent: "mid" },
  { name: "aunt", parent: "root" },
  { name: "cousin", parent: "aunt" },
  { name: "loner" },
  { name: "loop-a", parent: "loop-b" },
  { name: "loop-b", parent: "loop-a" },
];
check("ancestors of a leaf: the whole line up, nearest first",
  names(lineageAncestors([TREE[2]], TREE)), ["mid", "root"]);
check("an ancestor's other children are not added",
  names(lineageAncestors([TREE[2], TREE[4]], TREE)), ["mid", "root", "aunt"]);
check("a base row is never added as an ancestor",
  names(lineageAncestors([TREE[2], TREE[0]], TREE)), ["mid"]);
check("a cleared parent ends the line",
  names(lineageAncestors([{ name: "orphan", parent: "gone" }], TREE)), []);
check("a cycle ends",
  names(lineageAncestors([TREE[6]], TREE)), ["loop-b"]);

/* ---- the search -------------------------------------------------------- */
const sorted = (set) => set && [...set].sort();
ctx.search("");
check("an empty box keeps everything", sessionSearchLineage(TREE), null);
ctx.search("mid");
check("a match keeps its ancestors and descendants, nothing beside them",
  sorted(sessionSearchLineage(TREE)), ["leaf", "mid", "root"]);
ctx.search("cousin");
check("a deep match brings its whole line up",
  sorted(sessionSearchLineage(TREE)), ["aunt", "cousin", "root"]);
ctx.search("root");
check("a match at the top keeps its whole subtree",
  sorted(sessionSearchLineage(TREE)), ["aunt", "cousin", "leaf", "mid", "root"]);
ctx.search("mid");
check("lineage is counted only among the rows of the view",
  sorted(sessionSearchLineage([TREE[1], TREE[2]])), ["leaf", "mid"]);
check("the note counts the matches and the rows kept for their lineage",
  sessionSearchNote({ q: "mid", hits: null }, TREE),
  "1 of 8 match (+2 lineage) — Enter searches by meaning");
check("...and adds nothing when no lineage was kept",
  sessionSearchNote({ q: "loner", hits: null }, TREE),
  "1 of 8 match — Enter searches by meaning");

const row = (session) => ({ type: "session", session, depth: 0 });
const states = [
  { name: "p" }, { name: "c", parent: "p" },
  { name: "dead", parent: "p", status: "exited" }, { name: "other" },
];
ctx.search("c");
check("a tab counts the match and its parent, not the filtered-out sibling",
  sessionGroupRowsVisible(states.map(row)).map((r) => r.session.name), ["p", "c"]);
ctx.search("");
check("an empty box counts every row the state filter admits",
  sessionGroupRowsVisible(states.map(row)).map((r) => r.session.name), ["p", "c", "other"]);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("raillineagekin checks passed");
