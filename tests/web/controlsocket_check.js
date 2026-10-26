/* The connection the page keeps, and the one it stops spending.

   Batching put the dashboard's tick on one request instead of nine, which
   was not enough. A browser keeps a connection in its pool after the answer
   arrives — Firefox for 115 seconds by default — so the count that matters
   is not how many requests are in flight but how many connections the page
   has ever needed at once. That was five against a ceiling of six: two for
   the read budget, one for the liveness probe, one for the terminal socket
   and one for the parked one. A second tab reached the ceiling, and there
   the next terminal's handshake sat in the browser's own connection queue
   and was never sent, so the daemon had nothing to open, close or refuse and
   every record it kept read as calm (claunch-riq5).

   The control socket is one connection for as long as the page is open. The
   reads ride it, the liveness question is answered by it being open, and the
   page is left holding it and the terminal.

   What this file pins is the part that could turn one failure into two. A
   socket is a single point the page could die at, so:

     - the socket is preferred, and its rejection is not a failed read: the
       same paths go out over HTTP unchanged;
     - reads waiting on a socket that dies are handed back, not left pending;
     - the retry never gives up, because nobody is looking at this connection
       and the whole tick rides it;
     - a read that is never answered times out rather than stalling the tick;
     - liveness is answered from the socket only while it is open, and the
       HTTP probe that answers otherwise gives up after HEALTH_TIMEOUT_MS;
     - a socket taken for dead (its ping deadline, claunch-fm3ud) is closed
       from this side and handled as if it had closed itself; a wake-up
       reopens a closed socket at once instead of after its backoff.

   The real functions are sliced out of the shipped app.js and driven against
   a stub WebSocket, so what is tested is the page's own machine. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

function constOf(name) {
  const m = new RegExp(`const ${name} = ([^;]+);`).exec(src);
  if (!m) throw new Error("missing const " + name);
  return m[1];
}

/* A slice that may be missing, as "" — so a harness written against the fix
   still loads against the commit before it and fails on its assertions
   rather than on a missing function. What that costs is the reason to run
   it there: a harness that cannot load reports nothing about behaviour. */
function maybeSlice(name) {
  return src.includes(`function ${name}(`) ? slice(name) : "";
}

/* Everything between two anchors, by their exact text. For a block whose
   members are not one function: the boot-progress machine is a `let` and
   two functions, and its state is what the check below reads back. */
function sliceRange(from, to, what) {
  const a = src.indexOf(from);
  const b = a < 0 ? -1 : src.indexOf(to, a + from.length);
  if (a < 0 || b <= a) throw new Error("cannot locate " + what);
  return src.slice(a, b);
}

/* ---- the harness ------------------------------------------------------- */
/* Timers are driven by hand: every setTimeout is recorded and fired only
   when a check asks for it, so a backoff is a value to assert rather than a
   wait to sit through. */
