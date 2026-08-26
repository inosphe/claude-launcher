/* The terminal keep-alive cache: what a session switch no longer pays.

   Clicking between sessions used to tear the terminal down and rebuild it —
   a new xterm object, a new WebSocket, and the daemon repainting its whole
   screen — on every hop. Now the visitable terminals are parked: the xterm
   object, its element (hidden in place, never moved) and the socket
   underneath stay up, and returning to a parked one is a swap of the live
   globals and a re-fit, not a reconnect. The rules pinned here are the ones
   that make that honest:

     - a fresh session opens exactly one socket;
     - walking to a parked session opens none at all;
     - while parked, the socket still feeds the hidden terminal's buffer but
       says nothing to the viewer's machine — no link changes, no frames, no
       badge;
     - the oldest is evicted when the cache fills, and its socket and xterm
       die with it;
     - a socket that died while parked reconnects exactly once, on return;
     - a session that exited while parked comes back as exited, with no
       reconnect offered.

   None of that is visible to a stylesheet or to Python, so the real
   functions are sliced out of the shipped app.js and driven here against a
   stub world: fake terminals, fake sockets, and the live state globals
   declared in the harness so the sliced code has the same machine it runs
   inside in the browser. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.TERMCACHE_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* ---- source slicing ---------------------------------------------------- */
/* The whole keep-alive block is contiguous: the constant, the helpers, then
   attach() itself. openSocket is elsewhere in the file and comes sliced
   separately, so a fresh attach() opens real sockets rather than a stub. */
