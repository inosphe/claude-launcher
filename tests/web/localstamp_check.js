/* Detail-panel timestamps in the reader's own zone (claunch-ox5qs).

   The daemon records every stamp in UTC and the panel used to print it as
   written, so a reader at UTC+9 had to add nine hours to every line of the
   session input journal and the meta rows. `localStamp` moves a stamp into
   the browser's zone; these checks pin the zone to Asia/Seoul (UTC+9, the
   machine the report came from) and drive the real function and the real
   journal row sliced out of the shipped app.js. */
process.env.TZ = "Asia/Seoul";
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
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

let failures = 0;
function check(what, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) return;
  failures += 1;
  console.log(`FAIL ${what}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
}

function el(tag, cls, text) {
  return { tag, className: cls || "", textContent: text === undefined ? "" : String(text),
           title: "", children: [], appendChild(c) { this.children.push(c); return c; } };
}

const ctx = {};
new Function("exports", "el", "api",
  slice("localStamp") +
  "const SESS_INPUT_STATUS_TITLE = {};\n" +
  slice("sessInputJournalRow") +
  "Object.assign(exports, { localStamp, row: sessInputJournalRow });"
)(ctx, el, () => Promise.resolve({}));
const { localStamp } = ctx;

/* ---- every shape the daemon writes ---- */
check("Z suffix (session_input._now)",
      localStamp("2026-10-14T09:41:02.653581Z"), "2026-10-14 18:41:02");
check("+00:00 suffix (manager._now_iso)",
      localStamp("2026-10-14T09:41:02+00:00"), "2026-10-14 18:41:02");
check("no offset is read as UTC, the daemon's convention",
      localStamp("2026-10-14T09:41:02"), "2026-10-14 18:41:02");
check("minutes only",
      localStamp("2026-10-14T09:41Z"), "2026-10-14 18:41:00");
check("a stamp already carrying an offset keeps its instant",
      localStamp("2026-10-14T18:41:02+09:00"), "2026-10-14 18:41:02");
check("crossing midnight moves the date too",
      localStamp("2026-12-31T20:00:00Z"), "2027-01-01 05:00:00");
check("a space instead of T",
      localStamp("2026-10-14 09:41:02+00:00"), "2026-10-14 18:41:02");

/* ---- what is not a date-time comes back as written ---- */
check("empty", localStamp(""), "");
check("missing", localStamp(undefined), "");
check("a bare date is not shifted", localStamp("2026-10-14"), "2026-10-14");
check("a word", localStamp("?"), "?");

/* ---- the Session input journal row ---- */
const row = ctx.row(null, "s1",
  { at: "2026-10-14T09:41:02.1Z", status: "sent", text: "hello" });
check("the journal row prints the stamp in the reader's zone",
      row.children[0].textContent, "2026-10-14 18:41:02  ");

if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log("localstamp_check ok");
