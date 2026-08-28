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
const b = src.indexOf("async function archiveExitedSession", a);
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

  console.log(failures ? `\n${failures} failure(s)` : "all kill state checks passed");
  process.exit(failures ? 1 : 0);
})();
