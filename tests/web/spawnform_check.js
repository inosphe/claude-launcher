/* The create form as `claunch spawn`, run against the real functions from
   app.js.

   Naming a parent turns Create into a spawn, and the two differ in what may
   be asked. What a child inherits is the spawn policy's call, field by field
   (the `spawn.allow_*` unlocks in ~/.claunch.yaml), and the daemon publishes
   it per parent — so this form asks, greys what the policy keeps shut, and
   hands back what it opened. Both halves are mistakes waiting to happen: a
   form that offers what it cannot send teaches the policy wrong, and one
   that withholds what the policy opened lies to the person who unlocked it.
   Driven here: the gating against a report, the payload reading through the
   disables (the directory travelling as a workspace NAME), the soft child
   cap being offered as a crossing rather than a dead end, and the fork —
   only on offer where there is a conversation to copy. */
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

/* The constant the disabling reads from — sliced too, so the harness cannot
   drift from the list app.js actually uses. */
function sliceConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const end = src.indexOf(";", src.indexOf("]", start));
  return src.slice(start, end + 1);
}

/* The module-level state the gating keeps between calls: which parent the
   report is about, which one the rows were seeded for. Sliced rather than
   redeclared, for the same reason as the constant. */
function sliceLet(name) {
  const start = src.indexOf(`let ${name} =`);
  if (start < 0) throw new Error(`cannot locate let ${name} in app.js`);
  return src.slice(start, src.indexOf(";", start) + 1);
}

/* ---- stub DOM: the form's controls, and the rows that hide ---- */
function control(value = "") {
  return { value, checked: false, disabled: false, options: [] };
}
function option(label, value, available) {
  return { label, value, textContent: label, disabled: false, available };
}
/* A <select> the gating can rebuild, insert into and remove from. */
function picker(pairs = []) {
  const sel = {
    value: "", disabled: false,
    options: pairs.map(([l, v, a]) => option(l, v, a)),
    appendChild(o) { sel.options.push(o); return o; },
    insertBefore(o, ref) {
      const at = ref ? sel.options.indexOf(ref) : -1;
      sel.options.splice(at < 0 ? sel.options.length : at, 0, o);
      return o;
    },
    remove(i) { sel.options.splice(i, 1); },
    get innerHTML() { return ""; },
    set innerHTML(v) { if (v === "") sel.options.length = 0; },
  };
  return sel;
}
const box_ = {};
function box(id) {
  return {
    id, textContent: "", title: "", classes: new Set(),
    classList: {
      add: (c) => box_[id].classes.add(c),
      remove: (c) => box_[id].classes.delete(c),
      contains: (c) => box_[id].classes.has(c),
      toggle: (c, on) => (on ? box_[id].classes.add(c) : box_[id].classes.delete(c)),
    },
  };
}
for (const id of ["parent-hint", "new-fork-row", "new-over-row", "new-over-text"]) {
  box_[id] = box(id);
}
box_["new-over-row"].classes.add("hidden");

const form = {
  parent: picker(), name: control(""),
  profile: picker([["work", "work"], ["home", "home"]]),
  borrow: picker([["(this profile's own token)", ""], ["work", "work"]]),
  null_token: control(""),
  cwd: picker([["(daemon cwd)", ""], ["repo — F:/repo", "F:/repo"]]),
  args: control(""), resume: control(""), fork: control(""),
  fork_parent: control(""),
  role: picker([["(no role)", ""], ["worker", "worker"]]),
  over_limit: control(""),
};
form.profile.value = "work";

let forkSyncs = 0;
let stances = 0;
let wfRefreshes = 0;
const fetched = [];
/* The daemon's answer per parent, scripted per test. A parent with no entry
   answers null — a fetch that failed, which must open nothing. */
