/* Kill button state: immediate request feedback, wind-down escalation and
   the final wait for an exited session, on both desktop and mobile controls. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
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

  /* The header's visibility, which is what the reader sees first. */
  const hdr = {};
  for (const id of ["term-status", "term-resume", "term-rebrief", "term-kill",
                    "term-pause", "term-archive"]) {
    const classes = new Set();
    hdr[id] = { textContent: "", className: "", classes,
      classList: { toggle(n, on) { on ? classes.add(n) : classes.delete(n); } } };
  }
  const s = src.indexOf("function setStatusBadge(status)");
  const e = src.indexOf("/* ---- the link ----", s);
  if (s < 0 || e <= s) throw new Error("cannot locate setStatusBadge");
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
  const shown = () => ["term-kill", "term-pause", "term-archive", "term-resume"]
    .filter((id) => !hdr[id].classes.has("hidden"));
  badge.setSessions([{ name: "s1", status: "exited", paused_at: "2026-09-01T00:00:00Z" }]);
  badge.setStatusBadge("exited");
  check("a paused header offers kill, archive and resume",
        [hdr["term-status"].textContent, shown()],
        ["paused", ["term-kill", "term-archive", "term-resume"]]);
  badge.setSessions([{ name: "s1", status: "exited" }]);
  badge.setStatusBadge("exited");
  check("a killed header has no kill left to offer",
        shown(), ["term-archive", "term-resume"]);
  badge.setSessions([{ name: "s1", status: "exited", paused_at: "x", archived_at: "y" }]);
  badge.setStatusBadge("exited");
  check("an archived paused record keeps kill and resume",
        shown(), ["term-kill", "term-resume"]);

  console.log(failures ? `\n${failures} failure(s)` : "all kill state checks passed");
  process.exit(failures ? 1 : 0);
})();
