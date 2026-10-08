/* cflow v2 beside v1 in the dashboard, run against the real functions from
   app.js (docs/cflow-v2.md sections 9-10).

   /api/cflow/workflows lists both formats, told apart by `version`, with v1
   entries marked `deprecated`. Pinned here: the label marker each picker
   appends (cflowVersionTag, wfOptionLabel), the value a v2 entry is offered
   under when a v1 workflow shares its name and what that value sends
   (cflowPickValue / cflowPickBody / cflowPickEntry), the role ranking keeping
   v1 as the auto-pick (spawnRankWorkflows), and the indented chart rows the
   v2 run view draws (cflow2TreeRows). */
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
function sliceConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`cannot locate const ${name} in app.js`);
  return src.slice(start, src.indexOf("};", start) + 2);
}

const fns = new Function(
  [sliceConst("CFLOW2_ICONS"), slice("cflowVersionTag"), slice("cflowPickValue"),
   slice("cflowPickBody"), slice("cflowPickEntry"), slice("cflow2NodeText"),
   slice("cflow2NodeBits"), slice("cflow2TreeRows"), slice("cflow2TreeOpen"),
   slice("cflow2Reports"), slice("wfOptionLabel"), slice("spawnWorkflowEntry"),
   slice("spawnWorkflowAdmits"), slice("spawnRankWorkflows"),
   slice("cflow2RunActions"), slice("cflow2ForceOutcomes")].join("\n") +
  "\nreturn { cflowVersionTag, cflowPickValue, cflowPickBody, cflowPickEntry," +
  " cflow2TreeRows, wfOptionLabel, spawnRankWorkflows, cflow2RunActions," +
  " cflow2ForceOutcomes, cflow2TreeOpen, cflow2NodeBits, cflow2Reports };"
)();

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- markers ---- */
check("a v2 entry is marked v2", fns.cflowVersionTag({ name: "a", version: 2 }), " · v2");
check("a deprecated v1 entry says so",
      fns.cflowVersionTag({ name: "a", version: 1, deprecated: true }),
      " · v1 (deprecated)");
check("an older daemon's entry carries no marker", fns.cflowVersionTag({ name: "a" }), "");
check("a bare name carries no marker", fns.cflowVersionTag("a"), "");
check("the start panel's label carries the marker before the description",
      fns.wfOptionLabel({ name: "w", version: 1, deprecated: true, description: "two steps" }),
      "w · v1 (deprecated) — two steps");
check("...and without a description",
      fns.wfOptionLabel({ name: "w", version: 2 }), "w · v2");

/* ---- values and what they send ---- */
const LIST = [
  { name: "demo", version: 1, deprecated: true },
  { name: "demo", version: 2 },
  { name: "pick", version: 2 },
  { name: "old", version: 1, deprecated: true },
];
check("a v2 entry sharing a v1 name is offered as v2:<name>",
      fns.cflowPickValue(LIST[1], LIST), "v2:demo");
check("the v1 one keeps the bare name", fns.cflowPickValue(LIST[0], LIST), "demo");
check("a v2-only name stays bare", fns.cflowPickValue(LIST[2], LIST), "pick");
check("v2:<name> sends workflow_version 2",
      fns.cflowPickBody("v2:demo", LIST), { workflow: "demo", workflow_version: 2 });
check("a bare v2-only name sends workflow_version 2 too",
      fns.cflowPickBody("pick", LIST), { workflow: "pick", workflow_version: 2 });
check("a bare shared name is the v1 one",
      fns.cflowPickBody("demo", LIST), { workflow: "demo" });
check("a v1 name sends no version", fns.cflowPickBody("old", LIST), { workflow: "old" });
check("the entry behind v2:demo is the v2 one",
      fns.cflowPickEntry("v2:demo", LIST).version, 2);
check("the entry behind demo is the v1 one",
      fns.cflowPickEntry("demo", LIST).deprecated, true);

/* ---- the role ranking: v2 offered, v1 auto-picked ---- */
const ROLE = [
  { name: "improv-worker", default_role: "worker", version: 1, deprecated: true },
  { name: "improv-worker", default_role: "worker", version: 2 },
  { name: "aaa-v2", default_role: "worker", version: 2 },
];
const ranked = fns.spawnRankWorkflows(ROLE, "worker");
check("both formats are offered, marked",
      ranked.options.map((o) => [o.name + o.tag, o.value]),
      [["aaa-v2 · v2", "aaa-v2"],
       ["improv-worker · v1 (deprecated)", "improv-worker"],
       ["improv-worker · v2", "v2:improv-worker"]]);
check("the auto-pick is the v1 workflow", ranked.auto, "improv-worker");
check("a v2 workflow is auto-picked only when it is the role's only default",
      fns.spawnRankWorkflows([ROLE[2]], "worker").auto, "aaa-v2");
check("an entry with no version reads as v1 without a marker",
      fns.spawnRankWorkflows([{ name: "x" }], "").options[0].tag, "");

