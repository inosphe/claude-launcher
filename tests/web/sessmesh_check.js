/* The mesh tags a rail row wears, run against the real functions from app.js.

   Each session row carries a pill per mesh that session belongs to. What has
   to hold: the tags come from the mesh poll (the session poll knows nothing
   about meshes) and name only the rooms this session is actually in; a
   REMOTE member is never mistaken for a local one, since two daemons may
   both have an `s1` and tagging our row with somebody else's room would be a
   claim the reader cannot check; several rooms are ordered and capped, with
   the remainder counted rather than pushed onto the row; and a session in no
   mesh grows nothing at all. The hover text is the rest of the fact — which
   handle, and which role, the session joined as. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* The cap is app.js's, not a number restated here: a harness that carries its
   own copy would keep passing after the app changed its mind. */
const capLine = src.match(/^const RAIL_MESH_TAGS = .+$/m);
if (!capLine) throw new Error("cannot locate RAIL_MESH_TAGS in app.js");

const ctx = {};
new Function(
  "exports", "meshCache",
  [capLine[0], slice("sessMeshes"), slice("railMeshTags")].join("\n") + `
exports.cap = RAIL_MESH_TAGS;
exports.meshes = sessMeshes;
exports.tags = railMeshTags;
exports.setMeshes = (ms) => { meshCache = ms; };
`)(ctx, []);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const member = (session, handle, role, extra) => ({
  session, handle, role, local: true, machine: "", ...extra,
});

/* One mesh, one member: the pill is bare (the row has no space to spell
   out what a name is), so the hover text is what says this is a mesh and
   who the session is in it. */
ctx.setMeshes([
  { name: "mesh0", members: [member("s23", "s23", "worker"),
                             member("s20", "s20", "leader")] },
]);
check("the row names the mesh its session joined",
      ctx.tags("s23").map((t) => t.text), ["mesh0"]);
check("the hover text carries the handle and the role",
      ctx.tags("s23")[0].title, "mesh mesh0 — joined as s23 (worker)");
check("another member's row is not tagged with this one's handle",
      ctx.tags("s20")[0].title, "mesh mesh0 — joined as s20 (leader)");
check("a session in no mesh grows nothing", ctx.tags("nobody"), []);

/* A handle that is not the session name (--as) still resolves by session. */
ctx.setMeshes([
  { name: "build", members: [member("s23", "coder2", "worker")] },
]);
check("a renamed handle is found by its session",
      ctx.tags("s23").map((t) => t.text), ["build"]);
check("and the hover text names the handle, not the session",
      ctx.tags("s23")[0].title, "mesh build — joined as coder2 (worker)");

/* A role-less member: the pill is the room, and the hover text simply stops
   rather than trailing an empty bracket. */
ctx.setMeshes([{ name: "adhoc", members: [member("s23", "s23", "")] }]);
check("a member with no role gets no empty bracket",
      ctx.tags("s23")[0].title, "mesh adhoc — joined as s23");

/* Remote members are somebody else's sessions. Same name, other daemon: the
   local row must not claim their room. */
ctx.setMeshes([
  { name: "remote-only", members: [
    { session: "s23", handle: "s23", role: "worker", local: false,
      machine: "other-box" },
  ] },
  { name: "ours", members: [member("s23", "s23", "worker")] },
]);
check("a remote member of the same name is not our membership",
      ctx.tags("s23").map((t) => t.text), ["ours"]);

/* Several rooms: sorted by name, capped, and the rest counted — the name the
   pills sit beside must survive a session that joined five meshes. */
const many = ["delta", "alpha", "charlie", "bravo"];
ctx.setMeshes(many.map((n) => (
  { name: n, members: [member("s23", "s23", "worker")] }
)));
const sorted = [...many].sort();
check("memberships are ordered by mesh name",
      ctx.meshes("s23").map((m) => m.mesh), sorted);
check("the row shows the cap and counts the remainder",
      ctx.tags("s23").map((t) => t.text),
      [...sorted.slice(0, ctx.cap), `+${sorted.length - ctx.cap}`]);
check("the overflow pill names what it stands for",
      ctx.tags("s23").slice(-1)[0].title,
      "also in " + sorted.slice(ctx.cap).map((n) => `${n} (s23)`).join(", "));

/* Exactly at the cap there is nothing left over, so no counter appears. */
ctx.setMeshes(sorted.slice(0, ctx.cap).map((n) => (
  { name: n, members: [member("s23", "s23", "worker")] }
)));
check("a session exactly at the cap grows no overflow pill",
      ctx.tags("s23").map((t) => t.text), sorted.slice(0, ctx.cap));

/* An empty poll (the daemon answered before any mesh existed) is not an
   error — the rail simply carries no tags. */
ctx.setMeshes([{ name: "empty" }]);
check("a mesh with no member list is skipped", ctx.tags("s23"), []);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("sessmesh_check: ok");
