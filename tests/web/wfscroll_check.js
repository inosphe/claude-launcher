/* The workflow run view wipes its container on every 2s poll (renderWfInto
   sets innerHTML = "" and rebuilds), and until captureWfScrolls/restoreWfScrolls
   a rebuilt view came back scrolled to the top — the place a reader had taken
   in the diagram, the reports column, or a long gate's prose in the button
   bar was gone two seconds after they moved it. This checks the pair that
   hold it: capture reads the run view's four scrollers before the wipe,
   restore puts each back after the rebuild — exactly for the sideways travel
   of .wf-cols, and for the columns that grow as the run moves the same
   near-end rule the message trace uses: whoever is riding the tail keeps
   riding it, anywhere else a poll leaves the position alone. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

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

const captureWfScrolls =
  new Function(`${slice("captureWfScrolls")}; return captureWfScrolls;`)();
const restoreWfScrolls =
  new Function(`${slice("restoreWfScrolls")}; return restoreWfScrolls;`)();

function assert(cond, msg) {
  if (!cond) { console.error(`FAIL: ${msg}`); process.exit(1); }
}

/* ---- stub DOM: a view whose querySelector reaches the scrollers, and
   scrollers that clamp like the browser does ---------------------------- */
function makeView() {
  const els = new Map();
  return {
    querySelector: (sel) => els.get(sel) || null,
    slot(sel, el) { els.set(sel, el); },
    drop(sel) { els.delete(sel); },
  };
}

function scroller({ scrollHeight, clientHeight, scrollWidth, clientWidth }) {
  let top = 0, left = 0;
  const clamp = (v, max) => (v < 0 ? 0 : v > max ? max : v);
  return {
    scrollHeight, clientHeight, scrollWidth, clientWidth,
    get scrollTop() { return top; },
    set scrollTop(v) { top = clamp(v, Math.max(0, scrollHeight - clientHeight)); },
    get scrollLeft() { return left; },
    set scrollLeft(v) { left = clamp(v, Math.max(0, scrollWidth - clientWidth)); },
  };
}

function fullView(over) {
  const view = makeView();
  view.slot(".wf-cols", scroller({ scrollHeight: 1000, clientHeight: 1000,
    scrollWidth: 4000, clientWidth: 1000 }));
  view.slot(".wf-diagram", scroller({ scrollHeight: 2000, clientHeight: 800,
    scrollWidth: 900, clientWidth: 900 }));
  view.slot(".wf-side", scroller({ scrollHeight: 3000, clientHeight: 500,
    scrollWidth: 760, clientWidth: 760 }));
  view.slot(".wf-bar", scroller({ scrollHeight: 1500, clientHeight: 600,
    scrollWidth: 4000, clientWidth: 4000 }));
  return view;
}

/* ---- a reader mid-way down the reports column: the poll puts them back -- */
{
  const view = fullView();
  const side = view.querySelector(".wf-side");
  side.scrollTop = 400;              // 2100px of headroom — plain mid-list
  const keep = captureWfScrolls(view);
  assert(keep.side.top === 400, "capture reads the side column's scrollTop");
  assert(keep.side.nearEnd === false,
         "a mid-list reader is not counted as riding the tail");
  assert(keep.diagram !== null && keep.bar !== null && keep.cols !== null,
         "capture records every scroller of the run view");

  // the poll wipes the tree: fresh scrollers, same dims, nothing scrolled
  const fresh = scroller({ scrollHeight: 3400, clientHeight: 500,
    scrollWidth: 760, clientWidth: 760 });
  view.drop(".wf-side");
  view.slot(".wf-side", fresh);
  restoreWfScrolls(view, keep);
  assert(fresh.scrollTop === 400, "a poll restores the reader's exact place");
}

/* ---- a reader at the very bottom: new content keeps them at the tail ---- */
{
  const view = fullView();
  const side = view.querySelector(".wf-side");
  side.scrollTop = 2500;             // the physical bottom of 3000@500
  const keep = captureWfScrolls(view);
  assert(keep.side.nearEnd === true,
         "reading the last line counts as riding the tail");
  const fresh = scroller({ scrollHeight: 3600, clientHeight: 500,
    scrollWidth: 760, clientWidth: 760 });   // the run moved; the column grew
  view.drop(".wf-side");
  view.slot(".wf-side", fresh);
  restoreWfScrolls(view, keep);
  assert(fresh.scrollTop === 3600 - 500,
         "a tail rider follows the new bottom, not their old line");
}

/* ---- the tolerance band: within 8px of the bottom still rides ----------- */
{
  const view = fullView();
  const side = view.querySelector(".wf-side");
  side.scrollTop = 2493;             // 7px of headroom — inside the band
  const keep = captureWfScrolls(view);
  assert(keep.side.nearEnd === true,
         "the 8px near-end band is captured as riding the tail");
}

/* ---- horizontal travel is restored exactly, even at the right edge ------ */
{
  const view = fullView();
  const cols = view.querySelector(".wf-cols");
  cols.scrollLeft = 120;             // mid-sideways on a narrow pane
  const keep = captureWfScrolls(view);
  const fresh = scroller({ scrollHeight: 1000, clientHeight: 1000,
    scrollWidth: 5000, clientWidth: 1000 });  // the columns got wider
  view.drop(".wf-cols");
  view.slot(".wf-cols", fresh);
  restoreWfScrolls(view, keep);
  assert(fresh.scrollLeft === 120,
         "a poll restores the sideways place, not the new right edge");

  const view2 = fullView();
  const edge = view2.querySelector(".wf-cols");
  edge.scrollLeft = 3000;            // hard against the current right edge
  const keep2 = captureWfScrolls(view2);
  const wider = scroller({ scrollHeight: 1000, clientHeight: 1000,
    scrollWidth: 6000, clientWidth: 1000 });
  view2.drop(".wf-cols");
  view2.slot(".wf-cols", wider);
  restoreWfScrolls(view2, keep2);
  assert(wider.scrollLeft === 3000,
         "a right-edge reader is left alone when the pane widens");
}

/* ---- content that shrank: the browser clamps to the new bottom ---------- */
{
  const view = fullView();
  view.querySelector(".wf-side").scrollTop = 400;
  const keep = captureWfScrolls(view);
  const shrunken = scroller({ scrollHeight: 300, clientHeight: 500,
    scrollWidth: 760, clientWidth: 760 });
  view.drop(".wf-side");
  view.slot(".wf-side", shrunken);
  restoreWfScrolls(view, keep);
  assert(shrunken.scrollTop === 0,
         "a shrunken column clamps the reader to its new end");
}

/* ---- first render and ragged trees: nothing to restore, no throw -------- */
{
  restoreWfScrolls(makeView(), null);
  restoreWfScrolls(makeView(), {});
  const empty = captureWfScrolls(makeView());
  assert(empty.cols === null && empty.side === null && empty.diagram === null
           && empty.bar === null,
         "a bare view captures nothing");
  // a tree missing one scroller still restores the ones it has
  const view = fullView();
  view.drop(".wf-bar");
  view.querySelector(".wf-side").scrollTop = 400;
  const keep = captureWfScrolls(view);
  const fresh = scroller({ scrollHeight: 3400, clientHeight: 500,
    scrollWidth: 760, clientWidth: 760 });
  view.drop(".wf-side");
  view.slot(".wf-side", fresh);
  restoreWfScrolls(view, keep);
  assert(fresh.scrollTop === 400,
         "the scrollers that exist are restored when one is missing");
}

console.log("wfscroll_check: all assertions passed");
