/* A spawn request's Manual form, and the card that opens it (claunch-8x6v2).

   Two defects, one file:

   1. The Manual form seeded only a handful of rows (name, task, project,
      profile, workflow, issue, and for a child mesh/role/worktree/workspace).
      Every other row opened on what the browser remembered — the localStorage
      draft for the New tab, the launch recall for the child tab — so the
      agent's own answers showed up as the person's last form. The seed now
      carries every answer the spec does, and openSessionModal applies the
      remembered values FIRST and the seed over them, which is the order the
      report asks for.

   2. The card's `workspace` row carried the directory, the checkout and the
      "(new worktree)" suffix in one cell, so a workspace read as if it were
      the worktree. They are two rows now: `workspace` is the registered
      directory, `worktree` is the git checkout inside it.

   3. The seed writes rows by firing the form's own change events, and the
      form saves its draft on every change — so the request's answers became
      the operator's standing answers. `saveNewFormDraft` now returns while
      `_seeding` is set. The last section exercises that guard on the sliced
      function (stub form, stub localStorage), so it fails when the guard is
      removed.

   Runs the real functions sliced out of app.js, like the other *_check.js
   harnesses: a stub DOM is unnecessary for `spawnRequestSeed` and
   `spawnGateSpecFact`, which are pure, and the ordering claim is checked on
   the source because the alternative is a Playwright run. The draft guard
   needs a form and a localStorage, so those two are stubbed and the real
   `saveNewFormDraft` runs against them — no browser involved. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

let failures = 0;
/* Key order is not the claim, the set of answers is: sort object keys
   before comparing so the harness does not pin an accidental order. */
function canon(value) {
  if (Array.isArray(value)) return value.map(canon);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((k) => [k, canon(value[k])]));
  }
  return value;
}
function check(label, got, want) {
  const g = JSON.stringify(canon(got)), w = JSON.stringify(canon(want));
  if (g === w) return;
  failures++;
  console.error(`FAIL ${label}\n  got  ${g}\n  want ${w}`);
}

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

function constant(name) {
  const m = src.match(new RegExp(`const ${name} = (\\[[\\s\\S]*?\\]);`));
  if (!m) throw new Error(`cannot locate ${name} in app.js`);
  return JSON.parse(m[1].replace(/,\s*\]/, "]"));
}

const spawnRequestSeed = eval(`(${slice("spawnRequestSeed")})`);
const spawnGateSpecFact = eval(`(${slice("spawnGateSpecFact")})`);

/* ---- the seed carries every answer the spec does ---------------------- */
const CHILD = {
  name: "kid", task: "write the parser tests", context: "the issue has detail",
  project: "alpha", profile: "p", harness: "codex", model: "small",
  effort: "high", args: ["--verbose"], null_token: true,
  mesh: "g5", role: "worker", subroles: ["reviewer"],
  workflow: "improv-worker-v2", cflow_inputs: { goal: "g" }, prompt_packs: ["beads"],
  workspace: "alpha", cwd: "/repo/alpha", issue: "alpha-7", issue_text: "title",
  worktree: "kid-wt", branch_from: "master", rebase_onto: "",
  beads: false,
};
const childSeed = spawnRequestSeed(CHILD, { scope: "child" });
check("a child's spec reaches every row it has", childSeed, {
  name: "kid", task: "write the parser tests", context: "the issue has detail",
  project: "alpha", profile: "p", harness: "codex", model: "small",
  effort: "high", args: ["--verbose"], null_token: true,
  mesh: "g5", role: "worker", subroles: ["reviewer"],
  workflow: "improv-worker-v2", workflow_version: 2,
  cflow_inputs: { goal: "g" }, prompt_packs: ["beads"],
  workspace: "alpha", cwd: "/repo/alpha", issue: "alpha-7",
  issue_text: "title", beads: false,
  worktree: true, worktree_name: "kid-wt", branch_from: "master",
});

/* A child body's empty worktree is "none of its own" (it works in the
   asker's checkout); a root body's empty worktree is "a new one, name
   generated" — the same spelling, two answers. */
check("a child's empty worktree seeds no checkout",
      spawnRequestSeed({ worktree: "" }, { scope: "child" }).worktree, false);
check("a root body's empty worktree seeds a new one",
      spawnRequestSeed({ worktree: "" }, { scope: "root" }).worktree, true);
check("`true` seeds a new one with the generated name",
      spawnRequestSeed({ worktree: true }, { scope: "child" }),
      { name: "", task: "", worktree: true });
check("a child's named worktree rides as its name",
      spawnRequestSeed({ worktree: "w1" }, { scope: "child" }),
      { name: "", task: "", worktree: true, worktree_name: "w1" });
