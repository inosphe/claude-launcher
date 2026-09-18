/* The page's terminals, carried on the control socket.

   A WebSocket costs a browser one of its per-server connections — six in
   Firefox — and the dashboard opened one per session it was looking at, so
   the count grew as a person moved between sessions and the next upgrade
   waited in the browser's own connection queue, unsent and invisible to the
   daemon (claunch-gh4f). ChannelLink carries a terminal over the control
   socket instead, presenting the part of WebSocket the terminal code uses,
   so openSocket, suspendActive and shimFrame are unchanged.

   What this file pins is the client half of that framing, and the fallback
   that keeps one socket from becoming the single point the page dies at:

     - a binary send carries two bytes of channel id in front of the payload,
       matching daemon/channel.py;
     - a text send carries the id inside the JSON;
     - frames come back to the link that asked, with the tag removed;
     - a channel that ends is reported to its own onclose and nothing else;
     - the control socket going reports 1006 to every link on it, which is
       what the retry machine already knows how to answer;
     - with no control socket up, openChannel returns null, which is what
       sends openSocket down its own-socket path.

   The real functions are sliced out of the shipped app.js. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function sliceFrom(head) {
  const start = src.indexOf(head);
  if (start < 0) throw new Error("missing " + head);
  const body = src.indexOf("{", start);
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + head);
}

function constOf(name) {
  const m = new RegExp(`const ${name} = ([^;]+);`).exec(src);
  if (!m) throw new Error("missing const " + name);
  return m[1];
}

/* ---- the harness ------------------------------------------------------- */
function build() {
  const sent = [];
  const sock = {
    readyState: 1,
    send: (data) => sent.push(data),
  };
  const scope = { sent, sock, up: true };

  const code = [
    "let controlSock = sockRef;",
    "function controlUp() { return up(); }",
    `const CHANNEL_HEADER = ${constOf("CHANNEL_HEADER")};`,
    "const channelLinks = new Map();",
    "let channelSeq = 0;",
    sliceFrom("class ChannelLink {"),
    sliceFrom("function openChannel(name, opts) {"),
    sliceFrom("function channelBinary(buf) {"),
    sliceFrom("function channelFrame(msg) {"),
    sliceFrom("function channelsCarrierGone() {"),
    "return { openChannel, channelBinary, channelFrame, channelsCarrierGone,"
    + " links: () => channelLinks.size };",
  ].join("\n");

  const make = new Function("WebSocket", "sockRef", "up", "Date", code);
  Object.assign(scope, make(
    { OPEN: 1, CONNECTING: 0, CLOSED: 3 },
    sock,
    () => scope.up,
    Date,
  ));
  return scope;
}

/* Bring a link up the way the daemon does: the attach goes out, the daemon
   answers `attached`, and only then is the link open. */
function attached(w, name) {
  const link = w.openChannel(name, { scrollback: true });
  const ask = JSON.parse(w.sent.pop());
  w.channelFrame({ type: "attached", ch: ask.ch, session: name });
  return { link, ch: ask.ch, ask };
}

/* ---- the checks -------------------------------------------------------- */

function attachAsksForTheSession() {
  const w = build();
  const { link, ask } = attached(w, "s7");
  assert.strictEqual(ask.type, "attach");
  assert.strictEqual(ask.session, "s7");
  assert.strictEqual(ask.scrollback, true,
    "the page is the client that wants the scrollback seed");
  assert.strictEqual(link.readyState, 1, "attached is what opens the link");
  assert.ok(link.linkOpenedAt, "the timing mark the link chip reads");
}

function binarySendsCarryTheChannelId() {
  const w = build();
  const { link, ch } = attached(w, "s7");
  link.send(new TextEncoder().encode("hi"));
  const frame = new Uint8Array(w.sent.pop());
  assert.strictEqual((frame[0] << 8) | frame[1], ch,
    "two big-endian bytes of channel id, as daemon/channel.py unpacks them");
  assert.strictEqual(Buffer.from(frame.slice(2)).toString(), "hi",
    "the payload rides verbatim — no base64, no re-encoding");
}

