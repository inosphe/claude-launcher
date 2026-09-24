/* The link latency badge (claunch-8ufey).

   "A session is slow to answer" can be spent in two places the page cannot
   tell apart: browser -> daemon (through the relay tunnel when the page is
   served as /t/<name>/), and the daemon's own uplink to the relay. The page
   pings the daemon over the control socket with its own clock and the
   daemon echoes it, carrying the relay status beside the echo; the badge in
   the rail header shows both.

   What this file pins:
     - one ping in flight at a time, the next one only after the interval;
     - an echo turns into a sample; a bare pong or a stale `t` does not;
     - an unanswered ping outgrowing the last sample is shown as ">=" its
       age, so a stall reads as a stall and not as the last good number;
     - the relay half is the worst connected uplink, and a relay with an
       unanswered PING shows that wait;
     - the colour follows the worse of the two;
     - with the socket down the badge says so instead of a stale number.

   The real functions are sliced out of the shipped app.js. */
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
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

function constOf(name) {
  const m = new RegExp(`const ${name} = ([^;]+);`).exec(src);
  if (!m) throw new Error("missing const " + name);
  return `const ${name} = ${m[1]};`;
}

function build({ base = "/" } = {}) {
  const badge = {
    textContent: "", title: "", className: "badge hidden",
    classList: {
      add(c) { if (!badge.className.split(" ").includes(c)) badge.className += " " + c; },
      remove(c) { badge.className = badge.className.split(" ").filter((x) => x !== c).join(" "); },
    },
  };
  const scope = {
    clock: 1000,
    up: true,
    said: [],
    relayBadges: [],
    badge,
  };
  const code = [
    constOf("LATENCY_PING_MS"),
    constOf("LATENCY_TICK_MS"),
    constOf("LATENCY_KEEP"),
    constOf("LATENCY_SLOW_MS"),
    constOf("LATENCY_BAD_MS"),
    "let latencyTimer = null;",
    "let latencySentAt = null;",
    "let latencyLastPing = -Infinity;",
    "let latencySamples = [];",
    "let latencyRelay = null;",
    "function latencyNow() { return S.clock; }",
    slice("latencyStart"),
    slice("latencyStop"),
    slice("latencyTick"),
    slice("latencyPong"),
    slice("fmtLatency"),
    slice("latencyWeb"),
    slice("latencyRelayWorst"),
    slice("latencyGrade"),
    slice("renderLatencyBadge"),
    "return { latencyStart, latencyStop, latencyTick, latencyPong, fmtLatency,"
    + " samples: () => latencySamples, inFlight: () => latencySentAt };",
  ].join("\n");
  const make = new Function(
    "S", "$", "BASE", "controlUp", "controlSay", "renderRelayBadge",
    "setInterval", "clearInterval", code
  );
  Object.assign(scope, make(
    scope,
    (id) => (id === "latency-badge" ? badge : null),
    base,
    () => scope.up,
    (frame) => { if (!scope.up) return false; scope.said.push(frame); return true; },
    (relay) => scope.relayBadges.push(relay),
    () => 1,
    () => {}
  ));
  return scope;
}

const pings = (app) => app.said.filter((f) => f.type === "ping");

/* ---- 1. one ping in flight, the next after the interval ----------------- */
function pingCadence() {
  const app = build();
  app.latencyStart();
  assert.strictEqual(pings(app).length, 1, "the first ping goes at once");
  assert.strictEqual(pings(app)[0].t, 1000, "carrying the page's clock");

  app.clock += 7000;
  app.latencyTick();
  assert.strictEqual(pings(app).length, 1, "none while one is unanswered");

  app.latencyPong({ type: "pong", t: 1000 });
  app.latencyTick();
  assert.strictEqual(pings(app).length, 2, "interval passed: the next one goes");

  app.latencyPong({ type: "pong", t: pings(app)[1].t });
  app.clock += 1000;
  app.latencyTick();
  assert.strictEqual(pings(app).length, 2, "not before the interval");
}

