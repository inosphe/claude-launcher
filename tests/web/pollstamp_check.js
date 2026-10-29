/* What one poll costs the rail's redraw check.

   `refreshSessions` compares a signature of the whole session cache to decide
   whether to redraw, and that signature used to be a `JSON.stringify` of the
   cache itself -- every poll re-serialised every row (6.0/4.7 ms of the main
   thread in Trace-20261021T194912, claunch-hwbuc.2).

   A delta poll hands back the rows the daemon called unchanged re-parsed from
   the text the page kept, so that text is the row's identity: the stamp
   computed for a text is the answer for every row that text produces. Held
   here, against the real `sessionRowsTake` and `sessionRowStamp`:

   - a poll that only re-parses unchanged rows stringifies nothing, and
   - stamping every row of such a poll stringifies nothing either, because
     the memo already holds that text,
   - a poll with one changed row pays for that row and not the fleet,
   - and the stamp still carries the row, filtered exactly as before (the
     volatile keys dropped), so what counts as a redraw does not move.

   The re-parse itself was the other half of the poll (2.1 ms, claunch-hwbuc.3):
   a held text is parsed once, every later poll gets a fresh shallow copy of
   that parse, and a caller editing its row does not reach the copy the next
   poll is built from.

   The last property is the one that keeps this from being a speed-up that
   changes what the page does: it compares the stamp against
   `JSON.stringify(parsedRow, unstamped)`. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.POLLSTAMP_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .+$`, "m"));
  if (!m) throw new Error(`cannot locate ${name} in app.js`);
  return m[0] + "\n";
}

const api = {};
new Function(
  "exports",
  constLine("sessionRowsKept")
  + slice("takeParsed") + slice("keepRowText") + slice("sessionRowStamp") + slice("sessionRowsTake")
  + slice("statusCheckDefsStamp")
  + `
Object.assign(exports, {
  take: sessionRowsTake,
  stamp: sessionRowStamp,
  parsed: takeParsed,
  defsStamp: statusCheckDefsStamp,
});`)(api);

/* The filter the redraw signature uses, as refreshSessions writes it. Kept
   here as a copy only so the harness can ask what the stamp should equal; the
   production filter lives in refreshSessions and is exercised by the rail
   harnesses. */
