/* The detail head's "Spawn same setup" (claunch-z6ltt).

   The plain Spawn opens the spawn wizard under a session and fills it from
   what the browser last used. This button opens the same wizard under the
   same session, filled instead with what that session was itself created
   with, so "another one like this" is one press.

   What must hold:

   1. the head carries the button, it opens the spawn tab under THIS session
      with spawnSameSeed's answer and the New session tab beside it (with
      the repository as the directory), and an exited session gets the New
      session tab alone (claunch-vg6bv);
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
const afterSpawn = head.indexOf("head.appendChild(same)", head.indexOf("head.appendChild(spawn)"));
check("...appended right after the plain Spawn, before Open PR",
      afterSpawn > 0 && afterSpawn < head.indexOf('"Open PR"'));
const docked = head.slice(head.indexOf("if (mine && terminalOnScreen()"));
check("...and kept when the panel is docked beside its own terminal (claunch-b6ril)",
      /^if \(mine && terminalOnScreen\(\) && !MOBILE_MQ\.matches\) \{\s*head\.appendChild\(same\);/
        .test(docked));
check("...which opens the wizard through openSpawnSame",
      /same\.addEventListener\("click", \(\) => openSpawnSame\(s\)\)/.test(head));
check("...and stays pressable on an exited session (it opens New session only)",
      !/same\.disabled = true/.test(head));
const opener = slice("openSpawnSame");
check("openSpawnSame offers both tabs, Child session first, pinned to s (claunch-vg6bv)",
      /tab: "spawn", parent: s\.name, seed,\s*modes: \["spawn", "new"\], newSeed/.test(opener));
check("...an exited session opens on New session alone",
      /s\.status === "exited"[\s\S]*tab: "new", seed, newSeed/.test(opener));
check("the dialog view takes the modes it is given",
      /options\.modes \|\| \[mode\]/.test(slice("createSessionFormView")));
check("switching to New session spends the held directory",
      /if \(want === "new"\) spendSpawnSameDirectory\(f, view\);/.test(slice("setSessionModalTab")));
// After the awaited option fills, wherever the rest of the opener settles:
// the point is the order against the fills, not that the call is the last
// statement before the closing brace.
{
  const openerSrc = slice("openSessionModal");
  const fills = openerSrc.indexOf("await Promise.all");
  check("...and openSessionModal asks again once the workspace list has landed",
        fills >= 0 &&
        openerSrc.indexOf("spendSpawnSameDirectory(f, f._sessionView)") > fills);
}
{
  const rctx = {};
  vm.createContext(rctx);
  vm.runInContext(slice("spawnSameRoot") + "\nthis.f = spawnSameRoot;", rctx);
  check("the root of a launcher worktree is its repository",
        rctx.f("F:\\works\\repo\\.claude\\worktrees\\wt-1") === "F:\\works\\repo"
        && rctx.f("C:/r/.claude/worktrees/x/") === "C:/r");
  check("...and a main checkout is itself", rctx.f("C:/repo") === "C:/repo");
}
{
  const dctx = { calls: 0, applySessionCwdChange() { dctx.calls++; } };
  vm.createContext(dctx);
  vm.runInContext(slice("worktreePathKey") + "\n" + slice("spendSpawnSameDirectory")
    + "\nthis.f = spendSpawnSameDirectory;", dctx);
  const form = { cwd: { value: "D:/other", options: [] } };
  const view = { mode: "new", newSeed: { cwd: "F:\\Works\\Repo" } };
  dctx.f(form, view);
  check("a directory the row does not offer yet is held", !!view.newSeed && dctx.calls === 0);
  form.cwd.options = [{ value: "D:/other" }, { value: "f:/works/repo" }];
  dctx.f(form, view);
  check("...and written once it is offered, matched by path key",
        form.cwd.value === "f:/works/repo" && view.newSeed === null && dctx.calls === 1);
  const spawnView = { mode: "spawn", newSeed: { cwd: "f:/works/repo" } };
  form.cwd.value = "D:/other";
  dctx.f(form, spawnView);
  check("...never on the Child session tab", form.cwd.value === "D:/other" && !!spawnView.newSeed);
}

/* ---- 2. the seed ------------------------------------------------------- */
const ctx = {
  meshes: {}, runs: {}, cflow2RailCache: {},
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

/* ---- 2b. the v2 run's start inputs (claunch-76iqh) ---------------------- */
/* A v2 run lives on its own rail poll (cflow2RailCache), which the v1 cache
   does not see: the workflow comes from there when sessCflowRun is silent,
   and the start inputs come with it -- minus the issue, which is `s`'s
   record; the child settles its own on the Board. */
ctx.cflow2RailCache.s9 = {
  workflow: "improv-worker-v2", status: "running",
  inputs: { goal: "carry the variables", issue: "claunch-parent",
            relation: "stacked", scope: ["src/a"], max_rounds: 5 },
};
const seededInputs = ctx.spawnSameSeed({
  name: "s9", cwd: "C:/repo", issue: "claunch-abc",
});
check("a v2 rail row's start inputs come over as cflow_inputs",
      eq(seededInputs.cflow_inputs,
         { goal: "carry the variables", relation: "stacked",
           scope: ["src/a"], max_rounds: 5 }),
      seededInputs.cflow_inputs);
check("...the issue input does not (the child settles its own)",
      !("issue" in (seededInputs.cflow_inputs || {})), seededInputs.cflow_inputs);

ctx.cflow2RailCache.s11 = { workflow: "improv-worker-v2", status: "running",
                            inputs: null };
const fromRailOnly = ctx.spawnSameSeed({ name: "s11", cwd: "C:/repo" });
check("a v2 run the v1 cache does not see still seeds the workflow",
      fromRailOnly.workflow === "improv-worker-v2"
      && fromRailOnly.workflow_version === 2, fromRailOnly);
check("...a run with no inputs seeds none",
      !("cflow_inputs" in fromRailOnly), fromRailOnly);

/* ---- 2c. syncCflowInputs spends the parked seed ------------------------- */
/* The box is built when the workflow pick's fields are on hand; the seed is
   spent on that build, and only then -- a pick with no v2 fields leaves the
   seed held, and a second build of the same pick does not re-spend it. */
{
  const classList2 = () => {
    const set = new Set();
    return { add: (c) => set.add(c), remove: (c) => set.delete(c),
             contains: (c) => set.has(c),
             toggle: (c, on) => { if (on === undefined ? !set.has(c) : on)
               set.add(c); else set.delete(c); } };
  };
  const sctx = {
    workflowsCache: [{ name: "improv-worker-v2", version: 2,
      inputs: [{ name: "goal", type: "text", default: "" },
               { name: "relation", type: "enum", choices: ["stacked"],
                 default: "stacked" }] }],
    newInputsFor: null, pendingSeedInputs: null,
    newSessionCwd: () => "C:/repo",
    cflowPickEntry: (v, cache) => cache.find((e) => e.name === v) || null,
    cflowInputsLive() {},
    el: (tag, cls, text) => ({ tag, className: cls || "", textContent: text,
                               children: [], appendChild() {} }),
  };
  const forms = [];
  sctx.forms = forms;
  sctx.cflowInputsForm = (box, fields, constraints, seeded) => {
    forms.push({ fields, seeded });
  };
  sctx.swf = { workflow: { value: "improv-worker-v2" } };
  sctx.sbox = { innerHTML: "", hidden: true, classList: classList2(),
                appendChild() {}, querySelector: () => null };
  sctx.$ = (id) => (id === "new-session" ? sctx.swf : sctx.sbox);
  vm.createContext(sctx);
  vm.runInContext(slice("syncCflowInputs")
    + "\nthis.sync = syncCflowInputs;"
    + "\nthis.parked = () => pendingSeedInputs;"
    + "\nthis.formSeeds = () => forms.map((f) => f.seeded);"
    + "\nthis.pick = (v) => { swf.workflow.value = v; };", sctx);

  sctx.pendingSeedInputs = { goal: "g" };
  sctx.sync();
  check("the box is built with the seeded values, and the seed is spent",
        eq(sctx.formSeeds(), [{ goal: "g" }]) && sctx.parked() === null,
        sctx.formSeeds());
  sctx.pendingSeedInputs = { goal: "g2" };
  sctx.sync();
  check("the same pick again does not rebuild -- the seed stays held",
        sctx.formSeeds().length === 1 && eq(sctx.parked(), { goal: "g2" }));
  sctx.pick("");
  sctx.sync();
  check("a pick with no v2 fields builds nothing and holds the seed",
        sctx.formSeeds().length === 1 && eq(sctx.parked(), { goal: "g2" }));
  sctx.pick("improv-worker-v2");
  sctx.sync();
  check("the fields' return spends it", eq(sctx.formSeeds().length, 2)
        && sctx.parked() === null);
}

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
  pendingSeedInputs: null, newInputsFor: null,
  workspacesCache: [],
  Option: function (label, value) { this.text = label; this.value = value; },
  renderRoleStance() {}, renderSubroles() { actx.renders++; },
  renderPromptPackRow() {}, syncNewWorktree() {}, applySessionCwdChange() {},
  syncBeadsRow() {}, refreshIssueChoices() {},
  newSessionCwd() { return actx.cwd; },
};
vm.createContext(actx);
vm.runInContext(
  [slice("ensureOption"), slice("applySessionModalSeed"), slice("applyPendingPackSeed"),
   slice("newPackDefaults")]
    .join("\n")
  + "\nthis.apply = applySessionModalSeed; this.applyPacks = applyPendingPackSeed;"
  + "\nthis.state = () => ({ pendingNewMesh, newWfPicked, newSubroleTicks,"
  + " pendingSeedPacks, newPackTicks, pendingSeedInputs });"
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
// Spent, the way syncOnboardPickers spends it: a value left armed is spent
// by the row's own change handler on the NEXT pick, putting the seed's mesh
// back over the one the operator chose (claunch-xlqwq).
check("...and the held value is spent by that write", st.pendingNewMesh === "",
      st.pendingNewMesh);
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

/* ---- 3b. the seeded start inputs are parked, not lost (claunch-76iqh) --- */
/* The inputs box is built by syncCflowInputs when the workflow lists arrive,
   so the seed holds the values until a box with fields exists -- the same
   hold the pack ticks use. (This context has no syncCflowInputs: the guard
   skips it, and the park is what is checked here.) */
const f3 = { ...f, workflow: select(["", "improv-worker-v2", "v2:improv-worker-v2"]) };
actx.apply(f3, { workflow: "improv-worker-v2", workflow_version: 2,
                 cflow_inputs: { goal: "g", relation: "stacked" } });
check("a seeded workflow's start inputs are parked for the inputs box",
      eq(actx.state().pendingSeedInputs, { goal: "g", relation: "stacked" }),
      actx.state().pendingSeedInputs);
actx.apply(f3, { workflow: "improv-worker-v2", workflow_version: 2,
                 cflow_inputs: {} });
check("...an empty set parks nothing", actx.state().pendingSeedInputs === null);

/* ---- the wiring around the modal --------------------------------------- */
check("the offer's arrival applies a held seed",
      (slice("refreshPromptPackOffer").match(/applyPendingPackSeed\(\)/g) || []).length === 2);
check("closing the modal drops a seed that never landed",
      slice("sessionModalClose").includes("pendingSeedPacks = null;"));
check("...and a start-input seed with it",
      slice("sessionModalClose").includes("pendingSeedInputs = null;"));

if (failures) {
  console.log(`spawnsame_check: ${failures} failure(s)`);
  process.exit(1);
}
console.log("spawnsame_check: all ok");
