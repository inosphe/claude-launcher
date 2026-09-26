/* The control socket over a WebRTC DataChannel (claunch-mhzt4).

   Two halves, both run against the shipped files:

     - static/p2p.js on its own: the framing the daemon's p2p.py also speaks
       (a header byte, fragments of at most 16 KiB), the DirectSocket that
       stands in for a WebSocket, and one Link negotiation against a fake
       RTCPeerConnection -- the offer waits for gathering or its cap, later
       candidates trickle, the nonce is the first thing on the channel, and
       the link is ready only when the daemon's init comes back over it;
     - app.js's side: when to try (only through the tunnel, only when the
       daemon's init offers it), moving onto the channel (controlAdopt), the
       relay socket answering the reads it was already carrying, and the
       fallback when the channel closes mid-read or mid-keystroke -- the read
       is handed back for its HTTP retry, terminals get their 1006, and the
       socket reopens through the relay with the next P2P try on backoff.

   The relay stays the fallback at every step; nothing here may leave the
   page without a control socket road. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const p2pSrc = fs.readFileSync(path.join(STATIC, "p2p.js"), "utf8");

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

/* p2p.js publishes on globalThis; load a private copy per call. */
function loadP2P() {
  const g = {};
  new Function("globalThis", p2pSrc)(g);
  return g.ClaunchP2P;
}

function timers() {
  const list = [];
  return {
    list,
    setTimeout: (fn, ms) => { list.push({ fn, ms, cancelled: false }); return list.length - 1; },
    clearTimeout: (id) => { if (typeof id === "number" && list[id]) list[id].cancelled = true; },
    pending: () => list.filter((t) => !t.cancelled && !t.fired),
    fire: (t) => { t.fired = true; t.fn(); },
  };
}

const tick = () => new Promise((r) => setImmediate(r));

/* ---- a fake DataChannel and RTCPeerConnection -------------------------- */
class FakeDC {
  constructor(label, opts) {
    this.label = label;
    this.opts = opts;
    this.sent = [];
    this.bufferedAmount = 0;
    this.closed = false;
  }
  send(data) { if (this.closed) throw new Error("closed"); this.sent.push(new Uint8Array(data)); }
  close() { if (this.closed) return; this.closed = true; if (this.onclose) this.onclose(); }
  // -- what the far end does --
  opened() { if (this.onopen) this.onopen(); }
  deliver(bytes) { this.onmessage({ data: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) }); }
}

function fakeRTC() {
  const made = [];
  class FakePC {
    constructor(config) {
      this.config = config;
      this.iceGatheringState = "new";
      this.connectionState = "new";
      this.localDescription = null;
      this.remote = null;
      this.closed = false;
      made.push(this);
    }
    createDataChannel(label, opts) { this.dc = new FakeDC(label, opts); return this.dc; }
    async createOffer() { return { type: "offer", sdp: "v=0\r\na=candidate:1 1 udp 1 192.168.0.2 5000 typ host\r\n" }; }
    async setLocalDescription(d) { this.localDescription = d; this.iceGatheringState = "gathering"; }
    async setRemoteDescription(d) { this.remote = d; }
    close() { this.closed = true; }
    // -- what ICE does --
    gathered(extra) {
      if (extra) this.localDescription = { type: "offer", sdp: this.localDescription.sdp + extra };
      this.iceGatheringState = "complete";
      if (this.onicegatheringstatechange) this.onicegatheringstatechange();
    }
    candidate(c) { this.onicecandidate({ candidate: c }); }
    state(s) { this.connectionState = s; if (this.onconnectionstatechange) this.onconnectionstatechange(); }
  }
  return { FakePC, made };
}

function text(bytes) { return new TextDecoder().decode(bytes); }

