/* The nudge countdown on the session's OWN header (#term-timer).

   railtimer_check covers the strip above the rail's nav: which of the two
   clocks speaks, how the countdown ages, how the several silences stay told
   apart. All of that is shared — the header chip calls the very same
   railTimerPick and railTimerLine, deliberately, because two wordings for
   one clock drift and the strip's are the tested ones.

   What is NOT shared is the whole reason this chip exists, and it is what
   this check is about:

   * the strip answers "is the daemon about to type into something on this
     machine", and when the attached session drives no run it falls back to
     whichever run fires soonest. That is right for a rail and wrong for a
     header: a countdown drawn beside a session's name is read as that
     session's. So termTimerRun has NO fallback — the attached session's run,
     with timers of its own, or nothing;
   * a reading is stamped with the session it was taken for. The chip
     repaints every second while the poll that feeds it comes round every
     two, so walking from one terminal to another leaves a whole second in
     which the previous session's countdown would sit on the new session's
     name;
   * it is one line of a crowded header rather than a strip of its own, so it
     clips rather than shoving the buttons along, and it drops the scope
     label the strip carries — the name is already at the other end of the
     same row.

   And the three wirings, which no amount of correct arithmetic replaces: the
   2s poll feeds it, every attach path reseeds it, and one interval ages both
   faces of the clock so they cannot drift a second apart. */
const fs = require("fs");
const path = require("path");
const staticDir = path.join(__dirname, "..", "..", "src", "claude_launcher",
                            "web", "static");
const src = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const css = fs.readFileSync(path.join(staticDir, "style.css"), "utf8");
const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");

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

function table(name) {
  const start = src.indexOf(`const ${name} = {`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  return src.slice(start, src.indexOf("};", start) + 2);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {},
    dataset: {}, title: undefined,
    append(...cs) { cs.forEach((c) => n.kids.push(c)); },
    appendChild(c) { n.kids.push(c); return c; },
    addEventListener(k, fn) { (n.handlers[k] ||= []).push(fn); },
    fire(k, ev) { (n.handlers[k] || []).forEach((fn) => fn(ev)); },
    removeAttribute(a) { if (a === "title") n.title = undefined; },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); if (!v) n.kids = []; },
    get className() { return [...n.classes].join(" "); },
    set className(v) {
      n.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    classList: {
      contains: (c) => n.classes.has(c),
      toggle: (c, on) => {
        const want = on === undefined ? !n.classes.has(c) : !!on;
        if (want) n.classes.add(c); else n.classes.delete(c);
        return want;
      },
    },
  };
  return n;
}