function build() {
  const sockets = [];
  const timers = [];
  const scope = {
    sockets,
    timers,
    setTimeout: (fn, ms) => {
      timers.push({ fn, ms, cancelled: false });
      return timers.length - 1;
    },
    clearTimeout: (id) => {
      if (typeof id === "number" && timers[id]) timers[id].cancelled = true;
    },
    url: (p) => p,
    location: { protocol: "http:", host: "d09:8377" },
    // Fire the most recently scheduled timer that is still armed.
    run: () => {
      for (let i = timers.length - 1; i >= 0; i--) {
        if (!timers[i].cancelled && !timers[i].fired) {
          timers[i].fired = true;
          timers[i].fn();
          return timers[i];
        }
      }
      throw new Error("no timer to run");
    },
    pending: () => timers.filter((t) => !t.cancelled && !t.fired),
  };

  class FakeWebSocket {
    constructor(u) {
      this.url = u;
      this.readyState = 0;
      this.sent = [];
      sockets.push(this);
    }
    send(text) {
      if (this.readyState !== 1) throw new Error("not open");
      this.sent.push(JSON.parse(text));
    }
    close() {
      if (this.readyState === 3) return;
      this.readyState = 3;
      if (this.onclose) this.onclose({ code: 1000 });
    }
    // -- what the daemon does --
    opened() { this.readyState = 1; if (this.onopen) this.onopen(); }
    say(frame) { if (this.onmessage) this.onmessage({ data: JSON.stringify(frame) }); }
    died() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1006 }); }
  }
  FakeWebSocket.OPEN = 1;

  const code = [
    `const CONTROL_BACKOFF = ${constOf("CONTROL_BACKOFF")};`,
    `const CONTROL_READ_TIMEOUT_MS = ${constOf("CONTROL_READ_TIMEOUT_MS")};`,
    "let controlSock = null;",
    "let controlTry = 0;",
    "let controlTimer = null;",
    "let controlSeq = 0;",
    // Which daemon the open socket reached, from its init (claunch-r35k1).
    // Absent before that fix, and the harness still runs there: what it
    // then measures is the old page answering "up" with no identity.
    maybeSlice("controlBootFrom"),
    "let controlBoot = null;",
    // Whether the last `boot.phase` read said restoring. Absent before the
    // fix that added it, and the harness still runs there -- which is what
    // lets the check below fail on the older page instead of on a `ReferenceError`.
    "let controlBootRestoring = false;",
    "let lastBootSeen = null;",
    "const controlWaiting = new Map();",
    slice("controlUp"),
    slice("controlAbort"),
    slice("openControlSocket"),
    // Its frames and its closing are routed by these two, which the P2P
    // road shares (claunch-mhzt4); p2p_check.js exercises that road, so its
    // own entry points are stubbed here.
    slice("controlMessage"),
    slice("controlClosed"),
    "const controlRetired = new Set();",
    slice("controlRetiredMessage"),
    slice("controlRetireCheck"),
    "function p2pInit() {}",
    "function p2pSignal() {}",
    "function p2pFailed() {}",
    slice("scheduleControlReopen"),
    slice("ensureControlSocket"),
    slice("controlRead"),
    slice("controlAnswer"),
    slice("controlPart"),
    slice("controlSay"),
    // The terminals now ride this socket too (daemon/channel.py), so its
    // own machine reaches for the channel routing. Sliced in as it stands:
    // channels_check.js is where that half is actually exercised.
    `const CHANNEL_HEADER = ${constOf("CHANNEL_HEADER")};`,
    "const channelLinks = new Map();",
    "let channelSeq = 0;",
    slice("channelBinary"),
    slice("channelFrame"),
    slice("channelsCarrierGone"),
    `const HEALTH_TIMEOUT_MS = ${constOf("HEALTH_TIMEOUT_MS")};`,
    slice("daemonHealth"),
    // The rail's half of the boot phase (claunch-9akgh). Sliced in whole
    // because the check that matters is the path: a socket `init` ->
    // daemonHealth -> syncBootProgress(health.boot). restartboot_check.js
    // feeds syncBootProgress directly and so cannot see this seam move.
    sliceRange("let daemonBootPhase", "function railMetaText(", "daemon progress block"),
    // A failed probe while the socket is open keeps answering with what the
    // page has instead of null; the check below is that path.
    maybeSlice("probeFailed"),
    slice("controlRecycle"),
    slice("controlProbe"),
    // The ping half (the deadline, the probe's ping) is latency_check.js's.
    "let probesArmed = 0;",
    "function controlProbeArm() { probesArmed++; }",
    "function latencyTick() {}",
    // The socket's open/close/pong also drive the latency badge
    // (claunch-8ufey). Stubbed so the frames counted here are the reads';
    // latency_check.js exercises that machine.
    "function latencyStart() {}",
    "function latencyStop() {}",
    "function latencyPong() {}",
    // syncBootProgress turns "the fleet is back" into a session refresh, and
    // draws into the rail's banner box. Counted here so the check below can
    // see the edge the frozen snapshot used to swallow.
    "function refreshSessions() { rail.refreshes += 1; }",
    "return { controlUp, openControlSocket, ensureControlSocket, controlRead,"
    + " controlSay, daemonHealth, controlRecycle, controlProbe,"
    + " syncBootProgress,"
    + " armed: () => probesArmed, waiting: () => controlWaiting.size,"
    // Functions, not getters: Object.assign copies a getter's *value* once,
    // so `get phase()` would freeze the phase at whatever it was when this
    // object left the function.
    + " phase: () => daemonBootPhase, refreshed: () => rail.refreshes,"
    + " get sock() { return controlSock; } };",
  ].join("\n");

  // The rail's shared box: the counter the stubbed refreshSessions bumps.
  // Passed in rather than declared in the code above so the count lives
  // where the check can read it, which is the same trick `probed` uses.
  const rail = { refreshes: 0 };

  const make = new Function(
    "WebSocket", "url", "location", "setTimeout", "clearTimeout", "fetch",
    "rail", code
  );
  Object.assign(scope, make(
    FakeWebSocket, scope.url, scope.location, scope.setTimeout, scope.clearTimeout,
    async (u, opts) => {
      scope.probed = (scope.probed || 0) + 1;
      if (scope.fetch) return scope.fetch(u, opts);
      throw new Error("no http");
    },
    rail
  ));
  return scope;
}

/* ---- 1. one socket, and the reads ride it ------------------------------ */
async function readsRideTheSocket() {
  const app = build();
  app.ensureControlSocket();
  assert.strictEqual(app.sockets.length, 1, "one socket opened");
  assert.match(app.sockets[0].url, /^ws:\/\/d09:8377\/api\/control\/ws$/);

  const sock = app.sockets[0];
  sock.opened();
  const reading = app.controlRead(["/api/sessions", "/api/cflow"]);
  assert.strictEqual(sock.sent.length, 1, "one frame for two paths");
  assert.strictEqual(sock.sent[0].type, "read");
  assert.deepStrictEqual(sock.sent[0].paths, ["/api/sessions", "/api/cflow"]);

  sock.say({
    type: "read_result",
    id: sock.sent[0].id,
    answers: { "/api/sessions": [1], "/api/cflow": null },
    errors: {},
  });
  const got = await reading;
  assert.deepStrictEqual(got.answers, { "/api/sessions": [1], "/api/cflow": null });
  assert.deepStrictEqual(got.errors, {});
  assert.strictEqual(app.waiting(), 0, "nothing left waiting");

  // A second read on the same socket: still one connection.
  const again = app.controlRead(["/api/sessions"]);
  assert.strictEqual(app.sockets.length, 1, "no second socket for a second read");
  assert.notStrictEqual(sock.sent[1].id, sock.sent[0].id, "ids do not repeat");
  sock.say({ type: "read_result", id: sock.sent[1].id, answers: {}, errors: {} });
  await again;

  // ensureControlSocket is called from boot(), which runs again on every
  // recovery. It must not churn the connection it is checking on.
  app.ensureControlSocket();
  assert.strictEqual(app.sockets.length, 1, "ensure does not reopen a live socket");
}

