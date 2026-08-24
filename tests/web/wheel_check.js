/* The terminal's virtual scroll, run against a stub socket.

   xterm cannot scroll its alternate screen — the way its wheel event becomes
   arrow keys and its scrollback stays empty, a full-screen TUI (claude) reads
   as unscrollable in the dashboard. The fix is a contract, not a style: on
   the alt screen the wheel must become `scroll` controls the daemon answers
   with history repaints, and on the main buffer it must not touch xterm's own
   wheel at all. The real functions are sliced out of the shipped app.js and
   driven here with stub events, a stub socket and a fake timer wheel, exactly
   like reconnect_check does for the link. */
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
  const term = {
    cols: 80, rows: 24, disposed: false,
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
    + "\n" + slice("/* ---- virtual scroll ----", "/* Bind the terminal to a session");
  const api = new Function(
    "$", "url", "api", "fetch", "WebSocket", "window", "document", "location",
    "ws", "term", "fitAddon", "attachedPid", "applyingRemoteResize",
    "setStatusBadge", "refitSoon", "setTimeout", "clearTimeout", "Math", "Date",
    "let altScreen = false;\n" +
    "let scrollOffset = 0;\n" +
    "let wheelAccum = 0;\n" +
    "let wheelTimer = null;\n" +
    "const WHEEL_LINE_PX = 20;\n" +
    code +
    "\nreturn {openSocket, handleFrame, sendInput, detach, handleWheel," +
    " flushWheel, updateScrollChip, syncLinkChip," +
    " get state() { return linkState; }," +
    " get alt() { return altScreen; }," +
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
    { hidden: false },
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
  );

  return { api, nodes, sockets, statuses, term, pending, fire, settle,
           live: async (alt) => {
             api.openSocket("s8");
             const s = sockets[sockets.length - 1];
             s.opened();
             s.text({ type: "init", cols: 80, rows: 24, status: "idle",
                      pid: 4242, boot_id: "b1",
                      alt: alt === undefined ? false : alt });
             await settle();
             return s;
           } };
}

function wheel(dy, dm) {
  return { deltaY: dy, deltaMode: dm, preventDefault: () => {} };
}

/* --- the main buffer keeps xterm's own wheel ---------------------------- */
{
  const w = build();
  (async () => {
    const s = await w.live();
    check("a fresh socket knows the main buffer", w.api.alt === false, w.api.alt);
    check("the wheel passes through untouched",
          w.api.handleWheel(wheel(100, 0)) === true);
    check("nothing was accumulated, nothing asked of the daemon",
          w.api.accum === 0 && s.sent.length === 0, w.api.accum);
  })();
}

/* --- on the alt screen the wheel drives the daemon's history ------------- */
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
             boot_id: "b1", alt: true });
    check("an init frame resets to live at whatever buffer the program is in",
          w.api.offset === 0 && w.api.alt === true && w.api.accum === 0,
          { offset: w.api.offset, alt: w.api.alt });
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
    const s = await w.live();
    s.text({ type: "buffer", alt: true });
    s.text({ type: "scrolled", offset: 3 });
    s.text({ type: "exit", code: 0 });
    check("an exit frame resets buffer and scroll",
          w.api.offset === 0 && w.api.alt === false, w.api.offset);
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

/* --- the markup the code reaches for ------------------------------------- */
check("index.html declares #term-scroll", html.includes('id="term-scroll"'));
check("it sits in the terminal header, beside the socket chip",
      html.indexOf('id="term-scroll"') > html.indexOf('id="term-link"')
      && html.indexOf('id="term-scroll"') < html.indexOf('id="terminal"'));
check("it starts hidden — up only while someone is scrolled back",
      /id="term-scroll"[^>]*class="[^"]*hidden/.test(html));
check("the wheel handler is wired to the terminal inside attach()",
      src.indexOf("attachCustomWheelEventHandler(handleWheel)")
        > src.indexOf("function attach(name)"));

/* The checks run in async blocks, so the tally is only complete once the
   microtask queue has drained — and a throw inside one of them must not be
   reported as a pass on the way out. */
process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log("all wheel checks passed");
});
