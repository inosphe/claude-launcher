/* The terminal's link, run against a stub socket.

   Everything else on the dashboard is a poll and repairs itself by accident.
   The terminal is not: it holds one socket, and until this machine existed a
   daemon restart left it holding a dead one until the page was reloaded. The
   rules that make it come back are also the rules that must not make it a
   nuisance — it may only retry from a link that is actually down, it may not
   retry forever, and it must never replay a half-typed command into whatever
   process happens to answer next. None of that is visible in a stylesheet or
   to Python, so the real functions are sliced out of the shipped app.js and
   driven here, with time and the network in the harness's hands. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");

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

/* ---- stub world ------------------------------------------------------- */
/* Time, the network and the socket all belong to the harness, so a backoff
   that takes forty seconds in a browser takes none here and every retry is
   inspected rather than waited for. */
function build(opts) {
  const o = opts || {};
  const nodes = {};
  for (const id of ["term-link", "m-link"]) {
    nodes[id] = {
      id, textContent: "", title: "", className: "", on: {},
      addEventListener: (ev, fn) => { nodes[id].on[ev] = fn; },
      click: () => nodes[id].on.click && nodes[id].on.click(),
    };
  }

  // Fake timers: a list, fired by hand.
  const timers = [];
  let ticket = 0;
  const setTimeoutStub = (fn, ms) => {
    timers.push({ id: ++ticket, fn, ms });
    return ticket;
  };
  const clearTimeoutStub = (id) => {
    const i = timers.findIndex((t) => t.id === id);
    if (i >= 0) timers.splice(i, 1);
  };
  const pending = () => timers.length;
  const fire = async () => {
    const t = timers.shift();
    if (!t) throw new Error("nothing scheduled");
    await t.fn();
    await settle();
    return t.ms;
  };

  // The retry awaits a health probe and a cookie renewal before it opens
  // anything, so a firing has microtasks behind it.
  const settle = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

  const sockets = [];
  class FakeSocket {
    constructor(u) {
      this.url = u;
      this.readyState = 0;
      this.sent = [];
      this.binaryType = null;
      sockets.push(this);
    }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; }
    // what the browser would call
    opened() { this.readyState = 1; if (this.onopen) this.onopen(); }
    dropped() { this.readyState = 3; if (this.onclose) this.onclose(); }
    text(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
  }
  FakeSocket.OPEN = 1;

  const health = { up: o.health !== false, boot_id: o.boot || "b1", asked: 0 };
  const fetchStub = async (u) => {
    health.asked += 1;
    if (!health.up) throw new Error("connection refused");
    return { ok: true, json: async () => ({ status: "ok", boot_id: health.boot_id }) };
  };
  const apiCalls = [];
  const apiStub = async (p) => {
    apiCalls.push(p);
    if (!health.up || o.unauthorised) throw new Error("unauthorized");
    return { ok: true, json: async () => ({}) };
  };

  const now = { at: 100000 };   // the clock the throttle reads
  const written = [];
  const term = {
    cols: 80, rows: 24, disposed: false,
    write: (s) => written.push(s),
    resize: () => {},
    dispose: () => { term.disposed = true; },
  };
  const statuses = [];
  const winOn = {};

  const code = slice("/* ---- the link ----", "/* ---- text size ----");
  const api = new Function(
    "$", "url", "api", "fetch", "WebSocket", "window", "document", "location",
    // `ws` and the rest are app.js globals declared above the slice. They are
    // injected rather than left to leak into node's global object, where the
    // worlds these blocks build would otherwise share one socket between them.
    "ws", "term", "fitAddon", "attachedPid", "applyingRemoteResize",
    "setStatusBadge", "refitSoon", "setTimeout", "clearTimeout", "Math", "Date",
    code +
    "\nreturn {openSocket, closeLink, detach, reconnectNow, tryReconnect," +
    " sendInput, handleFrame, syncLinkChip," +
    " get state() { return linkState; }," +
    " get tries() { return linkTry; }," +
    " get queued() { return linkQueue.slice(); }," +
    " get pid() { return attachedPid; }," +
    " get sock() { return ws; }," +
    " backoff: LINK_BACKOFF, jitter: LINK_JITTER, kickMs: LINK_KICK_MS," +
    " queueMax: LINK_QUEUE_MAX};"
  )(
    (id) => nodes[id],
    (p) => `/${String(p).replace(/^\//, "")}`,
    apiStub,
    fetchStub,
    FakeSocket,
    { addEventListener: (ev, fn) => { winOn[ev] = fn; } },
    { hidden: false },
    { protocol: "http:", host: "d09:8377" },
    null,     // ws: the link is the only thing that ever assigns it
    term,
    null,     // fitAddon: the link never touches it, detach() nulls it
    null,     // attachedPid: what the first init frame teaches it
    false,    // applyingRemoteResize
    (s) => statuses.push(s),
    () => {},
    setTimeoutStub,
    clearTimeoutStub,
    // No jitter here: the delays are the thing being checked, and a browser's
    // jitter only ever stretches them (see LINK_JITTER).
    { random: () => 0, round: Math.round },
    { now: () => now.at },
  );

  return { api, nodes, sockets, health, apiCalls, term, written, statuses,
           winOn, now, pending, fire, settle,
           // the ordinary starting point: attached, socket open, frames flowing
           live: async (pid) => {
             api.openSocket("s7");
             const s = sockets[sockets.length - 1];
             s.opened();
             s.text({ type: "init", cols: 80, rows: 24, status: "idle",
                      pid: pid === undefined ? 4242 : pid, boot_id: "b1" });
             await settle();
             return s;
           } };
}