function textSendsCarryTheChannelIdInside() {
  const w = build();
  const { link, ch } = attached(w, "s7");
  link.send(JSON.stringify({ type: "resize", cols: 80, rows: 24 }));
  const frame = JSON.parse(w.sent.pop());
  assert.strictEqual(frame.ch, ch);
  assert.strictEqual(frame.cols, 80);
}

function framesComeBackToTheLinkThatAskedForThem() {
  const w = build();
  const a = attached(w, "s7");
  const b = attached(w, "s8");
  assert.notStrictEqual(a.ch, b.ch, "two live terminals, two ids");

  const got = { a: [], b: [] };
  a.link.onmessage = (ev) => got.a.push(ev.data);
  b.link.onmessage = (ev) => got.b.push(ev.data);

  w.channelFrame({ type: "state", status: "busy", ch: b.ch });
  assert.strictEqual(got.a.length, 0, "one terminal's frame reached another");
  assert.deepStrictEqual(JSON.parse(got.b[0]), { type: "state", status: "busy" },
    "the tag is taken off before the terminal code sees the frame");

  const out = new Uint8Array([a.ch >> 8, a.ch & 0xff, 111, 107]);
  w.channelBinary(out.buffer);
  assert.strictEqual(Buffer.from(got.a[0]).toString(), "ok");
  assert.strictEqual(got.b.length, 1, "output landed on the wrong terminal");
}

function detachEndsOneChannelAndSaysSo() {
  const w = build();
  const { link, ch } = attached(w, "s7");
  let closed = null;
  link.onclose = (ev) => { closed = ev; };
  link.close();
  const said = JSON.parse(w.sent.pop());
  assert.deepStrictEqual(said, { type: "detach", ch },
    "the daemon is told, so the attachment on its side ends too");
  assert.strictEqual(closed.wasClean, true);
  assert.strictEqual(link.readyState, 3);
  assert.strictEqual(w.links(), 0, "a closed link is not still routed to");
}

function anAttachTheDaemonRefusesEndsTheLink() {
  const w = build();
  const link = w.openChannel("nope", {});
  const ask = JSON.parse(w.sent.pop());
  let closed = null;
  link.onclose = (ev) => { closed = ev; };
  w.channelFrame({ type: "attach_error", ch: ask.ch, error: "no session named 'nope'" });
  assert.ok(closed, "silence here is a terminal that sits on 'connecting'");
  assert.strictEqual(closed.wasClean, false);
  assert.strictEqual(w.links(), 0);
}

function theCarrierGoingTakesEveryTerminalWithIt() {
  const w = build();
  const a = attached(w, "s7");
  const b = attached(w, "s8");
  const codes = [];
  a.link.onclose = (ev) => codes.push(ev.code);
  b.link.onclose = (ev) => codes.push(ev.code);
  w.channelsCarrierGone();
  assert.deepStrictEqual(codes, [1006, 1006],
    "1006 is what this outage looked like when each terminal had its own "
    + "socket, and the retry machine reads the code");
  assert.strictEqual(w.links(), 0);
}

function withNoControlSocketThereIsNoChannel() {
  const w = build();
  w.up = false;
  assert.strictEqual(w.openChannel("s7", {}), null,
    "null is what sends openSocket down its own-socket fallback");
  assert.strictEqual(w.sent.length, 0);
}

function aLinkThatIsNotOpenYetDoesNotWrite() {
  const w = build();
  const link = w.openChannel("s7", {});
  w.sent.length = 0;
  link.send(JSON.stringify({ type: "resize", cols: 80, rows: 24 }));
  assert.strictEqual(w.sent.length, 0,
    "a frame sent before the daemon has attached would name a channel that "
    + "does not exist there yet");
}

[
  attachAsksForTheSession,
  binarySendsCarryTheChannelId,
  textSendsCarryTheChannelIdInside,
  framesComeBackToTheLinkThatAskedForThem,
  detachEndsOneChannelAndSaysSo,
  anAttachTheDaemonRefusesEndsTheLink,
  theCarrierGoingTakesEveryTerminalWithIt,
  withNoControlSocketThereIsNoChannel,
  aLinkThatIsNotOpenYetDoesNotWrite,
].forEach((check) => check());

console.log("channels_check ok");
