/* Pull the pure layout helpers out of the real app.js and exercise them, so
   the tree/cluster maths is checked against the shipped source rather than a
   copy of it. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

const code = [
  slice("const RING = {", "let meshDrag"),
  slice("function ringRadius", "function svg("),
  slice("function meshClusters", "function meshReachable"),
  slice("function meshReachable", "function topoHint"),
  slice("function boxExit", "function topoHint"),
].join("\n");

const ctx = {};
new Function(
  "exports",
  code + "\nObject.assign(exports, {RING, ringRadius, ringPoint, meshClusters," +
  " meshForest, layoutForest, measureCluster, meshReachable, boxExit});"
)(ctx);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

const member = (handle, parent, machine) => ({
  handle, parent: parent || null, machine: machine || "",
  role: "worker", session: handle, reachability: "idle",
});

/* --- clustering ------------------------------------------------------- */
{
  const info = {
    self: "work-pc",
    peers: [{ machine: "work-pc", rank: 0 }, { machine: "laptop", rank: 1 }],
    members: [
      member("lead"), member("w-api", "lead"), member("scout", null, "laptop"),
    ],
  };
  const cs = ctx.meshClusters(info);
  check("two clusters", cs.length === 2, cs.length);
  check("blank machine lands on self", cs[0].members.length === 2,
        cs[0].members.map((m) => m.handle));
  check("remote member lands on its daemon",
        cs[1].members.map((m) => m.handle).join() === "scout");
}
{
  // A mesh that never federated: one cluster, still drawn.
  const cs = ctx.meshClusters({ self: "", peers: [], members: [member("s0")] });
  check("local-only mesh still clusters", cs.length === 1 && cs[0].members.length === 1);
}

/* --- forest ----------------------------------------------------------- */
{
  const f = ctx.meshForest([
    member("lead"), member("w-api", "lead"), member("w-web", "lead"),
    member("w-db", "w-api"),
  ]);
  check("one root", f.roots.join() === "lead", f.roots);
  check("two children of lead", (f.kids.get("lead") || []).join() === "w-api,w-web");
  check("grandchild", (f.kids.get("w-api") || []).join() === "w-db");
}
{
  // A parent nobody enrolled, and a parent that has left: both are roots.
  const f = ctx.meshForest([member("a", "ghost"), member("b", null)]);
  check("dangling parent is a root", f.roots.sort().join() === "a,b", f.roots);
}
{
  // A cycle from a hand-edited sessions.json must still draw both agents.
  const f = ctx.meshForest([member("a", "b"), member("b", "a")]);
  const pos = ctx.layoutForest(f);
  check("cycle promotes a root", f.roots.length >= 1, f.roots);
  check("cycle places both nodes", pos.size === 2, [...pos.keys()]);
  check("no null placement", [...pos.values()].every((v) => v && isFinite(v.x)));
}

/* --- layout ----------------------------------------------------------- */
{
  const members = [
    member("lead"), member("w-api", "lead"), member("w-web", "lead"),
    member("w-db", "w-api"),
  ];
  const m = ctx.measureCluster({ members });
  const at = m.at;
  check("all four placed", m.nodes.length === 4, m.nodes.length);
  check("depth sets the row", at.get("lead").y < at.get("w-api").y);
  check("grandchild is a row lower", at.get("w-api").y < at.get("w-db").y);
  const mid = (at.get("w-api").x + at.get("w-web").x) / 2;
  check("parent centres over its children",
        Math.abs(at.get("lead").x - mid) < 0.01, [at.get("lead").x, mid]);
  check("siblings do not overlap",
        Math.abs(at.get("w-api").x - at.get("w-web").x) >= ctx.RING.colW);
  check("box contains every node",
        m.nodes.every((n) => n.x >= 0 && n.x <= m.w && n.y >= 0 && n.y <= m.h),
        { w: m.w, h: m.h, nodes: m.nodes });
}
{
  // Two separate teams in one cluster must not collide.
  const m = ctx.measureCluster({ members: [
    member("lead-a"), member("wa", "lead-a"),
    member("lead-b"), member("wb", "lead-b"),
  ] });
  // Sharing a column is fine across rows (a parent centres over an only
  // child); what must never happen is two agents colliding within one row.
  const rows = new Map();
  for (const n of m.nodes) {
    if (!rows.has(n.y)) rows.set(n.y, []);
    rows.get(n.y).push(n.x);
  }
  for (const [y, xs] of rows) {
    xs.sort((a, b) => a - b);
    check(`row ${y} keeps a column between neighbours`,
          xs.every((x, i) => i === 0 || x - xs[i - 1] >= ctx.RING.colW), xs);
  }
}
{
  const m = ctx.measureCluster({ members: [] });
  check("empty cluster still has a box", m.w > 0 && m.h > 0, m);
}

/* --- ring + clipping --------------------------------------------------- */
{
  check("one cluster sits at the centre", ctx.ringRadius(1, 200) === 0);
  const r = ctx.ringRadius(4, 200);
  const p0 = ctx.ringPoint(0, 4, r), p1 = ctx.ringPoint(1, 4, r);
  check("rank 0 at 12 o'clock", Math.abs(p0.x) < 1e-9 && p0.y < 0, p0);
  check("clockwise from there", p1.x > 0 && Math.abs(p1.y) < 1e-9, p1);
  const gap = Math.hypot(p0.x - p1.x, p0.y - p1.y);
  check("neighbours clear a whole cell", gap >= 199.9, gap);
}
{
  const box = { cx: 0, cy: 0, w: 100, h: 60 };
  const e = ctx.boxExit(box, 0, 500);
  check("exit lands on the border", Math.abs(e.y - 30) < 1e-9 && e.x === 0, e);
  const d = ctx.boxExit(box, 500, 0);
  check("exit clips on x too", Math.abs(d.x - 50) < 1e-9, d);
}

/* --- reachability ------------------------------------------------------ */
{
  const info = { member_links: [
    { a: "lead", b: "w-api", enabled: true },
    { a: "lead", b: "scout", enabled: true },
    { a: "w-api", b: "scout", enabled: false },
  ] };
  check("reach follows enabled edges both ways",
        [...ctx.meshReachable(info, "lead")].sort().join() === "scout,w-api");
  check("a cut is not reachable",
        [...ctx.meshReachable(info, "w-api")].join() === "lead");
}

console.log(failures ? `\n${failures} failure(s)` : "all layout checks passed");
process.exit(failures ? 1 : 0);
