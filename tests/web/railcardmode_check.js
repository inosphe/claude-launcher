/* The rail's three card densities, its per-role colours, and the child stack.

   The rail is read as a list of many sessions at once — that is what the
   dashboard is for — so a row is drawn at one of three densities (app.js
   railCardMode, the row's `data-rail-mode`):

     min      one line: the dot, the name, the role. Reviewer and oneshot
              rows: a fleet holds many of them and their cards differ only
              in the name.
     compact  the name line, the briefing one-line and the buttons. Every
              other row.
     expanded every line, plus the briefing card. The session the reader is
              on — the terminal on screen, the detail panel's subject, or a
              pinned one.

   What has to hold here:

   * the density a session gets follows from state the rail already has
     (currentName / sessName / the pin list / the role), and the role is read
     the way the row draws it — the record's field, else the first mesh
     membership's label;
   * an unknown role gets no colour class at all: a colour is a claim about
     what the session is, and an unrecognised role has nothing to claim;
   * the density is part of the height model's key, so a card that opens at
     `expanded` is measured under a key of its own instead of inheriting the
     `compact` height — that is what keeps the virtual scrollbar from
     drifting when the reader picks a card;
   * the stylesheet hides the dense lines per density with `display: none`,
     and draws a child row as a card with a spine at its own level.

   The real functions from app.js build the rows, against a stub DOM. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

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
const rolesConst = src.match(/^const RAIL_ROLE_FAMILIES = new Map\(\);[\s\S]*?^}$/m);
if (!rolesConst) throw new Error("cannot locate RAIL_ROLE_FAMILIES in app.js");
const minConst = src.match(/^const RAIL_MIN_CARD_FAMILIES = .+$/m);
if (!minConst) throw new Error("cannot locate RAIL_MIN_CARD_FAMILIES in app.js");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
function ok(what, cond, detail) {
  if (!cond) { console.error(`FAIL ${what}${detail ? "\n  " + detail : ""}`); failures++; }
}

/* ---- the density decision ---------------------------------------------- */
const ctx = {};
new Function("exports",
  rolesConst[0] + "\n" + minConst[0] + "\n"
  + "let meshes = [];\n"
  + "function sessMeshes(name) { return meshes[name] || []; }\n"
  + "let currentName = null, sessName = null, pins = [];\n"
  + "function isSessionPinned(name) { return pins.includes(name); }\n"
  + slice("railRoleFamily") + slice("railSessionRole") + slice("railCardMode")
  + `
exports.family = railRoleFamily;
exports.role = railSessionRole;
exports.mode = railCardMode;
exports.set = (v) => {
  if ("current" in v) currentName = v.current;
  if ("detail" in v) sessName = v.detail;
  if ("pins" in v) pins = v.pins;
  if ("meshes" in v) meshes = v.meshes;
};
exports.mode = railCardMode;
`)(ctx);

/* ---- role families ------------------------------------------------------
   The packaged vocabulary (daemon/mesh_roles.py) and its aliases, both of
   which reach the rail: a membership carries the resolved label, and a
   record's own `role` field may still hold whatever the caller typed. */
for (const [role, family] of [
  ["leader", "leader"], ["mod", "leader"], ["pm", "leader"],
  ["operator", "operator"], ["op", "operator"],
  ["worker", "worker"], ["coder4", ""], ["dev", "worker"],
  ["reviewer", "reviewer"], ["qa", "reviewer"], ["critic", "reviewer"],
  ["oneshot", "oneshot"], ["analyst", "oneshot"],
  ["specialist", "specialist"], ["free-role", "free"], ["no-role", "free"],
  ["", ""], ["something-else", ""],
]) {
  check(`the ${role || "(blank)"} role reads as family ${family || "(none)"}`,
        ctx.family(role), family);
}

/* A case the alias table does not have must not be guessed at: `coder4` is a
   HANDLE-shaped label a mesh can carry, not one of worker's aliases, so it
   gets no family and keeps a neutral pill. */
check("an unrecognised role gets no colour", ctx.family("coder4"), "");

/* ---- the role the row draws --------------------------------------------
   The record's field first, then the first mesh's label — the same
   precedence the row's pill uses. */
ctx.set({ meshes: { s1: [{ mesh: "m", label: "reviewer" }] } });
check("a record with no role reads its first membership",
      ctx.role({ name: "s1" }), "reviewer");
check("a record's own role wins over the membership",
      ctx.role({ name: "s1", role: "worker" }), "worker");
check("a session in no mesh and with no role reads empty",
      ctx.role({ name: "nobody" }), "");

/* ---- which density ----------------------------------------------------- */
ctx.set({ current: null, detail: null, pins: [],
          meshes: { r2: [{ mesh: "m", label: "reviewer" }] } });
check("a plain worker is a compact card",
      ctx.mode({ name: "w1", role: "worker" }), "compact");
check("a reviewer is a one-line card",
      ctx.mode({ name: "r1", role: "reviewer" }), "min");
check("a oneshot child is a one-line card",
      ctx.mode({ name: "o1", role: "oneshot" }), "min");
