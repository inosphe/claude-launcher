/* The nudge countdown on the session's OWN header (#term-timer) — since the
   rail's strip was removed (it duplicated this chip), the countdown's only
   home.

   railtimer_check guards that the rail stays free of any timer strip and
   tests the shared clock vocabulary directly: which of the two clocks
   speaks, how the countdown ages, how the several silences stay told apart.
   The header chip calls the very same railTimerPick and railTimerLine,
   deliberately — one vocabulary, one wording for one clock.

   What this check adds is the chip's own subject, and it is the whole
   reason this chip exists:

   * termTimerRun has NO fallback. A countdown drawn beside a session's name
     is read as that session's, so the attached session's run — with timers
     of its own — or nothing;
   * a reading is stamped with the session it was taken for. The chip
     repaints every second while the poll that feeds it comes round every
     two, so walking from one terminal to another leaves a whole second in
     which the previous session's countdown would sit on the new session's
     name;
   * it is one line of a crowded header, so it clips rather than shoving the
     buttons along, and it drops the scope label — the name is already at
     the other end of the same row.

   And the three wirings, which no amount of correct arithmetic replaces: the
   2s poll feeds it, every attach path reseeds it, and the one interval ages
   it. */
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
/* The skip that sits against it. A second node and not a child, because a
   button cannot live inside a button — which is the one structural fact the
   feature had to be built around, so the stub states it too. */
