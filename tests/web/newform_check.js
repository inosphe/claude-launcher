/* The create form's reading order, checked against the shipped markup.

   The form asks for two unrelated things and used to interleave them: what a
   session IS (whose child, which mesh, what role, which run) and what it
   RUNS ON (harness, profile, login, directory, args). The second set is
   right by default nearly every time, so it now folds shut under the first —
   and the opening task, whose right answer depends on every row above it,
   sits near the bottom, with only the board question below it: two of that
   question's three answers are read off the task, so it cannot be asked
   first.

   Three things about that arrangement can break silently, so they are pinned
   here:

   - The order itself. A reorder done by moving blocks of HTML is exactly the
     edit that drops a row on the floor, so the named controls are checked as
     a list, not a set.
   - What the fold contains. It must be EXACTLY the rows a child inherits
     (SPAWN_INHERITS, sliced from app.js): the fold's summary speaks for the
     parent when a parent is named, and that is a lie the moment the fold
     holds a row that does not in fact travel.
   - That every field the submit handler reads is still on the form. `f.args`
     on a form with no args row is `undefined`, and the failure surfaces as a
     TypeError at Create time — after the user has filled the form in.

   Plus the two rules that make a shut fold safe, run against stubs:

   - renderRuntimeSummary, because the fold hides the directory and a session
     created in the wrong checkout does not report the mistake — the summary
     line is the only part that stays visible. On a child it may only speak
     for the rows the spawn policy left OPEN: a greyed row still holds
     whatever the form was last showing, which is not what gets created.
   - syncRuntimeFold, because a row the policy hands back is a decision the
     operator cannot see is theirs while it is folded away. It opens the fold
     for that and never shuts it — and, since syncSpawnMode runs on the
     two-second poll, it must not re-open one the operator closed. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

let failures = 0;
function cls() {
  const set = new Set();
  return {
    has: (c) => set.has(c),
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
    toggle: (c, on) => (on ? set.add(c) : set.delete(c)),
  };
}
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- the markup ---- */
function block(open, close, from = 0) {
  const start = html.indexOf(open, from);
  if (start < 0) throw new Error(`cannot locate ${open} in index.html`);
  const end = html.indexOf(close, start);
  if (end < 0) throw new Error(`unclosed ${open} in index.html`);
  return { start, end, text: html.slice(start, end) };
}

const form = block('<form id="new-session">', "</form>");
const fold = block('<details id="new-runtime">', "</details>", form.start);

function named(text) {
  return [...text.matchAll(/\bname="(\w+)"/g)].map((m) => m[1]);
}
function ids(text) {
  return [...text.matchAll(/\bid="([\w-]+)"/g)].map((m) => m[1]);
}

/* The reading order: who it is, what it joins, how it runs (folded), what
   it is told first. */
check("the form's controls read in the new order", named(form.text), [
  // who it is
  "parent", "fork_parent", "over_limit", "name",
  // what it joins, and what it drives
  "mesh", "handle", "role", "workflow", "context",
  // how it runs — folded
  "harness", "profile", "borrow", "null_token", "cwd", "resume", "fork", "args",
  // what it is told first
  "task",
  // where that job is written down — three radios sharing one name, then
  // the picker that only one of them opens
  "beads", "beads", "beads", "issue",
]);

check("the arrangement is asked before the machinery",
      named(form.text).indexOf("mesh") < named(form.text).indexOf("harness"),
      true);
check("the opening task is the last thing asked before the board",
      named(form.text).filter((n) => n !== "beads" && n !== "issue").slice(-1),
      ["task"]);
check("...and it is not inside the fold",
      named(fold.text).includes("task"), false);
check("the fold sits between the two",
      [form.text.indexOf("new-onboard") < form.text.indexOf("new-runtime"),
       form.text.indexOf("new-runtime") < form.text.indexOf("new-task")],
      [true, true]);
check("the board question comes after the task it is read off",
      form.text.indexOf("new-task") < form.text.indexOf("new-beads"), true);
check("...and is not inside the fold either",
      named(fold.text).includes("beads"), false);

/* The three answers, and the one that opens the picker. A radio group that
   lost its default would submit nothing at all — and the absent answer is
   the one that mints an issue, so the form would silently stop doing what it
   did before this row existed. */
