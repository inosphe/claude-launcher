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
let transcriptName = null;
let transcriptMachine = null;
let cflowCache = [];
let cflow2RailCache = {};
let sessionsCache = [];
function refreshCflow2Rail() {}
let refits = 0;
function refitSoon() { refits++; }
let actions = [];
function cflowAction(p, body, after) {
  actions.push(after ? [p, body, "after"] : [p, body]);
}
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
  + ["askWho", "answerFellToUs", "answerBranchOptions", "scopeSession", "sessCflowRun",
     "sessCflowGated", "sessCflowLabel", "sessCflowGateStep", "fmtOpensAt",
     "sessionCategory", "gateWakeNote",
     "termCflowAnswers", "termCflowQuestion", "cf2LimitPresses", "termCflow2Answers",
     "termCflow2Question", "sessionLineOnPage", "termCflowView", "renderTermCflow"]
    .map(slice).join("\n") + `
Object.assign(exports, {
  renderTermCflow,
  refits: () => refits,
  actions: () => actions,
  asked: () => asked,
  setRuns: (r) => { cflowCache = r; },
  setRail: (r) => { cflow2RailCache = r; },
  setSessions: (r) => { sessionsCache = r; },
  setPage: (p) => { currentPage = p; },
  setName: (n) => { currentName = n; },
  setTranscript: (n, m = null) => { transcriptName = n; transcriptMachine = m; },
  setConfirm: (v) => { confirmAnswer = v; },
  setPrompt: (v) => { promptAnswer = v; },
  reset: () => { actions = []; asked = []; },
});`
)(ctx, $, el);

/* ---- the run page, rendered for real (claunch-xxxsx.1) ------------------
   The wake note on the run page's presses was first checked by a source
   regex (".title = withWake(" somewhere in the body), which stays green
   when withWake joins nothing and the button ends up with an empty title.
   Here the real renderCflow2Into is sliced out of app.js and driven
   against a page-shaped stub, and the assertions read the rendered
   buttons' title values.
   The stub names its child arrays childNodes/children -- the names the
   real DOM uses. claunch-lx3r/claunch-cvfw: a stub-only name (kids) turns
   a production typo into a green check. */