function cutFrom(marker, endFunc) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error(`cannot locate ${JSON.stringify(marker)}`);
  const amid = src.indexOf(`function ${endFunc}(`, start);
  if (amid < 0) throw new Error(`cannot locate function ${endFunc}`);
  let depth = 0;
  for (let j = src.indexOf("{", amid); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${endFunc}`);
}
function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
const keepAliveSrc = cutFrom("const TERM_CACHE_MAX = 3;", "attach");
const openSocketSrc = slice("openSocket");

/* ---- stub world -------------------------------------------------------- */
function node() {
  const n = {
    kids: [], text: "", classes: new Set(), style: {}, dataset: {},
    appendChild(c) { n.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    removeChild(c) { const i = n.kids.indexOf(c); if (i >= 0) n.kids.splice(i, 1); },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    classList: {
      add: (...cs) => cs.forEach((c) => n.classes.add(c)),
      remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
      toggle: (c, on) => (on === undefined
        ? (n.classes.has(c) ? n.classes.delete(c) : n.classes.add(c))
        : (on ? n.classes.add(c) : n.classes.delete(c))),
      contains: (c) => n.classes.has(c),
    },
  };
  return n;
}

const termEl = node();
const docStub = { createElement: () => node(), querySelectorAll: () => [] };
function $stub(id) {
  if (id === "terminal") return termEl;
  if (id === "term-title") return node();
  if (id === "term-status") return node();
  return node();
}

function build() {
  const termInstances = [];
  function FakeTerminal(opts) {
    this.opts = opts || {};
    this.cols = 80;
    this.rows = 24;
    this.disposed = false;
    this.writes = [];
    this.sizes = [];
    this.element = node();
    termInstances.push(this);
  }
  FakeTerminal.prototype.loadAddon = function (a) { this.addon = a; };
  FakeTerminal.prototype.open = function (parent) {
    this.parent = parent; parent.appendChild(this.element); this.element.parentNode = parent;
  };
  FakeTerminal.prototype.onData = function (fn) { this.onData = fn; };
  FakeTerminal.prototype.onResize = function (fn) { this.onResize = fn; };
  FakeTerminal.prototype.attachCustomWheelEventHandler = function (fn) { this.wheel = fn; };
  FakeTerminal.prototype.dispose = function () {
    this.disposed = true;
    if (this.element.parentNode) this.element.parentNode.removeChild(this.element);
    this.element.parentNode = null;
  };
  FakeTerminal.prototype.write = function (d) { this.writes.push(d); };
  FakeTerminal.prototype.resize = function (c, r) { this.sizes.push([c, r]); this.cols = c; this.rows = r; };
  const FitAddonStub = { FitAddon: function () { this.fit = () => {}; } };

  const sockets = [];
  function FakeWebSocket(u) {
    this.url = u;
    this.readyState = 0;
    this.sent = [];
    this.binaryType = null;
    this.onopen = null; this.onmessage = null; this.onclose = null;
    sockets.push(this);
  }
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.prototype.send = function (d) { this.sent.push(d); };
  FakeWebSocket.prototype.close = function () { this.readyState = 3; };
  FakeWebSocket.prototype.opened = function () { this.readyState = 1; if (this.onopen) this.onopen(); };
  FakeWebSocket.prototype.dropped = function () { this.readyState = 3; if (this.onclose) this.onclose(); };
  FakeWebSocket.prototype.text = function (m) { if (this.onmessage) this.onmessage({ data: JSON.stringify(m) }); };
  FakeWebSocket.prototype.bytes = function (b) { if (this.onmessage) this.onmessage({ data: b }); };

  const records = [];
  function record(type, value) { records.push([type, value]); }
  const rec = (type) => records.filter(([t]) => t === type).map(([, v]) => v);
  const handled = [];

  /* The live state globals, declared in this Function's scope like the file
     declares them at the top, plus the small stubs the sliced code leans on
     which must also keep state (setLink, the badge, the header, the fit). */
  const stubs = `
let currentName = null, term = null, ws = null, fitAddon = null;
let attachedPid = null, attachedBoot = null;
let scrollOffset = 0, altScreen = false, sessionEnded = false;
let mouseTracking = false;
let linkState = "idle", linkName = null, linkTry = 0, linkTimer = null;
let linkTicket = 0, linkQueue = [], lastLocalKey = 0;
let wheelTimer = null, wheelAccum = 0, applyingRemoteResize = false;
let sessionsCache = [];
function setLink(s) { linkState = s; __record("link", s); }
function setStatusBadge(s) { __record("status", s); }
function markDetailRow() { __record("detail"); }
function showView(v) { __record("show", v); }
function refitSoon(d) { __record("fit", d); }
function updateScrollChip() {}
function localFit() {}
function stopWfPoll() {}
function stopMeshPoll() {}
function linkDown() {}
function sendInput() {}
function watchComposer() {}   // the typing marks belong to typing_check.js
function handleWheel() {}
`;

  /* NB: the live state must be read through getters on the returned object,
     not Object.assign — Object.assign would read each getter once and copy
     its stale value out as a plain property. */
  const api = new Function(
    "__record", "$", "document", "Terminal", "FitAddon", "WebSocket",
    "handleFrame", "url", "location", "fontSize",
    stubs + keepAliveSrc + "\n" + openSocketSrc + "\n"
    + `
return {
  attach, suspendActive, dropKept,
  keep: () => [...keptTerms.keys()],
  kept: (n) => keptTerms.get(n) || null,
  cap: TERM_CACHE_MAX,
  get current() { return currentName; },
  get term() { return term; },
  get ws() { return ws; },
  get linkState() { return linkState; },
  get sessionEnded() { return sessionEnded; },
};`
  )(
    record, $stub, docStub, FakeTerminal, FitAddonStub, FakeWebSocket,
    (m) => handled.push(m),
    (p) => "/" + String(p).replace(/^\//, ""),
    { protocol: "http:", host: "d09:8377" },
    14
  );

  return { api, sockets, terms: termInstances, rec, handled };
}

/* --- a fresh session opens one socket ----------------------------------- */
{
  const w = build();
  w.api.attach("a");
  const sa = w.sockets[w.sockets.length - 1];
  check("a fresh session opens one socket", w.sockets.length === 1, w.sockets.length);
  check("...pointed at that session",
        /\/api\/sessions\/a\/ws(\?|$)/.test(sa.url), sa.url);
  check("attach has taken the header", w.api.current === "a", w.api.current);
  check("and built a terminal", !!w.api.term && !w.api.term.disposed, !!w.api.term);
  check("nothing is parked yet", w.api.keep().length === 0, w.api.keep());
  sa.opened();
  check("the link comes up when the socket does",
        w.api.linkState === "live", w.api.linkState);
  sa.text({ type: "init", cols: 80, rows: 24, status: "idle", pid: 42, boot_id: "b1" });
  check("an init frame on the fresh socket reaches the live handler",
        w.handled.length === 1, w.handled.length);
}

/* --- a parked session comes back without a new socket ------------------- */
{
  const w = build();
  w.api.attach("a");
  const aTerm = w.api.term, aSock = w.api.ws;
  aSock.opened();                // the link is established before we leave it
  w.api.attach("b");
  check("a second fresh session opens a second socket",
        w.sockets.length === 2, w.sockets.length);
  check("and the first is parked with its terminal and socket alive",
        w.api.keep().length === 1 && w.api.keep()[0] === "a", w.api.keep());
  const parkedA = w.api.kept("a");
  check("the parked terminal is exactly the one that was on screen",
        parkedA.term === aTerm && parkedA.ws === aSock, !!parkedA);
  check("its socket is still open", aSock.readyState === 1, aSock.readyState);
  check("its element is hidden", aTerm.element.style.display === "none",
        aTerm.element.style.display);

  const before = w.sockets.length;
  w.api.attach("a");
  check("walking back to the parked one opens no socket",
        w.sockets.length === before, w.sockets.length);
  check("the parked terminal is on screen again",
        w.api.current === "a" && w.api.term === aTerm && w.api.ws === aSock,
        [w.api.current, w.api.term === aTerm, w.api.ws === aSock]);
  check("its element is shown again", aTerm.element.style.display === "",
        aTerm.element.style.display);
  check("it is out of the cache", !w.api.keep().includes("a"), w.api.keep());
  check("the session we left is parked in its turn",
        w.api.keep().includes("b"), w.api.keep());

  const h = w.handled.length;
  aSock.text({ type: "state", status: "idle" });
  check("a live frame on the restored socket reaches the live handler",
        w.handled.length === h + 1, w.handled.length);
}

/* --- while parked, the socket feeds the buffer and nothing else --------- */
{
  const w = build();
  w.api.attach("a");
  const aSock = w.api.ws, aTerm = w.api.term;
  aSock.opened();
  w.api.attach("b");
  const bTerm = w.api.term;
  const linksBefore = w.rec("link").length;
  const statusesBefore = w.rec("status").length;
  const handledBefore = w.handled.length;
  aSock.bytes(new Uint8Array([104, 105]));    // "hi"
  check("binary output while parked refreshes the hidden buffer",
        aTerm.writes.length === 1 && bTerm.writes.length === 0,
        [aTerm.writes.length, bTerm.writes.length]);
  check("...and touches no link, badge or handler",
        w.rec("link").length === linksBefore
        && w.rec("status").length === statusesBefore
        && w.handled.length === handledBefore,
        [w.rec("link").length, w.rec("status").length, w.handled.length]);
  aSock.text({ type: "state", status: "busy" });
  check("a state frame to a parked socket is absorbed, not delivered",
        w.handled.length === handledBefore
        && w.rec("status").length === statusesBefore,
        [w.handled.length, w.rec("status").length]);
  aSock.text({ type: "exit", code: 0 });
  check("an exit frame marks the parked terminal exited and writes the notice",
        w.api.kept("a").exited === true
        && aTerm.writes.some((t) => String(t).includes("exited")),
        [w.api.kept("a").exited, aTerm.writes.length]);
}

/* --- the oldest parked terminal is evicted when the cache fills --------- */
{
  const w = build();
  const first = {};
  w.api.attach("a");
  first.sock = w.api.ws; first.term = w.api.term;
  w.api.attach("b");
  w.api.attach("c");
  check("three sessions stay within the cache — the two earliest parked",
        w.api.keep().length === 2
        && w.api.keep().includes("a") && w.api.keep().includes("b"),
        w.api.keep());
  w.api.attach("d");
  const parked = w.api.keep();
  check("the oldest (a) is evicted, not the newest",
        parked.includes("b") && parked.includes("c") && !parked.includes("a"),
        parked);
  const aAfter = w.terms.find((t) => t === first.term);
  check("its socket was closed", first.sock.readyState === 3, first.sock.readyState);
  check("its terminal was disposed", aAfter.disposed === true, aAfter && aAfter.disposed);
  check("and the newest is on screen", w.api.current === "d", w.api.current);
}

/* --- a socket that died while parked reconnects once, on return --------- */
{
  const w = build();
  w.api.attach("a");
  const aSock = w.api.ws;
  aSock.opened();
  w.api.attach("b");
  aSock.readyState = 3;    // the daemon went away while a was parked
  const before = w.sockets.length;
  w.api.attach("a");
  check("a parked session whose socket died pays one reconnect, on return",
        w.sockets.length === before + 1, w.sockets.length);
  const fresh = w.sockets[w.sockets.length - 1];
  check("...opened for that session again",
        /\/api\/sessions\/a\/ws(\?|$)/.test(fresh.url), fresh.url);
  check("and the session is back on screen", w.api.current === "a", w.api.current);
}

/* --- a session that exited while parked comes back exited --------------- */
{
  const w = build();
  w.api.attach("a");
  const aSock = w.api.ws;
  aSock.opened();
  w.api.attach("b");
  aSock.text({ type: "exit", code: 0 });       // it finished while we were away
  const before = w.sockets.length;
  w.api.attach("a");
  check("the exited terminal comes back exited, not reconnecting",
        w.api.sessionEnded === true && w.api.linkState === "idle",
        [w.api.sessionEnded, w.api.linkState]);
  check("and no new socket was opened", w.sockets.length === before, w.sockets.length);
}

process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log("all terminal-cache checks passed");
});
