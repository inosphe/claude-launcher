/* Dragging the observer's cards sideways, run against the real handlers from
   observer.js with a stand-in element (claunch-ozuag).

   What has to hold:
   - a mouse press that moves past DRAG_START_PX mostly sideways scrolls the
     cards by the distance dragged and carries `.dragging` only while it lasts;
   - the click the browser sends on that release is swallowed, and the guard
     is gone by the next tick, so a later click goes through;
   - a short press, a mostly vertical move, a press on a control (button,
     link, field, fold), a touch press, and any press while the cards do not
     overflow sideways leave scrollLeft alone. The real browser path is in
     observer_browser.cjs's territory; this pins the decisions. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "observer.js"),
  "utf8");
const a = src.indexOf("const DRAG_START_PX"), b = src.indexOf('$("cards").addEventListener("pointerdown"');
if (a < 0 || b < a) throw new Error("cannot locate the drag handlers");

function makeCards() {
  const listeners = [];
  const classes = new Set();
  return {
    scrollWidth: 2000, clientWidth: 800, scrollLeft: 100,
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    setPointerCapture() {},
    addEventListener(type, fn, opts) { listeners.push({ type, fn, opts }); },
    removeEventListener(type, fn) { const i = listeners.findIndex(l => l.type === type && l.fn === fn); if (i >= 0) listeners.splice(i, 1); },
    click() {
      const ev = { prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() {} };
      for (const l of [...listeners].filter(l => l.type === "click")) {
        l.fn(ev);
        if (l.opts && l.opts.once) this.removeEventListener("click", l.fn);
      }
      return ev.prevented;
    },
  };
}
const timers = [];
let cards = makeCards();
const ctx = {};
new Function("exports", "$", "getSelection", "setTimeout", src.slice(a, b) + `
exports.start = dragStart; exports.move = dragMove; exports.end = dragEnd;
`)(ctx, () => cards, () => ({ removeAllRanges() {} }), fn => timers.push(fn));

const target = (control) => ({ closest: () => control ? {} : null });
const down = (over = {}) => ({ pointerType: "mouse", button: 0, pointerId: 1, clientX: 500, clientY: 300, target: target(false), ...over });
const at = (x, y) => ({ pointerId: 1, clientX: x, clientY: y, preventDefault() {} });

let failures = 0;
function check(what, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) { console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); failures++; }
}

ctx.start(down()); ctx.move(at(503, 300)); ctx.end(at(503, 300));
check("short press leaves it", cards.scrollLeft, 100);
check("short press click goes through", cards.click(), false);

ctx.start(down()); ctx.move(at(460, 302)); const during = cards.classList.contains("dragging");
ctx.move(at(380, 305)); ctx.end(at(380, 305));
check("drag scrolls by the distance", cards.scrollLeft, 220);
check("dragging only while it lasts", [during, cards.classList.contains("dragging")], [true, false]);
check("release click swallowed", cards.click(), true);
ctx.start(down()); ctx.move(at(440, 300)); ctx.end(at(440, 300));
timers.splice(0).forEach(fn => fn());
check("guard gone after the tick", cards.click(), false);

cards = makeCards();
ctx.start(down()); ctx.move(at(497, 340)); ctx.move(at(450, 360)); ctx.end(at(450, 360));
check("mostly vertical is not a drag", cards.scrollLeft, 100);
ctx.start(down({ target: target(true) })); ctx.move(at(400, 300)); ctx.end(at(400, 300));
check("a press on a control is left alone", cards.scrollLeft, 100);
ctx.start(down({ pointerType: "touch" })); ctx.move(at(400, 300)); ctx.end(at(400, 300));
check("touch keeps the browser's panning", cards.scrollLeft, 100);
cards.scrollWidth = 800;
ctx.start(down()); ctx.move(at(400, 300)); ctx.end(at(400, 300));
check("no overflow, no drag", cards.scrollLeft, 100);

if (failures) { console.error(`observerdrag_check: ${failures} failure(s)`); process.exit(1); }
console.log("observerdrag_check: ok");