/* ---- 2. a socket that is not up is not a failed read ------------------- */
/* The rejection says "use the other transport", and batchFlush does. If this
   ever resolved with empty answers instead, a page whose socket was down
   would render as a daemon with nothing in it. */
async function downSocketRejects() {
  const app = build();
  await assert.rejects(app.controlRead(["/api/sessions"]), /control socket down/);

  app.ensureControlSocket();
  // Opened but not yet OPEN: the handshake is still in flight.
  await assert.rejects(app.controlRead(["/api/sessions"]), /control socket down/);
  assert.strictEqual(app.sockets[0].sent.length, 0, "nothing sent before open");
}

/* ---- 3. reads waiting on a socket that dies are handed back ------------ */
async function deathHandsReadsBack() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];
  sock.opened();
  const first = app.controlRead(["/api/sessions"]);
  const second = app.controlRead(["/api/cflow"]);
  assert.strictEqual(app.waiting(), 2);

  sock.died();
  await assert.rejects(first, /control socket closed/);
  await assert.rejects(second, /control socket closed/);
  assert.strictEqual(app.waiting(), 0, "no read left pending on a dead socket");
  assert.strictEqual(app.controlUp(), false);
}

/* ---- 4. the retry never gives up --------------------------------------- */
/* The terminal's retry stops after eight tries because a person is looking
   at it and can press reconnect. Nobody is looking at this one. */
async function retryNeverGivesUp() {
  const app = build();
  app.ensureControlSocket();
  const backoff = JSON.parse(
    constOf("CONTROL_BACKOFF").replace(/\s+/g, "")
  );

  const waits = [];
  for (let i = 0; i < backoff.length + 4; i++) {
    const sock = app.sockets[app.sockets.length - 1];
    sock.died();
    const timer = app.pending()[app.pending().length - 1];
    assert.ok(timer, `a reopen is scheduled after failure ${i + 1}`);
    waits.push(timer.ms);
    app.run();
  }
  assert.deepStrictEqual(
    waits.slice(0, backoff.length), backoff,
    "the backoff is walked in order"
  );
  const last = backoff[backoff.length - 1];
  assert.deepStrictEqual(
    waits.slice(backoff.length), [last, last, last, last],
    "and then stays at its longest wait rather than stopping"
  );
  assert.strictEqual(
    app.sockets.length, backoff.length + 5,
    "every retry actually opened a socket"
  );

  // A success resets it, so the next outage starts short again.
  app.sockets[app.sockets.length - 1].opened();
  app.sockets[app.sockets.length - 1].died();
  assert.strictEqual(app.pending()[app.pending().length - 1].ms, backoff[0]);
}

/* ---- 5. an unanswered read times out ----------------------------------- */
/* A daemon that takes the frame and never answers must not stall the tick
   for as long as the page is open: the read is rejected and the paths go out
   over HTTP on that tick like any other. */
async function unansweredReadTimesOut() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];
  sock.opened();
  const reading = app.controlRead(["/api/sessions"]);
  const timer = app.pending()[app.pending().length - 1];
  assert.strictEqual(
    timer.ms, JSON.parse(constOf("CONTROL_READ_TIMEOUT_MS")),
    "the timeout is the declared one"
  );
  app.run();
  await assert.rejects(reading, /timed out/);
  assert.strictEqual(app.waiting(), 0);

  // The late answer arrives to nobody, and must not throw on the way.
  sock.say({ type: "read_result", id: sock.sent[0].id, answers: {}, errors: {} });
}

/* ---- 6. what rides the socket that is not a read ----------------------- */
async function reportsAndPings() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];

  // Down: a diagnostic that cannot be sent must not become a second failure.
  assert.strictEqual(app.controlSay({ type: "link_failed" }), false,
                     "silent while the socket is not up");

  sock.opened();
  assert.strictEqual(
    app.controlSay({ type: "link_failed", session: "s578", code: 1006, tries: 4 }),
    true
  );
  assert.deepStrictEqual(sock.sent[0], {
    type: "link_failed", session: "s578", code: 1006, tries: 4,
  });
}

/* ---- 7. liveness is the socket being open ------------------------------ */
/* The probe was a connection spent to learn what an open socket already
   says. It is still there for when the socket is not: `fetch` throws in this
   harness, so a null answer proves the HTTP path was the one taken.

   What an open socket answers is its own `init` (claunch-r35k1). The dump
   below is the shape that matters: `boot_id` is the one field that says a
   *different* daemon is serving, and the tick reads it to notice a restart.
   A socket that has not had its init yet cannot answer "up" -- the page
   would then have no identity to compare and would read the next tick as
   the same daemon. */
async function livenessComesFromTheSocket() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];

  assert.strictEqual(await app.daemonHealth(), null, "not open: HTTP answers");
  assert.strictEqual(app.probed, 1, "and the probe was actually sent");

  sock.opened();
  assert.strictEqual(
    await app.daemonHealth(), null,
    "open but not yet greeted: no identity to report, so the probe answers"
  );
  assert.strictEqual(app.probed, 2, "and that probe really went out");

  sock.say({
    type: "init", boot_id: "b2", version: "9.9", started_at: "t-2",
    boot: { phase: "ready", done: 10, total: 10 },
  });
  const health = await app.daemonHealth();
  assert.deepStrictEqual(health, {
    boot_id: "b2", via: "control", version: "9.9", started_at: "t-2",
  }, "the socket's identity, in the shape /api/health publishes");
  assert.strictEqual(app.probed, 2, "no connection spent while the socket is up");

  // The identity is per daemon, not per frame: the next tick must not read
  // it as a change, and must not spend a probe either.
  assert.deepStrictEqual((await app.daemonHealth()).boot_id, "b2");
  assert.strictEqual(app.probed, 2);

  sock.died();
  assert.strictEqual(await app.daemonHealth(), null,
    "a closed socket has no identity to report: the probe answers again");
  assert.strictEqual(app.probed, 3, "and the probe comes back when it is needed");
}