/* ---- 1. framing round trip --------------------------------------------- */
function framingRoundTrip() {
  const P = loadP2P();
  const small = P.encode("hello");
  assert.strictEqual(small.length, 1);
  assert.strictEqual(small[0][0], 0, "text, last fragment");
  const asm = new P.Reassembler();
  assert.deepStrictEqual(asm.feed(small[0]), { binary: false, data: "hello" });

  // A multibyte string past three fragments: split on bytes, joined whole.
  const big = "가".repeat(20000);                 // 60000 bytes of UTF-8
  const parts = P.encode(big);
  assert.strictEqual(parts.length, 4);
  for (const p of parts) assert.ok(p.length <= P.FRAGMENT + 1, "a fragment fits");
  assert.ok(parts.slice(0, 3).every((p) => p[0] & 2), "more on all but the last");
  assert.strictEqual(parts[3][0] & 2, 0);
  let got = null;
  for (const p of parts) got = asm.feed(p) || got;
  assert.strictEqual(got.data, big);

  const bin = P.encode(Uint8Array.of(0, 1, 2, 255));
  assert.strictEqual(bin[0][0], 1, "binary flag");
  const back = asm.feed(bin[0]);
  assert.strictEqual(back.binary, true);
  assert.deepStrictEqual([...new Uint8Array(back.data)], [0, 1, 2, 255]);

  assert.deepStrictEqual(asm.feed(P.encode("")[0]), { binary: false, data: "" });
  assert.throws(() => asm.feed("text"), /binary/);
  assert.throws(() => asm.feed(new Uint8Array(0)), /empty/);
  asm.feed(Uint8Array.of(2, 65));                 // text, more to come
  assert.throws(() => asm.feed(Uint8Array.of(1, 66)), /kind changed/);
}

/* ---- 2. a Link: offer, trickle, nonce, init ---------------------------- */
async function linkNegotiates() {
  const P = loadP2P();
  const { FakePC, made } = fakeRTC();
  const t = timers();
  const signals = [];
  let ready = null;
  let failed = null;
  const link = new P.Link({
    id: "L1", stun: ["stun:stun.example:3478"], RTC: FakePC,
    setTimeout: t.setTimeout, clearTimeout: t.clearTimeout,
    signal: (f) => { signals.push(f); return true; },
    onReady: (sock) => { ready = sock; },
    onFail: (r) => { failed = r; },
  });
  link.start();
  await tick();
  const pc = made[0];
  assert.deepStrictEqual(pc.config.iceServers, [{ urls: "stun:stun.example:3478" }]);
  assert.strictEqual(pc.dc.label, "claunch-control");
  assert.strictEqual(pc.dc.opts.ordered, true);

  // A candidate before the offer is held, not sent ahead of it.
  pc.candidate({ candidate: "candidate:2 1 udp 1 10.0.0.1 6000 typ host", sdpMid: "0", sdpMLineIndex: 0 });
  assert.strictEqual(signals.length, 0, "nothing before gathering completes");
  pc.gathered("a=candidate:2 1 udp 1 10.0.0.1 6000 typ host\r\n");
  await tick();
  assert.strictEqual(signals[0].type, "p2p_offer");
  assert.strictEqual(signals[0].id, "L1");
  // The held candidate is already in the offer: not sent twice.
  assert.strictEqual(signals.length, 1);

  // Found after the offer: trickled, then the end marker.
  pc.candidate({ candidate: "candidate:3 1 udp 1 1.2.3.4 7000 typ srflx", sdpMid: "0", sdpMLineIndex: 0 });
  pc.candidate(null);
  assert.deepStrictEqual(signals.slice(1).map((f) => f.type), ["p2p_ice", "p2p_ice"]);
  assert.strictEqual(signals[1].candidate.candidate, "candidate:3 1 udp 1 1.2.3.4 7000 typ srflx");
  assert.strictEqual(signals[2].candidate, null);

  // Somebody else's answer is not ours.
  assert.strictEqual(link.handle({ type: "p2p_answer", id: "other", sdp: "x", nonce: "n" }), false);
  assert.strictEqual(link.handle({ type: "p2p_answer", id: "L1", sdp: "v=0 answer", nonce: "N0NCE" }), true);
  await tick();
  assert.strictEqual(pc.remote.sdp, "v=0 answer");

  // The channel opens: the nonce goes first, as text.
  pc.dc.opened();
  assert.strictEqual(pc.dc.sent.length, 1);
  assert.strictEqual(pc.dc.sent[0][0], 0);
  assert.strictEqual(text(pc.dc.sent[0].subarray(1)), "N0NCE");
  assert.strictEqual(ready, null, "open is not ready: the daemon has not answered");

  // Anything but init is not the daemon accepting us.
  pc.dc.deliver(P.encode(JSON.stringify({ type: "pong" }))[0]);
  assert.strictEqual(ready, null);
  pc.dc.deliver(P.encode(JSON.stringify({ type: "init", v: 1 }))[0]);
  assert.ok(ready, "ready on init");
  assert.strictEqual(ready.transport, "p2p");
  assert.strictEqual(ready.readyState, 1);
  assert.strictEqual(failed, null);
  assert.strictEqual(t.pending().length, 0, "the ready timeout is disarmed");

  // Ready, and ICE fails: the socket reports closed at once with 1006.
  let closed = null;
  ready.onclose = (ev) => { closed = ev.code; };
  pc.state("failed");
  assert.strictEqual(closed, 1006);
  assert.strictEqual(failed, null, "onFail is never called after ready");
}

