/* The status dot's grade — how hard a busy session works, how long an idle
   one has sat (dotGrade and friends in app.js).

   The dot is drawn in six places (rail, grid, tabs, home card, mobile bar,
   the children list), and the defect this guards against is one of them
   quietly going back to the bare status word: the dot keeps its colour and
   nothing looks wrong, it just stops saying anything the others say. So the
   checks pin the grade itself, the stylesheet rules that make each grade
   visible, and the wiring that keeps a kept rail row's dot current without
   rebuilding the rail on every poll. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

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
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .+$`, "m"));
  if (!m) throw new Error(`cannot locate ${name} in app.js`);
  return m[0] + "\n";
}

const code = constLine("DOT_BUSY_LEVELS") + constLine("DOT_TOOL_LEVELS") +
  constLine("DOT_IDLE_AGES") +
  ["fmtAge", "seenAgo", "dotGrade", "dotClassOf", "dotTitle", "applyDotGrade"]
    .map(slice).join("\n");
const api = {};
new Function("exports", `${code}
  Object.assign(exports, { dotGrade, dotClassOf, dotTitle, applyDotGrade,
                           DOT_BUSY_LEVELS, DOT_TOOL_LEVELS, DOT_IDLE_AGES });`)(api);

let failures = 0;
function check(label, ok) {
  console.log(`${ok ? "ok  " : "FAIL"} ${label}`);
  if (!ok) failures++;
}
const ago = (secs) => new Date(Date.now() - secs * 1000).toISOString();

/* ---- busy: graded by rows moved in the last minute ---- */
check("a busy session parked on a tool call (0 rows) is level 1",
  api.dotGrade({ status: "busy", moved_rows: 0 }) === " lvl-1");
check("just under the first bound stays level 1",
  api.dotGrade({ status: "busy", moved_rows: api.DOT_BUSY_LEVELS[0] - 1 }) === " lvl-1");
check("the first bound is level 2",
  api.dotGrade({ status: "busy", moved_rows: api.DOT_BUSY_LEVELS[0] }) === " lvl-2");
check("a streaming reply past the second bound is level 3",
  api.dotGrade({ status: "busy", moved_rows: api.DOT_BUSY_LEVELS[1] + 500 }) === " lvl-3");
check("a daemon that sends no moved_rows keeps the plain busy dot",
  api.dotGrade({ status: "busy" }) === "");

/* ---- busy: tool calls in the last five minutes, the higher level wins ---- */
check("a quiet screen with many tool calls is graded by the tool calls",
  api.dotGrade({ status: "busy", moved_rows: 0, tool_calls: api.DOT_TOOL_LEVELS[1] }) === " lvl-3");
check("a few tool calls lift a still screen to level 2",
  api.dotGrade({ status: "busy", moved_rows: 0, tool_calls: api.DOT_TOOL_LEVELS[0] }) === " lvl-2");
check("tool calls never lower what the screen earned",
  api.dotGrade({ status: "busy", moved_rows: api.DOT_BUSY_LEVELS[1], tool_calls: 0 }) === " lvl-3");
check("tool calls alone grade when moved_rows is absent",
  api.dotGrade({ status: "busy", tool_calls: 1 }) === " lvl-1");
check("the busy title names both readings",
  /busy — 5 screen rows moved in the last minute, 9 tool calls in the last 5m/.test(
    api.dotTitle({ status: "busy", moved_rows: 5, tool_calls: 9 })));
check("the busy title leaves out a reading the daemon did not send",
  api.dotTitle({ status: "busy", tool_calls: 2 }) === "busy — 2 tool calls in the last 5m");

/* ---- idle: graded by the age of the last real screen change ---- */
check("an idle session that moved a minute ago is not aged",
  api.dotGrade({ status: "idle", last_activity_at: ago(60) }) === "");
check("past the first age bound it is age 1",
  api.dotGrade({ status: "idle", last_activity_at: ago(api.DOT_IDLE_AGES[0] + 5) }) === " age-1");
check("past the second it is age 2",
  api.dotGrade({ status: "idle", last_activity_at: ago(api.DOT_IDLE_AGES[1] + 5) }) === " age-2");
check("past the third it is age 3",
  api.dotGrade({ status: "idle", last_activity_at: ago(api.DOT_IDLE_AGES[2] + 5) }) === " age-3");
check("an idle session with no reading (daemon restarted) is not aged",
  api.dotGrade({ status: "idle", last_activity_at: null }) === "");
check("idle readings do not grade a busy dot and vice versa",
  api.dotGrade({ status: "busy", last_activity_at: ago(99999) }) === "" &&
  api.dotGrade({ status: "idle", moved_rows: 999 }) === "");

/* ---- exited / paused / starting are left as they were ---- */
check("an exited dot carries no grade",
  api.dotClassOf({ status: "exited", moved_rows: 500 }) === "dot exited");
check("a paused record keeps its paused class",
  api.dotClassOf({ status: "exited", paused_at: "2026-09-23T00:00:00Z" }) === "dot exited paused");
check("paused is only laid over exited",
  api.dotClassOf({ status: "busy", paused_at: "x", moved_rows: 0 }) === "dot busy lvl-1");
check("a status override grades by the override",
  api.dotClassOf({ status: "busy", moved_rows: 0, paused_at: "x" }, "exited") === "dot exited paused");
check("no record at all is the unknown dot",
  api.dotClassOf(null) === "dot unknown");

/* ---- the tooltip names the number the grade came from ---- */
check("a busy title prints the row count",
  /\b123 screen rows moved in the last minute/.test(api.dotTitle({ status: "busy", moved_rows: 123 })));
check("an idle title prints the age",
  /idle — the screen last moved 40m ago/.test(api.dotTitle({ status: "idle", last_activity_at: ago(2400) })));

/* ---- applyDotGrade touches the node only on a change ---- */
{
  let writes = 0;
  let cls = "";
  const dot = {
    get className() { return cls; },
    set className(v) { writes++; cls = v; },
    title: "",
  };
  const s = { status: "busy", moved_rows: 300 };
  api.applyDotGrade(dot, s);
  api.applyDotGrade(dot, s);
  check("applyDotGrade writes the class once for an unchanged session",
    cls === "dot busy lvl-3" && writes === 1);
  api.applyDotGrade(dot, { status: "idle", last_activity_at: ago(4000) });
  check("and rewrites it when the grade moves", cls === "dot idle age-2" && writes === 2);
}

/* ---- every grade has a stylesheet rule ---- */
for (const g of ["busy.lvl-1", "busy.lvl-2", "busy.lvl-3", "idle.age-1", "idle.age-2", "idle.age-3"]) {
  check(`style.css shades .dot.${g}`, css.includes(`.dot.${g} {`));
}

/* ---- wiring ---- */
const render = slice("refreshSessions");
check("the rail signature leaves moved_rows out (it moves on every poll)",
  /key === "moved_rows"/.test(render));
check("the rail signature leaves tool_calls out too",
  /key === "tool_calls"/.test(render));
check("refreshRailSeen re-shades a kept row's dot",
  /applyDotGrade\(/.test(slice("refreshRailSeen")));
check("the session grid's signature carries the grade",
  /dotGrade\(s\)/.test(slice("renderSessionGrid")));
check("the session tabs' signature carries the grade",
  /dotGrade\(rec\)/.test(slice("renderSessionTabs")));

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("all dot grade checks passed");
