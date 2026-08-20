/* The create form as `claunch spawn`, run against the real functions from
   app.js.

   Naming a parent turns Create into a spawn, and the two differ in what may
   be asked: a child inherits everything that decides what runs (harness,
   profile, login, directory, args), so those rows grey out and never travel
   — the spawn policy would refuse most of them field by field, and a form
   that offers what it cannot send teaches the policy wrong. What it gains is
   the fork: the child starts from a copy of the parent's conversation, which
   is only on offer where there is one to copy (a claude parent with a pinned
   conversation). And the request has to land on the right endpoint, with the
   child's own name — the spawn one wraps its answer, the create one does
   not. */
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

/* The constant the disabling reads from — sliced too, so the harness cannot
   drift from the list app.js actually uses. */
function sliceConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const end = src.indexOf(";", src.indexOf("]", start));
  return src.slice(start, end + 1);
}

/* ---- stub DOM: the form's controls, and the two rows that hide ---- */
function control(value = "") {
  return { value, checked: false, disabled: false, options: [] };
}
function box(id) {
  return {
    id, textContent: "", title: "", classes: new Set(),
    classList: {
      add: (c) => box_[id].classes.add(c),
      remove: (c) => box_[id].classes.delete(c),
      toggle: (c, on) => (on ? box_[id].classes.add(c) : box_[id].classes.delete(c)),
    },
  };
}
const box_ = {};
for (const id of ["parent-hint", "new-fork-row"]) box_[id] = box(id);

const form = {
  parent: control(""), name: control(""), harness: control("claude"),
  profile: control(""), borrow: control(""), null_token: control(""),
  cwd: control(""), args: control(""), resume: control(""), fork: control(""),
  fork_parent: control(""), role: control(""),
};
// The parent picker is a real <select> in the page; here it is the option
// list refreshParentChoices rebuilds, plus the value it settles on.
form.parent.innerHTML = "";
Object.defineProperty(form.parent, "innerHTML", {
  set(v) { if (v === "") form.parent.options.length = 0; },
  get() { return ""; },
});
form.parent.appendChild = (o) => form.parent.options.push(o);

let forkSyncs = 0;
const ctx = {};
new Function(
  "exports", "$", "document", "Option", "sessionsCache", "syncForkAvailability",
  [sliceConst("SPAWN_INHERITS"), slice("refreshParentChoices"),
   slice("spawnParent"), slice("syncSpawnMode")].join("\n") + `
exports.refresh = refreshParentChoices;
exports.sync = syncSpawnMode;
exports.parentOf = spawnParent;
exports.setSessions = (s) => { sessionsCache = s; };
`)(ctx,
   (id) => (id === "new-session" ? form : box_[id] || null),
   // The picker is reached the way the page reaches it, by selector.
   { querySelector: (sel) => (sel.includes("name=parent") ? form.parent : null) },
   function Option(label, value) { return { label, value }; },
   [],
   () => { forkSyncs++; });

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const SESSIONS = [
  { name: "lead", status: "idle", harness: "claude", conversation_id: "c-1" },
  { name: "quiet", status: "busy", harness: "claude" },      // nothing pinned
  { name: "pi", status: "idle", harness: "codex", conversation_id: "c-2" },
  { name: "gone", status: "exited", harness: "claude", conversation_id: "c-3" },
];
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
      ["harness", "cwd", "args"].map((k) => form[k].disabled), [false, false, false]);
check("the fork row is hidden until there is a parent",
      box_["new-fork-row"].classes.has("hidden"), true);
check("the create form's own rows are re-derived by their owner",
      forkSyncs > 0, true);

/* Naming a parent greys everything a child inherits and says why. */
form.parent.value = "lead";
ctx.sync();
check("a child inherits what decides how it runs",
      ["harness", "profile", "borrow", "null_token", "cwd", "args", "resume",
       "fork"].map((k) => form[k].disabled),
      [true, true, true, true, true, true, true, true]);
check("the rows that make it a different worker still travel",
      form.role.disabled, false);
check("the hint names the parent and what comes from it",
      [box_["parent-hint"].classes.has("hidden"),
       box_["parent-hint"].textContent.startsWith("a child of lead:")],
      [false, true]);

/* The fork: offered here, because this parent is claude and has a
   conversation pinned. */
check("the fork row appears with the parent",
      box_["new-fork-row"].classes.has("hidden"), false);
check("a claude parent with a conversation can be forked",
      form.fork_parent.disabled, false);
form.fork_parent.checked = true;

/* A parent with nothing pinned cannot be forked, and a tick already given
   is taken back rather than sent into a refusal. */
form.parent.value = "quiet";
ctx.sync();
check("a parent with no conversation cannot be forked",
      [form.fork_parent.disabled, form.fork_parent.checked], [true, false]);
check("...and the row says why",
      box_["new-fork-row"].title, "the parent has no claude conversation to copy");

/* Another harness: the transcript being copied is claude's. */
form.parent.value = "pi";
ctx.sync();
check("a non-claude parent has no transcript to copy",
      form.fork_parent.disabled, true);

/* Back to no parent: every row is handed back, and the create form's own
   rules decide them again. */
const before = forkSyncs;
form.parent.value = "";
ctx.sync();
check("clearing the parent hands the rows back",
      ["harness", "profile", "cwd", "args"].map((k) => form[k].disabled),
      [false, false, false, false]);
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
