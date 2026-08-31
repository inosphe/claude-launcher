/* The second door on a delegated decision, as the dashboard draws it.

   A `waiting_answer` is with another agent, so the panel says so and offers a
   takeover rather than a gate. For an *approval* that takeover has always
   worked — one press, `/api/cflow/approve`. For a *branch* it did not: the
   button existed, and clicking it popped an alert telling the reader to go
   and type `claunch cflow select <option>` in a shell, on the one screen that
   already had the question, the options and their descriptions in hand.

   So this pins the branch takeover to real presses — one button per option,
   each posting that option to `/api/cflow/select` — and pins that the dead
   end is gone. `run.ask.options` is where the options live in this state (an
   ask carries its own copy); reading them from the top level would draw an
   empty strip, which is why the option names are asserted and not just the
   count. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

/* ---- stub DOM --------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, cls: "", text: "", title: "", kids: [], handlers: {},
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    get className() { return this.cls; },
    set className(v) { this.cls = String(v); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
  };
  return n;
}
const document = { createElement: (tag) => node(tag) };

/* Every press is recorded rather than performed. */
const posted = [];
const alerted = [];
function cflowAction(url, body) { posted.push({ url, body }); }
function alert(msg) { alerted.push(String(msg)); }
function confirm() { return true; }           // the reader says yes
function fmtOpensAt() { return "soon"; }
function nudgeRun() {}
function reminderControl() { return node("div"); }

const code = [
  slice("function el(tag, cls, text) {", "/* ---------------------------"),
  slice("function askWho(ask) {", "function shortenPath("),
  slice("function wfActions(data, opts = {}) {", "let wfReminderBoxes = {};"),
].join("\n");

const wfActions = new Function(
  "document", "el", "askWho", "answerFellToUs", "cflowAction", "alert",
  "confirm", "fmtOpensAt", "nudgeRun", "reminderControl",
  code + "\nreturn wfActions;"
)(document, undefined, undefined, undefined, cflowAction, alert, confirm,
  fmtOpensAt, nudgeRun, reminderControl);

/* ---- helpers ---------------------------------------------------------- */
function buttons(box) {
  const out = [];
  (function walk(n) {
    if (n.tag === "button") out.push(n);
    (n.kids || []).forEach(walk);
  })(box);
  return out;
}
function texts(box) {
  const out = [];
  (function walk(n) {
    if (n.text) out.push(n.text);
    (n.kids || []).forEach(walk);
  })(box);
  return out.join("\n");
}
function ok(cond, what) {
  if (!cond) { console.error("FAIL: " + what); process.exitCode = 1; }
}

const BRANCH = {
  cwd: "C:/repo",
  scope: "s109",
  sessions: ["s109"],
  run: {
    status: "waiting_answer",
    step_id: "landing",
    ask: {
      id: "ask-1",
      kind: "branch",
      prompt: "request or hold?",
      asked: [{ kind: "member", handle: "s45" }],
      options: [
        { name: "request", description: "queue it for integration" },
        { name: "hold", description: "freeze the branch" },
      ],
    },
  },
};

/* Exact status shape when responder selection reached nobody: the branch
   remains at the top level and there is no ask object. */
const FALLEN_BRANCH = {
  cwd: "C:/repo",
  scope: "s109",
  sessions: ["s109"],
  run: {
    status: "waiting_answer",
    step_id: "landing-review",
    reason: "branch",
    options: [
      { name: "request", description: "queue it for integration" },
      { name: "hold", description: "freeze the branch" },
    ],
    user_door: { command: "claunch cflow select <request|hold>" },
  },
};

/* ---- a delegated BRANCH: one press per option, all of them real -------- */
{
  const box = wfActions(BRANCH, {});
  const opts = buttons(box).filter((b) => b.cls.includes("option"));
  ok(opts.length === 2, `branch draws one button per option (got ${opts.length})`);
  ok(opts.map((b) => b.text).join(",") === "request,hold",
     "the buttons are the ask's own options, in order");
  ok(opts[0].title === "queue it for integration",
     "each button keeps the description its workflow wrote");

  opts.forEach((b) => (b.handlers.click || []).forEach((fn) => fn()));
  ok(alerted.length === 0, "no dead end: the branch takeover does not alert");
  ok(posted.length === 2, `both presses posted (got ${posted.length})`);
  ok(posted.every((p) => p.url === "/api/cflow/select"),
     "a branch is settled with select, not approve");
  ok(posted.map((p) => p.body.option).join(",") === "request,hold",
     "each press carries its own option");
  ok(posted.every((p) => p.body.cwd === "C:/repo" && p.body.scope === "s109"),
     "each press names the run it settles");
  ok(texts(box).includes("your answer lands over theirs"),
     "the panel says whose answer wins");
  ok(texts(box).includes("request or hold?"), "the question is shown");
}

/* ---- a branch put to NOBODY: top-level options, never Approve --------- */
{
  posted.length = 0;
  const box = wfActions(FALLEN_BRANCH, {});
  const opts = buttons(box).filter((b) => b.cls.includes("option"));
  ok(opts.map((b) => b.text).join(",") === "request,hold",
     "a fallen branch draws its top-level options");
  ok(!buttons(box).some((b) => b.cls.includes("approve")),
     "a fallen branch does not draw an Approve gate");

  opts.forEach((b) => (b.handlers.click || []).forEach((fn) => fn()));
  ok(posted.length === 2 && posted.every((p) => p.url === "/api/cflow/select"),
     "a fallen branch settles through select");
  ok(posted.map((p) => p.body.option).join(",") === "request,hold",
     "a fallen branch sends the selected top-level option");
  ok(!posted.some((p) => p.url === "/api/cflow/approve"),
     "a fallen branch never calls approve");
}

/* ---- a delegated APPROVAL: unchanged, one press ----------------------- */
{
  posted.length = 0;
  const box = wfActions({
    cwd: "C:/repo", scope: "s109", sessions: ["s109"],
    run: {
      status: "waiting_answer", step_id: "ship",
      ask: { id: "ask-2", kind: "approval", prompt: "ship it?",
             asked: [{ kind: "member", handle: "s45" }] },
    },
  }, {});
  const btns = buttons(box).filter((b) => b.text === "Decide it myself");
  ok(btns.length === 1, "an approval still takes one press");
  btns[0].handlers.click.forEach((fn) => fn());
  ok(posted.length === 1 && posted[0].url === "/api/cflow/approve",
     "and that press is an approval");
}

if (!process.exitCode) console.log("askdoor_check ok");