check("a reviewer read off a membership is a one-line card too",
      ctx.mode({ name: "r2" }), "min");

ctx.set({ current: "r1" });
check("the session on screen is expanded whatever its role",
      ctx.mode({ name: "r1", role: "reviewer" }), "expanded");
ctx.set({ current: null, detail: "w1" });
check("the detail panel's subject is expanded",
      ctx.mode({ name: "w1", role: "worker" }), "expanded");
ctx.set({ current: null, detail: null, pins: ["w2"] });
check("a pinned session is expanded",
      ctx.mode({ name: "w2", role: "worker" }), "expanded");
check("and pinning one session does not expand its neighbour",
      ctx.mode({ name: "w3", role: "worker" }), "compact");
ctx.set({ pins: [] });

/* ---- the height model keys the density ---------------------------------
   Without this a card opened at `expanded` is planned with the `compact`
   height it was measured at, and every offset below it is wrong by the
   difference until something else remeasures it. */
const keyCtx = {};
new Function("exports",
  slice("railRowMode") + "\n" + slice("railRowKey")
  + "\nexports.key = railRowKey;")(keyCtx);
const key = keyCtx.key;
ok("a compact row's key and an expanded row's key differ",
   key({ type: "session", session: { name: "s1" }, mode: "compact" })
   !== key({ type: "session", session: { name: "s1" }, mode: "expanded" }),
   key({ type: "session", session: { name: "s1" }, mode: "compact" }));
check("a row with no mode keyed as a compact card, which is what it draws",
      key({ type: "session", session: { name: "s1" } }),
      key({ type: "session", session: { name: "s1" }, mode: "compact" }));
check("two sessions at one density keep separate keys",
      key({ type: "session", session: { name: "a" }, mode: "min" }),
      "s:a@min");

/* And the ESTIMATE a not-yet-measured row is planned with is sampled per
   density. One mean over every card would plan a rail of one-line reviewers
   as though each were a compact card — the scrollbar would be several times
   the fleet's height and every reveal would land in the wrong place. */
const estCtx = {};
new Function("exports", "document",
  "const RAIL_ROW_EST_PX = 64;"
  + "\nconst RAIL_ESTIMATE_SAMPLE = 32;"
  + "\nconst railRowPx = new Map();"
  + "\nconst railSeen = { rows: 0, px: 0, closed: false };"
  + "\nconst railSeenKeys = new Set();"
  + "\nconst railSeenByMode = new Map();"
  + slice("railSeenFor") + slice("railEstimatePx") + slice("railRowMode")
  + slice("railRowKey") + slice("railRowPxOf") + slice("railMeasureRows")
  + `
exports.api = { railEstimatePx, railRowPxOf, railMeasureRows, railSeen,
                railSeenFor, railRowKey };`)(estCtx, { createElement: () => ({}) });
const est = estCtx.api;

const measure = (rows) => est.railMeasureRows({
  querySelectorAll: (sel) => (sel === "li[data-name]" ? rows : []),
});
const drawn = (name, mode, px) => ({
  dataset: { name, railMode: mode },
  getBoundingClientRect: () => ({ height: px }),
});

check("with nothing measured, every density is planned at the stylesheet's figure",
      [est.railEstimatePx("compact"), est.railEstimatePx("min"),
       est.railEstimatePx("expanded")], [64, 64, 64]);

measure(Array.from({ length: 8 }, (_, i) => drawn(`c${i}`, "compact", 120)));
measure(Array.from({ length: 8 }, (_, i) => drawn(`m${i}`, "min", 30)));
measure(Array.from({ length: 8 }, (_, i) => drawn(`e${i}`, "expanded", 400)));

check("each density's estimate is its own rows' mean",
      [est.railEstimatePx("compact"), est.railEstimatePx("min"),
       est.railEstimatePx("expanded")], [120, 30, 400]);
check("an unmeasured one-line row is planned one line tall, not a card tall",
      est.railRowPxOf({ type: "session", session: { name: "new" }, mode: "min" }), 30);
check("an unmeasured compact row is planned at the compact mean",
      est.railRowPxOf({ type: "session", session: { name: "new" }, mode: "compact" }), 120);
check("an unmeasured expanded row is planned at the expanded mean",
      est.railRowPxOf({ type: "session", session: { name: "new" }, mode: "expanded" }), 400);
check("a row that says no density is planned as the compact card it draws",
      est.railRowPxOf({ type: "session", session: { name: "new" } }), 120);