/* The one field of that frame that moves (claunch-r35k1, from review).

   `boot_id`, `version` and `started_at` are constants of a daemon, so
   replaying the identity tick after tick is right. `boot` is not: a
   successor binds its port before it restores the fleet, and the page
   reopens on a 500ms backoff -- so the init that carries `boot` lands inside
   the restoring window by construction. Answered from that frozen frame, the
   rail says "restoring 3/33" for as long as the socket lives, and
   renderRestoreQueueBanner stays suppressed with it.

   This is the path the defect lived on: socket init -> daemonHealth ->
   syncBootProgress. restartboot_check.js feeds syncBootProgress a value
   directly and so cannot see it. */
async function theBootPhaseIsNotFrozenAtInit() {
  const app = build();
  // What the daemon answers while it is still restoring, and after.
  app.fetch = async () => ({ ok: true, json: async () => ({
    boot_id: "b2",
    boot: { phase: phase, done: phase === "ready" ? 10 : 3, total: 10 },
  }) });
  let phase = "restoring";

  app.ensureControlSocket();
  const sock = app.sockets[0];
  sock.opened();
  sock.say({
    type: "init", boot_id: "b2", version: "9.9", started_at: "t-2",
    boot: { phase: "restoring", done: 3, total: 10 },
  });

  // Inside the window the live value is what the page reports.
  app.syncBootProgress((await app.daemonHealth()).boot);
  assert.strictEqual(app.phase(), "restoring", "the restore is announced");
  const inside = app.probed;

  // The fleet is back. The next tick must say so -- this is what the frozen
  // snapshot swallowed, and what left the banner pinned.
  phase = "ready";
  app.syncBootProgress((await app.daemonHealth()).boot);
  assert.strictEqual(app.phase(), null,
    "the phase is read live, so the restoring banner comes down");
  assert.strictEqual(app.refreshed(), 1,
    "and the restoring -> ready edge still refreshes the rail");
  assert.ok(app.probed > inside, "the window is answered over HTTP");

  // The window then closes: the socket answers again, identity and all, and
  // the page stops spending a connection per tick.
  const spent = app.probed;
  const after = await app.daemonHealth();
  assert.deepStrictEqual(after, {
    boot_id: "b2", via: "control", version: "9.9", started_at: "t-2",
  }, "past the edge the socket answers the identity again");
  assert.strictEqual(app.probed, spent, "with no probe spent on it");
  assert.strictEqual(app.sockets[0], sock, "on the same socket");
  assert.strictEqual(app.sockets.length, 1, "and no second socket was opened");
}

/* A probe that cannot answer while the socket is up is not "no daemon"
   (claunch-r35k1, from the second review).

   The restoring window is answered over HTTP by design, and it opens exactly
   when the daemon is busiest -- right after a restart. A probe that times out
   there used to fall into the same `return null` a genuinely absent daemon
   does, and pollOnce reads null as offline: the page went dark every tick the
   window was open, which is the symptom this round was meant to remove,
   replaced by a worse one. tryReconnect postponed on the same null.

   The socket is the fact that settles it: it is OPEN, the daemon opened it and
   has not closed it. So the answer stays up, carrying the last boot the page
   saw. A null from daemonHealth still means what it always did -- no socket to
   vouch for the daemon, HTTP was the only source, and it failed. */
async function aFailedProbeIsNotAnAbsentDaemon() {
  const app = build();
  app.fetch = async () => { throw new Error("the probe timed out"); };

  // No socket: the probe was the only source, so this really is "no answer".
  assert.strictEqual(await app.daemonHealth(), null,
    "with no socket, a failed probe is still no answer");

  app.ensureControlSocket();
  const sock = app.sockets[0];
  sock.opened();
  sock.say({
    type: "init", boot_id: "b2", version: "9.9", started_at: "t-2",
    boot: { phase: "restoring", done: 3, total: 10 },
  });

  // Inside the window, and the probe is down. The page must stay up.
  const first = await app.daemonHealth();
  assert.ok(first, "a failed probe does not read as an absent daemon");
  assert.strictEqual(first.boot_id, "b2", "the socket's identity still answers");
  assert.strictEqual(first.via, "control");

  // Repeated failures do not accumulate into anything worse.
  const second = await app.daemonHealth();
  assert.ok(second, "and it stays up while the probe keeps failing");
  assert.strictEqual(second.boot_id, "b2");

  // A probe that succeeds again still ends the window.
  app.fetch = async () => ({ ok: true, json: async () => ({
    boot_id: "b2", boot: { phase: "ready", done: 10, total: 10 },
  }) });
  app.syncBootProgress((await app.daemonHealth()).boot);
  assert.strictEqual(app.phase(), null, "the window still closes on a good answer");
}