/* ---- 3. a Link fails once, and says bye -------------------------------- */
async function linkFails() {
  const P = loadP2P();
  // The daemon refuses: one failure, one bye, the pc closed.
  {
    const { FakePC, made } = fakeRTC();
    const t = timers();
    const signals = [];
    const fails = [];
    const link = new P.Link({ id: "L2", RTC: FakePC, setTimeout: t.setTimeout,
      clearTimeout: t.clearTimeout, signal: (f) => { signals.push(f); return true; },
      onFail: (r) => fails.push(r) });
    link.start();
    await tick();
    made[0].gathered();
    await tick();
    link.handle({ type: "p2p_error", id: "L2", error: "too many peers" });
    link.handle({ type: "p2p_error", id: "L2", error: "again" });
    made[0].state("failed");
    assert.deepStrictEqual(fails, ["daemon: too many peers"]);
    assert.ok(signals.some((f) => f.type === "p2p_bye" && f.id === "L2"));
    assert.ok(made[0].closed);
  }
  // Gathering never completes: the cap sends the offer anyway.
  {
    const { FakePC, made } = fakeRTC();
    const t = timers();
    const signals = [];
    const link = new P.Link({ id: "L3", RTC: FakePC, setTimeout: t.setTimeout,
      clearTimeout: t.clearTimeout, signal: (f) => { signals.push(f); return true; } });
    link.start();
    await tick();
    const cap = t.pending().find((x) => x.ms === 1500);
    assert.ok(cap, "a 1500ms gathering cap");
    t.fire(cap);
    await tick();
    assert.strictEqual(signals[0].type, "p2p_offer");
    // And no channel in time: the ready timeout fails the link.
    let why = null;
    link.onFail = (r) => { why = r; };
    t.fire(t.pending().find((x) => x.ms === 15000));
    assert.strictEqual(why, "no channel in time");
    assert.ok(made[0].closed);
  }
  // The relay went away before the offer: fail, do not wait.
  {
    const { FakePC, made } = fakeRTC();
    const t = timers();
    let why = null;
    const link = new P.Link({ id: "L4", RTC: FakePC, setTimeout: t.setTimeout,
      clearTimeout: t.clearTimeout, signal: () => false, onFail: (r) => { why = r; } });
    link.start();
    await tick();
    made[0].gathered();
    await tick();
    assert.strictEqual(why, "relay socket down");
  }
}

/* ---- 4. DirectSocket behaves as the control socket's WebSocket --------- */
function directSocket() {
  const P = loadP2P();
  const dc = new FakeDC("claunch-control", {});
  const sock = new P.DirectSocket(dc);
  assert.throws(() => sock.send("x"), /not open/, "CONNECTING throws, as a WebSocket does");
  sock._open();
  const got = [];
  sock.onmessage = (ev) => got.push(ev.data);
  sock.send("x".repeat(40000));
  assert.strictEqual(dc.sent.length, 3, "sent in fragments");
  const back = new P.Reassembler();
  let whole = null;
  for (const m of dc.sent) whole = back.feed(m) || whole;
  assert.strictEqual(whole.data.length, 40000);

  for (const part of P.encode(Uint8Array.of(9, 9))) dc.deliver(part);
  assert.ok(got[0] instanceof ArrayBuffer, "binary frames arrive as ArrayBuffer");

  // A frame the framing rejects closes the socket: nothing half-read is used.
  let code = null;
  sock.onclose = (ev) => { code = ev.code; };
  dc.onmessage({ data: new ArrayBuffer(0) });
  assert.strictEqual(code, 1006);
  assert.strictEqual(sock.readyState, 3);
  assert.ok(dc.closed);
}

