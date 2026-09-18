/* The dashboard's notices, run against a stub daemon.

   The recovery this code sits on top of always worked: pollOnce() has read
   /api/health's boot id since the reconnect machinery landed, and a restart
   already rebuilt the page and re-opened the terminal's socket. What it did
   not do was SAY so, and a recovery that succeeds silently looks exactly
   like nothing having happened — which is the complaint this harness pins
   the fix for.

   So what is checked here is not "does a card appear" but the things
   that make the card worth having: a restart is told apart from a blip
   (different sentence, different persistence), the first page load is not
   an event, a flapping daemon replaces its own card instead of stacking
   them, the restart card names when the daemon itself came up (not just
   when the page noticed — the question after an absence), and the
   sentence a person needs after the card is gone (when this daemon
   started) survives on the home card. Time, the timers and the
   network belong to the harness, so none of it is waited for. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

check("the global poll waits for its refresh batch before releasing the tick",
      /await Promise\.all\(refreshes\)/.test(src), true);

/* ---- stub world ------------------------------------------------------- */
function build(opts) {
  const o = opts || {};

  function node(cls, text) {
    const n = {
      className: cls || "", textContent: text === undefined ? "" : text,
      title: "", children: [], parentNode: null, on: {},
      classList: {
        contains: (c) => n.className.split(" ").includes(c),
        toggle: (c, on) => {
          const has = n.classList.contains(c);
          if (on === undefined ? has : !on) {
            n.className = n.className.split(" ").filter((x) => x !== c).join(" ");
          } else if (!has) { n.className = `${n.className} ${c}`.trim(); }
        },
        remove: (c) => {
          n.className = n.className.split(" ").filter((x) => x !== c).join(" ");
        },
      },
      appendChild: (c) => { c.parentNode = n; n.children.push(c); return c; },
      removeChild: (c) => {
        const i = n.children.indexOf(c);
        if (i >= 0) n.children.splice(i, 1);
        c.parentNode = null;
      },
      addEventListener: (ev, fn) => { n.on[ev] = fn; },
      click: () => n.on.click && n.on.click(),
      // what a card is, read back as one string
      text: () => [n.textContent, ...n.children.map((c) => c.textContent)].join(" "),
    };
    return n;
  }

  const nodes = {
    notices: node(),
    "daemon-info": node(),
    "auth-overlay": node("hidden"),
  };

  const timers = [];
  let ticket = 0;
  const setTimeoutStub = (fn, ms) => { timers.push({ id: ++ticket, fn, ms }); return ticket; };
  const clearTimeoutStub = (id) => {
    const i = timers.findIndex((t) => t.id === id);
    if (i >= 0) timers.splice(i, 1);
  };
  const fireAll = () => { const due = timers.splice(0); due.forEach((t) => t.fn()); };

  const clock = { at: 1700000000000 };
  class FakeDate {
    constructor(ms) { this.ms = ms === undefined ? clock.at : ms; }
    toLocaleTimeString() { return `T${this.ms}`; }
    // isNaN() is how app.js tells an unparseable started_at apart; a real
    // Date would answer from its own clock, the stub answers from what it
    // was handed (a string it cannot parse is still "a time it was given").
    valueOf() { return typeof this.ms === "number" ? this.ms : 1; }
  }
  FakeDate.now = () => clock.at;

  const daemon = { up: o.up !== false, boot: "b1", version: "9.9", uptime: 42,
                   started: "t-start-1" };
  const daemonHealth = async () =>
    (daemon.up ? { status: "ok", version: daemon.version, boot_id: daemon.boot,
                   started_at: daemon.started } : null);
  const apiCalls = [];
  const apiStub = async (p) => {
    apiCalls.push(p);
    if (!daemon.up) throw new Error("down");
    return { json: async () => ({ version: daemon.version, uptime: daemon.uptime, relay: null }) };
  };

  const code = slice("/* notices — the page's own voice",
                     "pollTimer = setInterval(pollTick, DASHBOARD_POLL_MS);");
  const api = new Function(
    "$", "el", "setTimeout", "clearTimeout", "Date", "api", "daemonHealth",
    "renderRelayBadge", "refreshProfiles", "refreshHarnesses", "refreshRoles",
    "refreshWorkspaces", "refreshSessions", "refreshMeshList", "refreshCflow",
    "refreshTermQueued", "reconnectNow", "route", "refreshNewWorktree",
    // boot() opens the page's control socket once the first authenticated
    // read has answered. Whether it does is controlsocket_check's subject;
    // here it only has to exist.
    "ensureControlSocket",
    code +
    "\nreturn {notify, dismissNotice, noticeClock, setDaemonOnline, boot, pollOnce," +
    " get cards() { return [...notices.keys()]; }," +
    " get booted() { return booted; }," +
    " get online() { return daemonOnline; }," +
    " get bootId() { return daemonBoot; }," +
    " get startedAt() { return daemonStartedAt; }," +
    " keys: {link: NOTICE_LINK, boot: NOTICE_BOOT}, ms: NOTICE_MS};"
  )(
    (id) => nodes[id],
    (tag, cls, text) => node(cls, text),
    setTimeoutStub, clearTimeoutStub, FakeDate, apiStub, daemonHealth,
    () => {}, () => {}, () => {}, () => {}, () => {}, () => {}, () => {},
    () => {}, () => {}, () => {}, () => {},
    () => {},   // ensureControlSocket
    // The poll's refresh batch gained the worktree picker with the spawn
    // form's checkout choice; what it fetches and greys is newform_check's
    // and spawnform_check's to hold — here it only has to exist.
    () => {},
  );

  return { api, nodes, daemon, clock, apiCalls, timers,
           fireAll, pending: () => timers.length,
           strip: () => nodes.notices.children,
           said: () => nodes.notices.children.map((c) => c.text()).join(" | ") };
}