const beadsBox = block('<fieldset id="new-beads">', "</fieldset>", form.start);
check("the board question offers exactly three answers",
      [...beadsBox.text.matchAll(/name="beads" value="(\w+)"/g)].map((m) => m[1]),
      ["new", "existing", "none"]);
check("minting from the task is the one that is checked",
      /value="new" checked/.test(beadsBox.text), true);
check("the issue picker is hidden until it is the answer",
      /id="new-issue-row" class="hidden"/.test(beadsBox.text), true);

/* The hints travel with the field they explain — a directory warning left
   above the fold would be pointing at a row that is not on screen. */
check("cwd's hint is inside the fold with cwd",
      ids(fold.text).includes("cwd-hint"), true);
check("the role stance and the parent hint are not",
      ["role-stance", "parent-hint"].map((i) => ids(fold.text).includes(i)),
      [false, false]);
check("the fold carries the summary line the JS writes into",
      ids(fold.text).includes("new-runtime-sum"), true);

/* ---- the fold's membership IS the inheritance list ---- */
function sliceConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const end = src.indexOf(";", src.indexOf("]", start));
  return src.slice(start, end + 1);
}
const INHERITS = new Function(
  `${sliceConst("SPAWN_INHERITS")}; return SPAWN_INHERITS;`)();
check("the fold holds exactly the rows a child inherits",
      named(fold.text).slice().sort(), INHERITS.slice().sort());

/* ---- every field Create reads is still on the form ---- */
function sliceFrom(marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error(`cannot locate ${marker} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${marker}`);
}
const submit = sliceFrom('$("new-session").addEventListener("submit"');
const read = [...new Set(
  [...submit.matchAll(/\bf\.(\w+)\.(?:value|checked)\b/g)].map((m) => m[1])
)].sort();
const present = named(form.text).slice().sort();
check("Create reads nothing the form does not offer",
      read.filter((k) => !present.includes(k)), []);
check("...and it does read a fair few of them", read.length > 8, true);

/* The board's own rule, read out of the submit handler: "new" is the absence
   of both keys. A request that says nothing gets an issue minted from its
   task, which is what every client written before this row still sends — so
   the default answer must produce exactly that request. */
check("only 'none' and 'existing' put a board key on the request",
      [/body\.beads = false/.test(submit),
       /body\.issue = f\.issue\.value/.test(submit),
       /body\.beads = true/.test(submit)],
      [true, true, false]);

/* ---- the board row, against stubs ----
   The whole reason this row exists is that picking an issue somebody is
   already working means something different from picking a free one, and the
   difference is invisible until a session has been created on it. So the two
   things pinned here are: the picker says "would JOIN" on the rows the daemon
   marked held, and the hint under it spells out what that costs. */
{
  const issueSel = {
    value: "", innerHTML: "", options: [],
    appendChild(o) { this.options.push(o); },
  };
  const hintBox = { textContent: "", classList: cls() };
  const rowBox = { classList: cls() };
  const bf = { beads: { value: "new" }, issue: issueSel };
  const bctx = {};
  new Function("exports", "$", "Option", "issuesCache", "issuesError",
    sliceFrom("function beadsMode()") + "\n" +
    sliceFrom("function syncBeadsRow()") + "\n" +
    sliceFrom("function renderIssueOptions()") + "\n" +
    "exports.mode = beadsMode;\n" +
    "exports.sync = syncBeadsRow;\n" +
    "exports.render = renderIssueOptions;\n")(
    bctx,
    (id) => ({ "new-session": bf, "new-issue-row": rowBox,
               "new-issue-hint": hintBox }[id] || null),
    function Opt(text, value) { return { text, value }; },
    [
      { id: "cl-1", title: "wire the rail", status: "open", held_by: null },
      { id: "cl-2", title: "the leader's own", status: "in_progress",
        held_by: "lead" },
    ],
    "");

  bctx.render();
  check("the picker leads with an unchosen row, then the board's",
        issueSel.options.map((o) => o.value), ["", "cl-1", "cl-2"]);
  check("a free issue reads as itself",
        issueSel.options[1].text, "cl-1  wire the rail [open]");
  check("an issue somebody holds says who, and what picking it would do",
        issueSel.options[2].text,
        "cl-2  the leader's own [in_progress] — held by lead, would JOIN");

  check("the picker is hidden while the answer is 'new'",
        [rowBox.classList.has("hidden"), hintBox.classList.has("hidden")],
        [true, true]);

  bf.beads.value = "existing";
  bf.issue.value = "cl-1";
  bctx.sync();
  check("choosing a free issue opens the row and warns about nothing",
        [rowBox.classList.has("hidden"), hintBox.classList.has("hidden")],
        [false, true]);

  bf.issue.value = "cl-2";
  bctx.sync();
  check("choosing a held one spells out that it is a JOIN, not a takeover",
        [hintBox.classList.has("hidden"),
         /lead is assigned to cl-2/.test(hintBox.textContent),
         /JOINS it/.test(hintBox.textContent),
         /assignment stays put/.test(hintBox.textContent)],
        [false, true, true, true]);
}

