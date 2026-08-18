/* The rail's bulk bar, run against the real syncBulkActions from app.js.

   Four buttons that act on every session at once. What has to hold is that
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

/* The stub DOM is the four buttons and nothing else — `hidden` is a class in
   this app, so that is what the check reads. */
const IDS = ["stop-all", "resume-all", "clear-exited", "delete-all"];
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
const ctx = {};
new Function("exports", "$", src.slice(a, end) +
             "\nexports.syncBulkActions = syncBulkActions;")(
  ctx, (id) => buttons[id] || null
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
function bar(sessions) {
  syncBulkActions(sessions);
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
  "a rail with only running sessions can be stopped or deleted, not resumed",
  bar(of("idle", "busy", "starting")),
  ["stop 3", "delete all 3"]
);

check(
  "a rail with only exited sessions offers the three that reach them",
  bar(of("exited", "exited")),
  ["resume 2", "clear 2 exited", "delete all 2"]
);

check(
  "a mixed rail counts each side separately, and delete counts both",
  bar(of("idle", "exited", "busy", "exited", "exited")),
  ["stop 2", "resume 3", "clear 3 exited", "delete all 5"]
);

/* Going back to nothing has to put the bar away again: these are toggled, not
   rebuilt, so a stale "stop 2" left up over an emptied rail would still be
   clickable and would still claim two sessions. */
syncBulkActions(of("idle", "idle"));
check("the bar empties when the rail does", bar([]), []);

/* Every button says what it does before it is pressed — these ask nothing of
   the daemon and are the only warning about which of them is destructive. */
syncBulkActions(of("idle", "exited"));
check(
  "every button carries a title",
  IDS.filter((id) => !buttons[id].title),
  []
);
check(
  "the two that make a session unresumable say so",
  ["clear-exited", "delete-all"].filter((id) => !/no longer|forget/.test(buttons[id].title)),
  []
);
check(
  "stop says the records survive it",
  /resumed/.test(buttons["stop-all"].title),
  true
);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("bulk_check: ok");
