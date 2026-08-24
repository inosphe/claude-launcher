/* The rail's cflow badge, run against the real functions from app.js.

   Each session row grows one line saying where that session's workflow run
   stands. What has to hold: the line appears only for a run scoped to that
   session; a run stopped on a HUMAN (gate approval, branch choice) is
   flagged as the reader's move, while one delegated to another agent is
   not; after a migrate-session leaves a stale run under the old cwd, the
   run whose canonical cwd still holds the live session wins; and the badge
   element survives re-application, because its click listener (the walk to
   the run page) is attached once for its lifetime. */
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

/* ---- stub DOM ---- */
function el(tag) {
  const node = {
    tag, className: "", title: "", dataset: {}, children: [],
    listeners: {}, parent: null,
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    append(...cs) { for (const c of cs) this.appendChild(c); },
    remove() {
      if (this.parent) {
        this.parent.children.splice(this.parent.children.indexOf(this), 1);
      }
    },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    querySelector(sel) {
      return this.children.find(
        (c) => c.className.split(" ").includes(sel.slice(1))
      ) || null;
    },
    querySelectorAll(sel) {
      if (sel === "li[data-name]") {
        return this.children.filter((c) => c.tag === "li" && c.dataset.name);
      }
      throw new Error(`unexpected selector ${sel}`);
    },
  };
  // Assigning textContent clears the children, like the real DOM — the
  // badge is rebuilt through exactly that assignment on every poll.
  let text = "";
  Object.defineProperty(node, "textContent", {
    get() { return text; },
    set(v) { text = v; node.children.length = 0; },
  });
  return node;
}

const list = el("ul");
function row(name) {
  const li = el("li");
  li.dataset.name = name;
  list.appendChild(li);
  return li;
}

const location = { hash: "" };
const ctx = {};
/* `cflowCache` is an app.js global; here it is a parameter of the wrapper,
   which the sliced functions and setRuns share as one binding. */
new Function(
  "exports", "$", "document", "location", "cflowCache",
  [slice("wfDotClass"), slice("askWho"), slice("answerFellToUs"),
   slice("sessCflowRun"),
   slice("sessCflowGated"), slice("sessCflowLabel"),
   slice("applyCflowBadges")].join("\n") + `
exports.apply = applyCflowBadges;
exports.setRuns = (runs) => { cflowCache = runs; };
`)(ctx, (id) => (id === "session-list" ? list : null),
   { createElement: el }, location, []);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const badge = (li) => li.querySelector(".sess-cflow");
const badgeText = (li) => {
  const b = badge(li);
  return b && b.children[1] ? b.children[1].textContent : null;
};

const rows = { s19: row("s19"), quiet: row("quiet"), old: row("old") };

/* A running step: the badge names the workflow and the step, blue dot, and
   the neighbouring session shows nothing. */
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "step", workflow: "ship",
               step_id: "build", title: "Build it", sessions: ["s19"] }]);
ctx.apply();
check("a running step is named on its session's row",
      badgeText(rows.s19), "ship · Build it");
check("the dot carries the running colour",
      badge(rows.s19).children[0].className, "dot wf-running");
check("a session with no run grows no badge", badge(rows.quiet), null);
check("a running step is not flagged as the reader's move",
      badge(rows.s19).className, "sess-cflow");

/* A branch choice in front of a human: amber-flagged, and the hover text
   carries the question and its options. */
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "waiting_selection",
               workflow: "ship", prompt: "which lane?",
               options: ["fast", "safe"], sessions: ["s19"] }]);
ctx.apply();
check("a branch choice is flagged as the reader's move",
      badge(rows.s19).className, "sess-cflow gated");
check("the flagged line says a choice is owed",
      badgeText(rows.s19), "ship · ⚑ choose an option");
check("the hover text carries the question and the options",
      badge(rows.s19).title, "which lane? — options: fast, safe");

/* Gate approvals, in their flavours. */
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "waiting_approval",
               reason: "gate", gate: "ship it?", sessions: ["s19"] }]);
ctx.apply();
check("a gate is flagged and asks for approval",
      [badge(rows.s19).className, badgeText(rows.s19)],
      ["sess-cflow gated", "cflow · ⚑ approval needed"]);
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "waiting_approval",
               reason: "loop_limit", workflow: "ship", sessions: ["s19"] }]);