/* ---- renderRuntimeSummary ---- */
const sumBox = { textContent: "" };
function ctl(v) { return { value: v, checked: false, disabled: false }; }
const f = {
  harness: ctl("claude"), profile: ctl(""), borrow: ctl(""),
  null_token: ctl(""), args: ctl(""), resume: ctl(""), parent: ctl(""),
  cwd: { value: "", disabled: false, selectedIndex: 0,
         options: [{ text: "(daemon cwd)" }] },
};
let parentSession = null;
const ctx = {};
new Function("exports", "$", "spawnParent", "PICKER",
  sliceFrom("function renderRuntimeSummary()") +
  "\nexports.render = renderRuntimeSummary;\n")(
  ctx,
  (id) => (id === "new-runtime-sum" ? sumBox : id === "new-session" ? f : null),
  () => parentSession,
  "@picker");

/* Before the workspace list arrives the directory row holds nothing, and a
   line that filled the gap with "(daemon cwd)" would be naming a directory
   the form has not in fact settled on. */
f.cwd.options = [];
f.cwd.selectedIndex = -1;
ctx.render();
check("an unfilled directory row is left unsaid, not guessed at",
      sumBox.textContent, "— claude");

f.cwd.options = [{ text: "(daemon cwd)" }];
f.cwd.selectedIndex = 0;
ctx.render();
check("the default says what it would create",
      sumBox.textContent, "— claude · (daemon cwd)");

f.profile.value = "nc";
f.cwd.options = [{ text: "(daemon cwd)" },
                 { text: "launcher — F:/works/claude-launcher" }];
f.cwd.selectedIndex = 1;
ctx.render();
check("a chosen profile and workspace ride on the fold's face",
      sumBox.textContent, "— claude · nc · launcher");

f.borrow.value = "work";
f.null_token.checked = true;
f.args.value = "--verbose";
f.resume.value = "@picker";
ctx.render();
check("the rest is named only once it is set",
      sumBox.textContent,
      "— claude · nc · launcher · borrow work · --null · resume (picker) · +args");

f.resume.value = "lead";
ctx.render();
check("a named conversation is named",
      sumBox.textContent.endsWith("resume lead · +args"), true);

/* A child whose policy opens nothing: every folded row is the parent's, and
   the values the form happens to be holding say nothing about what would be
   created. Naming them would be the fold's face lying about what it hides. */
parentSession = { name: "lead" };
for (const k of INHERITS) if (f[k]) f[k].disabled = true;
ctx.render();
check("a fully locked child shows the inherited profile harness",
      sumBox.textContent, "— lead's setup · claude");

/* A child whose policy hands two rows back. Those two speak; the rest stay
   the parent's and stay unnamed — which rows are shut is the parent hint's
   job, above the fold. */
f.profile.disabled = false;
f.cwd.disabled = false;
ctx.render();
check("only open rows plus read-only harness speak for a child",
      sumBox.textContent, "— lead's setup · claude · nc · launcher");

/* Unlocking the harness adds it; the still-locked borrow/--null/args do not
   come back with it. */
f.harness.disabled = false;
ctx.render();
check("an unlocked harness joins them, a locked borrow does not",
      sumBox.textContent, "— lead's setup · claude · nc · launcher");

/* Back to a session of its own: every row speaks again, disables and all —
   the create form's own greying (a non-claude harness) is not the policy's. */
