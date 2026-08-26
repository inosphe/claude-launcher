/* The nudge countdown above the rail's nav, run against the real functions
   from app.js.

   The strip answers two questions a dashboard could not answer before: is the
   daemon's automatic nudge going to happen, and how long is left of it. Two
   clocks can produce it (the step reminder, which types into a session that
   is WORKING, and the stall ping, which wakes one that STOPPED), and the
   whole difficulty is that at any moment at most one of them applies. Showing
   only the reminder reports "off" precisely when the ping is the live clock —
   a confident wrong answer, which is worse than no strip at all.

   What has to hold here:

   * the clock the strip speaks for is the one a reader needs, not the first
     in the object — something about to fire outranks something counting,
     which outranks a clock that is armed and deliberately quiet;
   * the countdown keeps running between the 2s polls, and CANNOT run past
     zero into negative time — the state is recomputed from the aged number
     rather than trusted, so "counting, 3s" becomes "due" four seconds later;
   * the several ways a clock can be silent stay told apart in words: off, not
     running, held by a session that stopped, and paused because the run is on
     a gate the clock is not allowed to touch. Collapsing any of those into
     "off" sends somebody to switch on a thing that is already on;
   * a run the daemon published no timers for (an older daemon) draws nothing
     rather than a zero;
   * the strip sits ABOVE the nav in the shipped markup, and the common case
     (a clock quietly counting) takes no colour — the rail already has one
     thing that speaks in colour and it is the session's liveness dot. */
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

/* The two lookup tables are consts, not functions, so they need their own
   cuts — and they are the tables under test (the ranking IS the pick). */
