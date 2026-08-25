/* Who owns the terminal's wheel, run against a stub socket.

   Three regimes, and the contract is that only one of them is ours:

   1. The program took the mouse (`?1000h` and friends — claude asserts them
      behind the alternate screen and never lets go). Wheel ticks are its own;
      xterm encodes the SGR report and the page must not spend the tick on
      anything else. Measured on real sessions, such a terminal yields one or
      two lines of daemon-side history for four hundred kilobytes of output —
      it repaints the grid rather than scrolling it — so the virtual scroll
      had nothing to serve it anyway.
   2. The main buffer. xterm's own scrollback holds the history (the daemon
      seeds it at attach), so the browser scrolls natively: scrollbar,
      momentum, find-in-page.
   3. The alternate screen with the mouse left alone — a pager reading arrow
      keys. Nothing scrolls off the alt buffer, so the daemon's history window
      is all there is and the `scroll` control serves it. This one is ours.

   The real functions are sliced out of the shipped app.js and driven here
   with stub events, a stub socket and a fake timer wheel, exactly like
   reconnect_check does for the link. */
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
function build(opts) {
  const o = opts || {};
  const nodes = {};
  for (const id of ["term-link", "m-link", "term-scroll"]) {
    nodes[id] = {
      id, textContent: "", title: "", className: "", on: {},
      addEventListener: (ev, fn) => { nodes[id].on[ev] = fn; },
      click: () => nodes[id].on.click && nodes[id].on.click(),
      classList: {
        add: (c) => { if (!nodes[id].className.split(" ").includes(c)) {
                        nodes[id].className += ` ${c}`; } },
        remove: (c) => { nodes[id].className = nodes[id].className.split(" ")
                           .filter((x) => x !== c).join(" "); },
      },
    };
  }

  // Fake timers: a list, fired by hand (the debounce is the thing being
  // checked, not something to wait out).
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
  const settle = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

  const sockets = [];
  class FakeSocket {
    constructor(u) {
      this.url = u;
      this.readyState = 0;
      this.sent = [];
      sockets.push(this);
    }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; }
    opened() { this.readyState = 1; if (this.onopen) this.onopen(); }
    dropped() { this.readyState = 3; if (this.onclose) this.onclose(); }
    text(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
  }
  FakeSocket.OPEN = 1;

  const fetchStub = async () => ({ ok: true, json: async () => ({}) });
  const apiStub = async () => ({ ok: true, json: async () => ({}) });
  const statuses = [];
  const winOn = {};
  // `modes` is xterm's own view of the private modes it has parsed out of the
  // byte stream. The page reads it first and falls back to the daemon's flag,
  // so the harness can drive either side.
  const term = {
    cols: 80, rows: 24, disposed: false,
    modes: { mouseTrackingMode: "none" },
    write: () => {},
    resize: () => {},
    dispose: () => { term.disposed = true; },
  };

  // The wheel state lives at the top of app.js, outside any sliceable block,
  // so the harness declares it in the wrapper's own scope — the sliced code
  // follows it in the same function body, invisible to node's globals. The
  // wheel handlers sit in their own block near attach(), past the text-size
  // section, so the slice is the link machine plus that block.
  const code = slice("/* ---- the link ----", "/* ---- text size ----")
    + "\n" + slice("/* ---- who owns the wheel ----", "/* Bind the terminal to a session");
  const api = new Function(
    "$", "url", "api", "fetch", "WebSocket", "window", "document", "location",
    "ws", "term", "fitAddon", "attachedPid", "applyingRemoteResize",
    "setStatusBadge", "refitSoon", "setTimeout", "clearTimeout", "Math", "Date",
    "fitView", "resyncTerminal", "terminalOnScreen",
    "let altScreen = false;\n" +
    "let mouseTracking = false;\n" +
    "let scrollOffset = 0;\n" +
    "let wheelAccum = 0;\n" +
    "let wheelTimer = null;\n" +
    // detach() reaches for the attached session's name; the slice below also
    // carries the keep-alive block (which declares keptTerms itself), so only
    // currentName needs a stand-in here.
    "let currentName = null;\n" +
    "const WHEEL_LINE_PX = 20;\n" +
    code +
    "\nreturn {openSocket, handleFrame, sendInput, detach, handleWheel," +
    " flushWheel, updateScrollChip, syncLinkChip, wheelBelongsToProgram," +
    " get state() { return linkState; }," +
    " get alt() { return altScreen; }," +
    " get mouse() { return mouseTracking; }," +
    " get offset() { return scrollOffset; }," +
    " get accum() { return wheelAccum; }," +
    " get chip() { return $('term-scroll'); }," +
    " get sock() { return ws; }," +
    " queueMax: LINK_QUEUE_MAX};"
  )(
    (id) => nodes[id],
    (p) => `/${String(p).replace(/^\//, "")}`,
    apiStub,
    fetchStub,
    FakeSocket,
    { addEventListener: (ev, fn) => { winOn[ev] = fn; } },
    { hidden: false, hasFocus: () => false },
    { protocol: "http:", host: "d09:8377" },
    null,
    term,
    null,
    null,
    false,
    (s) => statuses.push(s),
    () => {},
    setTimeoutStub,
    clearTimeoutStub,
    { random: () => 0, round: Math.round },
    { now: () => 0 },
    () => {},    // fitView: glyph shrinking is out of this harness's scope
    () => {},    // resyncTerminal: the resize branch's take-it-back move
    () => false, // terminalOnScreen: resize frames take the adopt branch
  );

  return { api, nodes, sockets, statuses, term, pending, fire, settle,
           live: async (init) => {
             const o = init || {};
             api.openSocket("s8");
             const s = sockets[sockets.length - 1];
             s.opened();
             s.text({ type: "init", cols: 80, rows: 24, status: "idle",
                      pid: 4242, boot_id: "b1",
                      alt: !!o.alt, mouse: !!o.mouse });
             await settle();
             return s;
           } };
}

