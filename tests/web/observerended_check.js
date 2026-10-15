/* Which observer read the page sends while it shows an exited session, run
   against the real wantEnded/refresh from observer.js (claunch-ozuag).

   The daemon leaves exited sessions' timelines out unless the read says
   ?ended=1. What has to hold:
   - with nothing exited in view, every poll is the light read;
   - with the detail panel (or a direct link) on an exited session, the first
     poll is light, finds the session exited and asks once more with
     ?ended=1, and every poll after that starts with ?ended=1 — it does not
     fall back to the light read when the full answer arrives (review of
     fdad7961: keyed on `events_omitted`, it alternated light and full on
     every poll and the timeline blinked);
   - 「종료된 세션 포함」 on makes every read full;
   - a daemon without the switch (no `ended` key) is never asked twice. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "observer.js"),
  "utf8");
const a = src.indexOf("/* Whether this read needs exited sessions' timelines.");
const b = src.indexOf("function navigate() {");
if (a < 0 || b < a) throw new Error("cannot locate wantEnded/refresh");

function answer(url, legacy) {
  const ended = url.includes("ended=1");
  const live = { name: "s1", running: true, events: [{ id: "a" }] };
  const gone = ended || legacy ? { name: "old", running: false, events: [{ id: "b" }] }
    : { name: "old", running: false, events: [], events_omitted: true };
  const body = { sessions: [live, gone] };
  if (!legacy) body.ended = ended;
  return body;
}

function harness({ panel = "", route = "", endedBox = false, legacy = false } = {}) {
  const urls = [], timers = [];
  const env = {
    urls, timers,
    api: async (url) => { urls.push(url); const t = JSON.stringify(answer(url, legacy)); return { ok: true, text: async () => t }; },
    document: { body: { classList: { contains: () => true } }, activeElement: null },
    $: (id) => id === "ended" ? { checked: endedBox } : {},
    setTimeout: (fn) => timers.push(fn),
  };
  const run = new Function("env", `
    const {api, document, $} = env; const setTimeout = env.setTimeout;
    let snapshot = {sessions: []}, lastSnapshot = "", refreshTask = null, panelLoaded = false, runs = [];
    let routeScope = ${JSON.stringify(route ? "session" : "global")}, routeName = ${JSON.stringify(route)};
    const panelBox = ${panel ? `{isConnected: true, dataset: {session: ${JSON.stringify(panel)}}}` : "null"};
    const appRuns = () => [], selections = () => {}, redraw = () => {}, showError = (m) => { throw Error(m); };
    ${src.slice(a, b)}
    return async function poll() {
      await refresh();
      while (env.timers.length) { await env.timers.shift()(); }
    };
  `);
  return { poll: run(env), urls };
}

let failures = 0;
function check(what, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
    failures++;
  }
}

(async () => {
  let h = harness();
  await h.poll(); await h.poll();
  check("nothing exited in view: light reads", h.urls, ["api/observer", "api/observer"]);

  h = harness({ panel: "old" });
  await h.poll(); await h.poll(); await h.poll();
  check("panel on an exited session: one retry, then full from the start",
        h.urls, ["api/observer", "api/observer?ended=1", "api/observer?ended=1", "api/observer?ended=1"]);

  h = harness({ route: "old" });
  await h.poll(); await h.poll();
  check("direct link to an exited session", h.urls, ["api/observer", "api/observer?ended=1", "api/observer?ended=1"]);

  h = harness({ panel: "s1" });
  await h.poll(); await h.poll();
  check("panel on a running session stays light", h.urls, ["api/observer", "api/observer"]);

  h = harness({ endedBox: true });
  await h.poll(); await h.poll();
  check("「종료된 세션 포함」 on", h.urls, ["api/observer?ended=1", "api/observer?ended=1"]);

  h = harness({ panel: "old", legacy: true });
  await h.poll();
  check("old daemon without the switch is not asked twice", h.urls, ["api/observer"]);

  if (failures) { console.error(`observerended_check: ${failures} failure(s)`); process.exit(1); }
  console.log("observerended_check: ok");
})().catch((err) => { console.error(err); process.exit(1); });
