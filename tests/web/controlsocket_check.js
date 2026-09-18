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
     - liveness is answered from the socket only while it is open.

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
    "const controlWaiting = new Map();",
    slice("controlUp"),
    slice("controlAbort"),
    slice("openControlSocket"),
    slice("scheduleControlReopen"),
    slice("ensureControlSocket"),
    slice("controlRead"),
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
    slice("daemonHealth"),
    "return { controlUp, openControlSocket, ensureControlSocket, controlRead,"
    + " controlSay, daemonHealth, waiting: () => controlWaiting.size,"
    + " get sock() { return controlSock; } };",
  ].join("\n");

  const make = new Function(
    "WebSocket", "url", "location", "setTimeout", "clearTimeout", "fetch", code
  );
  Object.assign(scope, make(
    FakeWebSocket, scope.url, scope.location, scope.setTimeout, scope.clearTimeout,
    async () => { scope.probed = (scope.probed || 0) + 1; throw new Error("no http"); }
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
   harness, so a null answer proves the HTTP path was the one taken. */
async function livenessComesFromTheSocket() {
  const app = build();
  app.ensureControlSocket();
  const sock = app.sockets[0];

  assert.strictEqual(await app.daemonHealth(), null, "not open: HTTP answers");
  assert.strictEqual(app.probed, 1, "and the probe was actually sent");

  sock.opened();
  const health = await app.daemonHealth();
  assert.deepStrictEqual(health, { ok: true, via: "control" });
  assert.strictEqual(app.probed, 1, "no connection spent while the socket is up");

  sock.died();
  assert.strictEqual(await app.daemonHealth(), null);
  assert.strictEqual(app.probed, 2, "and the probe comes back when it is needed");
}

/* ---- 8. batchFlush prefers the socket and falls back ------------------- */
/* The seam itself: the same paths, two transports, one decision. Driven
   through the real batchFlush with both a working socket and a broken one. */
async function flushPrefersSocketThenFallsBack() {
  function buildFlush(controlRead) {
    const calls = [];
    const scope = {
      calls,
      setTimeout,
      url: (p) => p,
      relogin: async () => false,
      showAuth: () => {},
      fetch: async (u, opts = {}) => {
        calls.push({ url: u, body: opts.body ? JSON.parse(opts.body) : null });
        const answers = {};
        for (const p of JSON.parse(opts.body).paths) answers[p] = { via: "http", path: p };
        return { ok: true, status: 200, json: async () => ({ answers, errors: {} }) };
      },
    };
    const code = [
      slice("api"),
      slice("budgeted"),
      slice("batchResponse"),
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
}

(async () => {
  await readsRideTheSocket();
  await downSocketRejects();
  await deathHandsReadsBack();
  await retryNeverGivesUp();
  await unansweredReadTimesOut();
  await reportsAndPings();
  await livenessComesFromTheSocket();
  await flushPrefersSocketThenFallsBack();
  console.log("controlsocket_check ok");
})().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exitCode = 1;
});
