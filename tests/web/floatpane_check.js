/* The floating split pane (claunch-xkw8o): the frame's float checkbox takes
   the pane out of the terminal's column and floats it over the terminal, so
   sizing it or closing it never resizes what is underneath.

   What has to hold:
     state    -- sessLayoutFor reads the flag and the box (px from #main's
                 right/bottom edges), junk falls back to "not set" instead of
                 to zero, and a write is already whole.
     canvas   -- while the pane floats it takes no height: opening it,
                 closing it, moving it or sizing it does not refit the
                 terminal. Docking and undocking do, exactly once each.
     box      -- the box is clamped to #main (min sizes, inside the edges),
                 painted as the four CSS variables the stylesheet reads, and
                 a shrinking window only pulls the painted box back in.
     drag     -- the head moves the box, the grip sizes it, the head's own
                 controls are presses and not drags, and one write lands at
                 release (no localStorage stream).
     wiring   -- mountSplitFrame hands the frame the remembered flag and
                 writes the checkbox's answer back; the stylesheet carries
                 the overlay, the hidden bar and the grip. */
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");

const STATIC = path.join(__dirname, "../../src/claude_launcher/web/static");
const read = (f) => fs.readFileSync(path.join(STATIC, f), "utf8").replace(/\r\n/g, "\n");
const app = read("app.js");
const css = read("style.css");

function sliceTo(from, to) {
  const a = app.indexOf(from);
  const b = app.indexOf(to, a);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return app.slice(a, b);
}
function sliceLine(decl) {
  const start = app.indexOf(decl);
  if (start < 0) throw new Error("missing " + decl);
  return app.slice(start, app.indexOf("\n", start) + 1);
}

/* ---- stub DOM ---------------------------------------------------------- */
const MAIN_BOX = { width: 900, height: 620, top: 40, left: 260, right: 1160, bottom: 660 };

function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", classes: new Set(), handlers: {},
    title: "", captured: [],
    style: { vars: {}, setProperty(k, v) { this.vars[k] = String(v); } },
    getBoundingClientRect: () => MAIN_BOX,
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    setPointerCapture(id) { this.captured.push(id); },
    fire(k, ev) { (this.handlers[k] || []).forEach((fn) => fn(ev || {})); },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
  };
  return n;
}

const prelude = `
let currentPage = "terminal";
let currentName = "s15";
// The pane the page believes is open (syncSplitPane's own state).
let splitFor = null;
let splitKind = null;
function sessionLineOnPage(page) { return page === "terminal" || page === "log"; }
function applySplitRatio(r) { calls.ratios.push(r); }
function openSplit(name, kind) { splitFor = name; splitKind = kind; calls.opened.push(name); calls.kinds.push(kind); }
function closeSplit() { splitFor = null; splitKind = null; calls.closed++; }
let operatorSessions = new Set();
function isOperatorSession(name) { return operatorSessions.has(name); }
let operatorQaSessions = new Set();
function operatorQa(name) { return operatorQaSessions.has(name); }
function threadsNews() { return false; }
function markThreadsSeen(name) {}
function threadNoticeClear(name) {}
`;
const expose = `
Object.assign(exports, {
  floatBox, paintFloatBox, clampFloatPane, clampFloat,
  sessLayoutFor, setSessLayout, syncSplitPane,
  FLOAT_MIN_W, FLOAT_MIN_H, FLOAT_EDGE,
  setName: (n) => { currentName = n; },
});`;

