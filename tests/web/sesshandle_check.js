/* The mesh HANDLE a session answers to, drawn beside the name the page calls
   it by — on the rail row, in the terminal header, and at the top of the
   details panel.

   The fact this exists for: a session's handle is chosen at join time and is
   free to differ from its session name (`s236` joined mesh-0826 as
   `merger-r13`). Everything said about that session on the mesh uses the
   handle, and until now the web UI drew the handle in two tooltips and one
   chip at the bottom of the details panel — so a reader holding a handle had
   nothing on screen to match it against.

   What has to hold: the value comes from the mesh poll and never from the
   session poll (which knows nothing of meshes); a handle EQUAL to the
   session name draws nothing, since a pill repeating the name beside it is
   noise and the mismatch is the whole point; one handle held across several
   rooms is one name, not several; several DIFFERENT handles are counted
   rather than listed on a 260px row, with the whole of it in the hover; a
   remote member is never mistaken for a local one; and the header's chip
   goes down again when the terminal is switched to a session that answers to
   its own name.

   Sliced out of app.js and run against a stub DOM, so the check moves when
   the app does. */
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

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, text: "", title: "", classes: new Set(),
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    classList: {
      toggle: (c, on) => {
        const want = on === undefined ? !n.classes.has(c) : !!on;
        if (want) n.classes.add(c); else n.classes.delete(c);
        return want;
      },
      contains: (c) => n.classes.has(c),
    },
  };
  return n;
}

const chip = node("span");
chip.classes.add("hidden");

const ctx = {};
new Function(
  "exports", "meshCache", "currentName", "$",
  [slice("sessMeshes"), slice("sessHandles"), slice("handleTag"),
   slice("renderTermHandle")].join("\n") + `
exports.handles = sessHandles;
exports.tag = handleTag;
exports.paintHeader = (name) => { currentName = name; renderTermHandle(); };
exports.setMeshes = (ms) => { meshCache = ms; };
`)(ctx, [], null, (id) => {
  if (id !== "term-handle") throw new Error("unexpected $: " + id);
  return chip;
});

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

/* The ordinary case: the handle IS the session name. Nothing is drawn —
   a pill reading `s181` beside a row labelled `s181` costs rail width and
   tells the reader nothing they cannot already see. */
ctx.setMeshes([
  { name: "mesh-0826", members: [member("s181", "s181", "reviewer")] },
]);
check("same name → no handles", ctx.handles("s181"), []);
check("same name → no tag", ctx.tag("s181"), null);

/* The case the feature exists for. The pill carries the handle itself, and
   the hover says which room it belongs to and what the session's own name
   is — the pill has room for one word, and "which of these two names is the
   session" is the question the reader arrived with. */
ctx.setMeshes([
  { name: "mesh-0826", members: [member("s236", "merger-r13", "worker")] },
]);
check("differing handle", ctx.handles("s236").map((h) => [h.mesh, h.handle]),
      [["mesh-0826", "merger-r13"]]);
check("differing handle → pill text", ctx.tag("s236").text, "merger-r13");
check(
  "differing handle → hover names both names, the room and the role",
  ctx.tag("s236").title,
  "session 's236' answers to 'merger-r13' in mesh-0826 (worker) — " +
  "address it by that name on the mesh"
);

/* One handle held in four rooms is ONE name. The rail already draws the
   rooms as their own pills; repeating `merger-r13` once per room would push
   the name it qualifies off the row to say a thing it said at the first
   pill. */
ctx.setMeshes([
  { name: "mesh-a", members: [member("s236", "merger-r13", "worker")] },
  { name: "mesh-b", members: [member("s236", "merger-r13", "worker")] },
  { name: "mesh-c", members: [member("s236", "merger-r13", "reviewer")] },
]);
check("one handle across rooms collapses", ctx.handles("s236").length, 1);
check("...and is not counted", ctx.tag("s236").text, "merger-r13");

/* Several DIFFERENT handles: the first, then a count. The row cannot spell
   out three names, and the alternative — showing one and saying nothing —
   would have the reader believe they had the whole answer. */