/* The failed-probe answer carries the last boot the probe actually saw.

   Two things here are load-bearing and neither is implied by "the answer is
   not null" above: the probe's answer is remembered when it succeeds, and it
   is handed back when a later probe fails. Drop the remember and the failure
   carries nothing, so syncBootProgress reads "not restoring" and the rail
   drops the restore banner in the middle of a restore; drop the hand-back and
   the same thing happens for a different reason. Each is one line in
   probeFailed, and the check above passes with both of them removed -- which
   is why this one asserts the value rather than its presence (claunch-r35k1,
   leader review of #531). */
async function aFailedProbeHandsBackTheLastBootItSaw() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];
  sock.opened();
  sock.say({
    type: "init", boot_id: "b7", version: "9.9", started_at: "t-7",
    // Init carries a boot, and the page deliberately does not replay it --
    // it is the snapshot of the handshake, not the live phase.
    boot: { phase: "restoring", done: 1, total: 10 },
  });

  // No probe has succeeded yet, so the failure has nothing to hand back.
  // This is the second mutation's assertion: without it a page that invents
  // a boot on the failure path would still pass everything below.
  app.fetch = async () => { throw new Error("the probe timed out"); };
  const before = await app.daemonHealth();
  assert.ok(before, "the socket still vouches for the daemon");
  assert.strictEqual(before.boot, undefined,
    "no probe has answered, so the failure carries no boot");

  // A probe answers with a live boot. The page must remember it. The boot is
  // `restoring`, which is also what keeps the window open -- the only state
  // in which a later failure reaches probeFailed at all, since a closed
  // window is answered by the socket alone.
  app.fetch = async () => ({ ok: true, json: async () => ({
    boot_id: "b7", boot: { phase: "restoring", done: 4, total: 10 },
  }) });
  const good = await app.daemonHealth();
  assert.deepStrictEqual(good.boot, { phase: "restoring", done: 4, total: 10 },
    "the answering probe is what the page reports");

  // Now it fails. The answer must carry the boot the probe last gave -- the
  // same value, not merely some boot.
  app.fetch = async () => { throw new Error("the probe timed out again"); };
  const failed = await app.daemonHealth();
  assert.ok(failed, "the socket still vouches for the daemon");
  assert.strictEqual(failed.boot_id, "b7");
  assert.deepStrictEqual(failed.boot, { phase: "restoring", done: 4, total: 10 },
    "a failed probe hands back the last boot the probe saw");

  // And it keeps doing so while the probe stays down.
  const again = await app.daemonHealth();
  assert.deepStrictEqual(again.boot, { phase: "restoring", done: 4, total: 10 },
    "the remembered boot survives repeated failures");
}

/* A probe that ANSWERS with an error status is a failed probe too
   (claunch-f2ffc, from the leader's review of #531).

   There are two ways the probe fails and they are different lines:
   `fetch` throwing lands in the `catch`, and a response that arrives with
   `ok === false` -- a 502 from the proxy in front of a daemon that is still
   binding, say -- lands on `if (!resp.ok)`. Both mean "the probe could not
   answer", so both must leave the socket's identity standing; a page that
   answers one of them with null goes dark on exactly the restart it was
   taught to survive.

   Every other check in this file reaches the failure through `throw`, which
   is why replacing that line's `probeFailed()` with a bare `null` left the
   harness green. The restoring window is open here for the usual reason:
   past its edge the socket answers alone and the probe is never called. */
async function anErrorStatusIsAFailedProbe() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];
  sock.opened();
  sock.say({
    type: "init", boot_id: "b8", version: "9.9", started_at: "t-8",
    boot: { phase: "restoring", done: 2, total: 10 },
  });

  // The window is open (the init above said restoring), so this tick spends
  // the probe. It answers -- with a status the page must read as failure.
  app.fetch = async () => ({ ok: false, status: 502, json: async () => ({}) });
  const answered = await app.daemonHealth();
  assert.ok(answered, "an error status is not an absent daemon");
  assert.strictEqual(answered.boot_id, "b8",
    "the socket's identity still answers when the probe returns 502");
  assert.strictEqual(answered.via, "control",
    "and it answers as the socket's, not as a probe result");

  // The same must hold for every later tick the window stays open -- a page
  // that recovers on the second try hides the first one going dark.
  const next = await app.daemonHealth();
  assert.ok(next, "and it keeps answering while the probe keeps erroring");
  assert.strictEqual(next.boot_id, "b8");

  // A 502 is not an answer, so it must not close the HTTP-probe window: the
  // next tick must spend the probe again rather than let the socket answer
  // alone. (What the rail shows is syncBootProgress's business and
  // restartboot_check.js measures that; here the question is only whether
  // this tick was read as "the fleet is back".)
  const spent = app.probed;
  await app.daemonHealth();
  assert.strictEqual(app.probed, spent + 1,
    "an error status does not close the window the probe answers in");

  // With no socket to vouch for the daemon the same status is a real
  // "no answer" -- the branch did not change what null means.
  const bare = build();
  bare.fetch = async () => ({ ok: false, status: 502, json: async () => ({}) });
  assert.strictEqual(await bare.daemonHealth(), null,
    "with no socket, an error status is still no answer");
}

/* An `init` that names no daemon (an older daemon) leaves the page with
   nothing to compare, so the probe keeps answering -- the page must not
   invent a boot id, which would read every tick as "same daemon". */
async function anInitWithNoBootIdDoesNotFakeOne() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];
  sock.opened();
  sock.say({ type: "init" });
  assert.strictEqual(await app.daemonHealth(), null);
  assert.strictEqual(app.probed, 1, "the HTTP probe still answers");
}