// Name and task are seeded even when empty: the request's answer is "none
// of these", never the form's previous session.
check("an anonymous request clears the form's name and task",
      spawnRequestSeed({ issue: "a-1" }, { scope: "root" }),
      { name: "", task: "", issue: "a-1" });

/* A row the request did not answer stays out of the seed: that is the row
   the browser's stored answer is allowed to fill. */
check("unanswered rows are not seeded",
      spawnRequestSeed({ task: "t" }, { scope: "root" }),
      { name: "", task: "t" });
check("root builders do not seed a child's mesh/role",
      spawnRequestSeed({ mesh: "g5", role: "worker" }, { scope: "root" }),
      { name: "", task: "" });

/* `"-"` is a declined workflow, which the form spells as the empty pick. */
check("a declined workflow seeds the empty pick",
      spawnRequestSeed({ workflow: "-" }, { scope: "root" }).workflow, "");

/* ---- the card's two rows ---------------------------------------------- */
const FACTS = constant("SPAWN_GATE_FACTS");
check("workspace and worktree are separate rows",
      FACTS, ["project", "profile", "workspace", "worktree", "role", "workflow"]);

const rootSpec = { profile: "p", cwd: "/repo/alpha", worktree: "" };
check("a root spec's workspace is the directory alone",
      spawnGateSpecFact({ scope: "root" }, rootSpec, "workspace"), "/repo/alpha");
check("...and its checkout is the new worktree row",
      spawnGateSpecFact({ scope: "root" }, rootSpec, "worktree"),
      "new (generated name)");

const childSpec = { workspace: "alpha", worktree: "kid-wt", branch_from: "master" };
check("a named workspace shows as the name",
      spawnGateSpecFact({ scope: "child" }, childSpec, "workspace"), "alpha");
check("a named checkout shows on the worktree row",
      spawnGateSpecFact({ scope: "child" }, childSpec, "worktree"),
      "kid-wt from master");
check("a child with no checkout of its own has an empty worktree row",
      spawnGateSpecFact({ scope: "child" }, { workspace: "alpha" }, "worktree"), "");

/* ---- the order: remembered values first, the seed over them ---------- */
const open = src.slice(src.indexOf("async function openSessionModal("));
const recallAt = open.indexOf("applySessionModalRecall(f);");
const seedAt = open.indexOf("if (opts.seed) applySessionModalSeed(f, opts.seed);");
if (recallAt < 0 || seedAt < 0 || recallAt > seedAt) {
  failures++;
  console.error("FAIL openSessionModal must fill the remembered answers first " +
                "and apply the caller's seed over them " +
                `(recall@${recallAt}, seed@${seedAt})`);
}

/* ---- the `_seeding` draft guard, on the real function ------------------ */
/* Two writes of the same row, differing only in `_seeding`: the seed's own
   change event must not reach the draft, and the operator's next change
   must. Everything `saveNewFormDraft` touches is stubbed here — the form,
   the route it checks, and the storage it reads and writes — so the checks
   are the guard and nothing else. */
const store = new Map();
const BASE = "/";
const NEW_FORM_DRAFT_KEY = `claunch_new_form:${BASE}`;
const NEW_FORM_DRAFT_FIELDS = constant("NEW_FORM_DRAFT_FIELDS");
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
};
let newFormDraftRestoring = false;

const readNewFormDraft = eval(`(${slice("readNewFormDraft")})`);
const newFormDraftAnswer = eval(`(${slice("newFormDraftAnswer")})`);
const saveNewFormDraft = eval(`(${slice("saveNewFormDraft")})`);

const form = { _seeding: false, elements: {} };
for (const name of NEW_FORM_DRAFT_FIELDS) {
  form.elements[name] = {
    tagName: "SELECT", type: "select-one", disabled: false, value: "", options: [],
  };
}
const $ = (id) => (id === "new-session" ? form : null);
const sessionFormView = () => ({ mode: "new" });

/* The operator's standing answers, as a previous session left them. The
   target row is seeded to a value the draft does not hold, so a leak is
   visible in the draft itself rather than in a pass that agrees anyway. */
store.set(NEW_FORM_DRAFT_KEY, JSON.stringify({ model: "browser-choice" }));
const draftBefore = readNewFormDraft();

form.elements.model.value = "seed-choice";
form._seeding = true;
saveNewFormDraft();
check("the seed's own change leaves the draft as the browser left it",
      readNewFormDraft(), draftBefore);

form._seeding = false;
form.elements.model.value = "operator-choice";
saveNewFormDraft();
check("the operator's change after seeding reaches the draft",
      (readNewFormDraft() || {}).model, "operator-choice");

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("spawnseed_check: ok");