/* --- a dropped socket goes and gets another one ------------------------ */
{
  const w = build();
  (async () => {
    const s = await w.live();
    check("a socket that opened is live", w.api.state === "live", w.api.state);
    s.dropped();
    check("losing it puts the link into reconnecting", w.api.state === "reconnecting",
          w.api.state);
    check("with a retry scheduled at the first backoff step",
          w.pending() === 1, w.pending());
    check("and the buffer says so", w.written.some((t) => t.includes("reconnecting")),
          w.written);
    const waited = await w.fire();
    check("the first wait is LINK_BACKOFF[0]", waited === w.api.backoff[0], waited);
    check("the retry asked health before opening anything", w.health.asked === 1,
          w.health.asked);
    check("it renewed the login cookie too — the old daemon's died with it",
          w.apiCalls.length === 1 && w.apiCalls[0] === "/api/daemon", w.apiCalls);
    check("and then opened a second socket", w.sockets.length === 2);
    check("pointed at the same session",
          w.sockets[1].url.endsWith("/api/sessions/s7/ws"), w.sockets[1].url);
    w.sockets[1].opened();
    check("which is live again", w.api.state === "live", w.api.state);
    check("with a full retry budget for the next outage", w.api.tries === 0,
          w.api.tries);
  })();
}

/* --- while the daemon is down it backs off, and then it stops ---------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    w.health.up = false;      // the daemon is gone, not merely this socket
    s.dropped();
    const waits = [];
    for (let i = 0; i < w.api.backoff.length; i += 1) waits.push(await w.fire());
    check("every step of the backoff was waited out",
          JSON.stringify(waits) === JSON.stringify(w.api.backoff), waits);
    check("the delays grow", waits[0] < waits[waits.length - 1]);
    check("no socket was opened against a daemon that never answered",
          w.sockets.length === 1, w.sockets.length);
    check("the retries are spent, so the link gives up", w.api.state === "lost",
          w.api.state);
    check("and nothing is left ticking in a tab nobody is watching",
          w.pending() === 0, w.pending());
    check("the chip says it is disconnected and can be pressed",
          w.nodes["term-link"].textContent.includes("disconnected"),
          w.nodes["term-link"].textContent);
    check("the phone's bar mirrors it",
          w.nodes["m-link"].className.includes("lost")
          && !w.nodes["m-link"].className.includes("hidden"),
          w.nodes["m-link"].className);

    // A person pressing it is worth a fresh budget — and it works, because by
    // now the daemon is back.
    w.health.up = true;
    w.nodes["term-link"].click();
    await w.settle();
    check("pressing the chip retries at once, without a wait",
          w.sockets.length === 2, w.sockets.length);
    w.sockets[1].opened();
    check("and the link is live again", w.api.state === "live", w.api.state);
    check("the chip goes away with the trouble it reported",
          w.nodes["term-link"].className.includes("hidden"),
          w.nodes["term-link"].className);
  })();
}

/* --- it only ever runs from a link that is down ------------------------ */
{
  const w = build();
  (async () => {
    await w.live();
    w.api.reconnectNow(true);
    check("a live socket is never replaced under the user",
          w.sockets.length === 1 && w.api.state === "live", w.sockets.length);
    await w.api.tryReconnect();
    check("and the retry itself refuses to run from `live`",
          w.sockets.length === 1 && w.health.asked === 0, w.health.asked);
  })();
}

