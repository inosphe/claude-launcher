/* Lineage depth on a GROUPED rail, run against the real helpers in app.js.

   `byLineage` numbers the whole fleet, and `sessionGroupRows` then cuts that
   forest into buckets — one per mesh, or per workspace. Nothing reunites a
   parent and a child that land in different buckets, so the child used to
   keep the depth it was given outside: the row builder indented it, added
   `li.class = "child"` (the `└` tick in style.css) and set the title to
   `spawned by <parent>`, naming a session that is not under that heading.
   The row above it — whatever happened to sort first in the bucket — read as
   its parent.

   Seen on s640-qf1, a quick-fork that joined no mesh: it was drawn one level
   in under s599 in the `(no mesh)` group while its origin s640 sat under
   mesh-0826. `claunch sessions` printed the pair correctly, because the CLI
   never splits the forest.

   What has to hold: inside a bucket, a row sits one level under its parent
   when that parent is in the same bucket and at level 0 when it is not; a
   subtree that moves together keeps its shape and does not carry its absent
   ancestors' levels; an ungrouped rail is unchanged; and the roots
   `byLineage` already made (a cleared parent, a cycle) stay roots. */
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

/* The rooms the sessions below are in, in the shape `sessMeshes` reads. The
   array is handed to the sandbox by reference, so a check can change who is
   in which room by rewriting the member list in place. */
const rooms = [{ name: "mesh-0826", members: [] }];
const member = (session) =>
  ({ local: true, session, handle: session, role: "worker", roles: ["worker"] });
const inMesh0826 = (...names) => { rooms[0].members = names.map(member); };

const ctx = {};
new Function("exports", "meshCache",
  [
    slice("byLineage"), slice("regroupDepths"), slice("sessionGroupRows"),
    slice("sessionGroupValue"), slice("sessionMeshGroup"),
    slice("sessionWorkspaceGroup"), slice("sessMeshes"),
    "exports.byLineage = byLineage;",
    "exports.regroupDepths = regroupDepths;",
    "exports.sessionGroupRows = sessionGroupRows;",
  ].join("\n"))(ctx, rooms);
const { byLineage, regroupDepths, sessionGroupRows } = ctx;

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* What the rail draws: a heading as `# mesh value`, a session indented by the
   depth the row builder would spend on it. */
const shape = (rows) => rows.map((r) => r.type === "group"
  ? `# ${r.group} ${r.value}`
  : `${"  ".repeat(r.depth)}${r.session.name}`);

/* `regroupDepths` answers in `byLineage`'s own [session, depth] shape, one
   layer below the rows above. */
const pairs = (rows) => rows.map(([s, d]) => `${"  ".repeat(d)}${s.name}`);

/* ---- the reported case ------------------------------------------------ */
const FLEET = () => [
  { name: "s599" },
  { name: "s640" },
  { name: "s640-qf1", parent: "s640" },
];

/* s640 is in mesh-0826; its quick-fork joined nothing (the scratch copy the
   fork dialog offers first), so the two land under different headings and
   the copy is a root of its own bucket. */
inMesh0826("s640", "s599");
check(
  "a child whose parent is under another heading is a root of its bucket",
  shape(sessionGroupRows(byLineage(FLEET()), ["mesh"])),
  ["# mesh (no mesh)", "s640-qf1", "# mesh mesh-0826", "s599", "s640"]
);

/* The same fleet with the fork enrolled in its origin's mesh (the dialog's
   "inherit" answer): one bucket, and the indent is the real one. */
inMesh0826("s640", "s599", "s640-qf1");
check(
  "a child under the same heading keeps its place under its parent",
  shape(sessionGroupRows(byLineage(FLEET()), ["mesh"])),
  ["# mesh mesh-0826", "s599", "s640", "  s640-qf1"]
);

/* ---- depth is counted again, not merely clamped ----------------------- */
/* lead → w1 → w1a, with only w1 and w1a moving to the other bucket: w1 is
   that bucket's root and w1a is one level under it, not two. */
check(
  "a subtree that moves together keeps its shape and loses the absent levels",
  pairs(regroupDepths([
    [{ name: "w1", parent: "lead" }, 1],
    [{ name: "w1a", parent: "w1" }, 2],
  ])),
  ["w1", "  w1a"]
);

/* A gap in the middle: the grandparent is present, the parent is not. The
   row is a root, the same answer `byLineage` gives for a parent it cannot
   see — indenting it under the grandparent would claim an edge that is not
   in the records. */
check(
  "a row whose parent is absent is a root even when its grandparent is there",
  pairs(regroupDepths([
    [{ name: "lead" }, 0],
    [{ name: "w1a", parent: "w1" }, 2],
  ])),
  ["lead", "w1a"]
);

/* ---- what must not change --------------------------------------------- */
check(
  "an ungrouped rail keeps every depth byLineage gave it",
  shape(sessionGroupRows(
    byLineage([
      { name: "lead" },
      { name: "w1", parent: "lead" },
      { name: "w1a", parent: "w1" },
      { name: "w2", parent: "lead" },
      { name: "solo" },
    ]),
    []
  )),
  ["lead", "  w1", "    w1a", "  w2", "solo"]
);

check(
  "a root byLineage already made stays a root",
  pairs(regroupDepths([
    [{ name: "orphan", parent: "cleared-away" }, 0],
    [{ name: "loop", parent: "loop" }, 0],
  ])),
  ["orphan", "loop"]
);

check("an empty bucket stays empty", regroupDepths([]), []);

/* ---- grouped by workspace, the other axis ----------------------------- */
/* A child spawned into a worktree of another repository: separate workspace
   bucket, so no indent follows it there. */
check(
  "the same rule holds when the rail is grouped by workspace",
  shape(sessionGroupRows(
    byLineage([
      { name: "lead", cwd: "F:/works/repo" },
      { name: "w1", parent: "lead", cwd: "F:/works/repo/.claude/worktrees/w1" },
      { name: "w2", parent: "lead", cwd: "F:/works/other" },
    ]),
    ["workspace"]
  )),
  ["# workspace F:/works/other", "w2",
   "# workspace F:/works/repo", "lead", "  w1"]
);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railgroupdepth_check: ok");