/* ---- the app.js harness ------------------------------------------------ */
function build(opts = {}) {
  const P = loadP2P();
  const t = timers();
  const sockets = [];
  const links = [];
  const channelEnds = [];
  const logs = [];

  class FakeWebSocket {
    constructor(u) { this.url = u; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(s) { if (this.readyState !== 1) throw new Error("not open"); this.sent.push(JSON.parse(s)); }
    close() { if (this.readyState === 3) return; this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
    opened() { this.readyState = 1; if (this.onopen) this.onopen(); }
    say(f) { if (this.onmessage) this.onmessage({ data: JSON.stringify(f) }); }
    died() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1006 }); }
  }
  FakeWebSocket.OPEN = 1;

  /* The Link is p2p.js's (checked above); here it is a stand-in the checks
     drive, so what is tested is app.js's use of it. */
  class FakeLink {
    constructor(o) { this.o = o; this.handled = []; this.closed = false; links.push(this); }
    start() { this.started = true; this.o.signal({ type: "p2p_offer", id: "X", sdp: "s" }); }
    handle(msg) { this.handled.push(msg); }
    close() { this.closed = true; }
    // -- what the negotiation does --
    ready() {
      const dc = new FakeDC("claunch-control", {});
      const sock = new P.DirectSocket(dc);
      sock._open();
      this.dc = dc;
      this.sock = sock;
      this.o.onReady(sock, this);
      return sock;
    }
    fail(r) { this.o.onFail(r, this); }
  }
  const ClaunchP2P = { ...P, Link: FakeLink };

  const code = [
    `const BASE = ${JSON.stringify(opts.base || "/t/abc/")};`,
    `const CONTROL_BACKOFF = ${constOf("CONTROL_BACKOFF")};`,
    `const CONTROL_READ_TIMEOUT_MS = ${constOf("CONTROL_READ_TIMEOUT_MS")};`,
    `const P2P_FIRST_MS = ${constOf("P2P_FIRST_MS")};`,
    `const P2P_RETRY = ${constOf("P2P_RETRY")};`,
    "let controlSock = null;",
    "let controlTry = 0;",
    "let controlTimer = null;",
    "let controlSeq = 0;",
    "const controlWaiting = new Map();",
    "let p2pLink = null;",
    "let p2pConfig = null;",
    "let p2pTry = 0;",
    "let p2pTimer = null;",
    "const controlRetired = new Set();",
    "const channelLinks = new Map();",
    ...["controlUp", "controlAbort", "openControlSocket", "controlMessage", "controlClosed",
        "p2pTunnel", "p2pInit", "p2pSchedule", "p2pAttempt", "p2pSignal", "p2pFailed",
        "controlAdopt", "controlRetire", "controlRetiredMessage", "controlRetireCheck",
        "scheduleControlReopen", "ensureControlSocket", "controlRead", "controlAnswer",
        "controlPart", "channelsCarrierGone"].map(slice),
    "let latencyStarts = 0;",
    "function latencyStart() { latencyStarts++; }",
    "function latencyStop() {}",
    "function latencyPong() {}",
    "function channelBinary() {}",
    "function channelFrame() {}",
    "return { ensureControlSocket, controlRead, controlUp, channelLinks,"
    + " get sock() { return controlSock; }, get link() { return p2pLink; },"
    + " get p2pTry() { return p2pTry; }, get controlTry() { return controlTry; },"
    + " get latencyStarts() { return latencyStarts; },"
    + " retired: () => controlRetired.size, waiting: () => controlWaiting.size };",
  ].join("\n");

  const make = new Function(
    "WebSocket", "RTCPeerConnection", "globalThis", "url", "location",
    "setTimeout", "clearTimeout", "console", code);
  const app = make(
    FakeWebSocket,
    opts.noRTC ? undefined : function RTCPeerConnection() {},
    { ClaunchP2P },
    (p) => p,
    { protocol: "https:", host: "relay.example" },
    t.setTimeout, t.clearTimeout,
    { info: (m) => logs.push(m) });
  return { app, t, sockets, links, channelEnds, logs };
}

const INIT = { type: "init", p2p: { stun: ["stun:stun.example:3478"] } };

/* Relay socket up, the daemon's init offers P2P, the first try fires. */
function relayWithLink(opts) {
  const h = build(opts);
  h.app.ensureControlSocket();
  const relay = h.sockets[0];
  relay.opened();
  relay.say(INIT);
  const first = h.t.pending().find((x) => x.ms === 1000);
  assert.ok(first, "the first try waits P2P_FIRST_MS");
  h.t.fire(first);
  return { ...h, relay };
}

