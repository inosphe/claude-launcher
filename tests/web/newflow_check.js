/* The create form's Role and Workflow rows, run against the real functions
   from app.js.

   Picking a role is how a session is told what it is for, and every workflow
   that volunteers for a role says so in its own file (`default_role`, ranked
   by `priority`, with `filter_roles` deciding who may drive it at all). The
   CLI wizard and the spawn modal both act on that; this form used to throw
   it away — it kept the daemon's entries as bare names, so the Workflow row
   could be neither ranked nor auto-picked, and a leader creating a worker
   had to know by heart which workflow that worker runs.

   Driven here: the ranking (the role's own defaults first, refused filters
   last), the auto-pick following the role, a pick made by hand surviving
   every later role change — "(none)" included — and the directory the list
   is fetched for, which for a child is where the CHILD will stand, not what
   the greyed Directory row happens to show. */
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
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
function sliceLet(name) {
  const start = src.indexOf(`let ${name} =`);
  if (start < 0) throw new Error(`cannot locate let ${name} in app.js`);
  return src.slice(start, src.indexOf(";", start) + 1);
}

/* ---- stub DOM ---- */
function option(label, value) {
  return { label, value, textContent: label, disabled: false };
}
function picker(pairs = []) {
  const sel = {
    value: "", disabled: false, options: pairs.map(([l, v]) => option(l, v)),
    appendChild(o) { sel.options.push(o); return o; },
    get innerHTML() { return ""; },
    set innerHTML(v) { if (v === "") sel.options.length = 0; },
  };
  return sel;
}
const box_ = {};
function box(id) {
  return {
    id, classes: new Set(),
    classList: {
      contains: (c) => box_[id].classes.has(c),
      toggle: (c, on) => (on ? box_[id].classes.add(c) : box_[id].classes.delete(c)),
    },
  };
}
for (const id of ["new-handle-row", "new-context-row"]) box_[id] = box(id);

const form = {
  parent: picker(), role: picker([["(no role)", ""], ["worker", "worker"],
                                  ["leader", "leader"]]),
  mesh: picker(), workflow: picker(), cwd: picker([["(daemon cwd)", ""]]),
};

/* What the daemon serves for a directory: the entries, not their names. Two
   directories, because a child's list is its parent's. */
const FLOWS = {
  "": [{ name: "solo" }],
  "F:/other": [{ name: "spare" }],
  "F:/repo": [
    { name: "improv-worker", default_role: "worker", priority: 0,
      filter_roles: { type: "whitelist", roles: ["worker"] } },
    { name: "improv-leader", default_role: "leader", priority: 0,
      filter_roles: { type: "whitelist", roles: ["leader"] } },
    { name: "hotfix", default_role: "worker", priority: 5, filter_roles: null },
    { name: "chores", default_role: "", priority: 0, filter_roles: null },
  ],
};
const asked = [];
/* The next answer the daemon fails to give: "throw" is a daemon that is not
   there, "status" one that is there and busy. Both are the same thing to this
   form — no answer — and neither is a fact about the directory. */
let failNext = "";
const api = async (p) => {
  const cwd = decodeURIComponent((p.split("cwd=")[1] || ""));
  asked.push(cwd);
  const how = failNext;
  failNext = "";
  if (how === "throw") throw new Error("daemon unreachable");
  if (how === "status") return { ok: false, status: 503, json: async () => ({}) };
  return { ok: true, json: async () => ({ workflows: FLOWS[cwd] || [] }) };
};

let sessions = [];
const ctx = {};
new Function(
  "exports", "$", "Option", "api", "meshCache", "sessionsCache",
  [sliceLet("workflowsCache"), sliceLet("workflowsFor"), sliceLet("newWfPicked"),
   slice("spawnWorkflowEntry"), slice("spawnWorkflowAdmits"),
   slice("spawnRankWorkflows"), slice("spawnParent"), slice("newSessionCwd"),
   slice("refreshWorkflowChoices"), slice("syncOnboardPickers")].join("\n") + `
exports.refresh = refreshWorkflowChoices;
exports.sync = syncOnboardPickers;
exports.pickedByHand = () => { newWfPicked = true; };
exports.cwdOf = newSessionCwd;
`)(ctx,
   (id) => (id === "new-session" ? form : box_[id] || null),
   function Option(label, value) { return option(label, value); },
   api,
   [{ name: "m0" }],
   { get length() { return sessions.length; },
     find: (fn) => sessions.find(fn) });

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
const listed = () => form.workflow.options.map((o) => o.value);

