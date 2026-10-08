/* The detail head's "Spawn same setup" (claunch-z6ltt).

   The plain Spawn opens the spawn wizard under a session and fills it from
   what the browser last used. This button opens the same wizard under the
   same session, filled instead with what that session was itself created
   with, so "another one like this" is one press.

   What must hold:

   1. the head carries the button, it opens the spawn tab under THIS session
      with spawnSameSeed's answer, and an exited session has it greyed;
   2. spawnSameSeed carries the mesh and roles the session holds, the
      workflow its run follows (v2 marked), its task, whether it stands in a
      launcher worktree, and the prompt packs it was given by tick — and
      never its name, handle or issue;
   3. applySessionModalSeed writes the mesh, subroles and a v2 workflow, and
      holds pack ticks until the offer for the form's directory is on hand,
      then ticks exactly the seeded packs. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.search(new RegExp(`(?:async )?function ${name}\\(`));
  if (start < 0) throw new Error("missing " + name);
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/* ---- 1. the head ------------------------------------------------------- */
const head = slice("sessHead");
check("the head builds a 'Spawn same setup' button",
      head.includes('el("button", "wf-btn", "Spawn same setup")'));
check("...placed right after the plain Spawn",
      head.indexOf('"Spawn same setup"') > head.indexOf("head.appendChild(spawn)")
      && head.indexOf('"Spawn same setup"') < head.indexOf('"Open PR"'));
check("...which opens the spawn wizard under this session with its own seed",
      /openSpawnModal\(s\.name,\s*\{ seed: spawnSameSeed\(s\) \}\)/.test(head));
check("...and is greyed on an exited session",
      /if \(s\.status === "exited"\) same\.disabled = true;/.test(head));

/* ---- 2. the seed ------------------------------------------------------- */
const ctx = {
  meshes: {}, runs: {},
  sessMeshes(name) { return ctx.meshes[name] || []; },
  sessCflowRun(name) { return ctx.runs[name] || null; },
};
vm.createContext(ctx);
vm.runInContext(slice("worktreePathKey") + "\n" + slice("spawnSameSeed")
  + "\nthis.spawnSameSeed = spawnSameSeed;", ctx);

ctx.meshes.s9 = [{ mesh: "mesh-1", handle: "s9", role: "worker",
                   roles: ["worker", "reviewer"] }];
ctx.runs.s9 = { workflow: "improv-worker-v2", version: 2 };
const full = ctx.spawnSameSeed({
  name: "s9", issue: "claunch-abc", task: "do the thing\nissue: claunch-xyz",
  cwd: "F:\\works\\repo\\.claude\\worktrees\\wt-1",
  prompt_packs: [
    { id: "house-style", text: "…" },
    { id: "role-worker", text: "…", source: "package", required_by: ["worker"] },
  ],
});
check("the mesh and the primary role the session holds",
      full.mesh === "mesh-1" && full.role === "worker", full);
check("the other roles it holds become subroles", eq(full.subroles, ["reviewer"]), full);
check("the run's workflow, marked v2",
      full.workflow === "improv-worker-v2" && full.workflow_version === 2, full);
check("the task it was opened with, verbatim", full.task === "do the thing\nissue: claunch-xyz");
check("a launcher worktree asks for a fresh one", full.worktree === true, full);
check("only the ticked packs; a role's required pack comes back with the role",
      eq(full.prompt_packs, ["house-style"]), full);
check("the name is blanked rather than copied", full.name === "", full);
check("no handle and no issue are carried",
      !("handle" in full) && !("issue" in full), full);

const bare = ctx.spawnSameSeed({ name: "lone", cwd: "C:/repo" });
check("a session in no mesh seeds no mesh or role",
      !("mesh" in bare) && !("role" in bare), bare);
check("...no run, no workflow", !("workflow" in bare), bare);
check("...its own checkout, no worktree", bare.worktree === false, bare);
check("...no task, no task row", !("task" in bare), bare);
check("...no packs recorded seeds an empty tick list", eq(bare.prompt_packs, []), bare);
check("a directory that merely contains 'worktrees' is not a launcher worktree",
      ctx.spawnSameSeed({ name: "x", cwd: "/home/u/worktrees/a" }).worktree === false);