/* ---- 5. when to try ---------------------------------------------------- */
function whenToTry() {
  // Through the tunnel with the daemon offering: one link, signalled on the relay.
  const h = relayWithLink();
  assert.strictEqual(h.links.length, 1);
  assert.deepStrictEqual(h.links[0].o.stun, ["stun:stun.example:3478"]);
  assert.strictEqual(h.relay.sent[0].type, "p2p_offer", "signalling rides the relay socket");
  // A second init (the relay reconnecting) does not start a second link.
  h.relay.say(INIT);
  assert.strictEqual(h.t.pending().filter((x) => x.ms === 1000).length, 0);
  assert.strictEqual(h.links.length, 1);
  // Answers go to the link.
  h.relay.say({ type: "p2p_answer", id: "X", sdp: "a", nonce: "n" });
  assert.strictEqual(h.links[0].handled[0].type, "p2p_answer");

  // The page reached directly (not /t/<name>/): the relay is not on the path.
  for (const opts of [{ base: "/" }, { noRTC: true }]) {
    const d = build(opts);
    d.app.ensureControlSocket();
    d.sockets[0].opened();
    d.sockets[0].say(INIT);
    assert.strictEqual(d.t.pending().filter((x) => x.ms === 1000).length, 0,
      "no try: " + JSON.stringify(opts));
  }
  // The daemon without aiortc: its init has no p2p block.
  const n = build();
  n.app.ensureControlSocket();
  n.sockets[0].opened();
  n.sockets[0].say({ type: "init" });
  assert.strictEqual(n.t.pending().filter((x) => x.ms === 1000).length, 0);
}

/* ---- 6. moving onto the channel ---------------------------------------- */
async function adoptKeepsReads() {
  const h = relayWithLink();
  // A read already on the relay, and a terminal attached to it.
  const reading = h.app.controlRead(["/api/sessions"]);
  const readId = h.relay.sent[h.relay.sent.length - 1].id;
  const ends = [];
  h.app.channelLinks.set(1, { _ended: (code) => ends.push(code) });

  const direct = h.links[0].ready();
  assert.strictEqual(h.app.sock, direct, "the DataChannel is the control socket");
  assert.deepStrictEqual(ends, [1006], "terminals reattach on the new road");
  assert.strictEqual(h.app.latencyStarts, 2, "the badge restarts on the new road");
  assert.strictEqual(h.app.retired(), 1);
  assert.notStrictEqual(h.relay.readyState, 3, "the relay stays until its read is answered");

  // New reads ride the channel.
  const next = h.app.controlRead(["/api/cflow"]);
  const onDc = JSON.parse(text(h.links[0].dc.sent[0].subarray(1)));
  assert.strictEqual(onDc.type, "read");

  // The relay's read is answered where it was asked, in parts with acks.
  h.relay.say({ type: "read_part", id: readId, seq: 0, more: true, data: '{"type":"read_result",' });
  h.relay.say({ type: "read_part", id: readId, seq: 1, more: false,
    data: `"id":${readId},"answers":{"/api/sessions":[7]},"errors":{}}` });
  assert.deepStrictEqual((await reading).answers, { "/api/sessions": [7] });
  assert.ok(h.relay.sent.some((f) => f.type === "read_ack" && f.seq === 1), "parts acked on the relay");
  assert.strictEqual(h.relay.readyState, 3, "retired relay closed once nothing waits on it");
  assert.strictEqual(h.app.retired(), 0);
  assert.strictEqual(h.app.sock, direct, "closing the retired relay changes nothing");

  // Parts and acks over the DataChannel too.
  for (const f of [
    { type: "read_part", id: onDc.id, seq: 0, more: true, data: '{"type":"read_result",' },
    { type: "read_part", id: onDc.id, seq: 1, more: false,
      data: `"id":${onDc.id},"answers":{"/api/cflow":1},"errors":{}}` },
  ]) for (const part of loadP2P().encode(JSON.stringify(f))) h.links[0].dc.deliver(part);
  assert.deepStrictEqual((await next).answers, { "/api/cflow": 1 });
  const acks = h.links[0].dc.sent.slice(1).map((m) => JSON.parse(text(m.subarray(1))));
  assert.deepStrictEqual(acks.map((a) => [a.type, a.seq]), [["read_ack", 0], ["read_ack", 1]]);

  // A frame from the retired relay that is not a read answer counts for nothing.
  h.relay.say({ type: "pong" });
  assert.strictEqual(h.app.sock, direct);
}

