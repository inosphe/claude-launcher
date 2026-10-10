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
let cflow2RailCache = {};
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
  + ["askWho", "answerFellToUs", "answerBranchOptions", "sessCflowRun",
     "sessCflowGated", "sessCflowLabel", "sessCflowGateStep", "fmtOpensAt",
     "termCflowAnswers", "termCflowQuestion", "cf2LimitPresses", "termCflow2Answers",
     "termCflow2Question", "termCflowView", "renderTermCflow"]
    .map(slice).join("\n") + `
Object.assign(exports, {
  renderTermCflow,
  refits: () => refits,
  actions: () => actions,
  asked: () => asked,
  setRuns: (r) => { cflowCache = r; },
  setRail: (r) => { cflow2RailCache = r; },
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

  /* a loop limit: allow one more / reset the count / stop */
  ctx.setRail({ s16: { ...ROW, status: "waiting_approval", gated: true,
    owed: ["loop_limit: round"], answers: [
    { kind: "loop_limit", instance: "round@3", label: "round", count: 5, max: 5 }] } });
  ctx.renderTermCflow();
  bs = buttons(strip);
  check("v2: loop limit presses",
        bs.map((b) => b.text).join() === "Allow one more,Reset the count,Stop the loop",
        bs.map((b) => b.text));
  check("v2: loop limit question", texts(strip).includes("loop reached its max (5/5)"));
  await bs[0].fire("click");
  await bs[1].fire("click");
  await bs[2].fire("click");
  check("v2: limit presses post extend",
        JSON.stringify(ctx.actions()) === JSON.stringify([
          ["/api/cflow2/extend", { cwd: "/repo", scope: "s16", node: "round@3", by: 1 }, "after"],
          ["/api/cflow2/extend", { cwd: "/repo", scope: "s16", node: "round@3", reset: true },
           "after"],
          ["/api/cflow2/extend", { cwd: "/repo", scope: "s16", node: "round@3", stop: true },
           "after"]]), ctx.actions());
  ctx.reset();

  /* a loop limit that ran no iteration (a parallel foreach over more items
     than max) offers no reset: it would stop again at once */
  ctx.setRail({ s16: { ...ROW, status: "waiting_approval", gated: true,
    owed: ["loop_limit: fan"], answers: [
    { kind: "loop_limit", instance: "fan@2", label: "fan", count: 0, max: 3 }] } });
  ctx.renderTermCflow();
  check("v2: no reset at count 0",
        buttons(strip).map((b) => b.text).join() === "Allow one more,Stop the loop",
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
