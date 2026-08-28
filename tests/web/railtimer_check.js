/* The nudge countdown's shared clock vocabulary, run against the real
   functions from app.js — and the guard that the rail carries no timer
   strip of its own.

   The countdown exists once, as a chip in the session's own header
   (#term-timer, rendered and wired in termtimer_check.js): the rail's strip
   was its duplicate and is gone. What stays is the machinery both faces
   shared — the pick that ranks the two clocks (the cflow reminder, which
   types into a session that is WORKING, and the stall ping, which wakes one
   that STOPPED, at most one of them live at any moment), the line that
   words the countdown and each of its silences, and the formatting that
   keeps seconds visible under a minute. All of it now serves the chip, and
   this file tests it directly while termtimer_check tests it through the
   chip's rendering.

   What has to hold here:

   * the clock the countdown speaks for is the one a reader needs, not the
     first in the object — something about to fire outranks something
     counting, which outranks a clock that is armed and deliberately quiet;
   * the countdown keeps running between the 2s polls, and CANNOT run past
     zero into negative time — the state is recomputed from the aged number
     rather than trusted, so "counting, 3s" becomes "due" four seconds later;
   * the several ways a clock can be silent stay told apart in words: off,
     not running, held by a session that stopped, and paused because the run
     is on a gate the clock is not allowed to touch. Collapsing any of those
     into "off" sends somebody to switch on a thing that is already on;
   * a run the daemon published no timers for (an older daemon) draws nothing
     rather than a zero;
   * and the shipped page ships that countdown exactly once — on the
     session's header, never on the rail. */
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
/* The wrapper's parameters mirror app.js's globals, though the vocabulary
   under test here reads none of them — it sees only the run object it is
   handed. The signature is kept so the slices stay drop-in-faithful. */
new Function(
  "exports", "currentName", "cflowCache",
  [table("RAIL_TIMER_RANK"), table("RAIL_TIMER_GLYPH"),
   slice("fmtCountdown"), slice("timerClockName"), slice("railTimerPick"), slice("railTimerTitle"),
   slice("railTimerLine")].join("\n") + `
exports.fmtCountdown = fmtCountdown;
exports.pick = railTimerPick;
exports.line = railTimerLine;
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
      ctx.line(ctx.pick(timers(REM(), PING()))).text, "cflow reminder in 6:00");

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
      "cflow reminder in 1:00");
check("...and past its deadline it becomes due, not a negative countdown",
      [ctx.line(counting, 70).state, ctx.line(counting, 70).text],
      ["due", "cflow reminder due now"]);
check("a state that was already due stays due however long it ages",
      ctx.line(ctx.pick(timers(REM({ state: "due", due_in: -4 }), PING())), 30).text,
      "cflow reminder due now");

/* ---- the several silences, told apart -------------------------------- */
const say = (rem) => ctx.line(ctx.pick(timers(REM(rem), PING()))).text;
check("switched off says so", say({ enabled: false, state: "off", due_in: null }),
      "cflow reminder off");
check("a tick that is not running is NOT the same as switched off",
      say({ running: false, state: "stopped", due_in: null }),
      "clock not running");
check("held names the reason it is silent, which is not the configuration",
      say({ state: "held", due_in: -12 }),
      "cflow reminder held — session stopped");
check("a gate is the protocol's silence, not the settings'",
      say({ state: "blocked", due_in: null }),
      "cflow reminder paused — not this run's move");
check("armed but not yet seen by a tick", say({ state: "arming", due_in: null }),
      "cflow reminder arming");
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
      "cflow reminder · counting · every 10:00 · last fired 4:00 ago");
check("...and the other clock, so 'why not the ping' is answerable here",
      title.split("\n")[2], "stall ping · waiting · every 15:00");

/* ---- which size the next fire will be -------------------------------- */
/* The reminder pastes the whole step the first time it speaks at a position
   and a short pointer after that, so the reader deciding whether to let it
   speak is choosing between two sizes, not just a moment. The block above
   builds its title without a `form`, and every check in it passes whether or
   not the size is reported -- so the two branches need a case each. That gap
   is the point: this harness already sliced railTimerTitle, which says it
   watches the function and not that it watches the change. */
const formLine = (form) => ctx.line(ctx.pick(timers(
  REM({ state: "counting", due_in: 360, fired_ago: 240, form }),
  PING({ enabled: true, state: "waiting", due_in: 900 })))).title.split("\n")[1];
check("a reminder that has not spoken at this position says the next fire is the whole step",
      formLine("full"),
      "cflow reminder · counting · every 10:00 · last fired 4:00 ago · next: full restatement");
check("...and one that already has says the next is the short form",
      formLine("short"),
      "cflow reminder · counting · every 10:00 · last fired 4:00 ago · next: short form");

/* ---- no timers, no countdown ------------------------------------------ */
/* An older daemon publishes no `timers` at all. Drawing a zero there would
   be inventing a countdown; nothing must be drawn instead. */
check("a run with no timers yields no pick",
      ctx.pick({ scope: "s19", cwd: "F:/repo", status: "step" }), null);
check("...and no pick yields no line", ctx.line(null), null);
check("an empty timers object is not a clock either",
      ctx.pick({ scope: "s19", cwd: "F:/repo", timers: {} }), null);

/* ---- the rail carries no timer strip --------------------------------- */
/* The countdown's one home is the header chip (#term-timer); the rail's
   strip was the duplicate this task removed. The guard is the absence —
   every check above would pass just as well if the countdown were rendered
   into a strip nobody asked about. */
const rules = css.replace(/\/\*[\s\S]*?\*\//g, "");
check("no timer strip ships in the rail's markup",
      html.indexOf('id="rail-timer"') < 0, true);
check("...nor a rail-timer rule in the stylesheet",
      /#rail-timer/.test(rules), false);
check("...nor a renderer for one in the script",
      /function renderRailTimer\(\)/.test(src), false);
/* and the countdown is still on the page once, on the session's header —
   the monotony that makes "duplicate" a meaningful verdict. */
check("the countdown lives once, on the session's header",
      /id="term-timer"/.test(html), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railtimer_check: ok");