parentSession = null;
ctx.render();
check("with no parent every row speaks again",
      sumBox.textContent,
      "— claude · nc · launcher · borrow work · --null · resume lead · +args");

/* Served against a page that predates the fold (a daemon serving older
   assets), the summary has nowhere to go — and must not take the form
   down with it. */
const bare = {};
new Function("exports", "$", "spawnParent", "PICKER",
  sliceFrom("function renderRuntimeSummary()") +
  "\nexports.render = renderRuntimeSummary;\n")(
  bare, () => null, () => null, "@picker");
let threw = false;
try { bare.render(); } catch { threw = true; }
check("no summary line is a no-op, not a crash", threw, false);

/* ---- the wiring, not just the rules ----
   Both rules above read `.disabled`, which only means anything AFTER the
   spawn policy has been applied — and syncSpawnMode is what applies it. A
   browser caught this the stubs could not: the summary was rendered from
   syncForkAvailability alone, which on a child runs BEFORE the policy, so
   picking a parent left the line reporting the create form's own values
   forever. The rules are checked in isolation below; that they are reached
   from the one place that knows the answer is checked here. */
const spawnMode = sliceFrom("function syncSpawnMode()");
check("child mode re-renders the summary where the policy has just spoken",
      /renderRuntimeSummary\(\)/.test(spawnMode), true);
check("...and asks the fold whether anything inside became the operator's",
      /syncRuntimeFold\(/.test(spawnMode), true);

/* ---- syncRuntimeFold ---- */
/* The fold is shut by default. That is right while everything inside it is
   the parent's, and wrong the moment the spawn policy hands a row back: an
   unlocked row folded away is a decision the operator cannot see is theirs.
   The rule opens the fold for that — and must not fight the operator, since
   syncSpawnMode runs on every two-second poll. */
const foldBox = { open: false };
const g = {};
for (const k of INHERITS) g[k] = { disabled: true };
const foldCtx = {};
new Function("exports", "$", "SPAWN_INHERITS",
  "let runtimeFoldOpenedFor = null;\n" + sliceFrom("function syncRuntimeFold(") +
  "\nexports.sync = syncRuntimeFold;\n")(
  foldCtx, (id) => (id === "new-runtime" ? foldBox : null), INHERITS);

foldCtx.sync(g, null);
check("a session of its own never opens the fold", foldBox.open, false);

foldCtx.sync(g, { name: "lead" });
check("a child whose policy opens nothing leaves the fold shut",
      foldBox.open, false);

g.profile.disabled = false;
foldCtx.sync(g, { name: "lead" });
check("a row the policy handed back opens the fold", foldBox.open, true);

/* The operator shuts it. The poll comes round again with the same parent and
   the same unlocks — and must leave it shut. */
foldBox.open = false;
foldCtx.sync(g, { name: "lead" });
foldCtx.sync(g, { name: "lead" });
check("the poll does not re-open a fold the operator shut",
      foldBox.open, false);

/* A different unlock set is new information, so it opens again. */
g.args.disabled = false;
foldCtx.sync(g, { name: "lead" });
check("a policy that opens another row is new information",
      foldBox.open, true);

/* So is a different parent. */
foldBox.open = false;
foldCtx.sync(g, { name: "other" });
check("so is a different parent", foldBox.open, true);

/* Leaving child mode never shuts it — the operator is looking at rows that
   are theirs again. */
foldCtx.sync(g, null);
check("clearing the parent leaves the fold as the operator had it",
      foldBox.open, true);

/* ...and having left, coming back to the same parent is new information
   again rather than a state the latch still remembers. */
foldBox.open = false;
foldCtx.sync(g, { name: "other" });
check("returning to a parent opens it again", foldBox.open, true);

/* A page that predates the fold has no element to open. */
const bareFold = {};
new Function("exports", "$", "SPAWN_INHERITS",
  "let runtimeFoldOpenedFor = null;\n" + sliceFrom("function syncRuntimeFold(") +
  "\nexports.sync = syncRuntimeFold;\n")(bareFold, () => null, INHERITS);
let foldThrew = false;
try { bareFold.sync(g, { name: "lead" }); } catch { foldThrew = true; }
check("no fold element is a no-op, not a crash", foldThrew, false);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("newform_check: ok");