function build(o = {}) {
  const store = new Map();
  if (o.stored !== undefined) store.set("claunch_sesslayout:/", JSON.stringify(o.stored));
  const storage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  };
  const nodes = {};
  for (const id of ["main", "term-split", "term-wf", "term-composer", "term-splitbtn",
                    "term-simsbtn", "term-categorybtn", "term-chartbtn", "term-rebrief",
                    "term-qabtn", "term-threadsbtn", "term-clibtn", "term-vmbtn",
                    "term-wrapupbtn", "term-planbtn"]) nodes[id] = node(id);
  // The session's input line under the column: a floating pane's default
  // corner must clear it (floatFloor). "hidden" is the page without one.
  const shelf = o.shelf === "hidden"
    ? { top: MAIN_BOX.bottom, bottom: MAIN_BOX.bottom, height: 0, left: MAIN_BOX.left, right: MAIN_BOX.right, width: MAIN_BOX.width }
    : { top: 560, bottom: 660, height: 100, left: MAIN_BOX.left, right: MAIN_BOX.right, width: MAIN_BOX.width };
  nodes["term-composer"].getBoundingClientRect = () => shelf;
  const body = node("body");
  const calls = { opened: [], kinds: [], closed: 0, ratios: [], refits: 0 };
  const ctx = {};
  new Function(
    "exports", "$", "document", "BASE", "localStorage", "MOBILE_MQ", "refitSoon", "calls",
    prelude +
    sliceTo("const SESSLAYOUT_KEY =", "/* ---- who owns the wheel ----") +
    sliceLine("let splitWasCanvas") +
    sliceTo("function syncSplitPane()", "/* Which row's ⓘ is lit.") +
    sliceTo("function clampFloat(", "$(\"term-wf\").addEventListener(\"pointercancel\", endFloatDrag);") +
    "$(\"term-wf\").addEventListener(\"pointercancel\", endFloatDrag);\n" +
    expose
  )(ctx, (id) => nodes[id], { body }, "/", storage, { matches: !!o.narrow },
    () => calls.refits++, calls);
  return { ctx, nodes, body, store, calls };
}

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* --- the remembered state ------------------------------------------------ */
{
  const w = build({});
  const lay = w.ctx.sessLayoutFor("never-seen");
  check("a session nobody has floated is docked", lay.float === false, lay.float);
  check("and has no box of its own yet",
        lay.floatW === null && lay.floatH === null
        && lay.floatRight === null && lay.floatBottom === null, lay);
}
{
  const w = build({ stored: { s15: { float: 1, floatW: "320", floatH: 210.4,
                                     floatRight: 0, floatBottom: 18 } } });
  const lay = w.ctx.sessLayoutFor("s15");
  check("a truthy flag is the flag", lay.float === true);
  check("px strings read as numbers", lay.floatW === 320 && lay.floatTop === undefined
        && lay.floatBottom === 18, lay);
  check("a fraction is a whole pixel", lay.floatH === 210, lay.floatH);
  check("zero is a real offset (the pane against that edge)", lay.floatRight === 0);
}
{
  // Junk must read as "not set", not as zero: a zero would pin the box to
  // the corner instead of letting the default follow the window.
  const w = build({ stored: { s15: { floatW: "wide", floatH: -4,
                                     floatRight: "12", floatBottom: true } } });
  const lay = w.ctx.sessLayoutFor("s15");
  check("a word is not a length", lay.floatW === null, lay.floatW);
  check("nor is a negative one", lay.floatH === null, lay.floatH);
  check("a numeric string is", lay.floatRight === 12, lay.floatRight);
  check("a boolean is not", lay.floatBottom === null, lay.floatBottom);
}
{
  const w = build({ stored: { s15: { split: true, float: true } } });
  w.ctx.setSessLayout("s15", { floatW: 420, floatH: 300, floatRight: 24, floatBottom: 36 });
  const stored = JSON.parse(w.store.get("claunch_sesslayout:/")).s15;
  check("a write lands whole, next to the choices already there",
        stored.split === true && stored.float === true && stored.floatW === 420
        && stored.floatH === 300 && stored.floatRight === 24 && stored.floatBottom === 36,
        stored);
}

/* --- the canvas keeps its size while the pane floats -------------------- */
{
  const w = build({ stored: { s15: { split: true, float: true } } });
  w.ctx.syncSplitPane();
  check("floating is marked on the page", w.body.classes.has("split-float"));
  check("...the pane is up", !w.nodes["term-wf"].classes.has("hidden"));
  check("...and the bar is gone (there is no boundary to drag)",
        w.nodes["term-split"].classes.has("hidden"));
  check("opening a floating pane does not resize the terminal",
        w.calls.refits === 0, w.calls.refits);
  check("...and the pane is pointed at the session",
        w.calls.opened.join() === "s15", w.calls.opened);
  const style = w.nodes["term-wf"].style.vars;
  check("the box is painted for the stylesheet",
        style["--float-w"] === "495px" && style["--float-h"] === "310px"
        && style["--float-right"] === "12px" && style["--float-bottom"] === "112px", style);
  // Closing it while floating gives the terminal nothing back, because the
  // terminal never gave anything up.
  w.ctx.setSessLayout("s15", { split: false });
  check("closing a floating pane does not resize the terminal either",
        w.calls.refits === 0 && !w.body.classes.has("split-float"), w.calls.refits);
  // Docking it is what moves the canvas, once.
  w.ctx.setSessLayout("s15", { split: true, float: false });
  check("docking takes the column back — one refit",
        w.calls.refits === 1 && w.nodes["term-split"].classes.has("hidden") === false,
        w.calls.refits);
  w.ctx.setSessLayout("s15", { float: true });
  check("floating again gives it back — one more refit",
        w.calls.refits === 2 && w.body.classes.has("split-float"), w.calls.refits);
  w.ctx.syncSplitPane();
  check("a second sync asks for nothing more", w.calls.refits === 2, w.calls.refits);
}
{
  // ⤢ enlarges within the column; a floating pane has no column to enlarge
  // into, so the page never marks the canvas maximized while it floats.
  const w = build({ stored: { s15: { split: true, float: true, max: true } } });
  w.ctx.syncSplitPane();
  check("⤢ does not claim the canvas while the pane floats",
        !w.body.classes.has("split-max"));
}
{
  // Below the breakpoint nothing floats either: the pane is down and the
  // choice is kept for when the screen comes back.
  const w = build({ stored: { s15: { split: true, float: true } }, narrow: true });
  w.ctx.syncSplitPane();
  check("a narrow window keeps the pane down and unfloated",
        w.nodes["term-wf"].classes.has("hidden") && !w.body.classes.has("split-float"));
}

