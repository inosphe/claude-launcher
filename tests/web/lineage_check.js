/* The sidebar's tree ordering, run against the real byLineage from app.js.

   The rail is where a fleet is read, and the ordering is what makes it
   readable: parent before child, child indented under it. The interesting
   cases are the broken ones — a parent that was cleared away, a cycle from a
   hand-edited sessions.json — where the rule is that nothing may vanish. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

const a = src.indexOf("function byLineage(");
const b = src.indexOf("async function refreshSessions(");
if (a < 0 || b <= a) throw new Error("cannot locate byLineage in app.js");
const ctx = {};
new Function("exports", src.slice(a, b) + "\nexports.byLineage = byLineage;")(ctx);
const { byLineage } = ctx;

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const shape = (rows) => rows.map(([s, d]) => `${"  ".repeat(d)}${s.name}`);
const sessions = (...defs) =>
  defs.map((d) => (typeof d === "string" ? { name: d } : d));

/* the ordinary case: a lead, its workers, and a worker of its own */
check(
  "a subtree is indented under the session that spawned it",
  shape(byLineage(sessions(
    "lead",
    { name: "w1", parent: "lead" },
    { name: "w1a", parent: "w1" },
    { name: "w2", parent: "lead" },
    "solo",
  ))),
  ["lead", "  w1", "    w1a", "  w2", "solo"]
);

/* roots keep the order the API sent (name order), and so do siblings */
check(
  "children follow their parent, not the flat ordering",
  shape(byLineage(sessions(
    { name: "aaa", parent: "zzz" },
    "mmm",
    "zzz",
  ))),
  ["mmm", "zzz", "  aaa"]
);

/* a parent that is not in the list at all — its record was cleared, or the
   definition was hand-edited. The child is a root, never a dropped row. */
check(
  "a dangling parent makes its child a root",
  shape(byLineage(sessions("keep", { name: "orphan", parent: "gone" }))),
  ["keep", "orphan"]
);

/* self-parenthood and cycles: only sessions.json can produce these, and the
   listing still has to account for every session it was given */
check(
  "a session that is its own parent is a root",
  shape(byLineage(sessions({ name: "loop", parent: "loop" }))),
  ["loop"]
);
/* Nobody in a cycle is a root, so the walk never reaches them; the sweep
   afterwards lists each at depth 0. Flat is the honest answer here — there is
   no top to indent from — and it is what the CLI's _by_lineage prints too. */
check(
  "every session in a cycle is still listed",
  shape(byLineage(sessions(
    { name: "a", parent: "b" },
    { name: "b", parent: "a" },
    "c",
  ))),
  ["c", "a", "b"]
);

check("an empty list stays empty", byLineage([]), []);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("lineage_check: ok");