ctx.setMeshes([
  { name: "mesh-a", members: [member("s236", "merger-r13", "worker")] },
  { name: "mesh-b", members: [member("s236", "auditor", "reviewer")] },
]);
check("two handles → first plus a count", ctx.tag("s236").text, "merger-r13 +1");
check(
  "...and the hover carries both, with their rooms",
  ctx.tag("s236").title,
  "session 's236' answers to 'merger-r13' in mesh-a (worker), " +
  "'auditor' in mesh-b (reviewer) — address it by that name on the mesh"
);

/* A handle that differs alongside one that does not: only the differing one
   is a fact worth a pill, and the count must not include the other. */
ctx.setMeshes([
  { name: "mesh-a", members: [member("s236", "s236", "worker")] },
  { name: "mesh-b", members: [member("s236", "merger-r13", "worker")] },
]);
check("own name is not counted alongside a handle",
      ctx.tag("s236").text, "merger-r13");

/* Remote members are somebody else's roster. Two daemons may each hold an
   `s236`, and reading the far one's handle onto our row would be a claim
   the reader has no way to check — the same rule sessMeshes already keeps
   for the room pills. */
ctx.setMeshes([
  {
    name: "mesh-far",
    members: [member("s236", "merger-r13", "worker",
                     { local: false, machine: "other-box" })],
  },
]);
check("remote member is not our session", ctx.handles("s236"), []);

/* A session in no room at all. */
ctx.setMeshes([]);
check("no meshes → nothing", ctx.tag("s236"), null);

/* ---- the header chip --------------------------------------------------- */

/* Up, with the handle, for a session that answers to another name. */
ctx.setMeshes([
  { name: "mesh-0826", members: [member("s236", "merger-r13", "worker")] },
]);
ctx.paintHeader("s236");
check("header chip is up", chip.classList.contains("hidden"), false);
check("header chip text", chip.text, "merger-r13");
check("header chip has the hover", chip.title.includes("merger-r13"), true);

/* And DOWN again when the reader switches to a session that answers to its
   own name. The chip is a single node reused across attaches: left as it
   was, it would go on naming the previous session — the one failure mode
   worse than not drawing the handle at all. */
ctx.setMeshes([
  { name: "mesh-0826", members: [
    member("s236", "merger-r13", "worker"),
    member("s181", "s181", "reviewer"),
  ] },
]);
ctx.paintHeader("s181");
check("header chip goes down", chip.classList.contains("hidden"), true);
check("...and is emptied, not merely hidden", chip.text, "");
check("...hover too", chip.title, "");

/* No session attached at all (the home page, or between attaches). */
ctx.paintHeader(null);
check("no session → chip down", chip.classList.contains("hidden"), true);

/* ---- the three drawing sites still call this ---------------------------- */

/* Each is a one-line grep rather than a rendered row: the rail row and the
   details head are built inside functions far too tangled to slice, and what
   would actually regress is the CALL going missing in a refactor. */
for (const [what, needle] of [
  ["rail row builds a handle pill", "handleTag(s.name)"],
  ["rail row puts it in the head box", "...(handleBox ? [handleBox] : [])"],
  ["details head draws a handle chip", 'el("span", "sess-handle", hTag.text)'],
  ["details list spells out the rooms", '"mesh handle"'],
  ["header repaints on the session poll", "renderTermHandle();"],
]) {
  if (!src.includes(needle)) {
    console.error(`FAIL ${what}\n  app.js no longer contains: ${needle}`);
    failures++;
  }
}

/* The header's chip has to be repainted from BOTH polls: /api/sessions
   rebuilds the rail, /api/mesh is where the handle actually arrives, and an
   attach that lands before the first mesh answer has nothing to draw. Three
   call sites at minimum (two attach paths, and the polls). */
const calls = (src.match(/renderTermHandle\(\);/g) || []).length;
if (calls < 4) {
  console.error(
    `FAIL header repaint sites\n  got  ${calls} call(s) to renderTermHandle()` +
    `\n  want at least 4 (fresh attach, restored attach, session poll, mesh poll)`
  );
  failures++;
}

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("sesshandle_check: ok");