/* --- the box: defaults, minimums, and staying inside #main -------------- */
{
  const w = build({});
  const box = w.ctx.floatBox("s15");
  check("the default is about the share the docked pane would have taken",
        box.w === Math.round(MAIN_BOX.width * 0.55)
        && box.h === Math.round(MAIN_BOX.height * 0.5), box);
  check("...sitting against the right edge",
        box.right === w.ctx.FLOAT_EDGE, box);
  check("...and above the session's input line, not over it",
        box.bottom === MAIN_BOX.bottom - 560 + w.ctx.FLOAT_EDGE, box);
}
{
  // A page with no session input line has nothing to clear.
  const w = build({ shelf: "hidden" });
  const box = w.ctx.floatBox("s15");
  check("without an input line the corner keeps only its margin",
        box.bottom === w.ctx.FLOAT_EDGE, box);
}
{
  const w = build({ stored: { s15: { floatW: 40, floatH: 10 } } });
  const box = w.ctx.floatBox("s15");
  check("a box below the minimum grows to it",
        box.w === w.ctx.FLOAT_MIN_W && box.h === w.ctx.FLOAT_MIN_H, box);
}
{
  const w = build({ stored: { s15: { floatW: 5000, floatH: 5000,
                                     floatRight: 5000, floatBottom: 5000 } } });
  const box = w.ctx.floatBox("s15");
  check("a box bigger than #main is cut to it",
        box.w <= MAIN_BOX.width && box.h <= MAIN_BOX.height, box);
  check("...and an offset that would push it out is pulled back in",
        box.right <= Math.max(0, MAIN_BOX.width - box.w)
        && box.bottom <= Math.max(0, MAIN_BOX.height - box.h), box);
}
{
  // The painted box is clamped; the remembered numbers are not rewritten, so
  // the pane comes back where it was when the window grows again.
  const w = build({ stored: { s15: { split: true, float: true,
                                     floatW: 8000, floatRight: 8000 } } });
  w.ctx.setName("s15");
  w.ctx.syncSplitPane();
  const style = w.nodes["term-wf"].style.vars;
  check("the painted box is clamped to the window",
        parseInt(style["--float-w"], 10) <= MAIN_BOX.width - 2 * w.ctx.FLOAT_EDGE, style);
  check("...while what is remembered stays as written",
        w.ctx.sessLayoutFor("s15").floatW === 8000
        && w.ctx.sessLayoutFor("s15").floatRight === 8000);
  w.ctx.clampFloatPane();
  check("clamping again is idempotent", w.nodes["term-wf"].style.vars["--float-w"]
        === style["--float-w"]);
}