/* ---- 8. batchFlush prefers the socket and falls back ------------------- */
/* The seam itself: the same paths, two transports, one decision. Driven
   through the real batchFlush with both a working socket and a broken one. */
async function flushPrefersSocketThenFallsBack() {
  function buildFlush(controlRead, httpAnswers = null) {
    const calls = [];
    const scope = {
      calls,
      setTimeout,
      url: (p) => p,
      relogin: async () => false,
      showAuth: () => {},
      fetch: async (u, opts = {}) => {
        calls.push({ url: u, body: opts.body ? JSON.parse(opts.body) : null });
        const paths = JSON.parse(opts.body).paths;
        let answers = {};
        if (httpAnswers) answers = httpAnswers(paths);
        else for (const p of paths) answers[p] = { via: "http", path: p };
        return { ok: true, status: 200, json: async () => ({ answers, errors: {} }) };
      },
    };
    const code = [
      slice("api"),
      slice("budgeted"),
      slice("batchResponse"),
      slice("batchResponses"),
      slice("batchKeptResponse"),
      slice("batchKeep"),
      "const BATCH_KEPT_MAX = 64;",
      "const batchKept = new Map();",
      slice("batchable"),
      slice("batchQueue"),
      slice("batchFlush"),
      "const BATCH_MAX = 24;",
      "const BATCH_WINDOW_MS = 0;",
      "const HTTP_BUDGET = 2;",
      "let httpInFlight = 0;",
      "const httpWaiting = [];",
      "let batchPending = null;",
      "return { api, batchFlush };",
    ].join("\n");
    const make = new Function(
      "url", "fetch", "relogin", "showAuth", "setTimeout", "controlRead", code
    );
    Object.assign(scope, make(
      scope.url, scope.fetch, scope.relogin, scope.showAuth, scope.setTimeout,
      controlRead
    ));
    return scope;
  }

  // The socket answers: no connection is spent at all.
  const asked = [];
  const onSocket = buildFlush(async (paths) => {
    asked.push(paths);
    const answers = {};
    for (const p of paths) answers[p] = { via: "control", path: p };
    return { answers, errors: {} };
  });
  const viaSocket = await Promise.all([
    onSocket.api("/api/sessions"),
    onSocket.api("/api/cflow"),
  ]);
  assert.strictEqual(onSocket.calls.length, 0, "no HTTP request while the socket answers");
  assert.deepStrictEqual(asked, [["/api/sessions", "/api/cflow"]]);
  assert.deepStrictEqual(await viaSocket[0].json(), { via: "control", path: "/api/sessions" });

  // The socket is down: the same reads, unchanged, over HTTP.
  const offSocket = buildFlush(async () => { throw new Error("control socket down"); });
  const viaHttp = await Promise.all([
    offSocket.api("/api/sessions"),
    offSocket.api("/api/cflow"),
  ]);
  assert.strictEqual(offSocket.calls.length, 1, "one batch request, as before");
  assert.strictEqual(offSocket.calls[0].url, "/api/batch");
  assert.deepStrictEqual(offSocket.calls[0].body.paths, ["/api/sessions", "/api/cflow"]);
  assert.deepStrictEqual(await viaHttp[0].json(), { via: "http", path: "/api/sessions" });

  // An error the socket reports for one path is still that path's error, and
  // the other seven reads of a tick are unaffected.
  const partial = buildFlush(async (paths) => ({
    answers: { [paths[0]]: { ok: 1 } },
    errors: { [paths[1]]: "404 Not Found" },
  }));
  const [good, bad] = await Promise.all([
    partial.api("/api/sessions"),
    partial.api("/api/nope"),
  ]);
  assert.strictEqual(good.ok, true);
  assert.strictEqual(bad.ok, false);
  assert.deepStrictEqual(await bad.json(), { error: "404 Not Found" });
  assert.strictEqual(partial.calls.length, 0, "a reported error is not a retry");

  // The route's status rides beside its error text, and the page answers
  // with it instead of a blanket 502 (claunch-authx).
  const refused = buildFlush(async (paths) => ({
    answers: {},
    errors: { [paths[0]]: "llm not configured" },
    statuses: { [paths[0]]: 400 },
  }));
  const unconfigured = await refused.api("/api/sessions/s1/briefing");
  assert.strictEqual(unconfigured.status, 400);
  assert.deepStrictEqual(await unconfigured.json(), { error: "llm not configured" });

  // A refusal of the whole read ("" keyed, no path answered or named) is the
  // transport saying no: the same paths go out over HTTP, instead of each
  // reading "not answered" (claunch-yty47).
  const busy = buildFlush(async () => ({
    answers: {}, errors: { "": "too many reads in flight" },
  }));
  const [s1, s2] = await Promise.all([
    busy.api("/api/sessions"),
    busy.api("/api/cflow"),
  ]);
  assert.strictEqual(busy.calls.length, 1, "the refused read went out over HTTP");
  assert.deepStrictEqual(busy.calls[0].body.paths, ["/api/sessions", "/api/cflow"]);
  assert.strictEqual(s1.ok, true);
  assert.deepStrictEqual(await s2.json(), { via: "http", path: "/api/cflow" });

  // If HTTP leaves a path unanswered too, the refusal is what it shows.
  const silent = buildFlush(async () => ({
    answers: {}, errors: { "": "too many reads in flight" },
  }), () => ({}));
  const unanswered = await silent.api("/api/sessions");
  assert.strictEqual(unanswered.ok, false);
  assert.deepStrictEqual(await unanswered.json(), { error: "too many reads in flight" });

  // A "" beside answers for the paths is not a refusal of the read: the
  // answers stand, and nothing is re-sent.
  const mixed = buildFlush(async (paths) => ({
    answers: { [paths[0]]: { ok: 1 } }, errors: { "": "late note" },
  }));
  const kept = await mixed.api("/api/sessions");
  assert.deepStrictEqual(await kept.json(), { ok: 1 });
  assert.strictEqual(mixed.calls.length, 0);

  // A conditional read (claunch-4thlk.2). The page sends the digest of what
  // it kept for each path; the daemon leaves out an answer whose digest
  // matches and names it in `unchanged`; the page answers from its keep.
  let version = 1;
  const heldSeen = [];
  const conditional = buildFlush(async (paths, held) => {
    heldSeen.push(new Map(held));
    const hash = `h${version}`;
    const out = { answers: {}, errors: {}, hashes: {}, unchanged: [],
                  text: (p) => JSON.stringify({ path: p, version }) };
    for (const p of paths) {
      out.hashes[p] = hash;
      if (held.has(p) && held.get(p).hash === hash) out.unchanged.push(p);
      else out.answers[p] = { path: p, version };
    }
    return out;
  });
  const first = await conditional.api("/api/cflow?view=rail");
  const firstBody = await first.json();
  assert.deepStrictEqual(firstBody, { path: "/api/cflow?view=rail", version: 1 });
  assert.strictEqual(heldSeen[0].size, 0, "nothing held before the first read");
  // The caller changes what it was given (the page does: optimistic
  // overrides on the cached rows). The keep is text, so that cannot leak.
  firstBody.version = "mutated";
  const second = await conditional.api("/api/cflow?view=rail");
  assert.strictEqual(heldSeen[1].get("/api/cflow?view=rail").hash, "h1",
                     "the next read carries the kept digest");
  assert.deepStrictEqual(await second.json(), { path: "/api/cflow?view=rail", version: 1 },
                         "an unchanged answer is the kept one, untouched by the caller");
  version = 2;
  const third = await conditional.api("/api/cflow?view=rail");
  assert.deepStrictEqual(await third.json(), { path: "/api/cflow?view=rail", version: 2 },
                         "a changed answer comes through whole");
  assert.strictEqual(conditional.calls.length, 0, "all of it on the socket");

  // Two callers of one path in one tick each get their own object: the
  // first the parsed answer, the second a copy (claunch-4thlk.4).
  const shared = buildFlush(async (paths) => ({
    answers: { [paths[0]]: { rows: [1] } }, errors: {},
  }));
  const [a, b] = await Promise.all([
    shared.api("/api/sessions"), shared.api("/api/sessions"),
  ]);
  const aBody = await a.json();
  aBody.rows.push(2);
  assert.deepStrictEqual(await b.json(), { rows: [1] },
                         "a change by one caller does not reach the other");
}

