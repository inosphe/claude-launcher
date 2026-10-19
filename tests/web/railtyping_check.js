/* Typing in a text box holds the rail until the keys go quiet
   (claunch-oo8au) — run against the real railHeld / railTyping /
   noteRailTyping from app.js with a hand-driven clock.

   A poll that changes a session redraws its rail rows, and on a large rail
   that redraw (its script and the Commit after it) blocked the main thread
   for over half a second every few seconds while a thread was being typed
   into: the keys pressed in that window waited for it. What has to hold: a
   key in a text box holds the rail; a key anywhere else does not; the hold
   ends on its own once the keys stop, and the redraw it owed runs then; and
   a burst that never pauses stops holding at its ceiling, so the rail is not
   frozen for as long as someone keeps typing. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.RAILTYPING_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
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
function line(re, what) {
  const m = src.match(re);
  if (!m) throw new Error(`cannot locate ${what} in app.js`);
  return m[0] + "\n";
}

let now = 1_000_000;
const timers = [];
const fakeSetTimeout = (fn, ms) => { timers.push({ fn, at: now + ms }); return timers.length; };
const fakeClearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].fn = null; };
function advance(ms) {
  now += ms;
  for (const t of timers) {
    if (t.fn && t.at <= now) { const fn = t.fn; t.fn = null; fn(); }
  }
}
let redraws = 0;
let lastRedraw;

const ctx = {};
new Function(
  "exports", "Date", "setTimeout", "clearTimeout", "refreshSessions",
  line(/^const RAIL_HOLD_MS = .+$/m, "RAIL_HOLD_MS")
  + line(/^let railHeldUntil = .+$/m, "railHeldUntil")
  + line(/^let railRedrawPending = .+$/m, "railRedrawPending")
  + line(/^const RAIL_TYPING_QUIET_MS = .+$/m, "RAIL_TYPING_QUIET_MS")
  + line(/^const RAIL_TYPING_MAX_MS = .+$/m, "RAIL_TYPING_MAX_MS")
  + line(/^let railTypedAt = .+$/m, "railTypedAt")
  + line(/^let railTypingSince = .+$/m, "railTypingSince")
  + line(/^let railTypingTimer = .+$/m, "railTypingTimer")
  + line(/^let railSeenPending = .+$/m, "railSeenPending")
  + line(/^const RAIL_TEXT_INPUT_TYPES = [\s\S]+?\);$/m, "RAIL_TEXT_INPUT_TYPES")
  + slice("railHeld") + slice("railTextTarget") + slice("railTyping")
  + slice("noteRailTyping")
  + `
Object.assign(exports, {
  held: () => railHeld(),
  key: noteRailTyping,
  owe: (v) => { railRedrawPending = v; },
  oweSeen: (v) => { railSeenPending = v; },
  quiet: RAIL_TYPING_QUIET_MS,
  max: RAIL_TYPING_MAX_MS,
});`)(ctx, { now: () => now }, fakeSetTimeout, fakeClearTimeout,
       (opts) => { redraws++; lastRedraw = opts; });

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
const textarea = { target: { tagName: "TEXTAREA" } };
const input = { target: { tagName: "INPUT" } };
const editable = { target: { tagName: "DIV", isContentEditable: true } };
const button = { target: { tagName: "BUTTON" } };

check("nothing typed yet holds nothing", ctx.held(), false);

ctx.key(button);
check("a key on a button does not hold the rail", ctx.held(), false);

ctx.key(textarea);
check("a key in a textarea holds the rail", ctx.held(), true);
ctx.owe(true);
advance(ctx.quiet - 100);
check("...while the keys keep coming", ctx.held(), true);
check("...and owes its redraw until they stop", redraws, 0);
advance(200);
check("the hold ends once the keys go quiet", ctx.held(), false);
check("...and the redraw it owed runs then", redraws, 1);
check("...from the cache, with no request of its own", lastRedraw, { cached: true });

ctx.owe(false);
ctx.key(input);
check("a key in an input holds the rail", ctx.held(), true);
advance(ctx.quiet + 1);
check("a quiet end with nothing owed redraws nothing", redraws, 1);

/* A checkbox, radio or button input takes keys without being typed into
   (claunch-4thlk.7); only the text kinds hold. */
for (const type of ["checkbox", "radio", "button", "submit", "range", "color", "file"]) {
  ctx.key({ target: { tagName: "INPUT", type } });
  check(`a key on an input of type ${type} does not hold the rail`, ctx.held(), false);
}
for (const type of ["text", "search", "email", "url", "tel", "password", "number", "TEXT"]) {
  ctx.key({ target: { tagName: "INPUT", type } });
  check(`a key in an input of type ${type} holds the rail`, ctx.held(), true);
  advance(ctx.quiet + 1);
}

/* The seen lines a held poll skipped are owed to the pause, even when no
   row changed: without that they waited for the next poll that changed. */
ctx.owe(false);
ctx.oweSeen(true);
const before = redraws;
ctx.key(textarea);
advance(ctx.quiet + 1);
check("a pause moves on the seen lines the burst held", redraws, before + 1);
check("...from the cache", lastRedraw, { cached: true });
ctx.oweSeen(false);

ctx.key(editable);
check("a key in an editable element holds the rail", ctx.held(), true);
advance(ctx.quiet + 1);

/* A burst with no pause: one key every half second, past the ceiling. */
ctx.owe(true);
let heldAt = [];
for (let t = 0; t <= ctx.max + 2000; t += 500) {
  ctx.key(textarea);
  heldAt.push([t, ctx.held()]);
  advance(500);
}
check("a burst holds the rail up to its ceiling",
      heldAt.filter(([t]) => t < ctx.max).every(([, h]) => h), true);
check("...and not past it, though the keys go on",
      heldAt.filter(([t]) => t >= ctx.max).every(([, h]) => !h), true);
advance(ctx.quiet + 1);
ctx.key(textarea);
check("a pause starts a new burst that holds again", ctx.held(), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railtyping_check: ok");