ctx.apply();
check("a loop limit says what approving does",
      badgeText(rows.s19), "ship · ⚑ loop limit — approve to continue");

/* Delegated to another agent: shown, but deliberately NOT the reader's
   move — it is with a peer, and amber would grow a queue of fake work. */
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "waiting_answer",
               workflow: "ship",
               ask: { asked: [{ kind: "member", handle: "reviewer" }] },
               sessions: ["s19"] }]);
ctx.apply();
check("a question with another agent is not flagged",
      badge(rows.s19).className, "sess-cflow");
check("it says who has it", badgeText(rows.s19), "ship · with reviewer");

/* After a migrate-session two runs share the scope; the one whose canonical
   cwd still holds the live session wins, and the click key follows it. */
ctx.setRuns([
  { scope: "s19", cwd: "F:/stale", status: "step", workflow: "ship",
    title: "Old", sessions: [] },
  { scope: "s19", cwd: "F:/fresh", status: "step", workflow: "ship",
    title: "New", sessions: ["s19"] },
]);
ctx.apply();
check("the run whose cwd holds the live session wins",
      badgeText(rows.s19), "ship · New");
check("the click key names that run", badge(rows.s19).dataset.wf,
      "s19|F:/fresh");
const firstBadge = badge(rows.s19);
firstBadge.listeners.click({ stopPropagation() {} });
check("clicking the badge walks to the run page",
      location.hash, "#/wf/" + encodeURIComponent("s19|F:/fresh"));

/* The badge element is reused across polls — the click listener is attached
   once, so a rebuilt-in-place badge must be the same node. */
ctx.setRuns([{ scope: "s19", cwd: "F:/fresh", status: "done",
               workflow: "ship", sessions: ["s19"] }]);
ctx.apply();
check("re-application reuses the badge element",
      badge(rows.s19) === firstBadge, true);
check("a finished run reads as done", badgeText(rows.s19), "ship · done");

/* A run that ends (archived) takes its badge with it; an idle slot with only
   a pending start never grows one. */
ctx.setRuns([{ scope: "s19", cwd: "F:/fresh", status: "idle",
               pending_start: { name: "ship" }, sessions: ["s19"] }]);
ctx.apply();
check("an idle slot shows no badge", badge(rows.s19), null);
ctx.setRuns([]);
ctx.apply();
check("no runs, no badges",
      Object.values(rows).map((li) => badge(li)), [null, null, null]);

/* ---- waiting_answer: with a peer, versus put to nobody ---------------- */
/* Same status word, opposite meaning for the person reading the rail. The
   delivered one must stay quiet (its own colour, not the gate's amber, or
   the rail grows a queue of things that are not the reader's); the stranded
   one must show up as theirs, because nobody else will ever clear it. */
const answerRow = (ask) => [{
  scope: "s19", cwd: "F:/repo", status: "waiting_answer", workflow: "ship",
  step_id: "plan", sessions: ["s19"], ask,
}];

ctx.setRuns(answerRow({ prompt: "ship?", asked: [{ handle: "lead" }] }));
ctx.apply();
check("a delivered ask names its holder", badgeText(rows.s19), "ship · with lead");
check("...and keeps the delegated colour, not the gate's amber",
      badge(rows.s19).children[0].className, "dot wf-delegated");

ctx.setRuns(answerRow({ prompt: "ship?", asked: [] }));
ctx.apply();
/* The flag is the whole point: sessCflowGated now counts this as the
   reader's move, so the rail marks it like any other gate. */
check("an ask that reached nobody says so, and is flagged as yours", badgeText(rows.s19),
      "ship · ⚑ asked of nobody — approve to continue");
check("...and takes the gate's amber, because it IS the reader's",
      badge(rows.s19).children[0].className, "dot wf-waiting");

ctx.setRuns(answerRow(undefined));
ctx.apply();
check("no ask at all reads the same way (a forced goto leaves this)",
      badgeText(rows.s19), "ship · ⚑ asked of nobody — approve to continue");

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railbadge_check: ok");