/* --- arriving is not an event ------------------------------------------ */
{
  const w = build();
  (async () => {
    await w.api.pollOnce();
    check("the first poll boots the page", w.api.booted, w.api.booted);
    check("and says nothing — opening a page is not something that happened",
          w.strip().length === 0, w.said());
    check("but it learned which daemon it is talking to", w.api.bootId === "b1",
          w.api.bootId);
    check("and when that daemon started, from the uptime it published",
          w.api.startedAt === w.clock.at - 42 * 1000, w.api.startedAt);
    await w.api.pollOnce();
    check("a quiet poll against the same daemon stays quiet",
          w.strip().length === 0, w.said());
  })();
}

/* --- the restart, which is the whole point ----------------------------- */
{
  const w = build();
  (async () => {
    await w.api.pollOnce();
    w.daemon.boot = "b2";              // the successor answers
    w.daemon.version = "9.10";
    w.daemon.started = "t-start-2";    // ...and says when it came up
    await w.api.pollOnce();
    check("a boot id it has not seen is announced", w.strip().length === 1, w.said());
    check("in words that name what happened", /restarted/.test(w.said()), w.said());
    check("with the version that answered", /9\.10/.test(w.said()), w.said());
    check("and the time it answered at", /T\d/.test(w.said()), w.said());
    check("and the time the daemon itself came up — the question the " +
          "card exists to answer after an absence",
          w.said().includes("restarted at Tt-start-2"), w.said());
    check("it warns rather than alarms",
          w.strip()[0].className.includes("warn"), w.strip()[0].className);
    check("nothing is scheduled to take it away: the tab was unwatched, " +
          "which is exactly why the card exists",
          w.pending() === 0, w.pending());
    w.fireAll();
    check("so no timeout can clear it", w.strip().length === 1, w.said());
    check("the page still recovered — the card is a sentence, not a gate",
          w.api.bootId === "b2" && w.apiCalls.includes("/api/daemon"), w.apiCalls);
    w.strip()[0].click();
    check("and a person clicking it is how it goes away",
          w.strip().length === 0, w.said());
  })();
}

/* --- a daemon that does not say when it started ------------------------- */
{
  const w = build();
  (async () => {
    await w.api.pollOnce();
    w.daemon.boot = "b2";
    w.daemon.started = undefined;      // an older daemon has no such field
    await w.api.pollOnce();
    check("the restart is still announced", /restarted/.test(w.said()), w.said());
    check("with the time it answered at, and no half-sentence where the " +
          "boot time would have been",
          /T\d/.test(w.said()) && !/restarted at/.test(w.said()), w.said());
  })();
}

