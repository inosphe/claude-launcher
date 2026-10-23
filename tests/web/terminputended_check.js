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
const logged = [];
new Function(
  "exports", "$", "logged",
  `let currentName = null, sessionEnded = false, snapshotName = null;
let sessionEndedBy = null, sessionEndedAt = null;
let attachedPid = null, attachedBoot = null;
let sessionsCache = [];
let lineSync = { session: null, mode: "local" };
let termInputVerdictKey = null;
function linkLog(kind, detail) { logged.push({ k: kind, ...(detail || {}) }); }
` + ["lineSyncShared", "termInputQueueNote", "termInputEnded", "termInputEndedWhy",
     "termInputLogVerdict", "noteSessionEnded", "shimEnded",
     "renderTermInputField", "renderTermInputState"].map(slice).join("\n") + `
Object.assign(exports, {
  termInputEnded, renderTermInputState, noteSessionEnded, shimEnded,
  origin: () => ({ by: sessionEndedBy, at: sessionEndedAt }),
  set: (o) => {
    if ("name" in o) currentName = o.name;
    if ("ended" in o) sessionEnded = o.ended;
    if ("snapshot" in o) snapshotName = o.snapshot;
    if ("pid" in o) attachedPid = o.pid;
    if ("rows" in o) sessionsCache = o.rows;
    if ("shared" in o) lineSync = { session: currentName, mode: o.shared ? "remote" : "local" };
  },
});`
)(ctx, $, logged);

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

/* --- the link log says which branch held the notice (claunch-ink90.1) --- */
/* The reports this answers are "session ended over a running session" after
   the fact: the log, not the screen, has to say which telling set the flag
   and which branch of termInputEnded kept the notice up. */
const verdicts = () => logged.filter((e) => e.k === "input.verdict");
logged.length = 0;
ctx.set({ name: "s9", ended: true, pid: 500, snapshot: null,
          rows: [{ name: "s9", status: "busy", pid: 500 }] });
ctx.renderTermInputState();
ctx.renderTermInputState();
ctx.renderTermInputState();
check("a held notice is logged with the branch that held it",
      verdicts().length === 1 && verdicts()[0].why === "record running, same pid"
        && verdicts()[0].ended === true && verdicts()[0].pid === 500
        && verdicts()[0].attached === 500 && verdicts()[0].status === "busy",
      verdicts());
ctx.set({ rows: [{ name: "s9", status: "idle" }] });
ctx.renderTermInputState();
check("a record without a pid is its own branch",
      verdicts().length === 2 && verdicts()[1].why === "record running, no pid", verdicts());
ctx.set({ rows: [{ name: "s9", status: "busy", pid: 501 }] });
ctx.renderTermInputState();
check("the release is logged too, with the record's new pid",
      verdicts().length === 3 && verdicts()[2].ended === false
        && verdicts()[2].why === "record running, new pid" && verdicts()[2].pid === 501,
      verdicts());
ctx.set({ rows: [] });
ctx.renderTermInputState();
check("no record is its own branch",
      verdicts().length === 4 && verdicts()[3].why === "no record", verdicts());
ctx.set({ ended: false, rows: [{ name: "s9", status: "busy", pid: 501 }] });
const before4 = verdicts().length;
ctx.renderTermInputState();
ctx.renderTermInputState();
check("a live socket writes no verdict", verdicts().length === before4, verdicts());

/* The telling that set the flag, and a repeat that does not move it. */
logged.length = 0;
ctx.set({ ended: false, pid: 600 });
ctx.noteSessionEnded("init", { status: "exited", code: 0 });
check("the first telling records its origin", ctx.origin().by === "init"
      && Number.isFinite(ctx.origin().at), ctx.origin());
check("and logs it with the socket's pid",
      logged.length === 1 && logged[0].k === "term.ended" && logged[0].by === "init"
        && logged[0].pid === 600 && logged[0].session === "s9" && logged[0].status === "exited",
      logged);
ctx.set({ ended: true });
ctx.noteSessionEnded("exit", { code: 0, again: true });
check("a repeated telling is logged but keeps the first origin",
      ctx.origin().by === "init" && logged.length === 2 && logged[1].by === "exit"
        && logged[1].again === true, [ctx.origin(), logged]);

/* A parked terminal keeps the origin on its own state for restoreTerminal. */
logged.length = 0;
const parked = { name: "s8", pid: 700, boot: "b1", exited: false };
ctx.shimEnded(parked, "exit", { code: 1 });
ctx.shimEnded(parked, "init", { status: "exited" });
check("a parked terminal records the first origin",
      parked.exited === true && parked.endedBy === "exit" && Number.isFinite(parked.endedAt),
      parked);
check("and logs every telling as parked",
      logged.length === 2 && logged.every((e) => e.k === "term.ended" && e.parked === true
        && e.session === "s8" && e.pid === 700), logged);

/* Every place that sets the flag names its origin. */
for (const [fn, re] of [
  ["endSession", /noteSessionEnded\(by,/],
  ["snapshotAttach", /noteSessionEnded\("snapshot"\)/],
  ["restoreTerminal", /sessionEndedBy = b\.exited/],
  ["shimFrame", /shimEnded\(b, "init"[\s\S]*shimEnded\(b, "exit"/],
]) {
  check(`${fn} records what ended the session`, re.test(slice(fn)));
}
check("the init frame is logged with pid, boot and exited",
      /linkLog\("term\.init", \{[^}]*pid: attachedPid[^}]*exited: !!msg\.exited/.test(src));
check("an init that says exited ends the session as \"init\"",
      /if \(msg\.exited\) endSession\("init"/.test(src));
check("an exit frame ends the session as \"exit\"", /endSession\("exit"/.test(src));

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