function wheel(dy, dm) {
  let prevented = false;
  return { deltaY: dy, deltaMode: dm,
           preventDefault: () => { prevented = true; },
           get prevented() { return prevented; } };
}

/* --- (2) the main buffer scrolls itself ---------------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    check("a fresh socket knows the main buffer", w.api.alt === false, w.api.alt);
    const e = wheel(-100, 0);
    check("the wheel is left to xterm — its own scrollback holds the history"
          + " the daemon seeded at attach",
          w.api.handleWheel(e) === true);
    check("nothing is prevented, so the browser scrolls natively", !e.prevented);
    check("no delta is banked", w.api.accum === 0, w.api.accum);
    check("and no control frame goes out",
          s.sent.filter((m) => typeof m === "string").length === 0, s.sent);
  })();
}

/* --- (1) a program that took the mouse keeps the wheel -------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live({ alt: true, mouse: true });
    check("init carries the mouse flag", w.api.mouse === true && w.api.alt === true,
          { mouse: w.api.mouse, alt: w.api.alt });
    check("the page agrees the wheel is the program's",
          w.api.wheelBelongsToProgram() === true);
    const e = wheel(-40, 0);
    check("so the tick passes through to xterm, which reports it as a mouse"
          + " event and lets claude scroll its own view",
          w.api.handleWheel(e) === true);
    check("no preventDefault", !e.prevented);
    check("no accumulation, no daemon round trip",
          w.api.accum === 0
          && s.sent.filter((m) => typeof m === "string").length === 0,
          { accum: w.api.accum, sent: s.sent });
  })();
}

/* --- xterm's own mode view is enough, without the daemon's flag ---------- */
{
  const w = build();
  (async () => {
    const s = await w.live({ alt: true });
    check("without tracking, an alt-screen wheel is the daemon's",
          w.api.handleWheel(wheel(-20, 0)) === false);
    w.term.modes.mouseTrackingMode = "any";
    check("once xterm has parsed the assertion out of the byte stream, the"
          + " wheel is the program's even before a control frame says so",
          w.api.wheelBelongsToProgram() === true
          && w.api.handleWheel(wheel(-20, 0)) === true);
  })();
}