const chip = node("button");
chip.className = "term-btn timer-chip hidden";
const $ = (id) => {
  if (id !== "term-timer") throw new Error("unexpected $: " + id);
  return chip;
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const location = { hash: "" };

/* The one call the chip makes at the daemon. Captured rather than performed:
   what this check is about is the body — a wrong `enabled` here is a switch
   that flips the wrong way, and nothing on screen would say so. `hold`
   parks it so a test can prove the chip stays disabled until it settles. */
const posts = [];
let holdPost = null;
function cflowAction(path, body) {
  posts.push({ path, body });
  return holdPost ? new Promise((r) => { holdPost = r; }) : Promise.resolve();
}

const ctx = {};
new Function(
  "exports", "$", "el", "location", "cflowAction",
  "let currentName = null, cflowCache = [];\n" +
  [table("RAIL_TIMER_RANK"), table("RAIL_TIMER_GLYPH"),
   slice("fmtCountdown"), slice("railTimerPick"), slice("railTimerTitle"),
   slice("railTimerLine"), slice("sessCflowRun"), slice("railTimerRun"),
   slice("termTimerRun"), table("TERM_TIMER_HOLD_GLYPH"),
   slice("renderTermTimer"), slice("termTimerHold"), slice("termTimerTitle"),
   slice("paintTermTimer"),
   // slice() cuts from the `function` keyword, so the modifier in front of
   // it has to be put back — and asserting it was there is the point: the
   // click writes to the daemon, and a synchronous version of it could not.
   "async " + slice("termTimerClick")].join("\n") + `
let termTimerRead = null, termTimerBusy = false;
exports.termRun = termTimerRun;
exports.railRun = railTimerRun;
exports.render = renderTermTimer;
exports.paint = paintTermTimer;
exports.hold = termTimerHold;
exports.read = () => termTimerRead;
exports.age = (sec) => { termTimerRead.at -= sec * 1000; };
exports.setWorld = (name, runs) => { currentName = name; cflowCache = runs; };
`)(ctx, $, el, location, cflowAction);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const run = (scope, over) => Object.assign({
  scope, cwd: "F:/repo", workflow: "ship", status: "step", sessions: [scope],
  timers: {
    reminder: { running: true, enabled: true, interval: 600, due_in: 360,
                fired_ago: null, state: "counting" },
    ping: { running: true, enabled: false, interval: 900, due_in: null,
            fired_ago: null, state: "off" },
  },
}, over);

/* ---- whose clock this is: no fallback ------------------------------- */
/* The header has a subject. Every case below is one the rail answers with
   somebody else's run — which is the correct rail answer and a lie here. */
const mine = run("s19");
const other = Object.assign(run("s20"), { cwd: "F:/other" });
other.timers.reminder.due_in = 30;

ctx.setWorld("s19", [other, mine]);
check("the attached session's own run is the subject", ctx.termRun().scope, "s19");

/* The attached session drives a run the daemon published no timers for (an
   older daemon, or a slot that is idle). The rail falls through to the fleet
   here; the header must not — both asserted, because the difference IS the
   feature and a later edit that "unified" them would pass one check alone. */
const untimed = Object.assign(run("s19"), { timers: undefined });
ctx.setWorld("s19", [untimed, other]);
check("a run without timers is not this session's countdown", ctx.termRun(), null);
check("...while the rail still falls through to the fleet, as it should",
      ctx.railRun().scope, "s20");

/* Attached to a session that drives no run at all. */
ctx.setWorld("s21", [mine, other]);
check("a session with no run of its own shows nothing", ctx.termRun(), null);
/* Nothing attached: there is no subject, so there is no chip — the rail's
   fallback would put a stranger's countdown on an empty header. */
ctx.setWorld(null, [mine, other]);
check("nothing attached, nothing to speak for", ctx.termRun(), null);
check("...where the rail would still have picked the soonest",
      ctx.railRun().scope, "s20");
/* An idle run is not this session's clock either — sessCflowRun drops it. */
ctx.setWorld("s19", [Object.assign(run("s19"), { status: "idle" })]);
check("an idle run is not a countdown", ctx.termRun(), null);

/* ---- what the chip draws -------------------------------------------- */
ctx.setWorld("s19", [mine]);
ctx.render();
check("the chip is shown", chip.classList.contains("hidden"), false);
check("...keeping the header's own button dress",
      ["term-btn", "timer-chip"].every((c) => chip.classList.contains(c)), true);
check("...and wearing the clock's state", chip.classList.contains("counting"), true);
/* The words are the strip's, unchanged — one clock, one vocabulary. The
   glyph is NOT: on a chip with a subject those pixels are the switch, and
   the strip's own vocabulary would put "⏱" here while the thing is running
   and "○" once somebody paused it, which is the wrong way round for
   something pressable. */
check("the strip's words, and the switch's glyph in front of them",
      chip.kids.map((k) => k.text), ["⏸", "step reminder in 6:00"]);
check("...and a running reminder is not dressed as a paused one",
      chip.classList.contains("timer-paused"), false);
/* The strip prints the scope because it may be speaking for a run the reader
   is not looking at. Here the session's name is at the other end of the same
   row, so a second copy of it is noise. */
check("no scope label: the name is already on this row",
      chip.kids.some((k) => k.classes.has("rt-scope") || k.text === "s19"), false);
check("the hover text still answers for the clock it is NOT reporting",
      chip.title.split("\n").slice(0, 3),
      ["ship · s19",
       "step reminder · counting · every 10:00",
       "stall ping · off · switched off"]);

/* ---- the countdown ages, and cannot run past zero -------------------- */
ctx.setWorld("s19", [run("s19", { timers: {
  reminder: { running: true, enabled: true, interval: 600, due_in: 65,
              fired_ago: null, state: "counting" },
  ping: { running: true, enabled: false, interval: 900, due_in: null,
          fired_ago: null, state: "off" },
} })]);
ctx.render();
ctx.age(5);
ctx.paint();
check("the reading ages between the 2s polls",
      chip.kids.map((k) => k.text)[1], "step reminder in 1:00");
ctx.age(65);
ctx.paint();
check("...and past its deadline it becomes due, not negative time",
      [chip.classList.contains("due"), chip.kids.map((k) => k.text)[1]],
      [true, "step reminder due now"]);

/* ---- a reading belongs to the session it was taken for ---------------- */
/* Walking to another terminal repaints long before the poll comes round.
   The last session's countdown left on this session's name is the one
   mistake a per-session chip must not be able to make. */
ctx.setWorld("s19", [mine]);
ctx.render();
check("the reading is stamped with its session", ctx.read().name, "s19");
ctx.setWorld("s20", [mine, other]);   // switched terminals; poll not yet round
ctx.paint();
check("a stale reading is not repainted onto the new session",
      chip.classList.contains("hidden"), true);
check("...and its title goes with it", chip.title, undefined);
/* The poll (or the attach) re-reads, and the new session's own clock shows. */
ctx.render();
check("the new session's own clock takes its place",
      [chip.classList.contains("hidden"), chip.kids.map((k) => k.text)[1]],
      [false, "step reminder in 0:30"]);

/* ---- no timers, no chip ---------------------------------------------- */
ctx.setWorld("s19", [untimed]);
ctx.render();
check("a run the daemon published no timers for draws nothing, not a zero",
      [chip.classList.contains("hidden"), chip.textContent], [true, ""]);

/* Everything from here on turns on a click, and the click is async: it
   writes to the daemon and repaints on the answer. A CommonJS check file has
   no top-level await, so the rest of the file runs inside one — the ordering
   IS part of what is being checked, and interleaving it with the synchronous
   tail would test a different thing. */
/* A press starts an async write and only repaints on its answer, so between
   firing one and reading the chip there are microtasks to let through. Two
   turns: one for the write, one for the repaint after it. */
const settle = () => Promise.resolve().then(() => Promise.resolve());

main();
async function main() {

/* ---- the click: the switch ------------------------------------------- */
/* The chip is pressable and wears a pause glyph, so pressing it has to
   pause. It used to navigate to the run page instead — which is the whole
   bug, and the kind that survives review because the destination is a real
   and useful page. A control that does something reasonable other than what
   its icon promises is still a control that lies. */
const paused = run("s19", { timers: {
  reminder: { running: true, enabled: false, interval: 600, due_in: null,
              fired_ago: null, state: "off" },
  ping: { running: true, enabled: false, interval: 900, due_in: null,
          fired_ago: null, state: "off" },
} });

ctx.setWorld("s19", [mine]);
ctx.render();
ctx.paint();
ctx.paint();
check("the listener is wired once for the node's lifetime, not per repaint",
      (chip.handlers.click || []).length, 1);

posts.length = 0;
location.hash = "";
chip.fire("click");
check("pressing a running reminder pauses it, for THIS run only",
      posts, [{ path: "/api/cflow/reminder",
                body: { cwd: "F:/repo", scope: "s19", enabled: false } }]);
check("...and does not navigate away from the terminal", location.hash, "");
/* The poll that would tell the truth is up to two seconds out, and a switch
   that takes two seconds to look flipped reads as a switch that did nothing.
   So the reading is amended in place and repainted at once. */
await settle();
check("...the chip flips immediately rather than waiting for the 2s poll",
      [chip.kids.map((k) => k.text)[0], chip.classList.contains("timer-paused")],
      ["▶", true]);

/* And back. The daemon's own answer for a paused run: `off`, whose line the
   strip words as "step reminder off". */
posts.length = 0;
ctx.setWorld("s19", [paused]);
ctx.render();
check("a paused reminder shows the play glyph and says so",
      [chip.kids.map((k) => k.text), chip.classList.contains("timer-paused")],
      [["▶", "step reminder off"], true]);
chip.fire("click");
check("pressing it again resumes",
      posts, [{ path: "/api/cflow/reminder",
                body: { cwd: "F:/repo", scope: "s19", enabled: true } }]);
await settle();

/* Only `enabled` travels. A run that had an interval set keeps it (the
   override merges, cflow_engine.set_reminder), and nothing in the browser
   has to know the floor the engine enforces. */
check("the interval is not resent, so a run's own interval survives a pause",
      Object.keys(posts[0].body).sort(), ["cwd", "enabled", "scope"]);

/* One flip per press. Two clicks on a slow daemon must not queue two
   opposite writes and land on whichever the network delivered last. */
posts.length = 0;
holdPost = true;
ctx.setWorld("s19", [mine]);
ctx.render();
chip.fire("click");
check("the chip is disabled while the flip is in flight", chip.disabled, true);
chip.fire("click");
check("...so a double-click is one flip, not two", posts.length, 1);
// Let the parked write land, or the chip stays disabled for every check below.
if (typeof holdPost === "function") holdPost();
holdPost = null;
await settle();
check("...and the chip takes presses again once the write lands",
      chip.disabled, false);

/* ---- the click: where there is no switch ----------------------------- */
/* A daemon old enough to publish timers without the reminder's standing.
   There is nothing to flip, so the chip keeps the strip's glyph and the
   strip's destination — degrading to the old behaviour beats a button that
   silently does nothing. */
posts.length = 0;
const noSwitch = run("s19", { timers: {
  ping: { running: true, enabled: true, interval: 900, due_in: 120,
          fired_ago: null, state: "counting" },
} });
ctx.setWorld("s19", [noSwitch]);
ctx.render();
check("no reminder published, no switch offered", ctx.hold(), null);
check("...so the status glyph keeps its place",
      chip.kids.map((k) => k.text), ["⏱", "stall ping in 2:00"]);
chip.fire("click");
check("...and the click still opens the run page for THIS session's run",
      [location.hash, posts.length],
      ["#/wf/" + encodeURIComponent("s19|F:/repo"), 0]);

/* ---- the hover text -------------------------------------------------- */
/* A readout owes the reader what it says; a control owes them what pressing
   it does, and — since pressing it no longer goes there — where the page it
   used to open still is. */
ctx.setWorld("s19", [mine]);
ctx.render();
const tip = chip.title.split("\n");
check("the clock's own lines still lead", tip.slice(0, 2),
      ["ship · s19", "step reminder · counting · every 10:00"]);
check("...then what the press does, in the direction it would go",
      tip.some((l) => /^Click to PAUSE this run's step reminder/.test(l)), true);
check("...and where the interval still lives",
      tip.some((l) => /run page/.test(l)), true);
ctx.setWorld("s19", [paused]);
ctx.render();
check("a paused chip offers the other direction",
      chip.title.split("\n")
        .some((l) => /^Click to RESUME this run's step reminder/.test(l)), true);
check("...and never both at once",
      chip.title.split("\n").filter((l) => /^Click to /.test(l)).length, 1);

/* ---- the wirings ----------------------------------------------------- */
/* Correct arithmetic that nothing calls is a chip that never moves. */
check("the 2s cflow poll feeds it",
      /renderRailTimer\(\);[^\n]*\n\s*renderTermTimer\(\);/.test(src), true);
/* setStatusBadge is the one point every attach path passes through
   (freshAttach and restoreTerminal both seed the header with it), which is
   what keeps the last session's clock off this session's name. */
const badge = slice("setStatusBadge");
check("every attach path reseeds it", /renderTermTimer\(\)/.test(badge), true);
check("one interval ages both faces of the clock",
      /setInterval\(\(\) => \{ paintRailTimer\(\); paintTermTimer\(\); \}, 1000\)/
        .test(src), true);

/* ---- the shipped page ------------------------------------------------ */
const headerAt = html.indexOf('id="term-header"');
const chipAt = html.indexOf('id="term-timer"');
const statusAt = html.indexOf('id="term-status"');
const actionsAt = html.indexOf('class="term-actions"');
check("the chip exists in the shipped markup", chipAt >= 0, true);
check("...inside the session's header", chipAt > headerAt, true);
check("...beside the badge it qualifies, before the actions",
      statusAt < chipAt && chipAt < actionsAt, true);
check("it starts hidden — an empty chip must not sit in the row",
      /id="term-timer"[^>]*class="[^"]*hidden"/.test(html), true);

/* ---- the stylesheet -------------------------------------------------- */
const rules = css.replace(/\/\*[\s\S]*?\*\//g, "");
check("counting takes no colour of its own — the common case must not glow",
      /\.timer-chip\.counting/.test(rules), false);
check("but due, held and a dead tick do",
      ["due", "held", "stopped"].every(
        (s) => new RegExp(`\\.timer-chip\\.${s} \\{[^}]*color:`).test(rules)),
      true);
/* Paused by a person is not the same fact as the line's own `off`, and the
   two can be true at different times: the chip may be reporting the stall
   ping while the reminder behind the switch is the thing somebody paused. */
check("a reminder paused at this chip has a state of its own to wear",
      /\.timer-chip\.timer-paused \{[^}]*color:/.test(rules), true);
check("...and the chip says when a flip is in flight, like the hold chip",
      /\.timer-chip:disabled \{[^}]*cursor:\s*wait/.test(rules), true);
check("it clips rather than shoving the header's buttons along",
      /\.timer-chip \{[^}]*white-space:\s*nowrap/.test(rules) &&
      /\.timer-chip \{[^}]*overflow:\s*hidden/.test(rules) &&
      /\.timer-chip \{[^}]*flex:\s*0 1 auto/.test(rules), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("termtimer_check: ok");

}   // main