const reports = {};
const ctx = {};
const PROFILE_DETAILS = {
  work: { name: "work", harness: "claude", harness_available: true,
          borrow_allowed: true, borrow_mode: "provider-token" },
  home: { name: "home", harness: "claude", harness_available: true,
          borrow_allowed: true, borrow_mode: "provider-token" },
};
new Function(
  "exports", "$", "document", "Option", "sessionsCache", "syncForkAvailability",
  "renderRoleStance", "refreshWorkflowChoices", "spawnReport", "workspacesCache",
  "profileDetails",
  "syncRuntimeFold", "renderRuntimeSummary", "renderProfileHint",
  "syncNewBorrowOptions",
  [sliceConst("SPAWN_INHERITS"), sliceLet("newSpawnReport"),
   sliceLet("newSpawnReportFor"), sliceLet("newSpawnDefaultsFor"),
   // The picker's signature guard against the two-second poll, which lives
   // at module scope because it has to outlive the call that wrote it. What
   // it holds off is pollselect_check's; here it only has to exist, so that
   // slicing the function does not slice it away from its own state.
   sliceLet("parentsRendered"),
   slice("profileBorrowCapability"), slice("profileHarnessName"),
   slice("spawnUnlocked"), slice("refreshSpawnPolicy"),
   slice("spawnWorkspaceName"), slice("refreshParentChoices"),
   slice("spawnParent"), slice("syncSpawnMode"),
   slice("syncSpawnProfileRow"), slice("syncSpawnCwdRow"),
   slice("syncSpawnOverRow"), slice("spawnChildFields")].join("\n") + `
exports.refresh = refreshParentChoices;
exports.sync = syncSpawnMode;
exports.parentOf = spawnParent;
exports.policy = refreshSpawnPolicy;
exports.fields = spawnChildFields;
exports.setSessions = (s) => { sessionsCache = s; };
`)(ctx,
   (id) => (id === "new-session" ? form : box_[id] || null),
   // The pickers are reached the way the page reaches them, by selector.
   { querySelector: (sel) => (sel.includes("name=parent") ? form.parent : null) },
   function Option(label, value) { return option(label, value); },
   [],
   () => { forkSyncs++; },
   () => { stances++; },
   () => { wfRefreshes++; },
   async (name) => { fetched.push(name); return reports[name] || null; },
   [{ name: "repo", path: "F:/repo", exists: true }],
   PROFILE_DETAILS,
   // The "How it runs" fold opens itself when the policy hands a row back.
   // That rule reads the fold element, which this stub page does not have,
   // and it is newform_check's to hold — here it only has to exist. The
   // summary line the fold carries, and the credential hint under the
   // promoted Profile row, are the same story.
   () => {}, () => {}, () => {}, () => {});

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
const INHERITED = ["profile", "borrow", "null_token", "cwd", "args",
                   "resume", "fork"];
const greyed = () => INHERITED.map((k) => form[k].disabled);
/* Re-reading a parent's report the way a changed policy would: the fetch is
   cached per parent, so this moves off it and back. */
function reread(name) {
  form.parent.value = "";
  ctx.sync();
  ctx.policy();
  form.parent.value = name;
  ctx.sync();
  return ctx.policy();
}

const SESSIONS = [
  { name: "lead", status: "idle", harness: "claude", conversation_id: "c-1",
    cwd: "F:/repo" },
  { name: "quiet", status: "busy", harness: "claude" },      // nothing pinned
  { name: "pi", status: "idle", harness: "codex", conversation_id: "c-2" },
  { name: "gone", status: "exited", harness: "claude", conversation_id: "c-3" },
];