/* ---- 9. a large answer in parts (claunch-iss86) ------------------------ */
/* The daemon sends a large read in parts, so a pong or a terminal's output
   is not stuck behind all of it, and sends no more than a window of parts
   ahead of the acknowledgements. So: every read says it takes parts, every
   part is acknowledged at once -- even for a read nobody waits on any more,
   or the window would stay full -- the parts are put back together into the
   answer, and a missing part voids the read instead of parsing a hole. */
async function readsInParts() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];
  sock.opened();
  const reading = app.controlRead(["/api/sessions"]);
  assert.strictEqual(sock.sent[0].parts, true, "the read says it takes parts");
  const id = sock.sent[0].id;
  const whole = JSON.stringify({ type: "read_result", id,
    answers: { "/api/sessions": { rows: ["a", "b"] } }, errors: {}, statuses: {} });
  const cut = [whole.slice(0, 10), whole.slice(10, 25), whole.slice(25)];
  const armed = app.pending().length;
  sock.say({ type: "read_part", id, seq: 0, more: true, data: cut[0] });
  sock.say({ type: "read_part", id, seq: 1, more: true, data: cut[1] });
  assert.deepStrictEqual(sock.sent.slice(1),
    [{ type: "read_ack", id, seq: 0 }, { type: "read_ack", id, seq: 1 }],
    "each part is acknowledged as it lands");
  assert.strictEqual(app.pending().length, armed, "a part restarts the wait, not adds one");
  assert.strictEqual(app.waiting(), 1, "not settled before the last part");
  sock.say({ type: "read_part", id, seq: 2, more: false, data: cut[2] });
  const got = await reading;
  assert.deepStrictEqual(got.answers, { "/api/sessions": { rows: ["a", "b"] } });
  assert.strictEqual(app.waiting(), 0);

  // The read asks for spans and says what it holds (nothing, here); with
  // spans in the reply each answer's own text is cut out of it, not
  // re-encoded (claunch-4thlk.4).
  assert.deepStrictEqual(sock.sent[0].hashes, {});
  assert.strictEqual(sock.sent[0].spans, true);
  assert.strictEqual(got.text("/api/sessions"), null, "no spans, no text");
  const spanned = app.controlRead(["/api/cflow"]);
  const sid = sock.sent.at(-1).id;
  const head = `{"type": "read_result", "id": ${sid}, "errors": {}, "statuses": {}, ` +
    `"hashes": {"/api/cflow": "ab"}, "unchanged": [], "answers": {"/api/cflow": `;
  const body = '{"k": "한글 😀", "n": [1, 2]}';
  const reply = head + body + `}, "spans": {"/api/cflow": [${head.length}, ` +
    `${head.length + body.length}]}}`;
  sock.say({ type: "read_part", id: sid, seq: 0, more: false, data: reply });
  const sgot = await spanned;
  assert.strictEqual(sgot.text("/api/cflow"), body);
  assert.deepStrictEqual(sgot.hashes, { "/api/cflow": "ab" });

  // Parts for a read already given up on are still acknowledged.
  sock.say({ type: "read_part", id: 999, seq: 0, more: true, data: "{" });
  assert.deepStrictEqual(sock.sent.at(-1), { type: "read_ack", id: 999, seq: 0 });

  // A hole in the sequence voids the read.
  const holed = app.controlRead(["/api/cflow"]);
  const hid = sock.sent.at(-1).id;
  sock.say({ type: "read_part", id: hid, seq: 1, more: false, data: "}" });
  await assert.rejects(holed, /lost a part/);
  assert.strictEqual(app.waiting(), 0);
}