function unstamped(key, value) {
  return (
    key === "due_in" || key === "fired_ago" ||
    key === "due_at" || key === "fired_at" ||
    key === "last_visited_at" || key === "last_input_at" ||
    key === "last_activity_at" || key === "last_output_at" ||
    key === "viewers" || key === "moved_rows" || key === "tool_calls" ||
    key === "age_s" ||
    (key === "live" && value && typeof value === "object" && "bin_s" in value)
  ) ? undefined : value;
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const ROWS = Number(process.env.POLLSTAMP_ROWS || 40);
const fleet = (tokens, age) => Array.from({ length: ROWS }, (_, i) => ({
  name: `s${i}`, status: "busy", harness: "claude", project: "default",
  age_s: age + i, viewers: 1,
  context: { tokens: tokens + i, model: "opus" },
}));

function countStringify(fn) {
  const real = JSON.stringify;
  let calls = 0;
  JSON.stringify = function (...a) { calls++; return real.apply(this, a); };
  try { const value = fn(); return { calls, value }; }
  finally { JSON.stringify = real; }
}

/* A poll costs two things: its row text (`JSON.stringify`) and the re-parse of
   the rows it did not change (`JSON.parse`). The second is what
   claunch-hwbuc.3 memoises, so a poll counts both. */
function countWork(fn) {
  const str = JSON.stringify, parse = JSON.parse;
  let strings = 0, parses = 0;
  JSON.stringify = function (...a) { strings++; return str.apply(this, a); };
  JSON.parse = function (...a) { parses++; return parse.apply(this, a); };
  try { const value = fn(); return { strings, parses, value }; }
  finally { JSON.stringify = str; JSON.parse = parse; }
}

const state = "s1";
const first = fleet(154_706, 0);
const taken1 = countStringify(() => api.take(state, {
  delta: true, names: first.map((r) => r.name), sessions: first,
  row_hashes: Object.fromEntries(first.map((r) => [r.name, `h1-${r.name}`])),
}));
check("the first poll takes every row", taken1.value.length, ROWS);
check("...and writes each row's text once", taken1.calls, ROWS);

/* The signature pass over that fleet: each distinct text is stamped once. */
const stamped1 = countStringify(() => taken1.value.map((r) => api.stamp(r, unstamped)));
const sig1 = JSON.stringify(stamped1.value);

/* The next poll calls every row unchanged: the page re-parses them from the
   kept text and nothing new is serialised. */
const answer = () => ({
  delta: true, names: first.map((r) => r.name), sessions: [],
  row_hashes: Object.fromEntries(first.map((r) => [r.name, `h1-${r.name}`])),
});
const second = countWork(() => api.take(state, answer()));
check("a poll that re-parses unchanged rows stringifies nothing", second.strings, 0);
check("...and parses each held text for the first time", second.parses, ROWS);
check("...and still returns every row", second.value.length, ROWS);

/* The poll after that: the same texts again, so the memo answers all of them.
   Without the memo this is one parse per row per poll, forever. */
const repeat = countWork(() => api.take(state, answer()));
check("a repeated unchanged poll parses nothing", repeat.parses, 0);
check("...and returns the same rows", JSON.stringify(repeat.value.map((r) => r.name)),
      JSON.stringify(second.value.map((r) => r.name)));

/* The rows are the caller's: a page that writes to one (sessionRowsJoin
   replaces status_checks) must not write into the memo the next poll reads. */
repeat.value[0].status = "edited-by-the-page";
const afterEdit = countWork(() => api.take(state, answer()));
check("a caller's edit does not reach the next poll", afterEdit.value[0].status, "busy");
check("...and that poll parses nothing either", afterEdit.parses, 0);
const stamped2 = countStringify(() => second.value.map((r) => api.stamp(r, unstamped)));
check("...and its redraw signature stringifies nothing", stamped2.calls, 0);
check("...and is the same signature as before", JSON.stringify(stamped2.value), sig1);

/* One row really moved. */
const one = fleet(154_706, 0).map((r, i) => (
  i === 0 ? { ...r, context: { ...r.context, tokens: 999_999 } } : r));
const third = countWork(() => api.take(state, {
  delta: true, names: one.map((r) => r.name), sessions: [one[0]],
  row_hashes: { s0: "h1-s0-2" },
}));
check("a poll with one changed row stringifies that row only", third.strings, 1);
/* The changed row arrives already parsed and the other 39 were parsed once
   above, so this poll does no parsing at all. */
check("...and parses nothing (the held rows are memoised)", third.parses, 0);
const stamped3 = countStringify(() => third.value.map((r) => api.stamp(r, unstamped)));
check("...and its signature pass stamps that row only", stamped3.calls, 1);
check("...and the signature moved", JSON.stringify(stamped3.value) === sig1, false);

/* The stamp is the whole-row serialisation under the same filter. */
const whole = JSON.stringify(JSON.parse(JSON.stringify(one[0])), unstamped);
check("a stamp equals the filtered whole-row serialisation",
      api.stamp(one[0], unstamped), whole);
check("...and drops the volatile keys",
      "age_s" in JSON.parse(api.stamp(one[0], unstamped)), false);

/* A volatile-only move is not a redraw. */
const volatile = { ...one[0], age_s: 900, viewers: 7 };
check("a volatile-only move stamps the same", api.stamp(volatile, unstamped), whole);

/* Both memos are bounded: they drop themselves at the cap rather than growing
   with every row text the page ever sees. A long-lived tab polls a fleet that
   churns, so this is the growth path a session never exercises on its own.
   The cap is read off the source so the harness follows the constant. */
const CAP = Number((src.match(/memo\.size >= (\d+)\) memo\.clear\(\)/) || [])[1]);
check("the source holds a memo cap", Number.isFinite(CAP), true);
const DISTINCT = CAP + 1000;
const distinct = (i) => JSON.stringify({
  name: `churn${i}`, status: "busy", context: { tokens: i, model: "opus" },
});
for (let i = 0; i < DISTINCT; i++) api.parsed(distinct(i));
check("the parse memo stays at or below its cap",
      api.parsed._byText.size <= CAP, true);
