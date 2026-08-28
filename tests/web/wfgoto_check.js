/* The step-change request, as the dashboard draws it.

   `waiting_goto` is the run stopped on a question the agent asked: it has hit
   something the workflow declares no route for and wants the position moved.
   The panel has to make that answerable, which is three separate things:

   * say where it wants to go and why, in the agent's own words — a reader
     cannot grant a jump they only know the step id of;
   * offer BOTH answers as presses. Only granting was ever cheap to draw, and
     a panel that offers grant-or-nothing collects grants;
   * carry the refusal's reason back. The agent reads it and continues down
     the declared route, so an empty refusal spends the stop and teaches
     nothing.

   The two presses post to the same endpoint with different decisions, and
   never to `/api/cflow/goto` — that route forces a position the operator
   chose, which is a third answer and belongs to the diagram, not here. */
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
const prompts = [];
let promptAnswer = "";
function cflowAction(url, body) { posted.push({ url, body }); }
function alert(msg) { alerted.push(String(msg)); }
function confirm() { return true; }              // the reader says yes
function prompt(q) { prompts.push(String(q)); return promptAnswer; }
function mdInto(host, text) { host.appendChild(node("div")).textContent = text; }
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
  "confirm", "prompt", "mdInto", "fmtOpensAt", "nudgeRun", "reminderControl",
  code + "\nreturn wfActions;"
)(document, undefined, undefined, undefined, cflowAction, alert, confirm,
  prompt, mdInto, fmtOpensAt, nudgeRun, reminderControl);

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

const ASKED = {
  cwd: "C:/repo",
  scope: "s241",
  sessions: ["s241"],
  run: {
    status: "waiting_goto",
    step_id: "commit",
    goto_request: {
      id: "gr-abc123",
      step: "work",
      from: "commit",
      reason: "the merge turned up a case the parser never handled",
      by: "s241",
    },
  },
};

/* ---- the question is legible, and both answers are presses ------------ */
{
  const box = wfActions(ASKED, {});
  const body = texts(box);
  ok(body.includes("'commit'") && body.includes("'work'"),
     "the panel names both ends of the move");
  ok(body.includes("the merge turned up a case the parser never handled"),
     "and the agent's reason, verbatim");
  const btns = buttons(box);
  const grant = btns.find((b) => b.text.startsWith("Move to"));
  const refuse = btns.find((b) => b.text === "Refuse");
  ok(!!grant, "granting is a press");
  ok(!!refuse, "refusing is a press too, not a shell command in prose");
  ok(grant.text.includes("work"), "the grant button names where it sends the run");
  ok(body.includes("does not advance until this is answered"),
     "the panel says holding off is not free");

  grant.handlers.click.forEach((fn) => fn());
  ok(posted.length === 1, `one press, one post (got ${posted.length})`);
  ok(posted[0].url === "/api/cflow/goto/resolve",
     "it answers the request rather than forcing a position");
  ok(posted[0].body.decision === "approve", "and it answers 'approve'");
  ok(posted[0].body.cwd === "C:/repo" && posted[0].body.scope === "s241",
     "the press names the run it answers");
  ok(alerted.length === 0, "no dead end into the CLI");
}

/* ---- a refusal carries the reader's reason back ----------------------- */
{
  posted.length = 0;
  prompts.length = 0;
  promptAnswer = "that step's outcome still holds; fix it here";
  const box = wfActions(ASKED, {});
  buttons(box).find((b) => b.text === "Refuse").handlers.click.forEach((f) => f());
  ok(prompts.length === 1, "refusing asks for a reason");
  ok(posted.length === 1 && posted[0].body.decision === "deny",
     "and posts the refusal");
  ok(posted[0].body.reason === "that step's outcome still holds; fix it here",
     "with the reason the reader typed — it is what the agent reads");
}

/* ---- cancelling the reason dialog cancels the refusal ----------------- */
{
  posted.length = 0;
  promptAnswer = null;                            // the reader pressed Cancel
  const box = wfActions(ASKED, {});
  buttons(box).find((b) => b.text === "Refuse").handlers.click.forEach((f) => f());
  ok(posted.length === 0,
     "backing out of the reason dialog refuses nothing");
  promptAnswer = "";
}

/* ---- an empty reason still refuses ------------------------------------ */
{
  posted.length = 0;
  promptAnswer = "";                              // typed nothing, pressed OK
  const box = wfActions(ASKED, {});
  buttons(box).find((b) => b.text === "Refuse").handlers.click.forEach((f) => f());
  ok(posted.length === 1 && posted[0].body.decision === "deny",
     "a reason is optional; the refusal is not lost for want of one");
}

if (!process.exitCode) console.log("wfgoto_check ok");