/* --- the drags ----------------------------------------------------------- */
function ev(target, x, y, id = 1) {
  return { target, clientX: x, clientY: y, pointerId: id, preventDefault() {} };
}
function partOf(name) {
  // An element that says which part of the frame it belongs to (closest).
  return { closest: (sel) => (sel.includes(name) ? partOf(name) : null) };
}
{
  const w = build({ stored: { s15: { split: true, float: true, floatW: 400, floatH: 300,
                                     floatRight: 40, floatBottom: 30 } } });
  w.ctx.setName("s15");
  w.ctx.syncSplitPane();
  const wf = w.nodes["term-wf"];
  const head = partOf("split-frame-head");
  wf.fire("pointerdown", ev(head, 500, 400));
  check("the head drag is captured", wf.captured.length === 1 && wf.classes.has("float-dragging"));
  wf.fire("pointermove", ev(head, 450, 380));
  check("moving right/down takes that much off the edge offsets",
        wf.style.vars["--float-right"] === "90px"
        && wf.style.vars["--float-bottom"] === "50px", wf.style.vars);
  wf.fire("pointerup", ev(head, 450, 380));
  const lay = w.ctx.sessLayoutFor("s15");
  check("the move is written once, at release",
        lay.floatRight === 90 && lay.floatBottom === 50
        && lay.floatW === 400 && lay.floatH === 300, lay);
  check("...and the drag is over", !wf.classes.has("float-dragging"));
}
{
  const w = build({ stored: { s15: { split: true, float: true, floatW: 400, floatH: 300,
                                     floatRight: 40, floatBottom: 30 } } });
  w.ctx.setName("s15");
  w.ctx.syncSplitPane();
  const wf = w.nodes["term-wf"];
  const grip = partOf("split-frame-grip");
  wf.fire("pointerdown", ev(grip, 800, 600));
  // Several moves: each is measured from where the drag started, so a burst
  // must land on the last pointer's delta, not on their sum.
  wf.fire("pointermove", ev(grip, 810, 610));
  wf.fire("pointermove", ev(grip, 830, 620));
  wf.fire("pointerup", ev(grip, 830, 620));
  const lay = w.ctx.sessLayoutFor("s15");
  check("the grip sizes the box without moving it",
        lay.floatW === 430 && lay.floatH === 320
        && lay.floatRight === 40 && lay.floatBottom === 30, lay);
}
{
  // A move drag likewise does not compound: right/down offsets follow the
  // total delta from the start, however many pointermoves the mouse sent.
  const w = build({ stored: { s15: { split: true, float: true, floatW: 400, floatH: 300,
                                     floatRight: 40, floatBottom: 30 } } });
  w.ctx.setName("s15");
  w.ctx.syncSplitPane();
  const wf = w.nodes["term-wf"];
  const head = partOf("split-frame-head");
  wf.fire("pointerdown", ev(head, 500, 400));
  wf.fire("pointermove", ev(head, 480, 390));
  wf.fire("pointermove", ev(head, 450, 380));
  wf.fire("pointerup", ev(head, 450, 380));
  const lay = w.ctx.sessLayoutFor("s15");
  check("a burst of moves lands on the last delta, not their sum",
        lay.floatRight === 90 && lay.floatBottom === 50, lay);
}
{
  const w = build({ stored: { s15: { split: true, float: true } } });
  w.ctx.setName("s15");
  w.ctx.syncSplitPane();
  const wf = w.nodes["term-wf"];
  // The head's own controls are presses (the float checkbox included): a
  // press there must not turn into a drag of the pane.
  for (const sel of ["button", "input", "label"]) {
    const t = { closest: (s) => (s === ".split-frame-head" || s.includes(sel)) ? partOf(sel) : null };
    wf.fire("pointerdown", ev(t, 500, 400));
    check(`a press on the head's ${sel} is not a drag`, wf.captured.length === 0);
  }
  // A pointerdown anywhere else in the pane (its body) is not a drag either.
  wf.fire("pointerdown", ev({ closest: () => null }, 500, 400));
  check("a press in the pane's body is not a drag", wf.captured.length === 0);
  // And while docked, even the head does not drag.
  w.ctx.setSessLayout("s15", { float: false, split: true });
  wf.fire("pointerdown", ev(partOf("split-frame-head"), 500, 400));
  check("a docked pane's head does not drag", wf.captured.length === 0);
}

/* --- the wiring the change hangs on ------------------------------------ */
check("mountSplitFrame hands the frame the remembered flag and writes the answer back",
      /const lay = sessLayoutFor\(name\);/.test(app)
      && /float: lay\.float,/.test(app)
      && /onFloat: \(on\) => setSessLayout\(name, \{ float: !!on \}\)/.test(app));
check("syncSplitPane drives the frame's float state",
      /SplitFrame\?\.setFloat\(\$\("term-wf"\), float\)/.test(app));
check("a window resize re-clamps the painted box",
      /clampFloatPane\(\);/.test(app));
check("the pane takes the whole column back when it is not floating",
      /body\.split-float #term-split \{ display: none; \}/.test(css)
      && /body\.split-float #term-wf \{[\s\S]*?position: absolute;/.test(css)
      && /#main \{[^}]*position: relative;/.test(css));
check("the grip is drawn only while floating",
      /\.split-frame-grip \{ display: none; \}/.test(css)
      && /body\.split-float \.split-frame-grip \{\s*display: block;/.test(css));

if (failures) { console.log(`${failures} check(s) failed`); process.exit(1); }
console.log("all floating-pane checks passed");
