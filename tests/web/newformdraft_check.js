/* The New tab's draft — the answers a reload must give back (claunch-9em4k).

   A hard reload (ctrl+shift+R) put every row of the New session form back on
   its default. Two things made it so: the only record the form kept was
   written by a launch that went through and held six rows, and its restore
   stepped over a row that already holds something — which the Profile row
   always does, since refreshProfiles picks the first profile before anybody
   asks. The draft is written on every change the operator makes on the New
   tab and read back once per page load, after the option lists are filled,
   in the order the rows depend on one another.

   This harness runs the real save/restore functions against a stub form
   whose listeners rebuild the dependent rows the way the page's do, so a
   restore that sets a row before its options exist fails here. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) return;
  failures++;
  console.error(`FAIL ${label}\n  got  ${g}\n  want ${w}`);
}

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
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

/* ---- what the draft holds --------------------------------------------- */
const FIELDS = constant("NEW_FORM_DRAFT_FIELDS");
const SPENT = constant("SESSION_FORM_SPENT");
check("no row one launch spends is kept",
      SPENT.filter((n) => FIELDS.includes(n)), []);
check("...nor the per-launch identity rows the launch record also leaves out",
      ["handle", "parent", "issue", "worktree_name", "worktree_existing",
       "worktree_from", "worktree_rebase", "project"]
        .filter((n) => FIELDS.includes(n)), []);
check("the rows the reload lost are all kept",
      ["cwd", "profile", "harness", "borrow", "null_token", "model", "effort",
       "mesh", "role", "workflow", "worktree_mode", "beads", "args"]
        .filter((n) => !FIELDS.includes(n)), []);
check("each row is restored after the rows its options follow",
      ["cwd", "profile", "harness", "borrow", "model"].map((n) => FIELDS.indexOf(n))
        .every((v, i, a) => i === 0 || v > a[i - 1]) &&
      FIELDS.indexOf("mesh") < FIELDS.indexOf("role") &&
      FIELDS.indexOf("role") < FIELDS.indexOf("workflow"), true);
check("the key is BASE-scoped like the other form records",
      /const NEW_FORM_DRAFT_KEY = `claunch_new_form:\$\{BASE\}`;/.test(src), true);

/* ---- where it runs ----------------------------------------------------- */
const boot = slice("boot");
check("the first boot restores the draft and only then routes",
      /await Promise\.resolve\(restoreNewFormDraft\(\)\)\.catch\(\(\) => \{\}\);\s*route\(\);/
        .test(boot), true);
