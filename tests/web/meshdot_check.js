/* A mesh member's dot carries the session dot's grade (meshDotGrade and
   friends in app.js).

   The mesh pages draw a member's dot from its reachability word alone, so a
   session working flat out and one parked on a prompt looked the same there
   while the rail told them apart. The member dot now borrows the rail
   record's grade — but only when that record agrees with the member's word,
   and never for a remote member, whose liveness this daemon does not read. */
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

const code = "let sessionsCache = [];\n" +
  constLine("DOT_BUSY_LEVELS") + constLine("DOT_TOOL_LEVELS") + constLine("DOT_IDLE_AGES") +
  ["fmtAge", "seenAgo", "dotGrade", "dotTitle", "meshDotClass",
   "meshMemberRecord", "meshDotGrade", "meshDotTitle"].map(slice).join("\n");
const api = {};
new Function("exports", `${code}
  exports.setSessions = (list) => { sessionsCache = list; };
  Object.assign(exports, { meshMemberRecord, meshDotGrade, meshDotTitle });`)(api);

let failures = 0;
function check(label, ok) {
  console.log(`${ok ? "ok  " : "FAIL"} ${label}`);
  if (!ok) failures++;
}
const ago = (secs) => new Date(Date.now() - secs * 1000).toISOString();

api.setSessions([
  { name: "s1", status: "busy", moved_rows: 300, tool_calls: 2 },
  { name: "s2", status: "idle", last_activity_at: ago(4000) },
  { name: "s3", status: "idle", last_activity_at: ago(10) },
  { name: "s4", status: "idle", last_activity_at: ago(9000) },
]);

check("a busy local member takes the rail's busy level",
  api.meshDotGrade({ session: "s1", reachability: "busy" }) === " lvl-3");
check("an idle local member takes the rail's idle age",
  api.meshDotGrade({ session: "s2", reachability: "idle" }) === " age-2");
check("a member idle only seconds is not aged",
  api.meshDotGrade({ session: "s3", reachability: "idle" }) === "");
check("a record that disagrees with the member's word grades nothing",
  api.meshDotGrade({ session: "s4", reachability: "busy" }) === "");
check("a remote member is never graded, even when a local record shares its name",
  api.meshDotGrade({ session: "s1", reachability: "remote-connected" }) === "");
check("an exited or missing member is never graded",
  api.meshDotGrade({ session: "s1", reachability: "exited" }) === "" &&
  api.meshDotGrade({ session: "s1", reachability: "missing" }) === "");
check("a member with no rail record is left plain",
  api.meshDotGrade({ session: "nobody", reachability: "busy" }) === "");
check("the member tooltip is the session dot's",
  /busy — 300 screen rows moved in the last minute, 2 tool calls in the last 5m/.test(
    api.meshDotTitle({ session: "s1", reachability: "busy" })));
check("no record, no tooltip (the caller falls back to the word)",
  api.meshDotTitle({ session: "nobody", reachability: "idle" }) === "");

/* ---- every drawing of a member's dot takes the grade ---- */
const renderMesh = slice("renderMesh");
check("the topology's agent ring is graded",
  /"mesh-agent " \+ meshDotClass\(m\.reachability\) \+ \(typeof meshDotGrade === "function" \? meshDotGrade\(m\)/.test(src));
{
  // Each call site (the call and the line after it, where a class string
  // continues) must add the grade; a new drawing of a member's dot that
  // forgets it would otherwise keep the bare word unnoticed.
  const lines = src.split("\n");
  const bare = [];
  lines.forEach((line, i) => {
    if (!line.includes("meshDotClass(") || line.includes("function meshDotClass(")) return;
    if (!(line + lines[i + 1]).includes("meshDotGrade(")) bare.push(i + 1);
  });
  check(`every meshDotClass call site adds the grade (bare at lines: ${bare.join(", ") || "none"})`,
    bare.length === 0);
}
check("the roster row's dot is graded and titled",
  /meshDotGrade\(m\)/.test(renderMesh) && /dot\.title = \(typeof meshDotTitle/.test(renderMesh));
check("the owed list's dot is graded",
  /meshDotGrade\(r\)/.test(src));
check("the flow card's dot is graded",
  /meshDotGrade\(member\)/.test(slice("flowCardSvg")));

/* ---- and the stylesheet shades each grade on each shape ---- */
for (const g of ["busy.lvl-1", "busy.lvl-2", "busy.lvl-3", "idle.age-1", "idle.age-2", "idle.age-3"]) {
  check(`style.css shades .mesh-agent.${g}`, css.includes(`.mesh-agent.${g} .mesh-agent-disc {`));
  check(`style.css shades .flow-dot.${g}`, css.includes(`.flow-dot.${g} {`));
}

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("all mesh dot checks passed");