check("...and still answers after the drop",
      api.parsed(distinct(DISTINCT - 1)).name, `churn${DISTINCT - 1}`);

for (let i = 0; i < DISTINCT; i++) api.stamp({ _pollText: distinct(i) }, unstamped);
check("the stamp memo stays at or below its cap",
      api.stamp._byText.size <= CAP, true);
check("...and still answers after the drop",
      api.stamp({ _pollText: distinct(DISTINCT - 1) }, unstamped),
      JSON.stringify(JSON.parse(distinct(DISTINCT - 1)), unstamped));

/* A whole answer -- no delta, no digests (HTTP, or an older daemon) -- takes
   every row as it arrived. Those rows must carry their text too, or the
   signature has to serialise the whole fleet on the next poll. */
const wholeAnswer = fleet(154_706, 0);
const takenWhole = countWork(() => api.take("s2", { delta: false, sessions: wholeAnswer }));
check("a whole answer returns every row", takenWhole.value.length, ROWS);
check("...and writes each row's text once", takenWhole.strings, ROWS);
check("...and parses nothing (the rows arrive parsed)", takenWhole.parses, 0);
check("every row of a whole answer carries its text",
      takenWhole.value.every((r) => typeof r._pollText === "string"), true);
check("...equal to its own serialisation",
      takenWhole.value[0]._pollText, JSON.stringify(wholeAnswer[0]));
/* The churn loop above cleared the stamp memo, so this pass pays one
   serialisation per distinct text -- and the next pass pays none, which is
   the property that matters. */
const stampedWhole = countWork(() => takenWhole.value.map((r) => api.stamp(r, unstamped)));
check("...and its signature pass pays one serialisation per text",
      stampedWhole.strings, ROWS);
check("...stamping every row from that text", stampedWhole.value.length, ROWS);
const stampedWholeAgain = countWork(() => takenWhole.value.map((r) => api.stamp(r, unstamped)));
check("...and the pass after that serialises nothing", stampedWholeAgain.strings, 0);
check("...with the same signature", stampedWholeAgain.value, stampedWhole.value);

/* The checks' names and questions are not in any row: a poll that renames a
   check hands back the same row text, so the row stamps stand still and the
   signature has to carry the defs itself or the rail keeps the old names
   (claunch-hwbuc.2 review, s215-rev). */
const DEFS = { "check-c": { name: "C", question: "커밋 되었는가?" } };
const RENAMED = { "check-c": { name: "C", question: "커밋이 원격에 있는가?" } };
check("the defs stamp moves when a check is renamed",
      api.defsStamp(RENAMED) === api.defsStamp(DEFS), false);
check("...and holds still when they are the same",
      api.defsStamp({ ...DEFS }), api.defsStamp(DEFS));
check("a poll without defs stamps as empty", api.defsStamp(null), "");
check("refreshSessions puts the defs stamp in its signature",
      /typeof keyPoolSignature === "function" \? keyPoolSignature\(\) : null,[\s\S]{0,300}defsStamp\]/.test(src),
      true);
check("...and reads it from the poll's own defs",
      /pollDefs = data && data\.status_check_defs;/.test(src), true);
check("...which the signature reads rather than the response block",
      /statusCheckDefsStamp\(pollDefs\)/.test(src), true);

console.log(`pollstamp_check: ${ROWS} rows — first poll ${taken1.calls} stringify calls ` +
            `(stamps ${stamped1.calls} + ${stamped1.value.length}), an unchanged poll ` +
            `${second.strings} + ${stamped2.calls} (parses ${second.parses}, then ` +
            `${repeat.parses}, then ${afterEdit.parses}), a one-row poll ` +
            `${third.strings} + ${stamped3.calls} (parses ${third.parses})`);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("pollstamp_check: ok");
