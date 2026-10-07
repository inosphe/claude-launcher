/* A page's own poll stands down while the tab is hidden (claunch-dumca).

   The dashboard tick slows down behind another tab; the per-page polls -- a
   workflow, the split pane, the beads board, the window page, reports, a
   session's detail, its run and its children, the transcript -- used to keep
   redrawing at full rate for nobody. Each redraw is a DOM rebuild that every
   content script observing the page walks again, so a hidden dashboard tab
   was spending seconds of CPU a minute on pages no one was reading.

   Pinned here, against the real `visiblePoll` sliced out of the shipped
   app.js and a stub document:
     - a tick on a visible tab runs the poll;
     - a tick on a hidden tab does not, and the poll runs once when the tab
       is shown again, however many ticks came due meanwhile;
     - a poll whose timer was stopped while hidden (its page closed) is not
       run on the way back;
     - a failing async poll on the way back does not throw out of the
       visibility handler;
     - every page poll's setInterval goes through visiblePoll. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* From the registry's declaration to the end of the guarded visibilitychange
   registration that follows runSkippedPolls -- one contiguous stretch. */
const start = src.indexOf("const hiddenSkippedPolls = new Map();");
if (start < 0) throw new Error("cannot locate hiddenSkippedPolls");
const guard = src.indexOf(
  'if (typeof document !== "undefined" && document.addEventListener) {',
  src.indexOf("function runSkippedPolls(", start));
if (guard < 0) throw new Error("cannot locate the visibilitychange registration");
let depth = 0;
let end = -1;
for (let j = src.indexOf("{", guard); j < src.length; j++) {
  if (src[j] === "{") depth += 1;
  else if (src[j] === "}") { depth -= 1; if (depth === 0) { end = j + 1; break; } }
}
const block = src.slice(start, end);
if (!block.includes('"visibilitychange"')) throw new Error("sliced the wrong guard");

function world() {
  const listeners = [];
  const document = {
    hidden: false,
    addEventListener(type, fn) { if (type === "visibilitychange") listeners.push(fn); },
  };
  const ctx = { document };
  vm.createContext(ctx);
  vm.runInContext(`${block}\nthis.visiblePoll = visiblePoll;`, ctx);
  const show = () => { document.hidden = false; listeners.forEach((fn) => fn()); };
  const hide = () => { document.hidden = true; listeners.forEach((fn) => fn()); };
  return { ctx, document, show, hide, listeners };
}

{
  const w = world();
  check("one visibilitychange listener is registered", w.listeners.length === 1, w.listeners.length);
  let runs = 0;
  const tick = w.ctx.visiblePoll(() => { runs += 1; }, () => true);
  tick();
  check("a visible tick runs the poll", runs === 1, runs);
  w.hide();
  tick(); tick(); tick();
  check("hidden ticks do not run the poll", runs === 1, runs);
  w.show();
  check("showing the tab runs the poll once", runs === 2, runs);
  w.hide(); w.show();
  check("a hide/show with no tick in between runs nothing", runs === 2, runs);
}

{
  const w = world();
  let runs = 0;
  let timer = 1;
  const tick = w.ctx.visiblePoll(() => { runs += 1; }, () => timer);
  w.hide();
  tick();
  timer = null; // the page closed while the tab was hidden
  w.show();
  check("a poll stopped while hidden is not run on return", runs === 0, runs);
}

{
  const w = world();
  let rejected = 0;
  const tick = w.ctx.visiblePoll(
    () => Promise.reject(new Error("offline")).catch((e) => { rejected += 1; throw e; }),
    () => true);
  w.hide();
  tick();
  let threw = false;
  try { w.show(); } catch { threw = true; }
  check("a failing poll does not throw out of the handler", !threw);
  setTimeout(() => check("the failing poll did run", rejected === 1, rejected), 0);
}

{
  const w = world();
  const order = [];
  const a = w.ctx.visiblePoll(() => order.push("a"), () => true);
  const b = w.ctx.visiblePoll(() => order.push("b"), () => true);
  w.hide();
  a(); b(); a();
  w.show();
  check("each skipped poll runs once on return", order.join("") === "ab", order);
}

/* Every page poll is wrapped. The dashboard tick (pollTick) has its own
   hidden-tab rule and the 1 s timer ticker paints only a clock; neither is a
   page poll. */
const PAGE_POLLS = [
  "pollTranscript", "refreshWf", "refreshSplit", "refreshBeads", "refreshWindow",
  "refreshReports", "refreshSession", "refreshSessRun", "refreshSessKids",
];
for (const fn of PAGE_POLLS) {
  const bare = new RegExp(`setInterval\\(\\s*${fn}\\b`);
  check(`${fn} is not polled bare`, !bare.test(src));
  const wrapped = new RegExp(`setInterval\\(\\s*visiblePoll\\(${fn},`);
  check(`${fn} is polled through visiblePoll`, wrapped.test(src));
}

process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log("all hidden-poll checks passed");
});
