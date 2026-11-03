/* A run stopped on its plan, on the dashboard's notification channel
   (claunch-rzepk).

   The rail carries the wait as a `plan-wait` answer (daemon/cflow2_web
   `_rail_answers`); this harness holds the parts app.js decides on its own:

   * the card's title names the plan, since the wait has no step label to
     name (a title like "waits on your plan decision: plan" is what the
     generic `${word}: ${label}` form would print; a card has one line to
     say what the reader is being asked for);
   * the card says where the decision is taken, because it offers no press
     -- the plan panel has no run endpoint;
   * clicking it opens the solution's own surface: the session with its plan
     pane up, written for THAT session before the hash moves (the pane is
     read from the session's remembered layout when the route renders);
   * the terminal strip points at the plan pane instead of the run page;
   * every other kind keeps the behaviour it had.

   The real functions are sliced out of app.js and driven against stubs. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

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

/* The word map is data the card reads, so it is sliced too: a kind renamed
   in the map and not in the branch is what this catches. */
const wordsStart = src.indexOf("const RUN_GATE_WORDS = {");
const wordsEnd = src.indexOf("\n};", wordsStart) + 3;
if (wordsStart < 0 || wordsEnd < 3) throw new Error("missing RUN_GATE_WORDS");
const WORDS = src.slice(wordsStart, wordsEnd);

let failures = 0;
function check(name, ok, detail) {
  if (!ok) { failures++; console.log("FAIL " + name + (detail === undefined ? "" : ` — ${JSON.stringify(detail)}`)); }
}

/* ---- the stub world --------------------------------------------------- */
function world(opts) {
  const o = opts || {};
  const layout = [];
  const ctx = {
    Math,
    console,
    location: { hash: "" },
    setSessLayout: (name, patch) => {
      if (o.layoutThrows) throw new Error("storage off");
      layout.push([name, patch]);
    },
    sessCflowRun: () => null,
    sessCflowGated: () => false,
    sessionLineOnPage: () => true,
    refreshCflow2Rail: () => {},
    termCflow2Answers: () => [],
    cflow2RailCache: {},
    currentPage: "cli",
    currentName: "s1",
  };
  vm.createContext(ctx);
  vm.runInContext([
    WORDS,
    slice("runGatePresses"),
    slice("runGateTitle"),
    slice("runGateShortfall"),
    slice("runGateOpen"),
    slice("runGateOpenPlan"),
    slice("runGateWhere"),
    slice("termCflow2Question"),
    slice("termCflowView"),
    "this.api = {runGatePresses, runGateTitle, runGateShortfall, runGateOpen, runGateOpenPlan," +
      " runGateWhere, termCflowView, termCflow2Question, words: RUN_GATE_WORDS};",
  ].join("\n"), ctx);
  return { ctx, layout, api: ctx.api };
}

/* ---- the card ---------------------------------------------------------- */
{
  const { api } = world();
  check("the kind has a word of its own",
        api.words["plan-wait"] === "waits on your plan decision", api.words["plan-wait"]);
  const title = api.runGateTitle({ kind: "plan-wait", label: "plan" });
  check("the title names the plan and not the label", title === api.words["plan-wait"], title);
  check("the card says where the decision is taken",
        /plan panel/.test(api.runGateShortfall({ kind: "plan-wait", presses: [] })),
        api.runGateShortfall({ kind: "plan-wait", presses: [] }));
}

/* ---- where a click goes ------------------------------------------------ */
{
  const { api, layout, ctx } = world();
  api.runGateWhere({ kind: "plan-wait", session: "s1" });
  check("the plan pane is opened for that session, before the hash moves",
        layout.length === 1 && layout[0][0] === "s1"
        && layout[0][1].split === true && layout[0][1].pane === "plan", layout);
  check("and the session itself opens", ctx.location.hash === "#/s/s1", ctx.location.hash);
}
{
  const { api, layout, ctx } = world();
  api.runGateWhere({ kind: "ask", session: "s2", label: "coding-approval" });
  check("every other kind opens the session alone, as before",
        layout.length === 0 && ctx.location.hash === "#/s/s2", { layout, hash: ctx.location.hash });
}
{
  const { api, ctx } = world({ layoutThrows: true });
  api.runGateWhere({ kind: "plan-wait", session: "s3" });
  check("a browser that refuses the layout still opens the session",
        ctx.location.hash === "#/s/s3", ctx.location.hash);
}

/* ---- the terminal strip ------------------------------------------------ */
{
  const { ctx } = world();
  ctx.cflow2RailCache = {
    s1: { run: "r1", status: "running", workflow: "improv-worker-v2",
          scope: "s1@main", cwd: "/repo", gated: true,
          answers: [{ kind: "plan-wait", instance: "i1", label: "plan",
                      prompt: "plan 미결정이 해소되기를 기다린다" }] },
  };
  const view = ctx.api.termCflowView();
  check("the strip offers no press for a plan wait", view && view.answers.length === 0, view);
  check("and points at the plan pane",
        view && /plan pane/.test(view.question), view && view.question);
}

/* ---- the two surfaces agree on the kind -------------------------------- */
check("the daemon's kind is the one app.js knows",
      /"plan-wait"/.test(fs.readFileSync(
        path.join(STATIC, "..", "..", "daemon", "cflow2_web.py"), "utf8")));

if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log("planwaitcard_check ok");