/* --- an exited session is not a broken link ---------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.text({ type: "exit", code: 0 });
    check("the machine goes idle when the program ends", w.api.state === "idle",
          w.api.state);
    s.dropped();
    check("so the socket closing behind it schedules nothing",
          w.pending() === 0 && w.api.state === "idle", w.api.state);
    check("resume is what the header offers, not a retry",
          w.statuses[w.statuses.length - 1] === "exited", w.statuses);
    w.api.reconnectNow(true);
    check("and asking for a reconnect by hand does not resurrect it",
          w.sockets.length === 1, w.sockets.length);
  })();
}

/* --- walking away takes the link with you ------------------------------ */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.dropped();
    check("a retry is pending", w.pending() === 1);
    w.api.detach();
    check("detaching cancels it", w.pending() === 0 && w.api.state === "idle",
          w.api.state);
    check("and disposes the terminal it was feeding", w.term.disposed);
    check("the pid goes with it — the next init is what re-learns it",
          w.api.pid === null, w.api.pid);
    // The old socket's close can arrive after all this. It speaks for a link
    // that no longer exists and must not schedule anything.
    s.dropped();
    check("a late close from the socket we dropped is ignored",
          w.pending() === 0 && w.sockets.length === 1, w.pending());
  })();
}

/* --- the losing half of a race stays quiet ----------------------------- */
{
  const w = build();
  (async () => {
    const first = await w.live();
    w.api.openSocket("s7");   // e.g. a respawn reattaching
    check("the newer socket is the link's", w.sockets.length === 2);
    first.dropped();
    check("the one it replaced closing does not count as an outage",
          w.api.state === "opening" && w.pending() === 0, w.api.state);
  })();
}

/* --- keystrokes typed into a dead link -------------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.dropped();
    w.api.sendInput("git st");
    w.api.sendInput("atus\r");
    check("what was typed while it was down is held, not dropped",
          w.api.queued.join("") === "git status\r", w.api.queued);
    await w.fire();
    const back = w.sockets[1];
    back.opened();
    back.text({ type: "init", cols: 80, rows: 24, status: "idle", pid: 4242,
                boot_id: "b1" });
    await w.settle();
    check("and replayed once the same child answers again",
          back.sent.length === 1 && Buffer.from(back.sent[0]).toString() === "git status\r",
          back.sent.map((b) => Buffer.from(b).toString()));
    check("the queue is spent", w.api.queued.length === 0, w.api.queued);
  })();
}

/* --- but never into a different one ------------------------------------ */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.dropped();
    w.api.sendInput("rm -rf ");
    await w.fire();
    const back = w.sockets[1];
    back.opened();
    // A daemon restart relaunched the session: same name, new child.
    back.text({ type: "init", cols: 80, rows: 24, status: "idle", pid: 5150,
                boot_id: "b2" });
    await w.settle();
    check("half a command is not typed into whatever answered next",
          back.sent.length === 0, back.sent);
    check("and the terminal says where it went",
          w.written.some((t) => t.includes("discarded")), w.written);
    check("the link follows the new child all the same", w.api.pid === 5150,
          w.api.pid);
  })();
}

