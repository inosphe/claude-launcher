/* The rail's ancestor band: the direct ancestors (parent, grandparent, ...) of
   the session at the top of the list, one line each, above the scroller.

   Why a harness rather than a screenshot: the band is computed, not drawn --
   whose chain it shows is a rule about the first VISIBLE row (the rail skips
   layout for rows out of view, hides filtered rows and pins sticky headings
   over its top edge), and the goto press has to re-point it at the card it
   lands on before the smooth scroll measures the list. Both are claims about
   numbers no picture proves. So the real functions are sliced out of app.js
   and driven against a stub band. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

/* ---- stub DOM ----
   Only what renderRailAncestors touches: elements with a class list, a dataset,
   children, a text value and the two append paths. */
function node(tag) {
  const n = {
    tag, _kids: [], _text: "", dataset: {}, attrs: {}, _classes: new Set(),
    _listeners: {},
    appendChild(k) { n._kids.push(k); return k; },
    append(...ks) { for (const k of ks) n.appendChild(k); },
    setAttribute(k, v) { n.attrs[k] = v; },
    addEventListener(ev, fn) { (n._listeners[ev] = n._listeners[ev] || []).push(fn); },
    fire(ev, target) {
      for (const fn of n._listeners[ev] || []) fn({ target, preventDefault() {} });
    },
    get children() { return n._kids; },
  };
  n.classList = {
    add(c) { n._classes.add(c); },
    remove(c) { n._classes.delete(c); },
    toggle(c, on) { if (on) n._classes.add(c); else n._classes.delete(c); },
    contains(c) { return n._classes.has(c); },
  };
  Object.defineProperty(n, "className", {
    get() { return [...n._classes].join(" "); },
    set(v) { n._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  Object.defineProperty(n, "textContent", {
    get() { return n._text || n._kids.map((k) => k.textContent).join(""); },
    set(v) { n._text = v; n._kids.length = 0; },
  });
  return n;
}

const band = node("ul");
band.classList.add("hidden");

/* Each row answers with the rect it was given: `top`/`height` in the same
   coordinates as the scroller, so "the first row a reader can see" is a
   measurement and not an index. */
const row = (name, top, height = 136) => ({
  dataset: { name },
  getBoundingClientRect() { return { top, bottom: top + height, height }; },
});

let rows = [];
const listeners = {};
const list = {
  _classes: new Set(),
  getBoundingClientRect() { return { top: 100, bottom: 500, height: 400 }; },
  querySelectorAll(sel) {
    if (sel !== "li[data-name]") throw new Error("unexpected list selector " + sel);
    return rows;
  },
  querySelector(sel) {
    const m = /^li\[data-name="(.*)"\]$/.exec(sel);
    if (!m) throw new Error("unexpected list selector " + sel);
    return rows.find((r) => r.dataset.name === m[1]) || null;
  },
  addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
};
list.classList = {
  add(c) { list._classes.add(c); },
  remove(c) { list._classes.delete(c); },
  toggle(c, on) { if (on) list._classes.add(c); else list._classes.delete(c); },
  contains(c) { return list._classes.has(c); },
};

const $ = (id) => (id === "session-list" ? list
  : id === "rail-ancestors" ? band : null);
const document = { createElement: (tag) => node(tag) };
const CSS = { escape: (s) => s };

/* A tree with a four-deep line (a -> b -> c -> d -> e), a root, a row whose
   parent is not in the list, and a two-node cycle. */
const sessionsCache = [
  { name: "a" },
  { name: "b", parent: "a" },
  { name: "c", parent: "b" },
  { name: "d", parent: "c" },
  { name: "e", parent: "d" },
  { name: "x", parent: "gone" },
  { name: "p", parent: "q" },
  { name: "q", parent: "p" },
];

let rafQueue = [];
const ctx = {};
new Function(
  "exports", "document", "$", "sessionsCache", "CSS",
  "requestAnimationFrame", "setTimeout",
  `const RAIL_ANCESTOR_MAX = ${/const RAIL_ANCESTOR_MAX = (\d+);/.exec(src)[1]};\n`
  + "let railAncestorPin = null;\nlet railAncestorHoldUntil = 0;\n"
  + "let railAncestorWired = false;\n"
  + "const revealCalls = [];\n"
  + "function revealSessionCard(n) { revealCalls.push(n); return true; }\n"
  + [slice("railAncestorChain"), slice("railTopVisibleRow"), slice("railRowOnScreen"),
     slice("renderRailAncestors"), slice("wireRailAncestors"),
     slice("pinRailAncestors"), slice("clearRailAncestorPin")].join("\n")
  + `
Object.assign(exports, {
  railAncestorChain, railTopVisibleRow, railRowOnScreen, renderRailAncestors,
  pinRailAncestors, clearRailAncestorPin,
  reveals: () => revealCalls,
  pin: () => railAncestorPin,
  max: () => RAIL_ANCESTOR_MAX,
});`
)(ctx, document, $, sessionsCache, CSS, (fn) => { rafQueue.push(fn); return 1; },
   (fn) => { rafQueue.push(fn); return 1; });

const names = () => band.children
  .map((li) => (li.children[0] || {}))
  .map((el) => (el.dataset && el.dataset.name) || null);
const buttons = () => band.children
  .map((li) => li.children[0])
  .filter((el) => el && el.classList && el.classList.contains("rail-ancestor"));
const hidden = () => band.classList.contains("hidden");
const chainNames = (n) => ctx.railAncestorChain(n).map((s) => s.name);

/* ---- the chain itself ---- */
check("parents come root first, the parent last",
      JSON.stringify(chainNames("d")) === JSON.stringify(["a", "b", "c"]),
      chainNames("d"));
check("a root has no ancestors", chainNames("a").length === 0, chainNames("a"));
check("a parent the list does not hold ends the walk",
      chainNames("x").length === 0, chainNames("x"));
check("a cycle ends the walk instead of repeating a name",
      JSON.stringify(chainNames("p")) === JSON.stringify(["q"]), chainNames("p"));
check("the list can be passed in instead of the cache",
      ctx.railAncestorChain("b", [{ name: "z" }, { name: "b", parent: "z" }])
        .map((s) => s.name).join(",") === "z");

/* ---- whose chain the band shows: the first row a reader can see ---- */
rows = [
  row("a", -386),   // scrolled above the top edge
  row("b", -250),
  row("c", -114),   // bottom 22: still above
  row("d", 22),     // bottom 158: the first one under the scroller's top
  row("e", 158),
];
ctx.renderRailAncestors();
check("the band is shown for a row with ancestors", !hidden());
check("it is anchored to the first visible row", band.dataset.anchor === "d",
      band.dataset.anchor);
check("one line per direct ancestor, root first",
      JSON.stringify(names()) === JSON.stringify(["a", "b", "c"]), names());
check("each line is a button naming its session",
      buttons().length === 3 && buttons().every((b) => b.tag === "button"
        && b.type === "button"), buttons().map((b) => b.tag));
check("each line says where it goes", buttons().every((b) => /go to .+ card/.test(b.title)));
check("a filtered row (no height) is skipped as an anchor", (() => {
  rows = [row("a", -386, 0), row("d", 22)];
  ctx.renderRailAncestors();
  return band.dataset.anchor === "d";
})(), band.dataset.anchor);
/* Most scroll frames land on the same chain: the band must not rebuild its
   lines for them (the nodes are the same objects after a second paint). */
check("an unchanged chain is not rebuilt", (() => {
  const first = band.children[0];
  ctx.renderRailAncestors();
  return band.children[0] === first;
})());

/* ---- a chain longer than the cap folds its oldest links ---- */
rows = [row("e", 22)];
ctx.renderRailAncestors();
check("the cap is three lines", ctx.max() === 3, ctx.max());
check("the chain is folded into a count plus the nearest links",
      band.children.length === 3
      && band.children[0].children[0].textContent === "⋯ +2"
      && JSON.stringify(names()) === JSON.stringify([null, "c", "d"]),
      { text: band.children[0] && band.children[0].children[0].textContent,
        names: names() });
check("the folded line names what it hides",
      /a → b/.test(band.children[0].children[0].title),
      band.children[0].children[0].title);

/* ---- a root session costs no space at all ---- */
rows = [row("a", 22), row("b", 158)];
ctx.renderRailAncestors();
check("a root anchor hides the band", hidden());
check("and the band keeps no lines while hidden", band.children.length === 0);
check("the anchor it read is still recorded", band.dataset.anchor === "a");

/* ---- the goto press pins the band to the card it lands on ---- */
rows = [row("a", -386), row("b", -250), row("c", -114), row("d", 22), row("e", 158)];
ctx.pinRailAncestors("e", 1000);
check("a pinned session wins over the row at the top",
      band.dataset.anchor === "e" && !hidden(), band.dataset.anchor);
check("the pin's lines are its own",
      JSON.stringify(names()) === JSON.stringify([null, "c", "d"]), names());
ctx.clearRailAncestorPin();
check("clearing the pin hands the band back to the scroll",
      band.dataset.anchor === "d", band.dataset.anchor);

/* A pin whose row is off screen and whose hold ran out falls back: the chain
   belongs to what is on screen, not to a session scrolled away. */
rows = [row("a", -386), row("b", -250), row("c", -114), row("d", 22), row("e", 700)];
ctx.pinRailAncestors("e", 0);
check("an off-screen pin without a hold falls back to the top row",
      band.dataset.anchor === "d", band.dataset.anchor);
ctx.pinRailAncestors("e", 5000);
check("...while the hold keeps it during the press's own scroll",
      band.dataset.anchor === "e", band.dataset.anchor);
ctx.clearRailAncestorPin();

/* ---- the grid view has no rows to anchor the band ---- */
list.classList.add("hidden");
ctx.renderRailAncestors();
check("the band hides with the list when the grid is shown", hidden());
check("and its anchor is cleared", band.dataset.anchor === "");
list.classList.remove("hidden");

/* ---- the wiring: one coalesced scroll frame, a click on a line jumps ---- */
rows = [row("a", -386), row("b", -250), row("c", -114), row("d", 22)];
ctx.renderRailAncestors();
rafQueue = [];
for (const fn of listeners.scroll || []) fn();
for (const fn of listeners.scroll || []) fn();
check("two scroll events share one frame", rafQueue.length === 1, rafQueue.length);
rafQueue.shift()();
check("the frame repaints the band", band.dataset.anchor === "d", band.dataset.anchor);
for (const ev of ["wheel", "touchstart", "keydown", "click"]) {
  check(`the rail's ${ev} drops the pin`, (listeners[ev] || []).length === 1);
}

ctx.pinRailAncestors("d", 1000);
const line = buttons().find((b) => b.dataset.name === "a");
band.fire("click", { closest: (sel) => (sel === ".rail-ancestor" ? line : null) });
check("a press on a line jumps to that ancestor's card",
      ctx.reveals().join(",") === "a", ctx.reveals());
band.fire("click", { closest: () => null });
check("a press between the lines jumps nowhere", ctx.reveals().join(",") === "a");

/* ---- the term-goto path ---- */
const reveal = slice("revealSessionCard");
const pinAt = reveal.indexOf("pinRailAncestors(name, GOTO_FLASH_MS)");
const scrollAt = reveal.indexOf("scrollIntoView");
check("the reveal pins the band before it measures the scroll",
      pinAt >= 0 && scrollAt >= 0 && pinAt < scrollAt, { pinAt, scrollAt });
check("the pin is held for the length of the mark (the smooth scroll's window)",
      /pinRailAncestors\(name, GOTO_FLASH_MS\)/.test(reveal));
check("the reveal is guarded for the sliced harnesses",
      /typeof pinRailAncestors === "function"/.test(reveal));
const refresh = slice("refreshSessions");
check("every rebuild repaints the band after the scroll position is restored",
      refresh.indexOf("renderRailAncestors()") > refresh.indexOf("list.scrollTop = keptScrollTop"));
check("the view switch repaints it too (the grid has no rows to anchor)",
      /renderRailAncestors/.test(slice("syncSessionView")));

/* ---- the markup and the stylesheet ---- */
check("the band is in the rail, above the list",
      html.indexOf('id="rail-ancestors"') > 0
      && html.indexOf('id="rail-ancestors"') < html.indexOf('id="session-list"'));
check("it starts hidden", /<ul id="rail-ancestors" class="hidden"/.test(html));
check("it is named for a screen reader",
      /<ul id="rail-ancestors"[^>]*aria-label="[^"]+"/.test(html));
check("the band sits above the scroller, not inside it",
      /#rail-ancestors\s*\{[^}]*flex:\s*none/.test(css));
check("a line is one row, clickable, and names its session",
      /#rail-ancestors \.rail-ancestor\s*\{/.test(css)
      && /#rail-ancestors \.rail-ancestor-name\s*\{/.test(css));
check("the folded count line has a style",
      /#rail-ancestors \.rail-ancestor-more\s*\{/.test(css));
check("the scroller reserves the sticky heading's height for rows brought into view",
      /#session-list\s*\{[^}]*scroll-padding-top:\s*\d+px/.test(css));

if (failures) {
  console.log(`${failures} failure(s)`);
  process.exit(1);
}
console.log("ok");