function pageNode(tag) {
  const n = {
    tag, text: "", title: "", type: "", href: "", value: "", placeholder: "",
    disabled: false, open: false, className: "", dataset: {}, handlers: {},
    kids: [], classes: new Set(),
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.kids.push(c)); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    contains(x) { return x === this || pageWalk(this).includes(x); },
    querySelector(sel) {
      if (!sel.startsWith(".")) return null;
      return pageWalk(this).find((k) => k.classes.has(sel.slice(1))) || null;
    },
    matches() { return false; },
    focus() {},
    get childNodes() { return this.kids; },
    get children() { return this.kids; },
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
function pageWalk(n, out = []) {
  for (const k of n.kids) { if (!k) continue; out.push(k); pageWalk(k, out); }
  return out;
}
function pageEl(tag, cls, text) {
  const n = pageNode(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined && text !== null) n.text = String(text);
  return n;
}
const runPage = (() => {
  const stub = `
let cf2PaneClose = null;
let cf2Drafts = {};
let sessionsCache = [];
const CFLOW2_STATUS_CLS = {};
const document = { activeElement: null, body: {}, createElement: (t) => pageEl(t) };
const el = pageEl;
function cflowPost() { return Promise.resolve({}); }
function captureCflow2View() { return () => {}; }
function wfOwnerChip() { return null; }
function wfFullLink() { return pageEl("a", null, "open"); }
function cflow2RunActions() { return []; }
function cflow2Callstack() { return pageEl("div"); }
function cflow2InputsBlock() { return pageEl("div"); }
function cflow2LayoutControls() { return pageEl("div"); }
function cflow2RecurText() { return "no"; }
function cflow2Reports() { return []; }
function cflow2UndecidedCounts() { return {}; }
function cflow2Undecidable() { return []; }
function cflow2ForceReport() { return pageEl("div"); }
function cflow2TriggerLines() { return []; }
function cflow2CollectionTable() { return pageEl("div"); }
function cflow2RunHistory() { return pageEl("div"); }
function cflow2Throughput() { return pageEl("div"); }
function cflow2TreeInto() {}
function cflow2TreeRows() { return []; }
function cf2NodePane() { return pageEl("div"); }
function cf2QuestionView() { return pageEl("div"); }
function cf2MovePlanView() { return pageEl("div"); }
function cf2AnswerHeld() { return ""; }
function cf2AnswerLegend() { return pageEl("div"); }
function cf2AskLegendItems() { return []; }
function alert() {}
function confirm() { return false; }
function prompt() { return null; }
function mdInto(box) { return box; }
function mdPlain(s) { return String(s); }
function localStamp(s) { return String(s); }
`;
  const ctx = {};
  new Function(
    "exports", "pageEl",
    stub
    + ["scopeSession", "sessionCategory", "gateWakeNote", "cf2Card", "cf2AskKind",
       "cf2Section", "cf2ReasonInput", "cf2LimitPresses", "cflow2RedrawAllowed",
       "renderCflow2Into"]
      .map(slice).join("\n")
    + `
Object.assign(exports, {
  renderCflow2Into,
  setSessions: (r) => { sessionsCache = r; },
});`
  )(ctx, pageEl);
  return {
    render(data) {
      const box = pageEl("div");
      ctx.renderCflow2Into(box, data, { host: "page" }, true);
      return box;
    },
    setSessions: ctx.setSessions,
  };
})();
const pageButtons = (box) => pageWalk(box).filter((k) => k.tag === "button");
const pageTitles = (box) => pageButtons(box).map((b) => b.title);

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
  // The session's HTML view (claunch-hjux5): its own transcript page keeps
  // the strip; another session's transcript, or a remote shadow's, does not.
  ctx.setPage("log");
  ctx.setTranscript("s16");
  ctx.renderTermCflow();
  check("on the session's HTML view it shows", !strip.classes.has("hidden"));
  ctx.setTranscript("s99");
  ctx.renderTermCflow();
  check("on another session's transcript it hides", strip.classes.has("hidden"));
  ctx.setTranscript("s16", "box2");
  ctx.renderTermCflow();
  check("on a remote shadow's transcript it hides", strip.classes.has("hidden"));
  ctx.setTranscript(null);
  ctx.setPage("terminal");
  ctx.setName("s99");
  ctx.renderTermCflow();
  check("switching to a session with no run hides it", strip.classes.has("hidden"));

  /* ---- a cflow v2 run (claunch-bpziz): the rail row's answers ---- */
  ctx.setName("s16");
  ctx.setRuns([]);
  ctx.reset();
  const ROW = { cwd: "/repo", scope: "s16", run: "r2", workflow: "improv-worker-v2",
                status: "running" };
  ctx.setRail({ s16: { ...ROW, gated: false, owed: [], answers: [] } });
  ctx.renderTermCflow();
  check("v2: an ungated run leaves it hidden", strip.classes.has("hidden"));

  ctx.setRail({ s16: { ...ROW, gated: true, owed: ["choose: end-decision"], answers: [
    { kind: "choose", instance: "round/end@40", label: "end-decision",
      prompt: "end\nthe session?", require_reason: false,
      options: [{ name: "end", description: "finish" }, { name: "hold" }] },
    { kind: "loop_limit", instance: "round@3", label: "round", count: 5, max: 5 }] } });
  let rf = ctx.refits();
  ctx.renderTermCflow();
  check("v2: a user choice shows the strip", !strip.classes.has("hidden"));
  check("v2: appearing refits", ctx.refits() === rf + 1);
  check("v2: names workflow, step and the count of the rest",
        texts(strip).includes("improv-worker-v2 · v2 · end-decision — choose (+1 more)"),
        texts(strip));
  check("v2: the question is one line", texts(strip).includes("end the session?"), texts(strip));
  check("v2: the head links to the v2 run page",
        byClass(strip, "tcf-head").href === "#/wf/" + encodeURIComponent("s16|/repo"));
  bs = buttons(strip);
  check("v2: a button per option", bs.map((b) => b.text).join() === "end,hold",
        bs.map((b) => b.text));
  ctx.renderTermCflow();
  check("v2: a same-shape repaint keeps the button node", buttons(strip)[0] === bs[0]);
  ctx.setConfirm(false);
  await bs[1].fire("click");
  check("v2: a declined confirm sends nothing", ctx.actions().length === 0);
  ctx.setConfirm(true);
  await bs[1].fire("click");
  check("v2: an option posts the run page's select",
        JSON.stringify(ctx.actions()) === JSON.stringify([["/api/cflow2/select",
          { cwd: "/repo", scope: "s16", node: "round/end@40", option: "hold", reason: "" },
          "after"]]), ctx.actions());
  check("v2: ...after the run page's confirm",
        ctx.asked().includes("Pick 'hold' for end-decision?"), ctx.asked());
  ctx.reset();

  /* the driver's move request (flow_request_move, claunch-kgfr5): Move or
     Refuse, the run page's /api/cflow2/move/resolve */
  ctx.setRail({ s16: { ...ROW, gated: true, owed: ["move: round"], answers: [
    { kind: "move", request: "mr-1", node: "round", label: "round", by: "s16",
      reason: "a new task came in", prompt: "s16 asks to move the run to round: a new task" }] } });
  ctx.renderTermCflow();
  bs = buttons(strip);
  check("v2 move: Move and Refuse", bs.map((b) => b.text).join() === "Move to 'round',Refuse",
        bs.map((b) => b.text));
  check("v2 move: the question names the asker",
        texts(strip).includes("s16 asks to move the run to round"), texts(strip));
  await bs[0].fire("click");
  check("v2 move: Move posts approve",
        JSON.stringify(ctx.actions()) === JSON.stringify([["/api/cflow2/move/resolve",
          { cwd: "/repo", scope: "s16", decision: "approve", reason: "" }, "after"]]),
        ctx.actions());
  ctx.reset();
  ctx.setPrompt("finish this first");
  await bs[1].fire("click");
  check("v2 move: Refuse carries the reason",
        ctx.actions().length === 1 && ctx.actions()[0][1].decision === "deny"
        && ctx.actions()[0][1].reason === "finish this first", ctx.actions());
  ctx.reset();

  /* the callstack effect of the move (claunch-1qsz4) rides on the confirm */
  ctx.setRail({ s16: { ...ROW, gated: true, owed: ["move: round"], answers: [
    { kind: "move", request: "mr-2", node: "round", label: "round", by: "s16",
      reason: "a new task came in", effect: "re-enters live 'round'; rewind 2, step in 1",
      prompt: "s16 asks to move the run to round (re-enters live 'round'; rewind 2, step in 1)" }] } });
  ctx.renderTermCflow();
  await buttons(strip)[0].fire("click");
  check("v2 move: the confirm says what the move does to the callstack",
        ctx.asked().some((q) => q.includes("Callstack: re-enters live 'round'; rewind 2, step in 1")),
        ctx.asked());
  ctx.reset();

  /* the report kind of the same request (claunch-vwmoj): Report / Refuse on
     the same endpoint; the row once threw a ReferenceError (claunch-cjb97) */
  ctx.setRail({ s16: { ...ROW, gated: true, owed: ["report: policy"], answers: [
    { kind: "move", request: "mr-2", node: "policy", label: "policy", by: "s16",
      report: true, outcome: "ok", reason: "verify cannot import claude_launcher",
      prompt: "s16 asks you to report policy as ok: verify cannot import" }] } });
  ctx.renderTermCflow();
  bs = buttons(strip);
  check("v2 report: Report and Refuse", bs.map((b) => b.text).join() === "Report 'ok',Refuse",
        bs.map((b) => b.text));
  await bs[0].fire("click");
  check("v2 report: the confirm names the task and the result",
        ctx.asked().some((q) => q.startsWith("Report 'policy' as 'ok', as s16 asked?")),
        ctx.asked());
  check("v2 report: Report posts approve",
        ctx.actions().length === 1 && ctx.actions()[0][0] === "/api/cflow2/move/resolve"
        && ctx.actions()[0][1].decision === "approve", ctx.actions());
  ctx.reset();

  /* require_reason constrains agents; a person may select without a reason,
     just as on the run page (docs 2.11). */
  ctx.setRail({ s16: { ...ROW, gated: true, owed: ["choose: x"], answers: [
    { kind: "choose", instance: "x@2", label: "x", prompt: "?", require_reason: true,
      options: [{ name: "go" }] }] } });
  ctx.renderTermCflow();
  bs = buttons(strip);
  ctx.setPrompt("  ");
  await bs[0].fire("click");
  check("v2: a person can select without the agent's required reason",
        ctx.actions().length === 1 && ctx.actions()[0][1].reason === "",
        ctx.actions());
  check("v2: a choice asks for confirmation, not a required reason",
        JSON.stringify(ctx.asked()) === JSON.stringify(["Pick 'go' for x?"]), ctx.asked());
  ctx.reset();

  /* an ask that fell to a person: its options, the recommendation marked */
  ctx.setRail({ s16: { ...ROW, status: "waiting_approval", gated: true,
    owed: ["ask: land"], answers: [
    { kind: "ask", instance: "land@9", label: "land", prompt: "land it?",
      require_reason: true, recommended: "approve",
      options: [{ name: "approve" }, { name: "decline" }] }] } });
  ctx.renderTermCflow();
  bs = buttons(strip);
  check("v2: an ask offers its options", bs.map((b) => b.text).join() === "approve,decline");
  check("v2: the recommended option is marked",
        bs[0].classes.has("proposed") && !bs[1].classes.has("proposed"));
  check("v2: recommendation preserves the run page's green approval style",
        bs[0].classes.has("approve") && !bs[1].classes.has("approve"));
  ctx.setPrompt("");
  await bs[1].fire("click");
  check("v2: an ask posts the run page's approve",
        JSON.stringify(ctx.actions()) === JSON.stringify([["/api/cflow2/approve",
          { cwd: "/repo", scope: "s16", node: "land@9", decision: "decline", reason: "" },
          "after"]]), ctx.actions());
  ctx.reset();

  /* a brief ask's reframe always asks for the reason (claunch-omph3): the
     engine takes a person's answer without one, and reframe re-asks from it.
     The other options still need none when require_reason is off. */
  ctx.setRail({ s16: { ...ROW, status: "waiting_approval", gated: true,
    owed: ["ask: scope"], answers: [
    { kind: "ask", instance: "scope@4", label: "scope", prompt: "which scope?",
      require_reason: true,
      options: [{ name: "narrow" }, { name: "reframe" }] }] } });
  ctx.renderTermCflow();
  bs = buttons(strip);
  check("v2: a brief ask offers reframe", bs.map((b) => b.text).join() === "narrow,reframe",
        bs.map((b) => b.text));
  ctx.setPrompt("");
  await bs[1].fire("click");
  check("v2: reframe with an empty reason sends nothing", ctx.actions().length === 0,
        ctx.actions());
  check("v2: ...and the prompt says what the reason is for",
        ctx.asked().some((q) => q.includes("reframe: none of the options fits")), ctx.asked());
  ctx.setPrompt("both miss the migration");
  await bs[1].fire("click");
  check("v2: reframe sends its reason with the approve",
        JSON.stringify(ctx.actions()) === JSON.stringify([["/api/cflow2/approve",
          { cwd: "/repo", scope: "s16", node: "scope@4", decision: "reframe",
            reason: "both miss the migration" }, "after"]]), ctx.actions());
  ctx.reset();
  ctx.setPrompt("");
  await bs[0].fire("click");
  check("v2: require_reason does not block a person's other brief answers",
        ctx.actions().length === 1 && ctx.actions()[0][1].decision === "narrow"
          && !ctx.asked().some((q) => q.includes("required")), [ctx.actions(), ctx.asked()]);
  ctx.reset();

  /* a loop limit: allow 5 more / allow 15 more / stop */
  ctx.setRail({ s16: { ...ROW, status: "waiting_approval", gated: true,
    owed: ["loop_limit: round"], answers: [
    { kind: "loop_limit", instance: "round@3", label: "round", count: 5, max: 5 }] } });
  ctx.renderTermCflow();
  bs = buttons(strip);
  check("v2: loop limit presses",
        bs.map((b) => b.text).join() === "Allow 5 more,Allow 15 more,Stop the loop",
        bs.map((b) => b.text));
  check("v2: loop limit question", texts(strip).includes("loop reached its max (5/5)"));
  await bs[0].fire("click");
  await bs[1].fire("click");
  await bs[2].fire("click");
  check("v2: limit presses post extend",
        JSON.stringify(ctx.actions()) === JSON.stringify([
          ["/api/cflow2/extend", { cwd: "/repo", scope: "s16", node: "round@3", by: 5 }, "after"],
          ["/api/cflow2/extend", { cwd: "/repo", scope: "s16", node: "round@3", by: 15 },
           "after"],
          ["/api/cflow2/extend", { cwd: "/repo", scope: "s16", node: "round@3", stop: true },
           "after"]]), ctx.actions());
  check("v2: an allow press asks with its own count",
        ctx.asked().some((q) => q.startsWith("Allow 15 more at ")), ctx.asked());
  ctx.reset();

  /* a loop limit that ran no iteration (a parallel foreach over more items
     than max) offers the same presses */
  ctx.setRail({ s16: { ...ROW, status: "waiting_approval", gated: true,
    owed: ["loop_limit: fan"], answers: [
    { kind: "loop_limit", instance: "fan@2", label: "fan", count: 0, max: 3 }] } });
  ctx.renderTermCflow();
  check("v2: same presses at count 0",
        buttons(strip).map((b) => b.text).join() === "Allow 5 more,Allow 15 more,Stop the loop",
        buttons(strip).map((b) => b.text));
  ctx.reset();

  /* a visit limit carries visits: true */
  ctx.setRail({ s16: { ...ROW, status: "waiting_approval", gated: true,
    owed: ["visit_limit: work"], answers: [
    { kind: "visit_limit", instance: "work@7", label: "work", count: 9, max: 9 }] } });
  ctx.renderTermCflow();
  await buttons(strip)[0].fire("click");
  check("v2: a visit limit posts visits: true",
        ctx.actions()[0][1].visits === true && ctx.actions()[0][1].by === 1, ctx.actions());
  ctx.reset();

  /* a halt has no dashboard press: the text and the run page link only */
  ctx.setRail({ s16: { ...ROW, status: "waiting_approval", gated: true,
    owed: ["halted: m1"], answers: [
    { kind: "halt", label: "m1", prompt: "halted by monitor m1 (claunch cflow2 resume)" }] } });
  ctx.renderTermCflow();
  check("v2: a halt shows the strip without presses",
        !strip.classes.has("hidden") && buttons(strip).length === 0);
  check("v2: ...and points at the run page", texts(strip).includes("answer it on the run page"),
        texts(strip));

  /* an older daemon's row without answers still shows what is owed */
  ctx.setRail({ s16: { ...ROW, gated: true, owed: ["choose: pick"] } });
  ctx.renderTermCflow();
  check("v2: a row without answers shows owed", texts(strip).includes("choose: pick"),
        texts(strip));

  /* a v1 gate on the same session takes the strip first */
  ctx.setRuns([{ ...RUN, status: "waiting_approval", step_id: "landing-gate", gate: "g" }]);
  ctx.renderTermCflow();
  check("v2: a v1 gate takes the strip first",
        buttons(strip).map((b) => b.text).join() === "Approve");
  ctx.setRuns([]);

  /* another session's v2 row is not drawn here */
  ctx.setRail({ other: { ...ROW, scope: "other", gated: true, owed: ["choose: x"],
                         answers: [] } });
  ctx.renderTermCflow();
  check("v2: another session's row is not drawn", strip.classes.has("hidden"));

  /* ---- a session that is not running: every press says it comes back
     (claunch-xxxsx). The daemon resumes the driver and queues the answer's
     nudge into it, so the tooltip has to say so. ---- */
  const GATE = { ...ROW, gated: true, owed: ["ask: end-decision"], answers: [
    { kind: "ask", instance: "round/end@41", label: "end-decision",
      prompt: "what next?", options: [{ name: "approve", description: "the task starts" },
                                      { name: "new-task" }] }] };
  ctx.setRail({ s16: GATE });
  ctx.setSessions([{ name: "s16", status: "exited", paused_at: "2026-10-09T12:05:00+00:00" }]);
  ctx.renderTermCflow();
  bs = buttons(strip);
  check("wake note: paused keeps the option's own title",
        bs[0].title === "the task starts — answering resumes this session — paused → running",
        bs[0].title);
  check("wake note: paused says the answer resumes it",
        bs.every((b) => b.title.includes("paused → running")), bs.map((b) => b.title));
  check("wake note: an option with no description is the note alone",
        bs[1].title === "answering resumes this session — paused → running", bs[1].title);

  ctx.setSessions([{ name: "s16", status: "exited", held_at: "2026-10-09T12:00:00+00:00" }]);
  ctx.renderTermCflow();
  check("wake note: hold says the answer wakes it",
        buttons(strip).every((b) => b.title.includes("hold → running")),
        buttons(strip).map((b) => b.title));

  ctx.setSessions([{ name: "s16", status: "exited" }]);
  ctx.renderTermCflow();
  check("wake note: killed says the answer respawns it",
        buttons(strip).every((b) => b.title.includes("killed → running")),
        buttons(strip).map((b) => b.title));

  ctx.setSessions([{ name: "s16", status: "idle" }]);
  ctx.renderTermCflow();
  check("wake note: a running session gets no note",
        buttons(strip).every((b) => !b.title.includes("→ running")),
        buttons(strip).map((b) => b.title));

  ctx.setSessions([{ name: "s16", status: "exited",
                     failed_at: "2026-10-09T12:10:00+00:00" }]);
  ctx.renderTermCflow();
  check("wake note: a failed record is not promised a resume",
        buttons(strip).every((b) => !b.title.includes("→ running")),
        buttons(strip).map((b) => b.title));
  ctx.setSessions([]);

  /* the v1 strip carries the same note */
  ctx.setRuns([{ ...RUN, status: "waiting_approval", step_id: "landing-gate", gate: "g" }]);
  ctx.setSessions([{ name: "s16", status: "exited", paused_at: "2026-10-09T12:05:00+00:00" }]);
  ctx.renderTermCflow();
  check("wake note: the v1 strip says it too",
        buttons(strip).map((b) => b.title).join(" ").includes("paused → running"),
        buttons(strip).map((b) => b.title));
  ctx.setSessions([]);
  ctx.setRuns([]);

  /* the run page's own presses carry the note too (claunch-xxxsx): the move
     and report cards answer /api/cflow2/move/resolve and the limit card
     answers /api/cflow2/extend, both of which nudge the driver -- so a
     stopped session comes back from those presses as well, and the two
     surfaces must not disagree.
     Rendered, not grepped (claunch-xxxsx.1): the boxes below come out of
     the real renderCflow2Into, and the button title VALUES are read. */
  const page = (extra) => ({
    cwd: "/repo", scope: "s16", session: "s16", sessions: ["s16"],
    status: "waiting_approval",
    run: { run: "run-1", workflow: "improv-worker-v2", round: 1,
           status: "waiting_approval", ...extra },
  });
  const PAUSED = [{ name: "s16", status: "exited",
                    paused_at: "2026-10-09T12:05:00+00:00" }];
  const RESUME = "answering resumes this session — paused → running";
  runPage.setSessions(PAUSED);

  let box = runPage.render(page({
    move_request: { kind: "report", id: "m1", node: "work@3", title: "work",
                    outcome: "ok", by: "s1521" } }));
  let pbs = pageButtons(box);
  check("run page: the report card renders two presses", pbs.length === 2,
        pbs.map((b) => b.text));
  check("run page: the rendered report grant carries the wake note",
        pbs[0].title === RESUME, pbs.map((b) => b.title));
  check("run page: the rendered report refusal keeps its words and the note",
        pbs[1].title === "the task stays open; the driver reads your reason " +
          "and continues — " + RESUME, pbs[1].title);

  box = runPage.render(page({
    move_request: { kind: "move", id: "m2", node: "land", title: "land",
                    by: "s1521" } }));
  pbs = pageButtons(box);
  check("run page: the rendered move grant carries the wake note",
        pbs.length === 2 && pbs[0].title === RESUME, pbs.map((b) => b.title));
  check("run page: the rendered move refusal keeps its words and the note",
        pbs[1].title === "the run stays where it is; the driver reads your " +
          "reason and continues — " + RESUME, pbs[1].title);

  box = runPage.render(page({
    loop_limits: [{ instance: "round@3", label: "round", iterations: 5, max: 5 }] }));
  pbs = pageButtons(box);
  check("run page: the rendered limit card carries the wake note",
        pbs.length === 3 && pbs[0].title === RESUME &&
        pbs[1].title === RESUME, pbs.map((b) => b.title));
  check("run page: the rendered stop press keeps its outcome and adds the note",
        pbs[2].title === "it finishes with outcome fail — " + RESUME, pbs[2].title);

  box = runPage.render(page({
    choices: [{ instance: "c1", label: "next", options: [
      { name: "next-round", description: "loop" }, { name: "finish" }] }],
    asks: [{ instance: "a1", label: "end-decision", options: [
      { name: "approve", description: "the task starts" }, { name: "new-task" }] }] }));
  pbs = pageButtons(box);
  check("run page: the rendered choice presses carry the note",
        pbs[0].title === "loop — " + RESUME &&
        pbs[1].title === RESUME, pbs.map((b) => b.title));
  check("run page: the rendered ask presses carry the note",
        pbs[2].title === "the task starts — " + RESUME &&
        pbs[3].title === RESUME, pbs.map((b) => b.title));

  /* a running session, a failed record and a session the page cannot see:
     no wake is promised where none will be delivered */
  const QUIET = {
    move_request: { kind: "report", id: "m1", node: "work@3", title: "work",
                    outcome: "ok", by: "s1521" },
    loop_limits: [{ instance: "round@3", label: "round", iterations: 5, max: 5 }],
  };
  runPage.setSessions([{ name: "s16", status: "idle" }]);
  check("run page: a running session renders no note",
        pageTitles(runPage.render(page(QUIET))).every((t) => !t.includes("→ running")),
        pageTitles(runPage.render(page(QUIET))));
  runPage.setSessions([{ name: "s16", status: "exited",
                         failed_at: "2026-10-09T12:10:00+00:00" }]);
  check("run page: a failed record is not promised a resume",
        pageTitles(runPage.render(page(QUIET))).every((t) => !t.includes("→ running")),
        pageTitles(runPage.render(page(QUIET))));
  runPage.setSessions([]);
  check("run page: a session it cannot see renders no note",
        pageTitles(runPage.render(page(QUIET))).every((t) => !t.includes("→ running")),
        pageTitles(runPage.render(page(QUIET))));

  check("the v2 rail poll repaints it",
        /applyCflow2Badges\(\);\s*(\/\/[^\n]*\n\s*)*if \(typeof renderTermCflow === "function"\) renderTermCflow\(\);/.test(src));

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