function table(name) {
  const start = src.indexOf(`const ${name} = {`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  return src.slice(start, src.indexOf("};", start) + 2);
}

const ctx = {};
/* `currentName` and `cflowCache` are app.js globals; here they are parameters
   of the wrapper, so the sliced functions and the setters share one binding. */
new Function(
  "exports", "currentName", "cflowCache",
  [table("RAIL_TIMER_RANK"), table("RAIL_TIMER_GLYPH"),
   slice("fmtCountdown"), slice("railTimerPick"), slice("railTimerTitle"),
   slice("railTimerLine"), slice("sessCflowRun"), slice("railTimerRun")].join("\n") + `
exports.fmtCountdown = fmtCountdown;
exports.pick = railTimerPick;
exports.line = railTimerLine;
exports.run = railTimerRun;
exports.setWorld = (name, runs) => { currentName = name; cflowCache = runs; };
`)(ctx, null, []);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- the countdown format ------------------------------------------- */
/* fmtAge would say "4m" for all of these. The whole point of this strip is
   watching the last minute run out, so seconds never disappear. */
check("seconds keep their place under a minute", ctx.fmtCountdown(7), "0:07");
check("minutes and seconds", ctx.fmtCountdown(252), "4:12");
check("hours pad the minutes", ctx.fmtCountdown(3852), "1:04:12");
check("a passed deadline floors at zero rather than going negative",
      ctx.fmtCountdown(-9), "0:00");
check("nothing at all is still a clock, not a blank", ctx.fmtCountdown(null), "0:00");

/* ---- which clock the strip speaks for -------------------------------- */
const timers = (rem, ping) => ({
  scope: "s19", cwd: "F:/repo", workflow: "ship", status: "step",
  sessions: ["s19"], timers: { reminder: rem, ping: ping },
});
const REM = (over) => Object.assign(
  { running: true, enabled: true, interval: 600, due_in: 360,
    fired_ago: null, state: "counting" }, over);
const PING = (over) => Object.assign(
  { running: true, enabled: false, interval: 900, due_in: null,
    fired_ago: null, state: "off" }, over);

check("a counting reminder beside a switched-off ping speaks for the reminder",
      ctx.pick(timers(REM(), PING())).clock, "reminder");
check("...and says how long is left",
      ctx.line(ctx.pick(timers(REM(), PING()))).text, "step reminder in 6:00");

/* The reminder is held because the session stopped — which is exactly when
   the ping is the clock that will actually speak. Ranking by need, not by
   key order, is what stops the strip reporting the quiet one. */
check("a counting ping outranks a reminder held by a stopped session",
      ctx.pick(timers(REM({ state: "held", due_in: -300 }),
                      PING({ enabled: true, state: "counting", due_in: 120 }))).clock,
      "ping");
check("...and names it in the reader's words",
      ctx.line(ctx.pick(timers(REM({ state: "held", due_in: -300 }),
                               PING({ enabled: true, state: "counting", due_in: 120 })))).text,
      "stall ping in 2:00");
check("a due clock outranks a counting one whatever the numbers say",
      ctx.pick(timers(REM({ state: "counting", due_in: 5 }),
                      PING({ enabled: true, state: "due", due_in: -1 }))).clock,
      "ping");
/* Two clocks in the same state: the one that speaks sooner. */
check("a tie goes to whichever fires first",
      ctx.pick(timers(REM({ due_in: 400 }),
                      PING({ enabled: true, state: "counting", due_in: 90 }))).clock,
      "ping");
/* ...and a missing deadline must never win that race — an unknown is not a
   zero, and sorting it first would put the least informative clock on screen. */
check("an unknown deadline sorts last, not first",
      ctx.pick(timers(REM({ due_in: null }),
                      PING({ enabled: true, state: "counting", due_in: 500 }))).clock,
      "ping");

/* ---- the countdown between polls ------------------------------------- */
/* The strip repaints every second off a reading taken every two. That ageing
   is the only moving part, and it must not be able to produce a negative
   clock or a stale word. */
const counting = ctx.pick(timers(REM({ due_in: 65 }), PING()));
check("the reading ages between polls", ctx.line(counting, 5).text,
      "step reminder in 1:00");
check("...and past its deadline it becomes due, not a negative countdown",
      [ctx.line(counting, 70).state, ctx.line(counting, 70).text],
      ["due", "step reminder due now"]);
check("a state that was already due stays due however long it ages",
      ctx.line(ctx.pick(timers(REM({ state: "due", due_in: -4 }), PING())), 30).text,
      "step reminder due now");

/* ---- the several silences, told apart -------------------------------- */
const say = (rem) => ctx.line(ctx.pick(timers(REM(rem), PING()))).text;
check("switched off says so", say({ enabled: false, state: "off", due_in: null }),
      "step reminder off");
check("a tick that is not running is NOT the same as switched off",
      say({ running: false, state: "stopped", due_in: null }),
      "clock not running");
check("held names the reason it is silent, which is not the configuration",
      say({ state: "held", due_in: -12 }),
      "step reminder held — session stopped");
check("a gate is the protocol's silence, not the settings'",
      say({ state: "blocked", due_in: null }),
      "step reminder paused — not this run's move");
check("armed but not yet seen by a tick", say({ state: "arming", due_in: null }),
      "step reminder arming");
check("the signal-only run says what it is watching",
      say({ state: "watching", interval: 0, due_in: null,
            awaits: "the sweep to go green" }),
      "watching: the sweep to go green");
check("a ping armed behind a working session is not 'off'",
      ctx.line(ctx.pick(timers(REM({ enabled: false, state: "off", due_in: null }),
                               PING({ enabled: true, state: "waiting", due_in: 900 })))).text,
      "stall ping armed — session working");

/* Each state gets its own glyph, so the strip is readable with the colour
   stripped — which is how it renders in the common (counting) case. */
const glyphs = ["due", "counting", "held", "waiting", "watching", "arming",
                "blocked", "off", "stopped"]
  .map((state) => ctx.line(ctx.pick(timers(REM({ state, due_in: null }), PING()))).glyph);
check("no state is left without a glyph", glyphs.filter(Boolean).length, 9);

/* ---- the hover text answers about the clock it is NOT reporting ------ */
const title = ctx.line(ctx.pick(timers(
  REM({ state: "counting", due_in: 360, fired_ago: 240 }),
  PING({ enabled: true, state: "waiting", due_in: 900 })))).title;
check("the title names the run", title.split("\n")[0], "ship · s19");
check("...the clock it is reporting, with its cadence and last firing",
      title.split("\n")[1],
      "step reminder · counting · every 10:00 · last fired 4:00 ago");
check("...and the other clock, so 'why not the ping' is answerable here",
      title.split("\n")[2], "stall ping · waiting · every 15:00");

/* ---- no timers, no strip -------------------------------------------- */
/* An older daemon publishes no `timers` at all. Drawing a zero there would be
   inventing a countdown; the strip has to disappear instead. */
check("a run with no timers yields no pick",
      ctx.pick({ scope: "s19", cwd: "F:/repo", status: "step" }), null);
check("...and no pick yields no line", ctx.line(null), null);
check("an empty timers object is not a clock either",
      ctx.pick({ scope: "s19", cwd: "F:/repo", timers: {} }), null);

/* ---- which run the strip is about ------------------------------------ */
/* The attached session's, when it drives one. */
const attached = timers(REM(), PING());
const other = Object.assign(timers(REM({ due_in: 30 }), PING()),
                            { scope: "s20", cwd: "F:/other" });
ctx.setWorld("s19", [other, attached]);
check("the attached session's run wins even when another fires sooner",
      ctx.run().scope, "s19");
/* Nothing attached: the run closest to being spoken to, so the strip is
   still answering a real question rather than going blank. */
ctx.setWorld(null, [attached, other]);
check("with nothing attached, the soonest run is shown", ctx.run().scope, "s20");
/* A run that is not counting is not a candidate for that fallback — a fleet
   of switched-off clocks must not put an arbitrary one on the rail. */
ctx.setWorld(null, [timers(REM({ enabled: false, state: "off", due_in: null }),
                           PING())]);
check("switched-off runs are not fallback candidates", ctx.run(), null);
ctx.setWorld(null, []);
check("no runs at all, no strip", ctx.run(), null);
/* The attached session drives a run the daemon published no timers for: fall
   through to the fleet rather than showing an empty strip for it. */
ctx.setWorld("s19", [Object.assign(timers(REM(), PING()), { timers: undefined }),
                     other]);
check("an attached run without timers falls through to the fleet",
      ctx.run().scope, "s20");

/* ---- the shipped page and stylesheet -------------------------------- */
/* The placement is the request: above the nav, in the rail. Held against the
   markup because every check above would pass just as well with the strip
   rendered into a page nobody has open. */
const railTimerAt = html.indexOf('id="rail-timer"');
const railNavAt = html.indexOf('id="rail-nav"');
const sessListAt = html.indexOf('id="session-list"');
check("the strip exists in the shipped markup", railTimerAt >= 0, true);
check("...above the nav", railTimerAt < railNavAt, true);
check("...and below the session list it belongs to",
      railTimerAt > sessListAt, true);
check("it starts hidden — an empty strip must not reserve a row",
      /id="rail-timer"\s+class="hidden"/.test(html), true);

/* The stylesheet's half: the common case stays the rail's own grey. A
   `counting` colour would put a second permanently-lit thing on a rail whose
   only colour is the session's liveness dot. */
const rules = css.replace(/\/\*[\s\S]*?\*\//g, "");
check("counting takes no colour of its own", /#rail-timer\.counting/.test(rules),
      false);
check("but due, held and a dead tick do",
      ["due", "held", "stopped"].every(
        (s) => new RegExp(`#rail-timer\\.${s} \\{[^}]*color:`).test(rules)),
      true);
check("the strip is one row and clips rather than shoving the nav down",
      /#rail-timer \{[^}]*white-space:\s*nowrap/.test(rules) &&
      /#rail-timer \{[^}]*overflow:\s*hidden/.test(rules), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railtimer_check: ok");
