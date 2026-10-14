/* The session line's "session ended" notice, against the session it names.

   The notice is the field's placeholder (claunch-tww0b), and it used to be
   painted straight from `sessionEnded` — the socket's word for the child it
   was bound to. Only a reattach clears that flag, and the poll reattaches
   only while the terminal is on screen; the line is up on other pages too.
   So a session that exited and was resumed while the reader was elsewhere
   kept "session ended — a line sent now is queued ..." over a running
   session (claunch-4lzc5). termInputEnded lets the rail's record overrule
   the flag once it shows a different, running child, and the poll repaints
   the field from it.

   The real functions are sliced out of the shipped app.js and driven
   against a stub field. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.TERMINPUTENDED_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

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

const classes = new Set();
const FIELD = {
  placeholder: "", title: "", dataset: {},
  classList: {
    toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    contains: (c) => classes.has(c),
  },
};
const $ = (id) => (id === "term-input-field" ? FIELD : null);

const ctx = {};
new Function(
  "exports", "$",
  `let currentName = null, sessionEnded = false, snapshotName = null;
let attachedPid = null;
let sessionsCache = [];
let lineSync = { session: null, mode: "local" };
` + ["lineSyncShared", "termInputQueueNote", "termInputEnded",
     "renderTermInputField", "renderTermInputState"].map(slice).join("\n") + `
Object.assign(exports, {
  termInputEnded, renderTermInputState,
  set: (o) => {
    if ("name" in o) currentName = o.name;
    if ("ended" in o) sessionEnded = o.ended;
    if ("snapshot" in o) snapshotName = o.snapshot;
    if ("pid" in o) attachedPid = o.pid;
    if ("rows" in o) sessionsCache = o.rows;
    if ("shared" in o) lineSync = { session: currentName, mode: o.shared ? "remote" : "local" };
  },
});`
)(ctx, $);

const ENDED = /^session ended/;
function paint(state) {
  ctx.set(state);
  ctx.renderTermInputState();
  return FIELD.placeholder;
}

/* --- a live socket: the plain line, whatever the record says ------------ */
check("a live socket shows the plain line",
      /^type for this session/.test(paint({
        name: "s1", ended: false, snapshot: null, pid: 100,
        rows: [{ name: "s1", status: "busy", pid: 100 }], shared: false })),
      FIELD.placeholder);
check("and no queued border", !classes.has("queued"));

/* --- ended, and the record agrees --------------------------------------- */
check("ended with an exited record says ended",
      ENDED.test(paint({ ended: true, rows: [{ name: "s1", status: "exited", pid: 100 }] })),
      FIELD.placeholder);
check("with the queued border", classes.has("queued"));

/* The exit frame arrives seconds before the next poll: the record still
   reads running for the very child that just ended. That is not a resume. */
check("ended with a stale running record for the same pid stays ended",
      ENDED.test(paint({ ended: true, rows: [{ name: "s1", status: "busy", pid: 100 }] })),
      FIELD.placeholder);
check("ended with a running record that has no pid stays ended",
      ENDED.test(paint({ ended: true, rows: [{ name: "s1", status: "idle" }] })),
      FIELD.placeholder);
check("ended with no record at all stays ended",
      ENDED.test(paint({ ended: true, rows: [] })), FIELD.placeholder);

/* --- the reported bug: resumed while the terminal was off screen ------- */
check("a resumed session (running record, new pid) shows the plain line",
      /^type for this session/.test(paint({
        ended: true, pid: 100, rows: [{ name: "s1", status: "busy", pid: 200 }] })),
      FIELD.placeholder);
check("and drops the queued border", !classes.has("queued"));
check("the starting status of a resume counts as running",
      !ENDED.test(paint({ ended: true, pid: 100,
                          rows: [{ name: "s1", status: "starting", pid: 201 }] })),
      FIELD.placeholder);
check("a shared line resumed shows the shared notice",
      /^shared with every browser/.test(paint({ ended: true, pid: 100, shared: true,
        rows: [{ name: "s1", status: "idle", pid: 200 }] })),
      FIELD.placeholder);
ctx.set({ shared: false });

/* A snapshot is drawn when the record said exited at attach time; it has no
   socket and no pid, so any running record behind it is a resume. */
check("a snapshot whose session came back shows the plain line",
      /^type for this session/.test(paint({
        ended: true, snapshot: "s1", pid: null,
        rows: [{ name: "s1", status: "busy", pid: 300 }] })),
      FIELD.placeholder);
check("a snapshot of a session still exited says ended",
      ENDED.test(paint({ ended: true, snapshot: "s1", pid: null,
                         rows: [{ name: "s1", status: "exited", pid: 300 }] })),
      FIELD.placeholder);
ctx.set({ snapshot: null });

/* Another session's record must not speak for this one. */
check("only the current session's record counts",
      ENDED.test(paint({ ended: true, pid: 100, rows: [
        { name: "s2", status: "busy", pid: 999 },
        { name: "s1", status: "exited", pid: 100 }] })),
      FIELD.placeholder);

/* --- the poll repaints the line, on screen or not ----------------------- */
/* refreshSessions is too wide to slice into a stub world (its rail rebuild
   is railhold_check's subject); what this pins is that the repaint sits
   outside the on-screen-only reattach branches. */
const refresh = slice("refreshSessions");
const at = refresh.indexOf("renderTermInputState()");
check("refreshSessions repaints the session line", at >= 0);
const before = refresh.slice(0, at);
const opens = (before.match(/\{/g) || []).length;
const closes = (before.match(/\}/g) || []).length;
check("at the function's own level, not inside a reattach branch",
      opens - closes === 1, opens - closes);

/* --- every painter reads the reconciled state ---------------------------- */
for (const fn of ["refreshTermInput", "renderLineSync"]) {
  const body = slice(fn);
  check(`${fn} paints from termInputEnded`,
        /renderTermInputField\([\s\S]*termInputEnded\(\)\)/.test(body));
  check(`${fn} does not paint from the raw socket flag`,
        !/renderTermInputField\([^;]*sessionEnded\)/.test(body));
}

if (failures) {
  console.log(`${failures} session-line ended check(s) failed`);
  process.exit(1);
}
console.log("all session-line ended checks passed");