const skipBtn = node("button");
skipBtn.className = "term-btn timer-skip hidden";
const boxes = { "term-timer": chip, "term-timer-skip": skipBtn };
const $ = (id) => {
  if (!(id in boxes)) throw new Error("unexpected $: " + id);
  return boxes[id];
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
   slice("railTimerLine"), slice("sessCflowRun"),
   slice("termTimerRun"), table("TERM_TIMER_HOLD_GLYPH"),
   slice("renderTermTimer"), slice("termTimerHold"), slice("termTimerTitle"),
   table("TERM_TIMER_SKIPPABLE"), slice("termTimerSkip"),
   slice("termTimerSkipTitle"), slice("paintTermTimerSkip"),
   slice("paintTermTimer"),
   // slice() cuts from the `function` keyword, so the modifier in front of
   // it has to be put back — and asserting it was there is the point: the
   // click writes to the daemon, and a synchronous version of it could not.
   "async " + slice("termTimerClick"),
   "async " + slice("termTimerSkipClick")].join("\n") + `
let termTimerRead = null, termTimerBusy = false, termTimerSkipBusy = false;
exports.termRun = termTimerRun;
exports.skip = termTimerSkip;
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
/* The header has a subject. A countdown drawn beside a session's name is
   read as that session's, so every case below must show nothing rather than
   the soonest run on the machine — there is no fallback to fall back to. */
const mine = run("s19");
const other = Object.assign(run("s20"), { cwd: "F:/other" });
other.timers.reminder.due_in = 30;

ctx.setWorld("s19", [other, mine]);
check("the attached session's own run is the subject", ctx.termRun().scope, "s19");

/* The attached session drives a run the daemon published no timers for (an
   older daemon, or a slot that is idle). Only the attached run counts — a
   fleet clock is somebody else's clock. */
const untimed = Object.assign(run("s19"), { timers: undefined });
ctx.setWorld("s19", [untimed, other]);
check("a run without timers is not this session's countdown", ctx.termRun(), null);

/* Attached to a session that drives no run at all. */
ctx.setWorld("s21", [mine, other]);
check("a session with no run of its own shows nothing", ctx.termRun(), null);
/* Nothing attached: there is no subject, so there is no chip. */
ctx.setWorld(null, [mine, other]);
check("nothing attached, nothing to speak for", ctx.termRun(), null);
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
/* The words are the shared vocabulary's, unchanged — one clock, one
   set of words. The glyph is NOT: on a chip with a subject those pixels
   are the switch, and that vocabulary would put "⏱" while it is running
   and "○" once somebody paused it, which is the wrong way round for
   something pressable. */
check("the clock's words, and the switch's glyph in front of them",
      chip.kids.map((k) => k.text), ["⏸", "step reminder in 6:00"]);
check("...and a running reminder is not dressed as a paused one",
      chip.classList.contains("timer-paused"), false);
/* The chip drops the scope label: the session's name is at the other end
   of the same row, so a second copy of it is noise. */
check("no scope label: the name is already on this row",
      chip.kids.some((k) => k.classes.has("rt-scope") || k.text === "s19"), false);
check("the hover text still answers for the clock it is NOT reporting",
      chip.title.split("\n").slice(0, 3),
      ["ship · s19",
       "step reminder · counting · every 10:00",
       "stall ping · off · switched off"]);

/* ---- the skip beside the switch -------------------------------------- */
/* The switch pauses the clock; this lets ONE reminder go by and keeps it.
   The two are one row apart, so the thing that has to be pinned first is
   that the skip is decided from the REMINDER's own standing and not from the
   line — the chip reports whichever clock is loudest, and a skip offered off
   that reading would be offered for a clock this button cannot touch. */
ctx.setWorld("s19", [mine]);
ctx.render();
check("a counting reminder has something to skip, and names the run",
      ctx.skip(),
      { state: "counting", cwd: "F:/repo", scope: "s19", interval: 600 });
check("...so the button is up, icon only, wearing the clock's state",
      [skipBtn.classList.contains("hidden"), skipBtn.textContent,
       skipBtn.classList.contains("counting")],
      [false, "⏭", true]);

/* Held is the state it is worth the most in: a held reminder is due and
   retried EVERY poll, so it lands the instant the agent starts its next
   turn — which is exactly the turn somebody is trying to keep clear. */
const heldRun = run("s19", { timers: {
  reminder: { running: true, enabled: true, interval: 600, due_in: -42,
              fired_ago: null, state: "held" },
  ping: { running: true, enabled: false, interval: 900, due_in: null,
          fired_ago: null, state: "off" },
} });
ctx.setWorld("s19", [heldRun]);
ctx.render();
check("a reminder held for a stopped session is skippable, and says so",
      [ctx.skip().state, skipBtn.classList.contains("held")], ["held", true]);

/* And the silences are not "not yet", they are nothing to skip. Offering the
   button on them would be a promise the daemon cannot keep: on `arming` the
   clock has no timer for this run at all, on `watching` it repeats nothing,
   and on the other three it is already quiet. */
for (const state of ["off", "blocked", "stopped", "arming", "watching"]) {
  ctx.setWorld("s19", [run("s19", { timers: {
    reminder: { running: true, enabled: true, interval: 600, due_in: null,
                fired_ago: null, state },
    ping: { running: true, enabled: false, interval: 900, due_in: null,
            fired_ago: null, state: "off" },
  } })]);
  ctx.render();
  check("nothing is coming in '" + state + "', so nothing to skip",
        [ctx.skip(), skipBtn.classList.contains("hidden"),
         skipBtn.textContent],
        [null, true, ""]);
}

/* And the skip is decided from the REMINDER, never from the line — which the
   five cases above cannot show, because a silent ping leaves the chip
   speaking for the reminder anyway. The one arrangement that separates them
   is a quiet reminder under a LOUD ping: the ranking hands the line to the
   ping, so a skip read off the line would be offered here — for a clock this
   button cannot touch (the stall ping is machine-wide) and with no reminder
   coming to skip at all.

   What it settles is narrower than it looks, and the narrowing is the
   point. Against a SILENT ping this is the only arrangement that separates
   the two readings: a skippable reminder (due 0, counting 1, held 2)
   outranks every silence a ping can be in (waiting 3 … off 8), so a quiet
   ping can never take the line from a reminder that has something to skip.

   It settles nothing about a LOUD ping, and an earlier version of this
   comment claimed it did (caught in review). A ping is `due` or `counting`
   often enough, and then it does take the line — ping due(0) over reminder
   counting(1) or held(2), ping counting(1) over reminder held(2). There
   BOTH readings are skippable, so the button is up either way and hiding
   cannot tell them apart. The case below is where that half is settled. */
ctx.setWorld("s19", [run("s19", { timers: {
  reminder: { running: true, enabled: false, interval: 600, due_in: null,
              fired_ago: null, state: "off" },
  ping: { running: true, enabled: true, interval: 900, due_in: 120,
          fired_ago: null, state: "counting" },
} })]);
ctx.render();
check("the line is the ping's, because the ping is the loud one",
      chip.kids.map((k) => k.text)[1], "stall ping in 2:00");
check("...but the skip speaks for the reminder, which has nothing to skip",
      [ctx.skip(), skipBtn.classList.contains("hidden")], [null, true]);

/* The half hiding cannot catch: a loud ping over a reminder that HAS
   something to skip. Both readings are skippable, so the button is up
   whichever one is read — what diverges is the state it wears and the
   sentence it says, because those come from the same target the press does.
   Read off the line instead and this press dresses in the ping's clothes
   while still sending the reminder's interval: a held reminder — a debt
   retried every poll — described as a countdown to the next one. */
ctx.setWorld("s19", [run("s19", { timers: {
  reminder: { running: true, enabled: true, interval: 600, due_in: -42,
              fired_ago: null, state: "held" },
  ping: { running: true, enabled: true, interval: 900, due_in: 300,
          fired_ago: null, state: "counting" },
} })]);
ctx.render();
check("a loud ping takes the line even from a skippable reminder",
      chip.kids.map((k) => k.text)[1], "stall ping in 5:00");
check("...and the skip is offered, because the reminder is the one it reads",
      [ctx.skip().state, skipBtn.classList.contains("hidden")],
      ["held", false]);
check("...wearing the reminder's state, not the line's",
      [skipBtn.classList.contains("held"),
       skipBtn.classList.contains("counting")], [true, false]);
check("...and saying the debt rather than a countdown",
      /retried every poll/.test(skipBtn.title), true);

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
/* The skip goes down with it. A live button that would skip the PREVIOUS
   session's reminder, sitting on this session's name, is the same mistake
   as the countdown — and worse, because it writes. */
check("...and so does the skip beside it",
      [skipBtn.classList.contains("hidden"), skipBtn.title], [true, undefined]);
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
check("...and no skip either — there is no clock to skip a beat of",
      [ctx.skip(), skipBtn.classList.contains("hidden")], [null, true]);

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

/* ---- the click: the skip --------------------------------------------- */
/* The narrow verb. Everything about this press is defined against the one
   beside it: it must reach a different door, it must not send a setting,
   and the switch must be exactly where it was afterwards. A skip that
   quietly paused would look identical for one interval and then be a run
   nobody gets reminded about. */
posts.length = 0;
holdPost = null;
ctx.setWorld("s19", [mine]);
ctx.render();
ctx.paint();
ctx.paint();
check("the skip's listener is wired once too, not once per repaint",
      (skipBtn.handlers.click || []).length, 1);

skipBtn.fire("click");
check("pressing it asks the daemon to skip ONE of this run's reminders",
      posts, [{ path: "/api/cflow/reminder/skip",
                body: { cwd: "F:/repo", scope: "s19" } }]);
check("...and it is not the switch's door, nor carries a setting",
      [posts[0].path === "/api/cflow/reminder",
       Object.keys(posts[0].body).sort()],
      [false, ["cwd", "scope"]]);
await settle();
/* `mine` was counting down from 6:00 of a 10:00 interval. The daemon has
   just re-armed it where it stood, so the honest local reading is a full
   interval — and the 2s poll is too far away to wait for it. */
check("...the countdown restarts at a full interval at once, not in two seconds",
      chip.kids.map((k) => k.text)[1], "step reminder in 10:00");
check("...with the clock still on: the switch is untouched",
      [chip.kids.map((k) => k.text)[0],
       chip.classList.contains("timer-paused"), ctx.hold().on],
      ["⏸", false, true]);
check("...and the skip is still on offer, because the next one is still coming",
      ctx.skip().state, "counting");

/* One press at a time, for the switch's reason: two presses on a slow daemon
   are two writes, and the second is a skip nobody asked for. */
posts.length = 0;
holdPost = true;
ctx.setWorld("s19", [mine]);
ctx.render();
skipBtn.fire("click");
check("the skip is disabled while the press is in flight", skipBtn.disabled, true);
skipBtn.fire("click");
check("...so a double-click skips one reminder, not two", posts.length, 1);
if (typeof holdPost === "function") holdPost();
holdPost = null;
await settle();
check("...and it takes presses again once the write lands",
      skipBtn.disabled, false);

/* A press with nothing to skip writes nothing. The button is down in those
   states, so this is the guard behind it rather than the visible path — but
   the node keeps its listener across repaints, and a hidden button that
   still writes is a hidden button that writes. */
posts.length = 0;
ctx.setWorld("s19", [paused]);
ctx.render();
check("a paused reminder offers no skip", ctx.skip(), null);
skipBtn.fire("click");
await settle();
check("...and pressing it anyway writes nothing", posts.length, 0);

/* ---- the skip's hover text ------------------------------------------- */
/* The one thing a reader cannot get from the icon: what it does NOT do.
   Two controls one row apart, one of which leaves a state behind. */
ctx.setWorld("s19", [mine]);
ctx.render();
check("it says it skips this one",
      /SKIP this one step reminder/.test(skipBtn.title), true);
check("...that the clock survives it, and when the next one is due",
      /clock stays on, and the next one is due in 10:00/.test(skipBtn.title),
      true);
check("...and that nothing is stored — the difference from the switch",
      /Nothing is stored/.test(skipBtn.title), true);
ctx.setWorld("s19", [heldRun]);
ctx.render();
check("a held reminder is described as the debt it is, not as a countdown",
      /retried every poll/.test(skipBtn.title), true);

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
      /renderTermTimer\(\);[^\n]*\n\s*if \(currentPage === "home"\) renderHome\(\);/
        .test(src), true);
/* setStatusBadge is the one point every attach path passes through
   (freshAttach and restoreTerminal both seed the header with it), which is
   what keeps the last session's clock off this session's name. */
const badge = slice("setStatusBadge");
check("every attach path reseeds it", /renderTermTimer\(\)/.test(badge), true);
check("the one interval ages the chip",
      /setInterval\(\(\) => \{ paintTermTimer\(\); \}, 1000\)/.test(src), true);

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
const skipAt = html.indexOf('id="term-timer-skip"');
check("the skip ships with it", skipAt >= 0, true);
check("...as its own button, because one cannot nest inside the other",
      skipAt > chipAt, true);
check("...still inside the header, before the actions",
      skipAt > headerAt && skipAt < actionsAt, true);
check("...and it starts hidden too: there is usually nothing to skip",
      /id="term-timer-skip"[^>]*class="[^"]*hidden"/.test(html), true);
/* One repaint paints both, which is what puts the skip on the same 1s
   interval as the countdown it sits against. A skip painted only by the 2s
   poll would linger for two seconds on a run that has just gone quiet. */
check("the chip's own repaint paints it",
      /paintTermTimerSkip\(line \? termTimerSkip\(\) : null\)/
        .test(slice("paintTermTimer")), true);

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
/* The skip is the one thing on this row that must NOT give way when the
   header is tight: the chip clips because its words degrade gracefully, and
   an icon has nothing to clip to. */
check("the skip keeps its width while the chip clips",
      /\.timer-skip \{[^}]*flex:\s*none/.test(rules), true);
check("it borrows the chip's two loud states so it is findable at a glance",
      ["due", "held"].every(
        (st) => new RegExp(`\.timer-skip\.${st} \{[^}]*color:`).test(rules)),
      true);
check("...and not the common one, for the chip's reason",
      /\.timer-skip\.counting/.test(rules), false);
/* It borrows neither paused dress, and that is the point of the pairing:
   this button leaves nothing behind for a dress to report. */
check("nothing dresses a skip as a state somebody left behind",
      /\.timer-skip\.timer-paused/.test(rules), false);
check("...and it says when a press is in flight, like both chips beside it",
      /\.timer-skip:disabled \{[^}]*cursor:\s*wait/.test(rules), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("termtimer_check: ok");

}   // main