/* ---- 3. applying it ---------------------------------------------------- */
function select(values) {
  return {
    value: "", disabled: false,
    options: values.map((v) => ({ value: v })),
    appendChild(o) { this.options.push(o); },
  };
}
const actx = {
  pendingNewMesh: "", newWfPicked: false, newSubroleTicks: new Set(),
  pendingSeedIssue: "", pendingSeedPacks: null, newPackOffer: null,
  newPackOfferFor: null, newPackTicks: new Map(), cwd: "C:/repo", renders: 0,
  workspacesCache: [],
  Option: function (label, value) { this.text = label; this.value = value; },
  renderRoleStance() {}, renderSubroles() { actx.renders++; },
  renderPromptPackRow() {}, syncNewWorktree() {}, applySessionCwdChange() {},
  syncBeadsRow() {}, refreshIssueChoices() {},
  newSessionCwd() { return actx.cwd; },
};
vm.createContext(actx);
vm.runInContext(
  [slice("ensureOption"), slice("applySessionModalSeed"), slice("applyPendingPackSeed")]
    .join("\n")
  + "\nthis.apply = applySessionModalSeed; this.applyPacks = applyPendingPackSeed;"
  + "\nthis.state = () => ({ pendingNewMesh, newWfPicked, newSubroleTicks,"
  + " pendingSeedPacks, newPackTicks });"
  + "\nthis.setOffer = (o, f) => { newPackOffer = o; newPackOfferFor = f; };",
  actx);
// The slices read these as bare globals; vm keeps `let`-less names on ctx.
const f = {
  name: { value: "old" }, task: { value: "" },
  mesh: select(["", "mesh-1"]), role: select(["", "worker"]),
  workflow: select(["", "improv-worker-v2", "v2:improv-worker-v2"]),
  cwd: select([]), args: { value: "" }, null_token: { checked: false },
};
actx.apply(f, {
  name: "", mesh: "mesh-1", role: "worker", subroles: ["reviewer"],
  workflow: "improv-worker-v2", workflow_version: 2, task: "t",
  prompt_packs: ["house-style"],
});
let st = actx.state();
check("the mesh row takes the seeded mesh", f.mesh.value === "mesh-1", f.mesh.value);
check("...and it is held for the next picker fill", st.pendingNewMesh === "mesh-1");
check("the role row takes the seeded role", f.role.value === "worker");
check("the subroles are the seeded ones, and the row is redrawn",
      eq([...st.newSubroleTicks], ["reviewer"]) && actx.renders === 1);
check("a v2 name a v1 workflow shares is picked by its v2: value",
      f.workflow.value === "v2:improv-worker-v2", f.workflow.value);
check("...as a pick the role's auto-pick must not overwrite", st.newWfPicked === true);
check("the name and the task rows are the seed's", f.name.value === "" && f.task.value === "t");
check("no offer yet: the pack ticks are held", eq(st.pendingSeedPacks, ["house-style"]));

actx.setOffer({ packs: [{ id: "house-style" }, { id: "default-on" }],
                shipped: [{ id: "role-worker" }] }, "C:/other");
actx.applyPacks();
check("an offer for another directory leaves them held",
      eq(actx.state().pendingSeedPacks, ["house-style"]));

actx.setOffer({ packs: [{ id: "house-style" }, { id: "default-on" }],
                shipped: [{ id: "role-worker" }] }, "C:/repo");
actx.applyPacks();
st = actx.state();
check("the offer for the form's directory spends them", st.pendingSeedPacks === null);
check("...ticking exactly the seeded packs and unticking the rest",
      st.newPackTicks.get("house-style") === true
      && st.newPackTicks.get("default-on") === false
      && st.newPackTicks.get("role-worker") === false,
      [...st.newPackTicks]);

const f1 = { ...f, workflow: select(["", "improv-worker"]) };
actx.apply(f1, { workflow: "improv-worker" });
check("a v1 workflow is picked by its bare name", f1.workflow.value === "improv-worker");

/* ---- the wiring around the modal --------------------------------------- */
check("the offer's arrival applies a held seed",
      (slice("refreshPromptPackOffer").match(/applyPendingPackSeed\(\)/g) || []).length === 2);
check("closing the modal drops a seed that never landed",
      slice("sessionModalClose").includes("pendingSeedPacks = null;"));

if (failures) {
  console.log(`spawnsame_check: ${failures} failure(s)`);
  process.exit(1);
}
console.log("spawnsame_check: all ok");