/* --- the mouse frame hands the wheel over mid-session --------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live({ alt: true });
    s.text({ type: "scrolled", offset: 6 });
    check("scrolled back on the daemon's history", w.api.offset === 6, w.api.offset);
    s.text({ type: "mouse", tracking: true });
    check("the program taking the mouse drops the viewer to live — nothing"
          + " would ever have scrolled them out of that frozen window",
          w.api.offset === 0 && w.api.mouse === true, w.api.offset);
    check("and the chip goes down with it",
          w.api.chip.className.includes("hidden"), w.api.chip.className);
    s.text({ type: "mouse", tracking: false });
    check("giving it back returns the wheel to the daemon",
          w.api.mouse === false && w.api.handleWheel(wheel(-20, 0)) === false);
  })();
}

/* --- (3) on the alt screen the wheel drives the daemon's history ---------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.text({ type: "buffer", alt: true });
    check("the handler knows the program went alt",
          w.api.alt === true && w.api.handleWheel(wheel(-40, 0)) === false);
    check("pixel deltas accumulate as lines", Math.abs(w.api.accum + 2) < 1e-9,
          w.api.accum);
    w.api.flushWheel();
    check("scrolling up asks the daemon for history",
          JSON.parse(s.sent[s.sent.length - 1]).lines === 2,
          s.sent);
    w.api.handleWheel(wheel(40, 0));
    w.api.flushWheel();
    check("scrolling down heads back toward live",
          JSON.parse(s.sent[s.sent.length - 1]).lines === -2,
          s.sent);
  })();
}

/* --- a burst coalesces into one control ---------------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.text({ type: "buffer", alt: true });
    for (let i = 0; i < 5; i += 1) w.api.handleWheel(wheel(-20, 0));
    check("a touchpad burst banks as a single pending message",
          w.pending() === 1, w.pending());
    w.api.flushWheel();
    check("and goes out as one control",
          s.sent.filter((m) => typeof m === "string").length === 1, s.sent);
    check("with the whole burst summed into it",
          JSON.parse(s.sent[s.sent.length - 1]).lines === 5, s.sent);
  })();
}

/* --- line- and page-mode deltas are honoured ----------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.text({ type: "buffer", alt: true });
    w.api.handleWheel(wheel(3, 1));   // DOM_DELTA_LINE: lines arrive verbatim
    w.api.flushWheel();
    check("line-mode deltas pass through", JSON.parse(s.sent[s.sent.length - 1]).lines === -3);
    w.api.handleWheel(wheel(1, 2));   // DOM_DELTA_PAGE: rows per page
    w.api.flushWheel();
    check("page-mode deltas scale by the terminal's rows",
          JSON.parse(s.sent[s.sent.length - 1]).lines === -24, s.sent);
  })();
}

/* --- the daemon's answer is the viewer's truth ---------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.text({ type: "scrolled", offset: 7 });
    check("a scrolled frame syncs the viewer's offset", w.api.offset === 7, w.api.offset);
    check("and lights the history chip", w.api.chip.textContent === "history"
          && !w.api.chip.className.includes("hidden"), w.api.chip.className);
    s.text({ type: "scrolled", offset: 0 });
    check("back to live, the chip goes away",
          w.api.chip.className.includes("hidden"), w.api.chip.className);
  })();
}

/* --- a fresh socket always starts live ----------------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.text({ type: "scrolled", offset: 5 });
    s.text({ type: "init", cols: 80, rows: 24, status: "idle", pid: 4242,
             boot_id: "b1", alt: true, mouse: true });
    check("an init frame resets to live at whatever buffer the program is in,"
          + " and with whoever owns the mouse owning it",
          w.api.offset === 0 && w.api.alt === true && w.api.mouse === true
          && w.api.accum === 0,
          { offset: w.api.offset, alt: w.api.alt, mouse: w.api.mouse });
  })();
}

/* --- the TUI leaving the alt screen unfreezes ---------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.text({ type: "buffer", alt: true });
    s.text({ type: "scrolled", offset: 5 });
    s.text({ type: "buffer", alt: false });
    check("leaving the alt screen drops the viewer back to live",
          w.api.offset === 0 && w.api.alt === false, w.api.offset);
    check("and the chip with it",
          w.api.chip.className.includes("hidden"), w.api.chip.className);
  })();
}

/* --- typing while scrolled away snaps to live first ---------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.text({ type: "buffer", alt: true });
    s.text({ type: "scrolled", offset: 4 });
    w.api.sendInput("x");
    const controls = s.sent.filter((m) => typeof m === "string");
    check("the keystroke first asks to return to live",
          controls.length >= 1
          && JSON.parse(controls[controls.length - 1]).lines === -999999,
          controls);
    check("then goes to the session as a key",
          Buffer.from(s.sent[s.sent.length - 1]).toString() === "x",
          s.sent[s.sent.length - 1]);
    check("and the chip is down", w.api.offset === 0
          && w.api.chip.className.includes("hidden"), w.api.offset);
  })();
}

/* --- exit ends the scroll state too -------------------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live({ alt: true, mouse: true });
    s.text({ type: "mouse", tracking: false });
    s.text({ type: "scrolled", offset: 3 });
    s.text({ type: "exit", code: 0 });
    check("an exit frame resets buffer, mouse and scroll — no program is left"
          + " to own any of them",
          w.api.offset === 0 && w.api.alt === false && w.api.mouse === false,
          { offset: w.api.offset, alt: w.api.alt, mouse: w.api.mouse });
  })();
}

/* --- walking away clears the wheel machinery ----------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    s.text({ type: "buffer", alt: true });
    w.api.handleWheel(wheel(-20, 0));       // a debounce is pending
    s.text({ type: "scrolled", offset: 2 });
    w.api.detach();
    check("detach cancels the pending debounce", w.pending() === 0, w.pending());
    check("and clears the scroll state", w.api.offset === 0 && w.api.accum === 0,
          w.api.offset);
  })();
}

/* --- the markup and the wiring the code reaches for ---------------------- */
check("index.html declares #term-scroll", html.includes('id="term-scroll"'));
check("it sits in the terminal header, beside the socket chip",
      html.indexOf('id="term-scroll"') > html.indexOf('id="term-link"')
      && html.indexOf('id="term-scroll"') < html.indexOf('id="terminal"'));
check("it starts hidden — up only while someone is scrolled back",
      /id="term-scroll"[^>]*class="[^"]*hidden/.test(html));
check("the wheel handler is wired to the terminal by the builder freshAttach " +
      "constructs, and attach() routes new terminals through that builder",
      src.indexOf("function freshAttach(name)") > 0
      && src.indexOf("attachCustomWheelEventHandler(handleWheel)")
         > src.indexOf("function freshAttach(name)")
      && src.indexOf("freshAttach(name);") > src.indexOf("function attach(name)"));
check("the session terminal is built with a real scrollback, so the main"
      + " buffer has something for a native wheel to move",
      /scrollback:\s*5000/.test(slice("function freshAttach(name)",
                                      "term.onData(sendInput)")));

/* The checks run in async blocks, so the tally is only complete once the
   microtask queue has drained — and a throw inside one of them must not be
   reported as a pass on the way out. */
process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log("all wheel checks passed");
});