/* ---- 7. the channel closes mid-read and mid-keystroke ------------------ */
async function fallbackWhenChannelCloses() {
  const h = relayWithLink();
  const direct = h.links[0].ready();
  h.relay.say({ type: "noop" });            // retired and idle: already closed
  assert.strictEqual(h.relay.readyState, 3);

  const reading = h.app.controlRead(["/api/sessions"]);
  const typed = [];
  h.app.channelLinks.set(2, { _ended: (code) => typed.push(code) });
  const socketsBefore = h.sockets.length;

  h.links[0].dc.close();                    // the DataChannel goes
  await assert.rejects(reading, /control socket closed/, "the read is handed back for HTTP");
  assert.deepStrictEqual(typed, [1006], "a terminal mid-keystroke gets its 1006 and queues");
  assert.strictEqual(h.app.sock, null);
  assert.strictEqual(h.app.link, null);
  assert.strictEqual(h.app.p2pTry, 1);
  assert.ok(h.logs.some((m) => /relay continues/.test(m)));

  // Straight back to the relay: the first reopen, no backoff owed.
  assert.strictEqual(h.app.controlTry, 1, "the reopen took the first backoff step");
  const reopen = h.t.pending().find((x) => x.ms === JSON.parse(constOf("CONTROL_BACKOFF").replace(/\s+/g, ""))[0]);
  assert.ok(reopen, "the relay reopen is scheduled at the first backoff");
  h.t.fire(reopen);
  assert.strictEqual(h.sockets.length, socketsBefore + 1);
  const relay2 = h.sockets[h.sockets.length - 1];
  relay2.opened();
  assert.strictEqual(h.app.sock, relay2);
  assert.strictEqual(direct.readyState, 3);

  // The next P2P try waits its backoff, not P2P_FIRST_MS.
  relay2.say(INIT);
  const retry = h.t.pending().filter((x) => x.ms === 10000);
  assert.strictEqual(retry.length, 1, "the retry after a failure is P2P_RETRY[0]");
  assert.strictEqual(h.t.pending().filter((x) => x.ms === 1000).length, 0);
}

/* ---- 8. a failed negotiation leaves the relay alone -------------------- */
function failedNegotiation() {
  const h = relayWithLink();
  const reading = h.app.controlRead(["/api/sessions"]);
  h.links[0].fail("ice failed");
  assert.strictEqual(h.app.sock, h.relay, "still on the relay");
  assert.strictEqual(h.relay.readyState, 1);
  assert.strictEqual(h.app.waiting(), 1, "the relay's read is untouched");
  assert.ok(h.links[0].closed);
  assert.strictEqual(h.app.p2pTry, 1);
  const retries = h.t.pending().filter((x) => x.ms === 10000);
  assert.strictEqual(retries.length, 1);
  h.t.fire(retries[0]);
  assert.strictEqual(h.links.length, 2, "tried again after its backoff");
  // The second fails too: the wait doubles.
  h.links[1].fail("again");
  assert.strictEqual(h.t.pending().filter((x) => x.ms === 20000).length, 1);
  reading.catch(() => {});

  // A link that becomes ready after the relay it signalled on is gone is
  // closed, not adopted: there is nothing to retire and a new relay owns
  // the page.
  const g = relayWithLink();
  const stale = g.links[0];
  g.relay.died();
  const sock = stale.ready();
  assert.ok(stale.closed, "a late link is closed");
  assert.notStrictEqual(g.app.sock, sock);
}

/* ---- 9. the badge does not split a road the relay is not on ------------ */
function badgeSplitOff() {
  const code = [
    "let latencySamples = [600];",
    "let latencyDaemon = 5;",
    "let latencyVia = null;",
    slice("latencyTunnelSides"),
    "return { sides: latencyTunnelSides, set via(v) { latencyVia = v; } };",
  ].join("\n");
  const l = new Function(code)();
  const relay = { ms: 400, stalled: false };
  assert.deepStrictEqual(l.sides({ stalled: false }, relay),
    { total: 600, daemonSide: 405, browserSide: 195 });
  l.via = "p2p";
  assert.strictEqual(l.sides({ stalled: false }, relay), null,
    "over a DataChannel the relay's round trip is not subtracted");
  // And the pong is what sets it: only a pong the daemon marked.
  assert.match(slice("latencyPong"), /msg\.via === "p2p" \? "p2p" : null/);
}

(async () => {
  framingRoundTrip();
  await linkNegotiates();
  await linkFails();
  directSocket();
  whenToTry();
  await adoptKeepsReads();
  await fallbackWhenChannelCloses();
  failedNegotiation();
  badgeSplitOff();
  console.log("p2p_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