/* ---- 2. echoes become samples; bare and stale pongs do not -------------- */
function echoesBecomeSamples() {
  const app = build();
  app.latencyStart();
  app.clock += 42;
  app.latencyPong({ type: "pong" });
  assert.strictEqual(app.samples().length, 0, "a bare pong measures nothing");
  app.latencyPong({ type: "pong", t: 12 });
  assert.strictEqual(app.samples().length, 0, "nor one for another ping");
  app.latencyPong({ type: "pong", t: 1000 });
  assert.deepStrictEqual(app.samples(), [42]);
  assert.strictEqual(app.inFlight(), null);
  assert.strictEqual(app.badge.textContent, "web 42ms");
  assert.match(app.badge.className, /latency-ok/);
  assert.doesNotMatch(app.badge.className, /hidden/);
  assert.match(app.badge.title, /browser ↔ daemon \(direct\): last 42ms/);
  assert.match(app.badge.title, /daemon ↔ relay: no relay configured/);

  // Only the last LATENCY_KEEP samples are kept.
  for (let i = 0; i < 20; i++) {
    app.clock += 5000;
    app.latencyTick();
    app.clock += 10;
    app.latencyPong({ type: "pong", t: pings(app).at(-1).t });
  }
  assert.strictEqual(app.samples().length, 12);
}

/* ---- 3. a stall reads as a stall ---------------------------------------- */
function stallShowsItsAge() {
  const app = build();
  app.latencyStart();
  app.clock += 30;
  app.latencyPong({ type: "pong", t: 1000 });
  app.clock += 5000;
  app.latencyTick();            // ping 2 goes out
  app.clock += 20;
  app.latencyTick();
  assert.strictEqual(app.badge.textContent, "web 30ms",
                     "younger than the last sample: still the sample");
  app.clock += 2480;
  app.latencyTick();
  assert.strictEqual(app.badge.textContent, "web ≥2.5s");
  assert.match(app.badge.className, /latency-bad/);
  assert.match(app.badge.title, /waiting 2\.5s for an answer/);
}

/* ---- 4. the relay half -------------------------------------------------- */
function relayHalf() {
  const app = build({ base: "/t/d09/" });
  app.latencyStart();
  app.clock += 20;
  const relay = {
    configured: true, connected: true,
    relays: [
      { id: "work", connected: true, rtt_ms: 180.4, rtt_age: 2.1, pending_ms: null },
      { id: "home", connected: true, rtt_ms: 60, rtt_age: 1, pending_ms: null },
      { id: "old", connected: false, rtt_ms: null, rtt_age: null, pending_ms: null },
    ],
  };
  app.latencyPong({ type: "pong", t: 1000, relay });
  assert.strictEqual(app.relayBadges.length, 1, "the relay badge is refreshed too");
  assert.strictEqual(app.badge.textContent, "web 20ms · relay 180ms",
                     "the worst connected relay");
  assert.match(app.badge.className, /latency-slow/, "the worse half colours it");
  assert.match(app.badge.title, /through the relay tunnel/);
  assert.match(app.badge.title, /daemon ↔ relay work: 180ms, 2s ago/);
  assert.match(app.badge.title, /daemon ↔ relay old: disconnected/);

  app.clock += 5000;
  app.latencyTick();
  app.clock += 20;
  relay.relays[1] = { id: "home", connected: true, rtt_ms: 60, rtt_age: 9,
                      pending_ms: 8000 };
  app.latencyPong({ type: "pong", t: pings(app).at(-1).t, relay });
  assert.strictEqual(app.badge.textContent, "web 20ms · relay ≥8.0s");
  assert.match(app.badge.className, /latency-bad/);
  assert.match(app.badge.title, /home: no answer for 8\.0s \(last 60ms\)/);

  relay.relays = relay.relays.map((r) => ({ ...r, rtt_ms: null, pending_ms: null }));
  app.clock += 5000;
  app.latencyTick();
  app.latencyPong({ type: "pong", t: pings(app).at(-1).t, relay });
  assert.strictEqual(app.badge.textContent, "web 0ms · relay —", "no sample yet");
  assert.match(app.badge.title, /work: measuring…/);
}

/* ---- 5. the socket going down ------------------------------------------- */
function socketDown() {
  const app = build();
  app.up = false;
  app.latencyStop();
  assert.match(app.badge.className, /hidden/, "never measured: nothing to show");

  app.up = true;
  app.latencyStart();
  app.clock += 15;
  app.latencyPong({ type: "pong", t: 1000 });
  app.up = false;
  app.latencyStop();
  assert.strictEqual(app.badge.textContent, "web down");
  assert.match(app.badge.className, /latency-bad/);
  assert.match(app.badge.title, /control socket is down/);
}

function formats() {
  const app = build();
  assert.strictEqual(app.fmtLatency(null), "—");
  assert.strictEqual(app.fmtLatency(4.4), "4ms");
  assert.strictEqual(app.fmtLatency(1234), "1.2s");
  assert.strictEqual(app.fmtLatency(12345), "12s");
}

pingCadence();
echoesBecomeSamples();
stallShowsItsAge();
relayHalf();
socketDown();
formats();
console.log("latency_check ok");