/* --- once it has given up, nothing is held for it ---------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    w.health.up = false;
    s.dropped();
    for (let i = 0; i < w.api.backoff.length; i += 1) await w.fire();
    check("the link is lost", w.api.state === "lost", w.api.state);
    w.api.sendInput("hello");
    check("typing into it is dropped rather than banked for an hour",
          w.api.queued.length === 0, w.api.queued);
  })();
}

/* --- events retry, but cannot be leaned on ----------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    w.health.up = false;
    s.dropped();
    for (let i = 0; i < w.api.backoff.length; i += 1) await w.fire();
    check("lost, with nothing scheduled", w.api.state === "lost" && w.pending() === 0);

    w.health.up = true;
    w.winOn.online();          // the network came back
    await w.settle();
    check("the network coming back is worth one try", w.sockets.length === 2,
          w.sockets.length);
    w.sockets[1].dropped();    // ...which failed anyway
    w.health.up = false;
    for (let i = 0; i < w.api.backoff.length; i += 1) await w.fire();
    check("back to lost", w.api.state === "lost", w.api.state);
    // The daemon is up from here, so anything that does NOT retry below is
    // the throttle refusing, not the network.
    w.health.up = true;
    const before = w.sockets.length;
    w.winOn.online();
    w.winOn.online();
    w.winOn.online();
    await w.settle();
    check("but a flapping event cannot become a retry loop of its own",
          w.sockets.length === before, w.sockets.length);
    w.now.at += w.api.kickMs + 1;
    w.winOn.online();
    await w.settle();
    check("once the throttle has passed, it tries again",
          w.sockets.length === before + 1, w.sockets.length);
  })();
}

/* --- the chip is the only thing that reports the socket ---------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    check("nothing is shown while all is well",
          w.nodes["term-link"].className.includes("hidden"),
          w.nodes["term-link"].className);
    s.dropped();
    check("a dropped link is counted out where it can be seen",
          /reconnecting… 1\/\d/.test(w.nodes["term-link"].textContent),
          w.nodes["term-link"].textContent);
    check("and it offers the way out of it",
          w.nodes["term-link"].title.includes("retry"),
          w.nodes["term-link"].title);
  })();
}

/* --- the markup the code reaches for ----------------------------------- */
for (const id of ["term-link", "m-link"]) {
  check(`index.html declares #${id}`, html.includes(`id="${id}"`));
}
check("the chip sits in the terminal header, beside the session's badge",
      html.indexOf('id="term-link"') > html.indexOf('id="term-status"')
      && html.indexOf('id="term-link"') < html.indexOf('id="terminal"'));
check("its mirror is on the bar that replaces that header",
      html.indexOf('id="m-link"') > html.indexOf('id="mobile-top"')
      && html.indexOf('id="m-link"') < html.indexOf('id="sidebar"'));
check("both start hidden — they are only ever up when something is wrong",
      /id="term-link"[^>]*class="[^"]*hidden/.test(html)
      && /id="m-link"[^>]*class="[^"]*hidden/.test(html));

/* The poll leads with the open endpoint, which is what lets a browser whose
   cookie died in the restart tell "gone" from "back". */
check("the health probe is the unauthenticated one",
      /fetch\(url\("\/api\/health"\)/.test(src));
check("and the poll outlives boot(), so a page opened against a dead daemon " +
      "still comes to life",
      src.indexOf("pollTimer = setInterval(pollTick, 2000);") > 0
      && src.indexOf("pollTimer = setInterval(pollTick, 2000);")
         > src.indexOf("async function pollTick("));

/* The checks run in async blocks, so the tally is only complete once the
   microtask queue has drained — and a throw inside one of them must not be
   reported as a pass on the way out. */
process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log("all reconnect checks passed");
});
