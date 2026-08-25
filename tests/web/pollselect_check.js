/* The two create-form pickers the session poll feeds, and the guard that
   keeps that poll from shutting them.

   #new-session has three <select>s built from data the rail polls every two
   seconds. The workspace one has been guarded for a while: it signs the list
   it drew and returns early when the next poll brings the same one, because
   rebuilding a <select>'s options closes the native popup the user has open.
   The resume and parent pickers were not, and they are the two the user
   spends the longest in - the resume list is every conversation this daemon
   knows and the parent list is every live session, so the reader who is
   scrolling one is exactly the reader the tick lands on. Two seconds of
   reading, and the list vanishes from under the cursor.

   Signing these is not the same job as signing the workspace list, which is
   why it gets its own harness rather than a line in newform_check. A session
   record is not a stable value: it carries a pid, a context size and a
   last-activity clock that move on their own, so JSON.stringify of the poll's
   answer differs on nearly every tick and a signature over it would be a
   guard that never holds - present, plausible, and doing nothing. So each
   picker signs the fields its own rebuild reads, and this check pins BOTH
   directions of that: a tick that changed nothing the picker draws must cost
   nothing, and a tick that changed something it draws must still redraw.

   The parent picker's signature is deliberately wider than its label,
   because its rebuild ends in syncSpawnMode, which reads the picked parent's
   harness (which rows a child may differ on) and whether it has a
   conversation (whether the fork is offered). A signature narrowed to what
   the label shows would leave those stale, which is the create form lying
   about what Create would send - so that is pinned here too.

   Sliced out of the shipped app.js, not a copy of it. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"), "utf8");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

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

/* The signatures live at module scope, alongside the workspace one, because
   they have to outlive the call that wrote them - that IS the guard. A
   rewrite that made them local would still pass every behavioural check
   below if they were re-declared inside the harness, so the declarations are
   read out of app.js rather than assumed. */
for (const name of ["workspacesRendered", "resumeRendered", "parentsRendered"]) {
  check(`${name} is module state, not a local`,
        new RegExp(`^let ${name} = null;$`, "m").test(src), true);
}

/* ---- a <select> that reports being rebuilt ----------------------------
   `options` is replaced, not emptied, so a check can hold the array the
   popup was drawn from and ask whether it is still the one on the element.
   That is the property that matters: same array, same options, popup still
   open. A counter alone would not catch a rebuild that happened to produce
   an identical list - which is precisely the bug, since the list IS
   identical two seconds later and the popup shuts anyway. */
function fakeSelect() {
  const s = { options: [], value: "", rebuilds: 0 };
  s.appendChild = (o) => { s.options.push(o); };
  Object.defineProperty(s, "innerHTML", {
    get: () => "",
    set(v) { if (v === "") { s.options = []; s.rebuilds++; } },
  });
  return s;
}
const resume = fakeSelect();
const parent = fakeSelect();
const document = {
  querySelector(sel) {
    if (sel.includes("name=resume")) return resume;
    if (sel.includes("name=parent")) return parent;
    throw new Error(`unexpected querySelector(${sel})`);
  },
};
function Option(text, value) { return { text, value, disabled: false }; }

const app = {};
new Function("exports", "document", "Option", "PICKER", `
  let resumeRendered = null;
  let parentsRendered = null;
  let sessionsCache = [];
  let forkCalls = 0, spawnCalls = 0;
  function syncForkAvailability() { forkCalls++; }
  function syncSpawnMode() { spawnCalls++; }
  ${sliceFrom("function refreshResumeChoices()")}
  ${sliceFrom("function refreshParentChoices()")}
  exports.poll = (list) => {
    sessionsCache = list;
    refreshResumeChoices();
    refreshParentChoices();
  };
  exports.tails = () => [forkCalls, spawnCalls];
`)(app, document, Option, "@picker");

const DASH = "—";  // the label separator app.js writes
const labels = (sel) => sel.options.map((o) => o.text);
const values = (sel) => sel.options.map((o) => o.value);

/* A session as the poll hands it over, with the fields that move on their
   own broken out so a tick can change only those. */
function sess(name, over = {}) {
  return Object.assign({
    name, status: "idle", harness: "claude", conversation_id: "c-" + name,
    pid: 1000, ctx_used: 0, last_activity: "t0",
  }, over);
}
let list = [
  sess("alice"),
  sess("bob", { status: "busy" }),
  sess("ghost", { status: "exited" }),
  sess("fresh", { conversation_id: null }),
];
app.poll(list);