async function main() {
  /* A session of its own, in a directory that declares one workflow. */
  form.cwd.value = "";
  await ctx.refresh();
  check("the list follows the directory picker", asked, [""]);
  check("with no role nothing is auto-picked",
        [listed(), form.workflow.value], [["", "solo"], ""]);

  /* The real directory, and a role. The role's own defaults come first, the
     highest priority among them is the one taken, and the workflows its
     filter turns away sort last — the CLI wizard's ranking exactly. */
  form.cwd.value = "F:/repo";
  await ctx.refresh();
  check("...still nothing without a role", form.workflow.value, "");
  form.role.value = "worker";
  ctx.sync();
  check("the role's own candidates rank first, its refusals last",
        listed(), ["", "hotfix", "improv-worker", "chores", "improv-leader"]);
  check("picking a role picks its highest-priority workflow",
        form.workflow.value, "hotfix");
  check("the reason is on the option itself",
        form.workflow.options[1].label, "hotfix — default for worker, priority 5");
  check("...and one the filter turns away says so",
        form.workflow.options[4].label,
        "improv-leader — default for leader, filter_roles turns 'worker' away");
  check("choosing a workflow opens the context row",
        box_["new-context-row"].classList.contains("hidden"), false);

  /* Another role re-homes the auto-pick — it was the role's, not anybody's
     choice. */
  form.role.value = "leader";
  ctx.sync();
  check("switching role re-homes the workflow the role had picked",
        form.workflow.value, "improv-leader");
  check("and re-ranks the list for the new role",
        listed(), ["", "improv-leader", "hotfix", "chores", "improv-worker"]);

  /* A pick made by hand is the operator's, and outlives every later role
     change — the wizard's rule. */
  form.workflow.value = "chores";
  ctx.pickedByHand();
  ctx.sync();
  form.role.value = "worker";
  ctx.sync();
  check("a workflow picked by hand survives a role change",
        form.workflow.value, "chores");

  /* Including the pick that says "no run at all": an auto that came back
     over it would be the form overruling the person. */
  form.workflow.value = "";
  ctx.sync();
  form.role.value = "leader";
  ctx.sync();
  check("...and so does choosing no workflow at all",
        [form.workflow.value,
         box_["new-context-row"].classList.contains("hidden")], ["", true]);

  /* A child's workflows are the ones declared where the CHILD will stand.
     The Directory row is greyed for a child whose policy keeps it shut, and
     reading it would list the daemon directory's workflows for a session
     booting in its parent's repository. */
  sessions = [{ name: "lead", status: "idle", cwd: "F:/repo" }];
  form.parent.value = "lead";
  form.cwd.value = "";
  form.cwd.disabled = true;
  check("a child's directory is its parent's", ctx.cwdOf(), "F:/repo");
  /* ...and the workspace it was moved to, when the policy opened that row. */
  form.cwd.disabled = false;
  form.cwd.value = "F:/elsewhere";
  check("a child moved to a workspace takes that one",
        ctx.cwdOf(), "F:/elsewhere");

  /* A fetch that did not answer is not an answer. The list is memoised by
     directory, and the memo used to be claimed before the fetch and kept
     whatever came back — so one busy tick was remembered as "this directory
     declares no workflows", every later call early-returned on the empty
     list, and the row stayed blank for the life of the tab. That is the
     create form's half of the reported symptom, and it is why it comes back
     when the machine is loaded rather than when anything changed. */
  form.parent.value = "";
  form.cwd.disabled = false;
  form.cwd.value = "F:/other";
  asked.length = 0;
  failNext = "throw";
  await ctx.refresh();
  check("an unreachable daemon empties the row", listed(), [""]);
  await ctx.refresh();
  check("...and is asked again rather than remembered",
        [asked, listed()], [["F:/other", "F:/other"], ["", "spare"]]);

  /* The other half of the same fact: a daemon that answered with a status,
     not with a list. */
  form.cwd.value = "F:/repo";
  await ctx.refresh();          // park the memo somewhere else
  form.cwd.value = "F:/other";
  asked.length = 0;
  failNext = "status";
  await ctx.refresh();
  check("a busy daemon empties the row too", listed(), [""]);
  await ctx.refresh();
  check("...and is also retried", [asked, listed()],
        [["F:/other", "F:/other"], ["", "spare"]]);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("newflow_check: ok");
}

main();
