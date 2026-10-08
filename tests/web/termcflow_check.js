/* The cflow answer strip over the terminal (#term-cflow, claunch-rf8vm):
   when the attached session's run is stopped on a person, one line on top
   of the terminal carries the step, the question and a button per answer.
   What it must get right — it exists exactly while sessCflowGated says the
   run is the reader's move and the terminal page is on screen, each press
   is the run page's own (same endpoint, same body, same confirm), it speaks
   for the attached session only, and it refits the terminal exactly when
   its shape changes (never on a same-shape 2s repaint). Slice the real
   functions out of app.js and drive them against a stub DOM. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {},
    dataset: {}, title: "", type: "", href: "",
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    set innerHTML(v) { if (v === "") this.kids = []; },
    classList: {
      add: (...cs) => cs.forEach((c) => n.classes.add(c)),
      remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
      contains: (c) => n.classes.has(c),
    },
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const texts = (n) => walk(n).map((k) => k.text).join(" | ");
const buttons = (n) => walk(n).filter((k) => k.tag === "button");
const byClass = (n, cls) => walk(n).find((k) => k.classes.has(cls));

const strip = node("div");
strip.classes.add("hidden");
const $ = (id) => {
  if (id !== "term-cflow") throw new Error("unexpected $: " + id);
  return strip;
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined && text !== null) n.text = String(text);
  return n;
}

const stubs = `
let currentPage = "terminal";
let currentName = "s16";
let cflowCache = [];
let refits = 0;
function refitSoon() { refits++; }
let actions = [];
function cflowAction(p, body) { actions.push([p, body]); }
let confirmAnswer = true;
let asked = [];
function confirm(q) { asked.push(q); return confirmAnswer; }
let promptAnswer = "";
function prompt(q) { asked.push(q); return promptAnswer; }
`;

const ctx = {};
new Function(
  "exports", "$", "el",
  stubs
  + ["askWho", "answerFellToUs", "answerBranchOptions", "sessCflowRun",
     "sessCflowGated", "sessCflowLabel", "sessCflowGateStep", "fmtOpensAt",
     "termCflowAnswers", "termCflowQuestion", "renderTermCflow"]
    .map(slice).join("\n") + `
Object.assign(exports, {
  renderTermCflow,
  refits: () => refits,
  actions: () => actions,
  asked: () => asked,
  setRuns: (r) => { cflowCache = r; },
  setPage: (p) => { currentPage = p; },
  setName: (n) => { currentName = n; },
  setConfirm: (v) => { confirmAnswer = v; },
  setPrompt: (v) => { promptAnswer = v; },
  reset: () => { actions = []; asked = []; },
});`
)(ctx, $, el);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

const RUN = {
  cwd: "/repo", scope: "s16", sessions: ["s16"], run: "run-1",
  workflow: "improv-worker", visit: 1,
};

(async () => {
  /* ---- a running run: nothing to press, no strip ---- */
  ctx.setRuns([{ ...RUN, status: "step", step_id: "work" }]);
  ctx.renderTermCflow();
  check("a running step leaves it hidden", strip.classes.has("hidden"));
  check("hidden with nothing to change does not refit", ctx.refits() === 0, ctx.refits());

  /* ---- an approval gate ---- */
  ctx.setRuns([{ ...RUN, status: "waiting_approval", step_id: "landing-gate",
                 gate: "land this branch?\nsee the report" }]);
  ctx.renderTermCflow();
  check("an approval gate shows the strip", !strip.classes.has("hidden"));
  check("appearing refits the terminal under it", ctx.refits() === 1, ctx.refits());
  check("it names the workflow and the step", texts(strip).includes("improv-worker · landing-gate"),
        texts(strip));
  check("the question is one line", texts(strip).includes("land this branch? see the report"),
        texts(strip));
  const head = byClass(strip, "tcf-head");
  check("the head links to the run page",
        head && head.href === "#/wf/" + encodeURIComponent("s16|/repo"), head && head.href);
  let bs = buttons(strip);
  check("one Approve button", bs.length === 1 && bs[0].text === "Approve",
        bs.map((b) => b.text));

  ctx.renderTermCflow();
  check("a same-shape repaint does not refit", ctx.refits() === 1, ctx.refits());
  check("...and keeps the same button node", buttons(strip)[0] === bs[0]);

  ctx.setConfirm(false);
  await bs[0].fire("click");
  check("a declined confirm sends nothing", ctx.actions().length === 0, ctx.actions());
  ctx.setConfirm(true);
  await bs[0].fire("click");
  check("Approve posts the run page's approve",
        JSON.stringify(ctx.actions()) ===
        JSON.stringify([["/api/cflow/approve", { cwd: "/repo", scope: "s16" }]]),
        ctx.actions());
  check("...after the run page's confirm",
        ctx.asked().includes("Approve the gate at step 'landing-gate'?"), ctx.asked());
  ctx.reset();

  /* ---- a loop limit relabels the press ---- */
  ctx.setRuns([{ ...RUN, status: "waiting_approval", step_id: "work", reason: "loop_limit" }]);
  ctx.renderTermCflow();
  check("loop limit says what the press does",
        buttons(strip).map((b) => b.text).join() === "Extend loop limit");
  check("a changed shape refits", ctx.refits() === 2, ctx.refits());

  /* ---- a user selection: a button per option, the proposal marked ---- */
  ctx.setRuns([{ ...RUN, status: "waiting_selection", step_id: "end-gate",
                 prompt: "what next?", proposal: { option: "next-round" },
                 options: [{ name: "next-round", description: "loop" },
                           { name: "finish" }] }]);
  ctx.renderTermCflow();
  bs = buttons(strip);
  check("one button per option", bs.map((b) => b.text).join() === "next-round,finish",
        bs.map((b) => b.text));
  check("the agent's proposal is marked", bs[0].classes.has("proposed") &&
        !bs[1].classes.has("proposed"));
  await bs[1].fire("click");
  check("an option posts select with its name",
        JSON.stringify(ctx.actions()) === JSON.stringify(
          [["/api/cflow/select", { cwd: "/repo", scope: "s16", option: "finish" }]]),
        ctx.actions());
  ctx.reset();

  /* ---- a goto request: move or refuse ---- */
  ctx.setRuns([{ ...RUN, status: "waiting_goto", step_id: "work",
                 goto_request: { from: "work", step: "intake", reason: "scope changed" } }]);
  ctx.renderTermCflow();
  bs = buttons(strip);
  check("goto offers move and refuse",
        bs.map((b) => b.text).join() === "Move to 'intake',Refuse", bs.map((b) => b.text));
  check("goto question carries the reason", texts(strip).includes("scope changed"));
  ctx.setPrompt(null);
  await bs[1].fire("click");
  check("a cancelled refusal sends nothing", ctx.actions().length === 0);
  ctx.setPrompt("not now");
  await bs[1].fire("click");
  check("refuse posts deny with the typed reason",
        JSON.stringify(ctx.actions()) === JSON.stringify([["/api/cflow/goto/resolve",
          { cwd: "/repo", scope: "s16", decision: "deny", reason: "not now" }]]),
        ctx.actions());
  ctx.reset();
  await bs[0].fire("click");
  check("move posts approve",
        JSON.stringify(ctx.actions()) === JSON.stringify([["/api/cflow/goto/resolve",
          { cwd: "/repo", scope: "s16", decision: "approve" }]]), ctx.actions());
  ctx.reset();

  /* ---- an ask: with a peer is not ours, fallen to nobody is ---- */
  ctx.setRuns([{ ...RUN, status: "waiting_answer", step_id: "review",
                 ask: { prompt: "pass?", asked: [{ handle: "s29" }] } }]);
  ctx.renderTermCflow();
  check("an ask with a peer leaves it hidden", strip.classes.has("hidden"));
  ctx.setRuns([{ ...RUN, status: "waiting_answer", step_id: "review",
                 ask: { prompt: "pass?", asked: [] } }]);
  ctx.renderTermCflow();
  check("an ask that reached nobody shows Approve",
        !strip.classes.has("hidden") &&
        buttons(strip).map((b) => b.text).join() === "Approve");
  ctx.setRuns([{ ...RUN, status: "waiting_answer", step_id: "review", reason: "branch",
                 options: [{ name: "pass" }, { name: "fail" }] }]);
  ctx.renderTermCflow();
  check("a branch that reached nobody shows its options",
        buttons(strip).map((b) => b.text).join() === "pass,fail");

  /* ---- whose run: the attached session's, on the terminal page only ---- */
  ctx.setRuns([{ ...RUN, scope: "other", sessions: ["other"],
                 status: "waiting_approval", step_id: "x" }]);
  ctx.renderTermCflow();
  check("another session's gate is not drawn here", strip.classes.has("hidden"));
  ctx.setRuns([{ ...RUN, status: "waiting_approval", step_id: "x" }]);
  ctx.renderTermCflow();
  check("own gate drawn", !strip.classes.has("hidden"));
  ctx.setPage("home");
  const r0 = ctx.refits();
  ctx.renderTermCflow();
  check("off the terminal page it hides", strip.classes.has("hidden"));
  check("...and hiding refits", ctx.refits() === r0 + 1);
  ctx.setPage("terminal");
  ctx.setName("s99");
  ctx.renderTermCflow();
  check("switching to a session with no run hides it", strip.classes.has("hidden"));

  /* ---- the markup, the stylesheet, the call sites ---- */
  const at = (id) => html.indexOf(`id="${id}"`);
  check("the strip is in the page", at("term-cflow") > 0);
  check("it sits between the header and the terminal",
        at("term-header") < at("term-cflow") && at("term-cflow") < at("terminal"));
  check("it starts hidden", /<div id="term-cflow" class="hidden"/.test(html));
  check("its height is taken from the terminal (flex: none)",
        /#term-cflow \{[^}]*flex: none/.test(css));
  check("the cflow poll repaints it",
        /renderTermTimer\(\);[^\n]*\n\s*renderTermCflow\(\);/.test(src));
  check("the page switch repaints it",
        /refreshTermQueued\(\);\s*(\/\/[^\n]*\n\s*)*renderTermCflow\(\);/.test(src));

  if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
  console.log("termcflow_check ok");
})();