check("the resume picker offers every conversation, exited ones included",
      values(resume), ["", "@picker", "alice", "bob", "ghost"]);
check("...labelled with the status each is in now",
      labels(resume).slice(2),
      [`alice ${DASH} idle`, `bob ${DASH} busy`, `ghost ${DASH} exited`]);
check("the parent picker offers the live ones, conversation or not",
      values(parent), ["", "alice", "bob", "fresh"]);
check("both drew once", [resume.rebuilds, parent.rebuilds], [1, 1]);

/* ---- the bug ----------------------------------------------------------
   The user opens the resume dropdown, finds alice, and is still reading the
   list when the poll ticks. Nothing they can see has changed; every session
   has a new pid reading, a new context size and a new clock. */
resume.value = "alice";
parent.value = "bob";
const resumeOpen = resume.options;
const parentOpen = parent.options;
const tailsBefore = app.tails();

list = list.map((s) =>
  Object.assign({}, s, { pid: s.pid + 1, ctx_used: 4096, last_activity: "t1" }));
app.poll(list);

check("a tick that changed nothing on screen leaves the open popup alone",
      [resume.options === resumeOpen, parent.options === parentOpen],
      [true, true]);
check("...which is to say neither was rebuilt",
      [resume.rebuilds, parent.rebuilds], [1, 1]);
check("...the user's pick is still theirs",
      [resume.value, parent.value], ["alice", "bob"]);
check("...and the rebuild's tail did not run either",
      app.tails(), tailsBefore);

/* ---- and the other direction ------------------------------------------
   The guard is not "never redraw". A status the label shows is part of the
   signature, so it redraws - and a parent that stopped being live leaves,
   taking the pick that pointed at it. */
list = list.map((s) =>
  (s.name === "bob" ? Object.assign({}, s, { status: "exited" }) : s));
app.poll(list);

check("a status the label shows redraws both",
      [resume.rebuilds, parent.rebuilds], [2, 2]);
check("...the exited session keeps its conversation on offer",
      labels(resume).slice(2),
      [`alice ${DASH} idle`, `bob ${DASH} exited`, `ghost ${DASH} exited`]);
check("...but stops being offerable as a parent",
      values(parent), ["", "alice", "fresh"]);
check("...so the pick that named it falls back rather than pointing at nothing",
      parent.value, "");
check("...while the untouched resume pick survives the redraw",
      resume.value, "alice");

/* A field the parent label does NOT show, that its rebuild's tail reads
   anyway: the harness decides which rows a child may differ on. The parent
   picker must redraw for it; the resume picker, which never consults it,
   must not - the guard is per-picker, not one blunt rule. */
resume.value = "alice";
const resumeStill = resume.options;
list = list.map((s) =>
  (s.name === "alice" ? Object.assign({}, s, { harness: "pi" }) : s));
app.poll(list);

check("a parent's harness redraws the parent picker",
      parent.rebuilds, 3);
check("...and leaves the resume picker, which does not read it, open",
      [resume.rebuilds, resume.options === resumeStill], [2, true]);

/* Whether a session has a conversation decides the fork checkbox on a
   child, and decides outright whether the resume picker lists it at all. */
list = list.map((s) =>
  (s.name === "fresh" ? Object.assign({}, s, { conversation_id: "c-fresh" }) : s));
app.poll(list);

check("a conversation appearing puts the session on the resume list",
      values(resume), ["", "@picker", "alice", "bob", "ghost", "fresh"]);
check("...and redraws the parent picker too, whose fork row reads it",
      [resume.rebuilds, parent.rebuilds], [3, 4]);

/* Nothing left to resume: the pick cannot survive a list that no longer
   holds it, and "(new conversation)" is the honest fallback. */
app.poll([]);
check("a cleared daemon empties both pickers",
      [values(resume), values(parent)], [["", "@picker"], [""]]);
check("...and the stale pick is dropped, not silently kept",
      [resume.value, parent.value], ["", ""]);

/* The guard has to survive going back to a list it has drawn before: a
   signature compared against the LAST drawn list, not against a set of
   every list ever seen. */
app.poll(list);
check("returning to a previously drawn list draws it again",
      [values(resume), values(parent)],
      [["", "@picker", "alice", "bob", "ghost", "fresh"],
       ["", "alice", "fresh"]]);
check("...which is a fifth and a sixth draw, not a cached one",
      [resume.rebuilds, parent.rebuilds], [5, 6]);

if (failures) {
  console.error(`${failures} failure(s)`);
  process.exit(1);
}
console.log("pollselect_check ok");