/* ---- the stylesheet ---------------------------------------------------- */
const plain = css.replace(/\/\*[\s\S]*?\*\//g, "");
function rule(sel) {
  const m = plain.match(new RegExp(
    sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}"));
  return m ? m[1] : "";
}

/* Every dense line is hidden on a one-line card, and the ones that remain
   are the name line and the role. Nothing is left unbuilt in the DOM — the
   same row becomes an expanded card when the reader picks it, and rebuilding
   the rail to reveal a line would lose focus, hover and the scroll position
   for a card already in hand. */
for (const line of [".rail-cwd", ".rail-ctx-line", ".rail-ctx-tokens",
                    ".rail-tps-line", ".rail-usage-line", ".rail-seen",
                    ".rail-life", ".rail-brief", ".rail-note", ".rail-beads",
                    ".rail-status-checks", ".rail-commits", ".rail-actions",
                    ".sess-cflow", ".sess-cflow2", ".sess-brief", ".meta"]) {
  const body = plain.match(new RegExp(
    `#session-list li\\[data-rail-mode="min"\\][^{]*\\${line}[^{]*\\{([^}]*)\\}`));
  ok(`a one-line card does not draw ${line}`,
     body && /display:\s*none/.test(body[1]),
     body ? body[1].trim() : "no rule");
}

/* The compact card keeps the one-line summary and the buttons, and drops the
   rest of the detail lines. */
for (const line of [".rail-cwd", ".rail-ctx-line", ".rail-seen",
                    ".sess-brief", ".rail-status-checks"]) {
  const body = plain.match(new RegExp(
    `#session-list li\\[data-rail-mode="compact"\\][^{]*\\${line}[^{]*\\{([^}]*)\\}`));
  ok(`a compact card does not draw ${line}`,
     body && /display:\s*none/.test(body[1]),
     body ? body[1].trim() : "no rule");
}
ok("a compact card keeps the briefing one-line — it is the card's summary",
   !/#session-list li\[data-rail-mode="compact"\][^{]*\.rail-brief[^{]*\{/.test(plain),
   "a rule hides .rail-brief on a compact card");
ok("a compact card keeps the buttons line",
   !/#session-list li\[data-rail-mode="compact"\][^{]*\.rail-actions[^{]*\{/.test(plain),
   "a rule hides .rail-actions on a compact card");

/* A run that wants the reader's keyboard survives every density: hiding it
   on the short cards is how a gated session goes unnoticed. */
ok("a gated cflow badge is drawn even on a compact card",
   /#session-list li\[data-rail-mode="compact"\] \.sess-cflow:not\(\.gated\)/.test(plain),
   "the compact rule does not exempt .gated");

/* The rail has no row fold any more: the toggle's rule is gone with it. */
check("the briefing toggle has no rule on the rail",
      rule("#session-list .sess-brief-toggle"), "");

/* ---- the child stack ---------------------------------------------------
   A child row is a card on the card above it, with a spine at its own level.
   The spine is placed from `data-rail-depth`, which the row builder sets
   alongside the indent (capped at four levels, like the indent). */
const child = rule("#session-list li.child");
ok("a child row is drawn as a card", /background:/.test(child),
   child.trim().slice(0, 80));
ok("...with its own rim", /box-shadow:/.test(child), child.trim().slice(0, 80));
ok("...and a rounded right edge", /border-radius:/.test(child), child.trim().slice(0, 80));
ok("the tick that hangs it off its parent is still drawn",
   /content:\s*"└"/.test(rule("#session-list li.child::before")));
const spine = rule("#session-list li.child::after");
ok("a child row draws a spine at its level",
   /position:\s*absolute/.test(spine) && /width:/.test(spine),
   spine.trim().slice(0, 80));
ok("...that runs the height of the row",
   /top:\s*0/.test(spine) && /bottom:\s*0/.test(spine), spine.trim().slice(0, 80));
for (const depth of [2, 3, 4]) {
  const r = rule(`#session-list li.child[data-rail-depth="${depth}"]::after`);
  ok(`a depth-${depth} child's spine moves out with its indent`,
     /left:\s*\d+px/.test(r), r.trim() || "no rule");
}
/* The four level positions must be distinct, or two depths would share a
   spine and the nesting would read as one level. */
{
  const lefts = [1, 2, 3, 4].map((d) => {
    const m = (d === 1 ? spine : rule(`#session-list li.child[data-rail-depth="${d}"]::after`))
      .match(/left:\s*(\d+)px/);
    return m ? Number(m[1]) : null;
  });
  ok("each depth's spine sits at its own offset",
     lefts.every((v) => v !== null) && new Set(lefts).size === lefts.length,
     lefts.join(","));
}

/* ---- the role pills ----------------------------------------------------
   One colour per family, and only the colours change: the pill's size, floor
   and ellipsis stay the shared rule, or a coloured pill would measure
   differently from a plain one and the rail's line budget would depend on
   which role happened to be in view. */
for (const family of ["leader", "operator", "worker", "reviewer", "oneshot",
                      "specialist", "free"]) {
  const body = rule(`#session-list .mesh-role.role-${family}`);
  ok(`the ${family} role has a colour of its own`, /color:/.test(body),
     body.trim() || "no rule");
  ok(`...that does not resize the pill (${family})`,
     !/padding:|font-size:|min-width:/.test(body), body.trim());
}
{
  const colours = ["leader", "operator", "worker", "reviewer", "oneshot",
                   "specialist", "free"]
    .map((f) => (rule(`#session-list .mesh-role.role-${f}`).match(/color:\s*([^;]+)/) || [])[1]);
  ok("no two families share a text colour",
     colours.every(Boolean) && new Set(colours).size === colours.length,
     colours.join(" | "));
}

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railcardmode_check: ok");