/* --- an outage that is not a restart ----------------------------------- */
{
  const w = build();
  (async () => {
    await w.api.pollOnce();
    w.daemon.up = false;
    await w.api.pollOnce();
    check("a daemon that stopped answering says so", w.strip().length === 1, w.said());
    check("in the colour of trouble", w.strip()[0].className.includes("bad"),
          w.strip()[0].className);
    check("and it stays up for as long as the outage does",
          w.pending() === 0, w.pending());
    check("the badge is still labelled too — the card does not replace it",
          w.nodes["daemon-info"].textContent === "daemon offline",
          w.nodes["daemon-info"].textContent);

    w.daemon.up = true;               // same boot id: it never died
    await w.api.pollOnce();
    check("coming back replaces that card rather than joining it",
          w.strip().length === 1, w.said());
    check("and says the daemon is the same one",
          /daemon back/.test(w.said()) && !/daemon restarted/.test(w.said()),
          w.said());
    check("a blip does time out — it needs no clearing by hand",
          w.pending() === 1, w.pending());
    w.fireAll();
    check("and then it is gone", w.strip().length === 0, w.said());
  })();
}

/* --- an outage that WAS a restart -------------------------------------- */
{
  const w = build();
  (async () => {
    await w.api.pollOnce();
    w.daemon.up = false;
    await w.api.pollOnce();
    w.daemon.up = true;
    w.daemon.boot = "b3";
    await w.api.pollOnce();
    check("the outage card is retired by the answer", w.api.cards.length === 1,
          w.api.cards);
    check("and what is left is the restart, not 'back'",
          /restarted/.test(w.said()) && !/never restarted/.test(w.said()), w.said());
    check("which is the sticky one", w.pending() === 0, w.pending());
  })();
}

/* --- a flapping daemon cannot paper the screen ------------------------- */
{
  const w = build();
  (async () => {
    await w.api.pollOnce();
    for (let i = 0; i < 6; i += 1) {
      w.daemon.up = false;
      await w.api.pollOnce();
      w.daemon.up = true;
      await w.api.pollOnce();
    }
    check("six round trips leave one card, not twelve",
          w.strip().length === 1, w.strip().length);
    check("and the bookkeeping does not leak either",
          w.api.cards.length === 1, w.api.cards);
  })();
}

/* --- a page opened against a daemon that is already down --------------- */
{
  const w = build({ up: false });
  (async () => {
    await w.api.pollOnce();
    check("says nothing: it never saw the daemon up, so nothing *changed*",
          w.strip().length === 0, w.said());
    check("though the badge still reports it",
          w.nodes["daemon-info"].textContent === "daemon offline",
          w.nodes["daemon-info"].textContent);
  })();
}

/* --- keys replace, and dismissing is idempotent ------------------------ */
{
  const w = build();
  w.api.notify("one", "first", { key: "k" });
  w.api.notify("two", "second", { key: "k" });
  check("the same key is one card", w.strip().length === 1, w.said());
  check("showing the newer sentence", /two/.test(w.said()), w.said());
  check("and the older card's timeout went with it", w.pending() === 1, w.pending());
  w.api.dismissNotice("k");
  w.api.dismissNotice("k");
  check("dismissing twice is not an error", w.strip().length === 0, w.said());
  w.api.notify("a");
  w.api.notify("b");
  check("keyless cards do not collide with each other",
        w.strip().length === 2, w.said());
}

/* --- the markup and the stylesheet the code reaches for ---------------- */
check("index.html declares #notices", /id="notices"/.test(html));
check("it is announced to a screen reader as it changes",
      /id="notices"[^>]*aria-live="polite"/.test(html));
check("and it sits outside #layout, so no page owns it",
      html.indexOf('id="notices"') < html.indexOf('id="layout"'));
check("the strip is fixed and above BOTH overlays — the token prompt a " +
      "restart raises is the thing the card has to explain",
      /#notices\s*\{[^}]*position:\s*fixed/.test(css)
      && /#notices\s*\{[^}]*z-index:\s*30/.test(css));
check("an empty strip cannot swallow the corner it floats over",
      /#notices\s*\{[^}]*pointer-events:\s*none/.test(css)
      && /\.notice\s*\{[^}]*pointer-events:\s*auto/.test(css));
check("the home card carries the start time the cards cannot",
      /started \$\{new Date\(daemonStartedAt\)\.toLocaleTimeString\(\)\}/.test(src));
check("and the restart button labels the gap it asked for",
      /restarting the daemon/.test(src));

process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log("all notice checks passed");
});