async function main() {
  ctx.setSessions(SESSIONS);

  /* The picker offers the sessions that could actually take a child — an
     exited one is refused by the daemon, so it is not on the list. */
  ctx.refresh();
  check("only live sessions are offered as parents",
        form.parent.options.map((o) => o.value), ["", "lead", "quiet", "pi"]);
  check("the first entry is a session of its own",
        form.parent.options[0].label, "(none — a session of its own)");

  /* With no parent the form is the create form: nothing greyed by this, and
     the row that offers the parent's conversation is not there at all. */
  check("no parent, nothing inherited",
        ["cwd", "args"].map((k) => form[k].disabled),
        [false, false]);
  check("the fork row is hidden until there is a parent",
        box_["new-fork-row"].classes.has("hidden"), true);
  check("the create form's own rows are re-derived by their owner",
        forkSyncs > 0, true);
  check("the directory's blank entry is the daemon's own",
        form.cwd.options[0].textContent, "(daemon cwd)");

  /* Naming a parent greys everything a child inherits — while nothing is
     known about what that parent's policy allows. A report that never
     arrives (an older daemon, a fetch that failed) leaves it exactly here,
     which is the reading that cannot invent a permission. */
  form.parent.value = "lead";
  ctx.sync();
  check("with no report every inherited row stays the parent's",
        greyed(), [true, true, true, true, true, true, true]);
  check("the rows that make it a different worker still travel",
        form.role.disabled, false);
  check("the hint names the parent",
        [box_["parent-hint"].classes.has("hidden"),
         box_["parent-hint"].textContent.startsWith("a child of lead:")],
        [false, true]);
  check("...and says which rows an unlock would open",
        box_["parent-hint"].textContent.includes(
          "profile, borrow, null_token, cwd, args stay its parent's"),
        true);
  check("the blank directory entry now means the parent's",
        form.cwd.options[0].textContent, "(inherit the parent's directory)");

  /* The policy arrives. Every row it opens is handed back — this is the case
     the form used to get wrong: `allow_profile: true` in ~/.claunch.yaml, the
     CLI wizard offering the row, and the browser greying it anyway. */
  reports.lead = {
    may_choose: ["args", "borrow", "null_token", "profile", "worktree"],
    spawnable_harnesses: [],
    workspaces: [{ name: "repo", path: "F:/repo", exists: true }],
    soft_blocked_by: [],
  };
  await ctx.policy();
  check("the report was fetched for the parent named", fetched, ["lead"]);
  check("what the policy opened is handed back, what it shuts stays grey",
        greyed(), [false, false, false, false, false, true, true]);
  check("a child's workflows are re-read for where the child will stand",
        wfRefreshes > 0, true);
  check("the profile row gains an inherit entry, and starts on it",
        [form.profile.options[0].label, form.profile.value],
        ["(inherit the parent's profile)", ""]);
  check("the dead compatibility harness unlock creates no second control",
        form.harness, undefined);

  /* Asking twice for the same parent does not ask the daemon twice. */
  await ctx.policy();
  check("the report is fetched once per parent", fetched, ["lead"]);

  /* The payload: only what the policy left open AND the operator filled in. */
  form.profile.value = "home";
  form.borrow.value = "work";
  form.args.value = "--verbose  --model x";
  form.cwd.value = "F:/repo";
  check("a child sends what was opened, spelt in the API's keys",
        ctx.fields(form, { name: "kid" }),
        { name: "kid", profile: "home", borrow: "work",
          args: ["--verbose", "--model", "x"], workspace: "repo" });
  check("...and the directory travels as a registry name, never a path",
        ctx.fields(form, {}).cwd, undefined);

  /* A path the registry no longer names (edited under the form): the pick is
     sent as a path so the daemon says why, rather than dropped here. */
  form.cwd.value = "F:/elsewhere";
  check("an unregistered directory is sent for the daemon to refuse",
        ctx.fields(form, {}).cwd, "F:/elsewhere");
  form.cwd.value = "F:/repo";

  /* --null is ungated (it takes a credential away), and ticking it takes the
     borrow row down rather than provoking the daemon's refusal of the pair. */
  form.null_token.checked = true;
  ctx.sync();
  check("--null greys the borrow row and empties it",
        [form.borrow.disabled, form.borrow.value], [true, ""]);
  check("and the payload carries the tick, not the emptied borrow",
        [ctx.fields(form, {}).null_token, ctx.fields(form, {}).borrow],
        [true, undefined]);
  form.null_token.checked = false;
  form.borrow.value = "work";
  ctx.sync();

  /* Even an old daemon advertising the dead harness unlock cannot open it. */
  reports.lead.spawnable_harnesses = ["codex"];
  await reread("lead");
  check("an old spawnable_harnesses field still creates no Harness row",
        form.harness, undefined);

  /* An OAuth child has no shared token route and no Claude-only rows. */
  PROFILE_DETAILS.home.harness = "codex";
  PROFILE_DETAILS.home.borrow_allowed = false;
  PROFILE_DETAILS.home.borrow_mode = "none";
  form.profile.value = "home";
  ctx.sync();
  check("a non-claude child has no token rows and no role",
        [form.null_token.disabled, form.borrow.disabled, form.role.disabled,
         form.borrow.value, form.role.value],
        [true, true, true, "", ""]);
  check("the stance was re-rendered when the role was taken back",
        stances > 0, true);
  /* An API-key child keeps Borrow, but still has no Claude-only rows. */
  PROFILE_DETAILS.home.harness = "pi";
  PROFILE_DETAILS.home.borrow_allowed = true;
  PROFILE_DETAILS.home.borrow_mode = "token";
  form.profile.value = "home";
  ctx.sync();
  check("an API-key child can borrow but cannot use Claude null/role",
        [form.null_token.disabled, form.borrow.disabled, form.role.disabled],
        [true, false, true]);
  PROFILE_DETAILS.home.harness = "claude";
  PROFILE_DETAILS.home.borrow_allowed = true;
  PROFILE_DETAILS.home.borrow_mode = "provider-token";
  ctx.sync();
  check("back on claude the rows come back",
        [form.null_token.disabled, form.borrow.disabled, form.role.disabled],
        [false, false, false]);

  /* The soft child cap: shown, PRE-TICKED, and only while the daemon says
     the cap is reached. The cap warns rather than refusing, so the row is
     there to report the crossing and to let anyone who wants the strict
     reading untick it — not to collect a permission the daemon no longer
     asks for. */
  check("no cap reached, no over-limit row",
        box_["new-over-row"].classList.contains("hidden"), true);
  reports.lead.soft_blocked_by = ["child limit reached (4 running/4)"];
  await reread("lead");
  check("a parent at its limit is told, and the crossing is pre-answered",
        [box_["new-over-row"].classList.contains("hidden"),
         box_["new-over-text"].textContent
           .startsWith("child limit reached (4 running/4)"),
         form.over_limit.checked],
        [false, true, true]);
  check("the pre-tick travels", ctx.fields(form, {}).over_limit, true);
  /* Untick = "hold me to the cap", and THAT has to travel too: it is the one
     answer that changes what the daemon does, and a falsy-dropping payload
     builder would swallow it into "you did not say", which crosses. */
  form.over_limit.checked = false;
  check("an untick travels as a false, not as nothing",
        ctx.fields(form, {}).over_limit, false);
  /* A re-sync on the SAME parent must not put the tick back over that
     untick: the row is synced by more than the parent changing under it (the
     session list refreshing does it too), and re-answering yes on every
     sync would quietly undo the one answer that changes anything.
     Deliberately not `reread`, which clears the parent first: that hides the
     row, and a hidden row's answer is cleared on purpose — see below. */
  ctx.sync();
  await ctx.policy();
  check("a re-sync on the same parent leaves the untick alone",
        [box_["new-over-row"].classList.contains("hidden"),
         form.over_limit.checked],
        [false, false]);
  /* ...and a slot freed while the form stood open takes the row and every
     answer with it: an answer to a question no longer asked must not travel
     as a silent override, in either direction. */
  reports.lead.soft_blocked_by = [];
  await reread("lead");
  check("a freed slot takes the row and the answer with it",
        [box_["new-over-row"].classList.contains("hidden"),
         form.over_limit.checked, ctx.fields(form, {}).over_limit],
        [true, false, undefined]);

  /* The fork: offered here, because this parent is claude and has a
     conversation pinned. */
  check("the fork row appears with the parent",
        box_["new-fork-row"].classes.has("hidden"), false);
  check("a claude parent with a conversation can be forked",
        form.fork_parent.disabled, false);
  form.fork_parent.checked = true;

  /* A parent with nothing pinned cannot be forked, and a tick already given
     is taken back rather than sent into a refusal. Its policy is its own, so
     the rows go back to inherited until that report arrives. */
  form.parent.value = "quiet";
  ctx.sync();
  check("a parent with no conversation cannot be forked",
        [form.fork_parent.disabled, form.fork_parent.checked], [true, false]);
  check("...and the row says why",
        box_["new-fork-row"].title,
        "the parent has no claude conversation to copy");
  check("another parent is another policy — nothing is carried over",
        greyed(), [true, true, true, true, true, true, true]);

  /* A report that arrives after the pick moved on is dropped: it describes a
     parent this form is no longer building a child of. */
  reports.quiet = { may_choose: ["profile"], spawnable_harnesses: [],
                    soft_blocked_by: [] };
  const late = ctx.policy();
  form.parent.value = "pi";
  ctx.sync();
  await late;
  check("a late report for an abandoned parent opens nothing",
        form.profile.disabled, true);

  /* Another harness: the transcript being copied is claude's. */
  check("a non-claude parent has no transcript to copy",
        form.fork_parent.disabled, true);

  /* Back to no parent: every row is handed back, and the create form's own
     rules decide them again. */
  const before = forkSyncs;
  form.parent.value = "";
  ctx.sync();
  check("clearing the parent hands the rows back",
        ["profile", "cwd", "args"].map((k) => form[k].disabled),
        [false, false, false]);
  check("the inherit entry goes with it, and a real profile is selected",
        [form.profile.options[0].value, form.profile.value], ["work", "work"]);
  check("the blank directory entry is the daemon's own again",
        form.cwd.options[0].textContent, "(daemon cwd)");
  check("the root form has only the qualified profile selector",
        form.harness, undefined);
  check("the fork row goes with it",
        box_["new-fork-row"].classes.has("hidden"), true);
  check("and the create form re-derives its own greying",
        forkSyncs > before, true);

  /* A parent that vanishes between polls (killed, cleared) takes the spawn
     with it rather than posting a child to a session that is not there. */
  form.parent.value = "lead";
  ctx.sync();
  check("the parent resolves while it is live", ctx.parentOf().name, "lead");
  ctx.setSessions(SESSIONS.filter((s) => s.name !== "lead"));
  ctx.refresh();
  check("a parent that vanished falls back to a session of its own",
        [form.parent.value, ctx.parentOf()], ["", null]);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("spawnform_check: ok");
}

main();
