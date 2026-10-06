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
   slice("cflow2TreeRows"), slice("wfOptionLabel"), slice("spawnWorkflowEntry"),
   slice("spawnWorkflowAdmits"), slice("spawnRankWorkflows")].join("\n") +
  "\nreturn { cflowVersionTag, cflowPickValue, cflowPickBody, cflowPickEntry," +
  " cflow2TreeRows, wfOptionLabel, spawnRankWorkflows };"
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

console.log(failures ? `\n${failures} failure(s)` : "cflow2view_check: all assertions passed");
process.exit(failures ? 1 : 0);