check("the form's change and input events write it",
      /for \(const type of \["change", "input"\]\) \{\s*\$\("new-session"\)\.addEventListener\(type, saveNewFormDraft\);/
        .test(src), true);

/* ---- the stub form ----------------------------------------------------- */
function el(tag, props = {}) {
  const n = Object.assign({
    tagName: tag.toUpperCase(), type: "", value: "", checked: false,
    disabled: false, handlers: {},
    addEventListener(t, fn) { (n.handlers[t] ||= []).push(fn); },
    dispatchEvent(ev) {
      ev.target ||= n;
      (n.handlers[ev.type] || []).forEach((fn) => fn(ev));
      if (ev.bubbles && n.form && n.form !== n) n.form.dispatchEvent(ev);
      return true;
    },
  }, props);
  return n;
}
function select(values) {
  const s = el("select");
  s.fill = (vs) => {
    s.options = vs.map((v) => ({ value: v, disabled: false }));
    if (!s.options.some((o) => o.value === s.value)) s.value = vs[0] ?? "";
  };
  s.fill(values);
  return s;
}
const checkbox = () => el("input", { type: "checkbox" });
function radios(values, on) {
  const group = values.map((v) => el("input", { type: "radio", value: v }));
  // One checked radio per group, as the browser keeps it.
  for (const r of group) {
    let state = r.value === on;
    delete r.checked;
    Object.defineProperty(r, "checked", {
      get: () => state,
      set: (v) => {
        if (v) group.forEach((o) => { if (o !== r) o.checked = false; });
        state = !!v;
      },
    });
  }
  Object.defineProperty(group, "value", {
    get: () => (group.find((r) => r.checked) || { value: "" }).value,
  });
  return group;
}

const stored = {};
const localStorage = {
  getItem: (k) => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
};
const calls = [];

/* What the daemon offers, per row, keyed by the row each list follows. */
const OFFER = {
  harnesses: { work: ["claude"], home: ["claude", "codex"] },
  lenders: { "work:claude": [""], "home:claude": ["", "lead"], "home:codex": [""] },
  models: { claude: ["", "opus", "sonnet"], codex: ["", "gpt-5"] },
  roles: { "": ["", "worker"], team: ["", "worker", "leader"] },
};

function buildForm() {
  const f = el("form");
  f.cwd = select(["", "F:/a", "F:/b"]);
  f.profile = select(["work", "home"]);
  f.harness = select(OFFER.harnesses.work);
  f.borrow = select([""]);
  f.null_token = checkbox();
  f.model = select([""]);
  f.effort = select(["", "low", "high"]);
  for (const n of ["skip_permissions", "codex_yolo", "codex_sandbox",
                   "auto_compact", "over_limit", "score_goal"]) f[n] = checkbox();
  f.args = el("input");
  f.mesh = select([""]);
  f.role = select(OFFER.roles[""]);
  f.workflow = select(["", "improv-worker", "improv-leader"]);
  f.worktree_mode = radios(["", "new", "existing"], "");
  f.beads = radios(["new", "existing", "none", "none-auto"], "new");
  f.name = el("input"); f.task = el("textarea");
  f.elements = f;
  for (const k of Object.keys(f)) {
    const c = f[k];
    if (c && c.tagName && c !== f) c.form = f;
    if (Array.isArray(c)) c.forEach((r) => { r.form = f; });
  }
  // The page's own listeners, reduced to what they do to the rows after them.
  f.profile.addEventListener("change", () => {
    f.harness.fill(OFFER.harnesses[f.profile.value] || []);
  });
  f.addEventListener("input", () => {
    // renderNew*Runtime: the Model row is rebuilt when its harness moves,
    // back on its default.
    if (f.model._for !== f.harness.value) {
      f.model.value = "";
      f.model.fill(OFFER.models[f.harness.value] || [""]);
      f.model._for = f.harness.value;
    }
  });
  for (const r of f.beads) r.addEventListener("change", () => calls.push("beads-row"));
  return f;
}

let form = buildForm();
let view = { mode: "new" };
function $(id) { return id === "new-session" ? form : null; }
function sessionFormView() { return view; }
function applySessionCwdChange() { calls.push("cwd-change:" + form.cwd.value); }
async function syncNewBorrowOptions() {
  await null; // the lender list is a fetch
  form.borrow.fill(OFFER.lenders[`${form.profile.value}:${form.harness.value}`] || [""]);
  calls.push("borrow-fill");
}
async function refreshRoles(mesh) {
  await null;
  form.role.fill(OFFER.roles[mesh] || [""]);
  calls.push("roles:" + mesh);
}
async function refreshWorkflowChoices() { await null; calls.push("workflows"); }
function syncOnboardPickers() {
  if (!form.mesh.options.some((o) => o.value === "team")) form.mesh.fill(["", "team"]);
  // The role's own workflow, until somebody picks one.
  if (!ctx.wfPicked() && form.role.value === "worker") form.workflow.value = "improv-worker";
}
function renderRoleStance() { calls.push("stance"); }
function refreshSessionConnect() { calls.push("connect"); }
function syncNewWorktree() { calls.push("worktree-sync"); }
class Event { constructor(type, opts = {}) { this.type = type; this.bubbles = !!opts.bubbles; } }

const ctx = {};
new Function("ctx", "localStorage", "$", "sessionFormView", "applySessionCwdChange",
  "syncNewBorrowOptions", "refreshRoles", "refreshWorkflowChoices",
  "syncOnboardPickers", "renderRoleStance", "refreshSessionConnect",
  "syncNewWorktree", "Event", `
const BASE = "b";
const NEW_FORM_DRAFT_FIELDS = ${JSON.stringify(FIELDS)};
const NEW_FORM_DRAFT_CHECKS = ${JSON.stringify(constant("NEW_FORM_DRAFT_CHECKS"))};
const NEW_FORM_DRAFT_KEY = \`claunch_new_form:\${BASE}\`;
let newFormDraftRestoring = false;
let newFormDraftRestored = false;
let newWfPicked = false;
let pendingWorktreeMode = "";
` + ["readNewFormDraft", "newFormDraftAnswer", "saveNewFormDraft",
     "setNewFormDraftSelect", "setNewFormDraftCheck", "restoreNewFormDraft"]
      .map(slice).join("\n") + `
Object.assign(ctx, {
  save: saveNewFormDraft, restore: restoreNewFormDraft, read: readNewFormDraft,
  key: NEW_FORM_DRAFT_KEY,
  // A new page load: the module's once-per-load flag starts over.
  reload: () => { newFormDraftRestored = false; newWfPicked = false; pendingWorktreeMode = ""; },
  wfPicked: () => newWfPicked, parked: () => pendingWorktreeMode,
  restoring: () => newFormDraftRestoring,
});
`)(ctx, localStorage, $, sessionFormView, applySessionCwdChange,
   syncNewBorrowOptions, refreshRoles, refreshWorkflowChoices,
   syncOnboardPickers, renderRoleStance, refreshSessionConnect,
   syncNewWorktree, Event);

async function main() {
  check("the key", ctx.key, "claunch_new_form:b");

  /* ---- writing it ----------------------------------------------------- */
  form.cwd.value = "F:/b";
  form.profile.value = "home";
  form.profile.dispatchEvent(new Event("change"));
  form.harness.value = "codex";
  form.args.value = "--verbose";
  form.codex_yolo.checked = true;
  form.beads[0].checked = false; form.beads[2].checked = true;
  form.worktree_mode[0].checked = false; form.worktree_mode[1].checked = true;
  form.role.value = "";
  form.workflow.value = "improv-leader";
  form.borrow.disabled = true; // a lender list still in flight
  ctx.save();
  const saved = JSON.parse(stored["claunch_new_form:b"]);
  check("an operator's change writes the rows it can see",
        [saved.cwd, saved.profile, saved.harness, saved.args, saved.codex_yolo,
         saved.beads, saved.worktree_mode, saved.role, saved.workflow],
        ["F:/b", "home", "codex", "--verbose", true, "none", "new", "", "improv-leader"]);
  check("...and nothing a launch spends", [saved.name, saved.task], [undefined, undefined]);
  check("a greyed row is not written", "borrow" in saved, false);
  stored["claunch_new_form:b"] = JSON.stringify({ ...saved, borrow: "lead" });
  ctx.save();
  check("...and keeps what the draft last held for it",
        JSON.parse(stored["claunch_new_form:b"]).borrow, "lead");
  form.borrow.disabled = false;

  view = { mode: "spawn" };
  form.args.value = "--child-only";
  ctx.save();
  check("the spawn tab does not write the New tab's draft",
        JSON.parse(stored["claunch_new_form:b"]).args, "--verbose");
  view = { mode: "new" };

  /* ---- a reload ------------------------------------------------------- */
  stored["claunch_new_form:b"] = JSON.stringify({
    cwd: "F:/b", profile: "home", harness: "claude", borrow: "lead",
    null_token: true, model: "sonnet", effort: "high", skip_permissions: true,
    codex_yolo: false, auto_compact: true, args: "--verbose", mesh: "team",
    role: "leader", workflow: "improv-leader", worktree_mode: "new", beads: "none",
  });
  form = buildForm();
  ctx.reload();
  calls.length = 0;
  check("a fresh form stands on its defaults",
        [form.cwd.value, form.profile.value, form.harness.value, form.role.value],
        ["", "work", "claude", ""]);
  await ctx.restore();
  check("every kept row is back after the reload",
        [form.cwd.value, form.profile.value, form.harness.value, form.borrow.value,
         form.null_token.checked, form.model.value, form.effort.value,
         form.skip_permissions.checked, form.auto_compact.checked, form.args.value,
         form.mesh.value, form.role.value, form.workflow.value, form.beads.value],
        ["F:/b", "home", "claude", "lead", true, "sonnet", "high", true, true,
         "--verbose", "team", "leader", "improv-leader", "none"]);
  check("the Profile row is restored although it was never empty",
        form.profile.value, "home");
  check("the lender waits for its list, the role for its mesh's list",
        [calls.indexOf("borrow-fill") >= 0, calls.indexOf("roles:team") >= 0],
        [true, true]);
  check("the directory's dependent rows are re-asked",
        calls.includes("cwd-change:F:/b"), true);
  check("a restored workflow is a pick, not the role's default",
        ctx.wfPicked(), true);
  check("the worktree mode is parked for the sync that can carry it",
        [ctx.parked(), calls.includes("worktree-sync")], ["new", true]);
  check("the board answer's own listener runs", calls.includes("beads-row"), true);
  check("restoring writes nothing back", JSON.parse(stored["claunch_new_form:b"]).workflow,
        "improv-leader");
  check("...and leaves the save open again", ctx.restoring(), false);

  /* ---- once per load -------------------------------------------------- */
  form.args.value = "--changed-on-this-page";
  await ctx.restore();
  check("a second call in the same load does not overwrite the page",
        form.args.value, "--changed-on-this-page");

  /* ---- answers the daemon no longer offers ---------------------------- */
  stored["claunch_new_form:b"] = JSON.stringify({
    cwd: "F:/gone", profile: "retired", model: "opus", role: "ghost", beads: "nope",
  });
  form = buildForm();
  ctx.reload();
  await ctx.restore();
  check("a value with no option is not forced onto its row",
        [form.cwd.value, form.profile.value, form.role.value, form.beads.value],
        ["", "work", "", "new"]);
  check("...while the rows it does offer still come back",
        form.model.value, "opus");

  /* ---- a draft that does not parse ------------------------------------ */
  stored["claunch_new_form:b"] = "{not json";
  form = buildForm();
  ctx.reload();
  await ctx.restore();
  check("a broken record leaves the defaults", form.profile.value, "work");
  check("...and reads as no draft", ctx.read(), null);

  console.log("newformdraft_check: " + (failures ? `${failures} failing` : "ok"));
  process.exitCode = failures ? 1 : 0;
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
