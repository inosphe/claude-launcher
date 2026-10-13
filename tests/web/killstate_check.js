/* Kill button state: immediate request feedback, wind-down escalation and
   the final wait for an exited session, on both desktop and mobile controls. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);
const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "index.html"),
  "utf8"
);
const css = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "style.css"),
  "utf8"
);

const a = src.indexOf("const killUiState = new Map()");
const b = src.indexOf("async function archiveSession", a);
if (a < 0 || b <= a) throw new Error("cannot locate the kill state section");
const code = src.slice(a, b);

function button() {
  const classes = new Set();
  return {
    textContent: "kill", disabled: false, title: "", handlers: {}, classes,
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    classList: {
      toggle(name, on) { on ? classes.add(name) : classes.delete(name); },
    },
    addEventListener(name, fn) { this.handlers[name] = fn; },
  };
}

const buttons = { "term-kill": button(), "m-kill": button() };
const calls = [];
const replies = [];
const errors = [];
let filter = null;
let refreshes = 0;

const api = (url, options) => {
  calls.push({ url, options });
  return new Promise((resolve, reject) => replies.push({ resolve, reject }));
};
const modalInfo = async (title, detail) => { errors.push([title, detail]); };
const refreshSessions = async () => { refreshes += 1; };
const setSessionFilter = (value) => { filter = value; };
const $ = (id) => buttons[id];

const ctx = {};
new Function(
  "exports", "$", "api", "modalInfo", "refreshSessions", "setSessionFilter",
  `let currentName = "s1";
   let sessionsCache = [{name: "s1", status: "busy"}];
   ${code}
   Object.assign(exports, {
     killControlState, reconcileKillUiState, syncSessionKillControls,
     killCurrentSession,
     sessions: () => sessionsCache,
     setSessions: (value) => { sessionsCache = value; },
   });`
)(ctx, $, api, modalInfo, refreshSessions, setSessionFilter);

let failures = 0;
function check(name, actual, expected) {
  if (JSON.stringify(actual) === JSON.stringify(expected)) return;
  failures += 1;
  console.log(`FAIL ${name}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
}

const response = (body, ok = true, status = 200) => ({
  ok, status, json: async () => body,
});

(async () => {
  check("wind-down has a persistent visual state",
        /\.kill-winddown\s*\{[^}]*background:/s.test(css), true);
  check("the mobile control has its own wind-down treatment",
        /#mobile-top #m-kill\.kill-winddown\s*\{[^}]*background:/s.test(css), true);

  const first = ctx.killCurrentSession();
  check("a press sends the ordinary kill route", calls[0], {
    url: "/api/sessions/s1/kill", options: { method: "POST" },
  });
  check("both controls acknowledge the request immediately",
        [buttons["term-kill"].textContent, buttons["m-kill"].textContent,
         buttons["term-kill"].disabled, buttons["m-kill"].disabled,
         buttons["term-kill"].classes.has("kill-pending"),
         buttons["m-kill"].classes.has("kill-pending"),
         buttons["term-kill"].attrs["aria-busy"],
         buttons["m-kill"].attrs["aria-busy"]],
        ["ending…", "ending…", true, true, true, true, "true", "true"]);

  replies.shift().resolve(response({ name: "s1", status: "busy", winding_down: true }));
  await first;
  check("wind-down changes both controls into the escalation action",
        [buttons["term-kill"].textContent, buttons["m-kill"].textContent,
         buttons["term-kill"].disabled, buttons["m-kill"].disabled,
         buttons["term-kill"].classes.has("kill-winddown"),
         buttons["m-kill"].classes.has("kill-winddown"),
         buttons["term-kill"].attrs["aria-busy"]],
        ["stop now", "stop now", false, false, true, true, "false"]);
  check("wind-down is reflected before the next session poll",
        !!ctx.sessions()[0].winddown, true);

  const second = ctx.killCurrentSession();
  check("the escalation press bypasses wind-down",
        calls[1].url, "/api/sessions/s1/kill?winddown=0");
  replies.shift().resolve(response({ name: "s1", status: "busy" }));
  await second;
  check("graceful termination remains visible until exit",
        [buttons["term-kill"].textContent, buttons["term-kill"].disabled,
         buttons["term-kill"].classes.has("kill-pending")],
        ["ending…", true, true]);

  ctx.setSessions([{ name: "s1", status: "exited" }]);
  ctx.reconcileKillUiState();
  ctx.syncSessionKillControls();
  check("the exited poll clears the local pending state",
        [buttons["term-kill"].textContent, buttons["term-kill"].disabled],
        ["kill", false]);

  ctx.setSessions([{ name: "s1", status: "busy" }]);
  const failed = ctx.killCurrentSession();
  replies.shift().resolve(response({ error: "denied" }, false, 409));
  await failed;
  check("an API refusal restores the action and reports the reason",
        [buttons["term-kill"].textContent, buttons["term-kill"].disabled,
         errors[0]],
        ["kill", false, ["Could not kill 's1'", "denied"]]);

  const offline = ctx.killCurrentSession();
  replies.shift().reject(new Error("offline"));
  await offline;
  check("a network failure also restores the action and reports the reason",
        [buttons["term-kill"].textContent, buttons["term-kill"].disabled,
         errors[1]],
        ["kill", false, ["Could not kill 's1'", "offline"]]);
  check("refresh follows every completed request", refreshes, 4);
  check("a wind-down response does not select the killed filter", filter, null);

  /* A paused record (claunch-zpyzf): the press files the pause as killed.
     Nothing is running, so there is no wind-down to ask for and the reply
     lands exited at once. */
  ctx.setSessions([{ name: "s1", status: "exited", paused_at: "2026-09-01T00:00:00Z" }]);
  ctx.syncSessionKillControls();
  check("a paused record offers kill, and says what it does there",
        [buttons["term-kill"].textContent, buttons["term-kill"].disabled,
         /paused/.test(buttons["term-kill"].title)],
        ["kill", false, true]);
  const unpause = ctx.killCurrentSession();
  check("the press sends the ordinary kill route", calls[calls.length - 1].url,
        "/api/sessions/s1/kill");
  replies.shift().resolve(response({
    name: "s1", status: "exited", paused_at: null,
    already_exited: true, unpaused: true,
  }));
  await unpause;
  check("the record reads killed before the next poll",
        [ctx.sessions()[0].paused_at, buttons["term-kill"].disabled],
        [null, false]);

  /* A failed record (claunch-dba28): no program is left to terminate, so the
     title says what the press does there — it settles the record as killed
     and runs the board sweep the exit put off. */
  ctx.setSessions([{ name: "s1", status: "exited", exit_code: 1, failed_at: "2026-09-01T00:00:00Z" }]);
  ctx.syncSessionKillControls();
  check("a failed record offers kill, and says it files the record as killed",
        [buttons["term-kill"].textContent, buttons["term-kill"].disabled,
         /failed session as killed/.test(buttons["term-kill"].title),
         /terminate the program/.test(buttons["term-kill"].title),
         buttons["m-kill"].title === buttons["term-kill"].title],
        ["kill", false, true, false, true]);

  /* The header's lifecycle group (claunch-ndz47): resume | pause | kill |
     archive are one radio group, with the state the session is in checked
     and a move the state does not allow disabled — never hidden. */
  const hdr = {};
  for (const id of ["term-status", "term-resume", "term-rebrief", "term-retro", "term-kill",
                    "term-pause", "term-archive", "term-idle-hold"]) {
    const classes = new Set();
    hdr[id] = { textContent: "", className: "", classes, disabled: false,
      attrs: {}, dataset: {},
      setAttribute(n, v) { this.attrs[n] = String(v); },
      classList: { toggle(n, on) { on ? classes.add(n) : classes.delete(n); } } };
  }
  const s = src.indexOf("function syncTermLife(");
  const e = src.indexOf("/* ---- the link ----", s);
  if (s < 0 || e <= s) throw new Error("cannot locate syncTermLife/setStatusBadge");
  const badge = {};
  new Function("exports", "$", `
    let currentName = "s1", sessionsCache = [];
    function syncSessionKillControls() {}
    function renderTermTimer() {}
    function syncMobileBars() {}
    ${src.slice(s, e)}
    Object.assign(exports, { setStatusBadge,
      setSessions: (v) => { sessionsCache = v; } });`
  )(badge, (id) => hdr[id]);
  const LIFE = ["term-resume", "term-pause", "term-kill", "term-archive"];
  // [checked segment, enabled segments, hidden segments]
  const life = () => [
    LIFE.filter((id) => hdr[id].attrs["aria-checked"] === "true"),
    LIFE.filter((id) => id === "term-kill"
      ? hdr[id].dataset.lifeOff !== "1" : !hdr[id].disabled),
    LIFE.filter((id) => hdr[id].classes.has("hidden")),
  ];
  check("the four lifecycle controls are one radio group in the header",
        /<span id="term-life"[^>]*role="radiogroup"[^>]*>\s*(?:<!--[\s\S]*?-->\s*)?<button id="term-resume"[^>]*role="radio"[\s\S]*?<button id="term-pause"[^>]*role="radio"[\s\S]*?<button id="term-kill"[^>]*role="radio"[\s\S]*?<button id="term-archive"[^>]*role="radio"[^>]*>[^<]*<\/button>\s*<\/span>/.test(html),
        true);
  badge.setSessions([{ name: "s1", status: "idle" }]);
  badge.setStatusBadge("idle");
  check("a live session checks resume and offers pause, kill and archive",
        life(), [["term-resume"], ["term-pause", "term-kill", "term-archive"], []]);
  badge.setSessions([{ name: "s1", status: "exited", paused_at: "2026-09-01T00:00:00Z" }]);
  badge.setStatusBadge("exited");
  check("a paused header checks pause and offers resume, kill and archive",
        [hdr["term-status"].textContent, life()],
        ["paused", [["term-pause"], ["term-resume", "term-kill", "term-archive"], []]]);
  badge.setSessions([{ name: "s1", status: "exited" }]);
  badge.setStatusBadge("exited");
  check("a killed header checks kill and has no kill left to offer",
        life(), [["term-kill"], ["term-resume", "term-archive"], []]);
  // A failed record ended on its own (claunch-z3nc7): nobody pressed kill,
  // so kill is not checked, and the press is still what settles it (unhold).
  badge.setSessions([{ name: "s1", status: "exited", exit_code: 1, failed_at: "x" }]);
  badge.setStatusBadge("exited");
  check("a failed header checks none and offers resume, kill and archive",
        [hdr["term-status"].textContent, life()],
        ["failed", [[], ["term-resume", "term-kill", "term-archive"], []]]);
  // A clean exit 0 the harness made by itself carries no marker: the daemon
  // files it killed (_failed_on_its_own), and the header agrees.
  badge.setSessions([{ name: "s1", status: "exited", exit_code: 0 }]);
  badge.setStatusBadge("exited");
  check("a clean exit with no marker reads killed, as the daemon files it",
        life(), [["term-kill"], ["term-resume", "term-archive"], []]);
  badge.setSessions([{ name: "s1", status: "exited", held_at: "x" }]);
  badge.setStatusBadge("exited");
  check("a held header checks none and offers resume, kill and archive",
        life(), [[], ["term-resume", "term-kill", "term-archive"], []]);
  badge.setSessions([{ name: "s1", status: "exited", paused_at: "x", archived_at: "y" }]);
  badge.setStatusBadge("exited");
  check("an archived paused record checks archive and keeps kill and resume",
        life(), [["term-archive"], ["term-resume", "term-kill"], []]);

  /* syncSessionKillControls folds the group's verdict into kill's disabled
     state, so a ready kill on a killed record still reads disabled. */
  buttons["term-kill"].dataset = { lifeOff: "1" };
  ctx.setSessions([{ name: "s1", status: "exited" }]);
  ctx.syncSessionKillControls();
  check("the group's verdict disables a header kill the state has no use for",
        [buttons["term-kill"].disabled, buttons["m-kill"].disabled], [true, false]);
  buttons["term-kill"].dataset = { lifeOff: "" };
  ctx.syncSessionKillControls();
  check("and leaves it enabled where the state allows kill",
        buttons["term-kill"].disabled, false);
  // rebrief and retro type into a live terminal: on an exited one there is
  // nobody to read them (claunch-ygbsd).
  check("an exited header hides rebrief and retro",
        ["term-rebrief", "term-retro"].map((id) => hdr[id].classes.has("hidden")),
        [true, true]);
  badge.setSessions([{ name: "s1", status: "idle" }]);
  badge.setStatusBadge("idle");
  check("a live header shows rebrief and retro",
        ["term-rebrief", "term-retro"].map((id) => hdr[id].classes.has("hidden")),
        [false, false]);

  console.log(failures ? `\n${failures} failure(s)` : "all kill state checks passed");
  process.exit(failures ? 1 : 0);
})();
