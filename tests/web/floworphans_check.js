/* The Flows page's Orphans tab, run against the real functions from app.js.

   The tab exists because the daemon already reports these runs and nobody
   could act on the report: the run event clock types "nobody is driving"
   into the session that oversees the run, which is an agent's terminal and
   a CLI command. What has to hold here is that the tab is a FILTER over the
   daemon's own answer and not a second judgment of it — `orphaned` is set by
   /api/cflow from the predicate the clock fires on, so a run with no session
   record (a CLI run) and a run whose session is alive are both absent for
   the same reason, without this file knowing either rule. The summary line
   is checked separately because it is the one sentence a reader gets before
   deciding between resuming the session and archiving the run. */
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

const cflowOrphans = new Function(
  `${slice("cflowOrphans")}; return cflowOrphans;`
)();
const cflowOrphanSummary = new Function(
  `${slice("cflowOrphanSummary")}; return cflowOrphanSummary;`
)();

function assert(cond, msg) {
  if (!cond) { console.error(`FAIL: ${msg}`); process.exit(1); }
}

function eq(got, want, what) {
  assert(JSON.stringify(got) === JSON.stringify(want),
         `${what}: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
}

// --- which runs the tab lists ------------------------------------------- //

const runs = [
  { scope: "s655", run: "run-eeaa6522", status: "step", orphaned: true },
  { scope: "s668", run: "run-11", status: "step" },              // has a driver
  { scope: "default", run: "run-22", status: "waiting_approval" }, // a CLI run
  { scope: "s400", run: "run-33", status: "done" },              // finished
];

const listed = cflowOrphans(runs).map((r) => r.scope);
assert(listed.length === 1 && listed[0] === "s655",
       `only the marked run is listed (got ${JSON.stringify(listed)})`);

// The mark is the daemon's, and nothing here second-guesses it: a run that
// merely LOOKS driverless (no sessions, blocked on a person) stays off the
// tab unless the endpoint said so.
assert(cflowOrphans([{ scope: "x", status: "waiting_approval", sessions: [] }])
         .length === 0,
       "an empty sessions list is not read as orphaned");
// ...and a marked run is listed whatever else it carries.
assert(cflowOrphans([{ scope: "x", status: "waiting_approval", sessions: [],
                       orphaned: true }]).length === 1,
       "the daemon's mark alone decides");

// An answer that has not arrived, or arrived empty, draws an empty tab
// rather than throwing on the count in the tab label.
assert(cflowOrphans(null).length === 0, "a missing answer lists nothing");
assert(cflowOrphans(undefined).length === 0, "an absent answer lists nothing");
assert(cflowOrphans([]).length === 0, "an empty answer lists nothing");
assert(cflowOrphans([null, undefined]).length === 0,
       "holes in the answer are skipped rather than dereferenced");

// --- what the card says about one ---------------------------------------- //

const full = cflowOrphanSummary({
  scope: "s655", run: "run-eeaa6522", status: "step", step_id: "intake",
});
assert(full.includes("s655"), "the summary names the session to resume");
assert(full.includes("run-eeaa6522"), "the summary names the run");
assert(full.includes("'intake'"), "the summary names the step it stands at");

// The step's title is what the run page shows, so it wins over the id.
const titled = cflowOrphanSummary({
  scope: "s655", run: "r1", step_id: "intake", title: "read the brief",
});
assert(titled.includes("'read the brief'") && !titled.includes("'intake'"),
       "a step with a title is named by it");

// A run parked somewhere with no step (an error slot, a start request) still
// gets a sentence: the status stands in for the position.
const stepless = cflowOrphanSummary({ scope: "s9", run: "r2", status: "error" });
assert(stepless.includes("error"), "a run with no step falls back to status");
assert(!stepless.includes("''"), "no empty quotes where a step would be");

// Missing fields are named rather than printed as "undefined" — the tab is
// read while something is already wrong, and a blank is one more puzzle.
const bare = cflowOrphanSummary({});
assert(!bare.includes("undefined"), `no raw undefined in "${bare}"`);
assert(bare.includes("its session"), "a scopeless run still reads as a sentence");

const defaulted = cflowOrphanSummary({ scope: "default", run: "r3" });
assert(!defaulted.includes("default"),
       "the placeholder scope is not printed as a session name");

// --- the route ----------------------------------------------------------- //
/* The tab is a place, not a toggle: a link to it survives a reload and Back
   returns to the Runs list rather than leaving the page. */

const parseHash = new Function(`${slice("parseHash")}; return parseHash;`)();

eq(parseHash("#/flows"), { page: "flows", section: "" }, "the bare page");
eq(parseHash("#/flows/orphans"), { page: "flows", section: "orphans" },
   "one section deep");
// An unknown section is the page itself, the way an unknown link is a wrong
// turn rather than an error.
eq(parseHash("#/flows/nonsense"), { page: "flows", section: "" },
   "an unknown section falls back to the run list");

// --- the tab strip, drawn against a stub DOM ---------------------------- //
/* The strip is also what shows and hides the two panes, so a wrong id here
   would leave the page blank with nothing thrown. */

function stubNode(id) {
  const node = {
    id, kids: [], hiddenState: null, html: "",
    appendChild(child) { this.kids.push(child); return child; },
    classList: { toggle: (cls, on) => { node.hiddenState = on; } },
  };
  Object.defineProperty(node, "innerHTML", {
    get() { return node.html; },
    set(v) { node.html = String(v); node.kids = []; },
  });
  return node;
}

function drawTabs(section, cache) {
  const nodes = {
    "flows-tabs": stubNode("flows-tabs"),
    "flows-runs": stubNode("flows-runs"),
    "flows-orphans": stubNode("flows-orphans"),
  };
  const render = new Function(
    "$", "el", "cflowOrphans", "cflowCache", "flowsSection",
    `${slice("renderFlowsTabs")}; return renderFlowsTabs;`
  )(
    (id) => nodes[id],
    (tag, cls, text) => ({ tag, cls, text, href: "" }),
    cflowOrphans,
    cache,
    section
  );
  render();
  return nodes;
}

const onOrphans = drawTabs("orphans", runs);
const tabs = onOrphans["flows-tabs"].kids;
assert(tabs.length === 2, `two tabs (got ${tabs.length})`);
assert(tabs[0].text === "Runs" && tabs[0].href === "#/flows",
       "the first tab is Runs and links to the bare page");
assert(tabs[1].href === "#/flows/orphans",
       "the second tab links one section deep");
assert(tabs[1].text === "Orphans (1)",
       `the count rides the label (got "${tabs[1].text}")`);
assert(tabs[1].cls.includes("on") && !tabs[0].cls.includes("on"),
       "the section on screen is the lit tab");
assert(onOrphans["flows-runs"].hiddenState === true &&
       onOrphans["flows-orphans"].hiddenState === false,
       "the Orphans section hides the Runs pane and shows its own");

const onRuns = drawTabs("runs", runs);
assert(onRuns["flows-tabs"].kids[0].cls.includes("on"),
       "Runs lights its own tab");
assert(onRuns["flows-runs"].hiddenState === false &&
       onRuns["flows-orphans"].hiddenState === true,
       "Runs hides the Orphans pane");

// Nothing to report: the label stays a plain word rather than reading "(0)".
const quiet = drawTabs("runs", [{ scope: "s1", status: "step" }]);
assert(quiet["flows-tabs"].kids[1].text === "Orphans",
       "an empty tab carries no count");

// The tab strip is rebuilt on every poll, so a second draw must replace the
// anchors rather than append a third and a fourth.
const nodes = {
  "flows-tabs": stubNode("flows-tabs"),
  "flows-runs": stubNode("flows-runs"),
  "flows-orphans": stubNode("flows-orphans"),
};
const render = new Function(
  "$", "el", "cflowOrphans", "cflowCache", "flowsSection",
  `${slice("renderFlowsTabs")}; return renderFlowsTabs;`
)((id) => nodes[id], (tag, cls, text) => ({ tag, cls, text, href: "" }),
  cflowOrphans, runs, "runs");
render();
render();
assert(nodes["flows-tabs"].kids.length === 2,
       `a repeated draw leaves two tabs (got ${nodes["flows-tabs"].kids.length})`);

console.log("floworphans_check: all assertions passed");
