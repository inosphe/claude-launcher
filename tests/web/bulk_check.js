/* The rail's bulk bar, run against the real syncBulkActions from app.js.

   Five buttons that act on the working fleet — stop, pause, resume paused,
   resume, archive. What has to hold is that
   each one is up exactly when it would do something and carries the count of
   what that is: a bar showing "stop 3" on a rail with nothing running is a
   button that lies about the fleet, and a bar that hides `resume` while there
   are exited sessions is a rail you cannot get back. The counts also split
   the two sides — running vs exited — and every status that is not "exited"
   counts as running, including `starting`, which is a session in the middle
   of coming up and very much something `stop` should reach. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

const a = src.indexOf("function syncBulkActions(");
if (a < 0) throw new Error("cannot locate syncBulkActions in app.js");
let depth = 0, end = -1;
for (let i = src.indexOf(") {", a) + 2; i < src.length; i++) {
  if (src[i] === "{") depth++;
  else if (src[i] === "}" && !--depth) { end = i + 1; break; }
}
if (end < 0) throw new Error("unbalanced syncBulkActions");

/* The stub DOM is the five buttons and nothing else — `hidden` is a class in
   this app, so that is what the check reads. In DOM order, which is the
   order the bar reads in. */
const IDS = ["stop-all", "pause-all", "resume-paused", "resume-all", "archive-exited"];
const buttons = {};
for (const id of IDS) {
  buttons[id] = {
    id, textContent: "", title: "", classes: new Set(["hidden"]),
    classList: {
      toggle(cls, on) {
        if (on) buttons[id].classes.add(cls); else buttons[id].classes.delete(cls);
      },
    },
  };
}
/* The bar itself, which the same function puts away when no button is up. */
const barEl = {
  classes: new Set(),
  classList: {
    toggle(cls, on) { if (on) barEl.classes.add(cls); else barEl.classes.delete(cls); },
  },
};
const ctx = {};
new Function("exports", "$", src.slice(a, end) +
             "\nexports.syncBulkActions = syncBulkActions;")(
  ctx, (id) => id === "bulk-actions" ? barEl : (buttons[id] || null)
);
const { syncBulkActions } = ctx;

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* What the bar reads as: the label of every button that is up, in order. */
function bar(sessions, filter = "current") {
  syncBulkActions(sessions, filter);
  return IDS.filter((id) => !buttons[id].classes.has("hidden"))
    .map((id) => buttons[id].textContent.replace(/^[^a-z]+/, ""));
}
const of = (...statuses) =>
  statuses.map((status, i) => ({ name: `s${i}`, status }));

check(
  "an empty rail offers nothing at all",
  bar([]),
  []
);

check(
  "a rail with only running sessions can be stopped or paused",
  bar(of("idle", "busy", "starting")),
  ["stop 3", "pause 3"]
);

check(
  "a rail with only exited sessions offers resume and archive",
  bar(of("exited", "exited")),
  ["resume 2", "archive 2 exited"]
);

check(
  "a mixed rail counts each working state separately",
  bar(of("idle", "exited", "busy", "exited", "exited")),
  ["stop 2", "pause 2", "resume 3", "archive 3 exited"]
);

check(
  "archived records are excluded from the working actions",
  bar([
    { name: "live", status: "idle" },
    { name: "old", status: "exited", archived_at: "2026-08-28T00:00:00Z" },
  ]),
  ["stop 1", "pause 1"]
);

/* A paused record is exited with the marker: it is counted by the paused
   resume and by nothing else — not by resume, not by archive — so no button
   on the bar can claim it twice, and "resume paused" cannot bring back a
   session somebody killed on purpose. */
const paused = [
  { name: "live", status: "idle" },
  { name: "held", status: "exited", paused_at: "2026-09-02T00:00:00Z" },
  { name: "dead", status: "exited" },
  { name: "gone", status: "exited", paused_at: "2026-09-02T00:00:00Z",
    archived_at: "2026-09-02T01:00:00Z" },
];
check("paused records get their own resume and leave the killed counts alone",
      bar(paused), ["stop 1", "pause 1", "resume 1 paused", "resume 1", "archive 1 exited"]);
check("paused mode exposes only the paused resume", bar(paused, "paused"),
      ["resume 1 paused"]);
// The bar is gated on every partition, not on running plus killed: in the
// Paused view those two are 0 by construction, and a bar hidden on their
// sum took the paused resume button down with it.
check("and the bar itself stays up for it", barEl.classes.has("hidden"), false);
bar(paused, "running");
check("the bar stays up in running mode", barEl.classes.has("hidden"), false);
check("killed mode does not count the paused", bar(paused, "killed"),
      ["resume 1", "archive 1 exited"]);

const mixed = [
  { name: "live", status: "idle" },
  { name: "dead", status: "exited" },
  { name: "old", status: "exited", archived_at: "2026-08-28T00:00:00Z" },
];
check("running mode exposes only its running actions", bar(mixed, "running"),
      ["stop 1", "pause 1"]);
check("killed mode exposes only its killed actions", bar(mixed, "killed"),
      ["resume 1", "archive 1 exited"]);
check("archived mode does not act on hidden current records", bar(mixed, "archived"), []);

/* Going back to nothing has to put the bar away again: these are toggled, not
   rebuilt, so a stale "stop 2" left up over an emptied rail would still be
   clickable and would still claim two sessions. */
syncBulkActions(of("idle", "idle"));
check("the bar empties when the rail does", bar([]), []);
check("and is put away with it", barEl.classes.has("hidden"), true);

/* Every button says what it does before it is pressed — these ask nothing of
   the daemon and are the only warning about which of them is destructive. */
syncBulkActions(of("idle", "exited"));
check(
  "every button carries a title",
  IDS.filter((id) => !buttons[id].title),
  []
);
check(
  "stop says the records survive it",
  /resumed/.test(buttons["stop-all"].title),
  true
);
check(
  "archive says the records and resume capability survive it",
  /retaining.*resume/.test(buttons["archive-exited"].title),
  true
);
check(
  "pause says it ends the program as a kill does and can be undone",
  /kill/.test(buttons["pause-all"].title) && /resumed/.test(buttons["pause-all"].title),
  true
);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("bulk_check: ok");