/* ---- 10. a socket open with nothing behind it (claunch-fm3ud) ---------- */
async function deadSocketIsRecycled() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];
  sock.opened();
  const reading = app.controlRead(["/api/sessions"]);
  // A dead socket's close() never reaches onclose: the page must not wait
  // for it.
  sock.close = function () { this.closedByPage = true; };
  app.controlRecycle("ping unanswered for 20s");
  await assert.rejects(reading, /control socket closed/, "its read is handed back");
  assert.strictEqual(sock.closedByPage, true, "closed from this side");
  assert.strictEqual(sock.onclose, null, "and its late close is not heard");
  assert.strictEqual(sock.onmessage, null);
  assert.strictEqual(app.controlUp(), false);
  assert.strictEqual(app.sock, null);
  const backoff = JSON.parse(constOf("CONTROL_BACKOFF").replace(/\s+/g, ""));
  assert.deepStrictEqual(app.pending().map((t) => t.ms), [backoff[0]],
    "the reopen is scheduled like any close");
  app.run();
  assert.strictEqual(app.sockets.length, 2, "a new socket");
  // Recycling with nothing open does nothing.
  const none = build();
  none.controlRecycle("x");
  assert.strictEqual(none.sockets.length, 0);
}

async function wakeReopensAtOnce() {
  const app = build();
  app.ensureControlSocket();
  app.sockets[0].opened();
  // Several failures deep: the next try is seconds away.
  for (let i = 0; i < 4; i++) {
    app.sockets.at(-1).died();
    app.run();
  }
  app.sockets.at(-1).died();
  const before = app.sockets.length;
  assert.ok(app.pending().length === 1 && app.pending()[0].ms > 1000, "waiting out a backoff");
  app.controlProbe();
  assert.strictEqual(app.sockets.length, before + 1, "woken: opened now");
  assert.strictEqual(app.pending().length, 0, "the backoff wait is dropped");

  // Still opening: its own open or close decides.
  app.controlProbe();
  assert.strictEqual(app.sockets.length, before + 1);
  assert.strictEqual(app.armed(), 0);

  // Open: the probe ping is armed instead.
  app.sockets.at(-1).opened();
  app.controlProbe();
  assert.strictEqual(app.armed(), 1);
  assert.strictEqual(app.sockets.length, before + 1, "an open socket is not replaced");

  // Before boot ever opened one, a wake-up opens nothing.
  const fresh = build();
  fresh.controlProbe();
  assert.strictEqual(fresh.sockets.length, 0);
}

async function healthProbeIsBounded() {
  const app = build();
  let signal = null;
  app.fetch = (u, opts) => new Promise((resolve, reject) => {
    signal = opts && opts.signal;
    if (signal) signal.addEventListener("abort", () => reject(new Error("aborted")));
  });
  const asking = app.daemonHealth();
  assert.ok(signal, "the probe carries an abort signal");
  const limit = app.pending().find((t) => t.ms === Number(constOf("HEALTH_TIMEOUT_MS")));
  assert.ok(limit, "and a timer to fire it");
  app.run();
  assert.strictEqual(await asking, null, "a hung probe answers 'down'");

  // An answered probe clears its timer.
  const ok = build();
  ok.fetch = async () => ({ ok: true, json: async () => ({ ok: true, boot_id: "b" }) });
  assert.deepStrictEqual(await ok.daemonHealth(), { ok: true, boot_id: "b" });
  assert.strictEqual(ok.pending().length, 0, "nothing left armed");
}

(async () => {
  await readsRideTheSocket();
  await readsInParts();
  await downSocketRejects();
  await deathHandsReadsBack();
  await retryNeverGivesUp();
  await unansweredReadTimesOut();
  await reportsAndPings();
  await livenessComesFromTheSocket();
  await theBootPhaseIsNotFrozenAtInit();
  await aFailedProbeIsNotAnAbsentDaemon();
  await aFailedProbeHandsBackTheLastBootItSaw();
  await anErrorStatusIsAFailedProbe();
  await anInitWithNoBootIdDoesNotFakeOne();
  await flushPrefersSocketThenFallsBack();
  await deadSocketIsRecycled();
  await wakeReopensAtOnce();
  await healthProbeIsBounded();
  console.log("controlsocket_check ok");
})().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exitCode = 1;
});