/* ---- the chart tree of a v2 run ---- */
const CHART = {
  root: "n0",
  nodes: {
    n0: { id: "n0", type: "seq", children: ["n1", "n2"], roles: ["", ""] },
    n1: { id: "n1", type: "choose", chooser: "user", options: ["left", "right"],
          children: ["n3", "n4"], roles: ["option left", "option right"] },
    n2: { id: "n2", type: "loop", mode: "while", max: 2, guard: "true",
          children: ["n5"], roles: ["do"] },
    n3: { id: "n3", type: "task", label: "a", title: "Step A", children: [], roles: [] },
    n4: { id: "n4", type: "task", label: "b", children: [], roles: [] },
    n5: { id: "n5", type: "task", label: "c", executor: ["subagent", "inline"],
          children: [], roles: [] },
  },
};
const DETAIL = {
  instances: [{ instance: "n0@1", node: "n0", await: null },
              { instance: "n1@2", node: "n1", await: "choose" }],
  visits: { a: 3, n1: 1 },
};
const rows = fns.cflow2TreeRows(CHART, DETAIL);
check("pre-order, indented by depth, with the role under the parent",
      rows.map((r) => [r.depth, r.role, r.text]),
      [[0, "", "seq"],
       [1, "", "choose (user)"],
       [2, "option left", "a — Step A"],
       [2, "option right", "b"],
       [1, "", "while (max 2) true"],
       [2, "do", "c ⇢ subagent/inline"]]);
check("live instances and what they wait on",
      rows.map((r) => [r.id, r.active, r.awaiting]).filter((r) => r[1]),
      [["n0", true, ""], ["n1", true, "choose"]]);
check("visits are read by label, else id",
      rows.map((r) => r.visits), [0, 1, 3, 0, 0, 0]);
check("a chart with a cycle through `on` is still walked once",
      fns.cflow2TreeRows({ root: "x", nodes: { x: { id: "x", type: "seq",
        children: ["x"], roles: [""] } } }, null).length, 1);

check("a force_target node is flagged on its row",
      fns.cflow2TreeRows({ root: "r", nodes: {
        r: { id: "r", type: "seq", children: ["t", "u"], roles: ["", ""] },
        t: { id: "t", type: "task", label: "t", force_target: true },
        u: { id: "u", type: "task", label: "u" } } }, null).map((r) => [r.id, r.force]),
      [["r", false], ["t", true], ["u", false]]);

check("rows carry their parent and child count",
      rows.map((r) => [r.id, r.parent, r.kids]),
      [["n0", null, 2], ["n1", "n0", 2], ["n3", "n1", 0], ["n4", "n1", 0],
       ["n2", "n0", 1], ["n5", "n2", 0]]);
check("a node in parts: a task's label and title, a loop's mode and guard",
      [fns.cflow2NodeBits(CHART.nodes.n3), fns.cflow2NodeBits(CHART.nodes.n2),
       fns.cflow2NodeBits(CHART.nodes.n5).executor],
      [{ head: "a", sub: "Step A", executor: "" },
       { head: "", sub: "while ≤ 2 · true", executor: "" }, "subagent/inline"]);

/* ---- which rows are open ---- */
check("the root and the live path are open, the rest folded",
      [...fns.cflow2TreeOpen(rows, null, {})].sort(), ["n0"]);
const liveRows = fns.cflow2TreeRows(CHART, { instances: [{ instance: "x", node: "n5", await: "task" }] });
check("a live node opens its ancestors",
      [...fns.cflow2TreeOpen(liveRows, null, {})].sort(), ["n0", "n2"]);
check("the selected node's ancestors open too",
      [...fns.cflow2TreeOpen(rows, "n4", {})].sort(), ["n0", "n1"]);
check("a person's toggles win both ways",
      [...fns.cflow2TreeOpen(liveRows, null, { n2: false, n1: true })].sort(), ["n0", "n1"]);

/* ---- the agent's reports ---- */
check("reported events, newest first, with the task's own name",
      fns.cflow2Reports([
        { ts: "2026-10-08T10:00:00+00:00", event: "reported", node: "round/item/work",
          outcome: "ok", text: "did it", value: { files: [] } },
        { ts: "2026-10-08T10:01:00+00:00", event: "entered", node: "x" },
        { ts: "2026-10-08T10:02:00+00:00", event: "reported", node: "commit", outcome: "fail",
          by: "user" },
      ]).map((r) => [r.label, r.outcome, r.by, r.ts, r.text]),
      [["commit", "fail", "user", "2026-10-08 10:02:00", ""],
       ["work", "ok", "", "2026-10-08 10:00:00", "did it"]]);

/* ---- the run page's presses (the v1 bar's counterparts) ---- */
check("a live run offers nudge and abort",
      fns.cflow2RunActions({ run: { status: "running" } }, {}), ["nudge", "abort"]);
check("a run waiting on a person is still live",
      fns.cflow2RunActions({ run: { status: "waiting_approval" } }, {}), ["nudge", "abort"]);
check("a finished run offers archive only",
      fns.cflow2RunActions({ run: { status: "failed" } }, {}), ["archive"]);
check("the session fold keeps abort and archive to the run page",
      fns.cflow2RunActions({ run: { status: "running" } }, { archive: false }), ["nudge"]);
check("nothing for a finished run in the fold",
      fns.cflow2RunActions({ run: { status: "done" } }, { archive: false }), []);

/* ---- a person's forced outcome ---- */
check("declared outcomes with their required fields, then fail",
      fns.cflow2ForceOutcomes({ returns: { ok: { n: "int", note: "str?" }, blocked: {} } }),
      [{ name: "ok", required: ["n"] }, { name: "blocked", required: [] },
       { name: "fail", required: [] }]);
check("a declared fail is not listed twice",
      fns.cflow2ForceOutcomes({ returns: { ok: {}, fail: { why: "str" } } }).map((o) => o.name),
      ["ok", "fail"]);
check("no returns reads as a bare ok",
      fns.cflow2ForceOutcomes({}).map((o) => o.name), ["ok", "fail"]);

console.log(failures ? `\n${failures} failure(s)` : "cflow2view_check: all assertions passed");
process.exit(failures ? 1 : 0);
