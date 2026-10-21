/* The header's `⇱ card`: the one control up there that moves the READER and
   not the session. The rail lists every session at once in a column that
   scrolls, so the row for the terminal you are typing in is as likely to be
   out of sight as not, and until this button there was nothing on the page
   that could put it back.

   Worth a harness rather than a stylesheet claim because the failure is not
   "it did not scroll" — it is scrolling to the WRONG row (the active class
   and the attached name are not the same question), and it is the mark being
   thrown away half a second later by the 2s poll that rebuilds the rail
   whole. Both are invisible in a screenshot and both are exactly what this
   checks: the real functions are sliced out of app.js and driven against a
   stub rail. */
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

/* ---- stub rail ----
   A row is what refreshSessions builds: an <li> carrying the session name in
   its dataset, plus whatever classes the other painters put on it. Grouped,
   only the open tab's rows are in the list. */
function node(dataset, cls, parentElement) {
  const n = { dataset, classes: new Set(cls || []), scrolls: [], parentElement };
  n.classList = {
    add: (c) => n.classes.add(c),
    remove: (c) => n.classes.delete(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
  };
  n.scrollIntoView = (opts) => n.scrolls.push(opts);
  return n;
}
/* The rail box itself: rows live in it, and the reveal lays it out for real
   by putting a class on it (layRailOut, claunch-ayaig). `offsetHeight` is the
   read that forces that layout in a browser; here it only has to exist. */
const list = { id: "session-list", parentElement: null, offsetHeight: 0 };
list.classList = {
  _set: new Set(),
  add(c) { list.classList._set.add(c); },
  remove(c) { list.classList._set.delete(c); },
  contains(c) { return list.classList._set.has(c); },
};
list.querySelectorAll = (sel) => {
  if (sel !== "li[data-name]") throw new Error("unexpected list selector " + sel);
  return rail;
};
const row = (name, cls, parent = list) => node({ name }, cls, parent);
let rail = [];
/* The tab strip as renderSessionGroupTabs builds it: one tab per group, each
   carrying the rows its group holds. Opening a tab redraws the rail from the
   records in hand (refreshSessions with `cached`), which puts that tab's
   rows in the list. */
let tabs = [];
let opened = [];
let redraws = [];
let onRedraw = null;   // what the real refreshSessions calls at its end
const tab = (group, value, names) => ({
  dataset: { group, value },
  _groupRows: names.map((name) => ({ type: "session", session: { name } })),
});
const tabBar = { querySelectorAll: (sel) => {
  if (sel !== ".session-group-tab") throw new Error("unexpected tab selector " + sel);
  return tabs;
} };
const setSessionGroupTab = (group, value) => opened.push([group, value]);
const refreshSessions = (opts) => {
  redraws.push(opts);
  const [group, value] = opened.at(-1);
  const open = tabs.find((t) => t.dataset.group === group && t.dataset.value === value);
  rail = open._groupRows.map((r) => row(r.session.name));
  if (onRedraw) onRedraw(opts);
};

/* ---- stub grid ----
   The layout answers where a session sits; the drawn cells are what
   renderSessionGrid put on screen, which leaves out a folded stretch. */
let sessionView = "list";
let cells = [];
let hiddenInFold = new Set();    // sessions whose cell a folded stretch hides
let renders = 0;
const sessionGridUnfolded = new Set();
const layout = {
  rows: [{ id: "r0" }, { id: "r1" }],
  place: {},
  positionOf: (name) => layout.place[name] || null,
  foldRuns: (r, perLine, present, minLines) =>
    (r === 1 && perLine === 4 && minLines === 3 && !present.has("s050"))
      ? [{ start: 0, end: 3 }] : [],
};
const sessionGridLayout = () => layout;
const sessionGridPerLine = 4;
const SESSION_GRID_FOLD_LINES = 3;
const sessionGridVisible = () => [{ name: "s193" }, { name: "s127" }];
const renderSessionGrid = (force) => {
  renders++;
  if (sessionGridUnfolded.has("r1:0")) hiddenInFold.clear();
  cells = cells.filter((c) => !hiddenInFold.has(c.dataset.name));
};
const cell = (name) => node({ name }, ["sg-cell"], null);
const grid = {
  querySelector: (sel) => {
    const m = /^\.sg-cell\[data-name="(.*)"\]$/.exec(sel);
    if (!m) throw new Error("unexpected grid selector " + sel);
    return cells.find((c) => c.dataset.name === m[1] && !hiddenInFold.has(m[1])) || null;
  },
};
const $ = (id) => (id === "session-list" ? list : id === "session-grid" ? grid
  : id === "session-group-tabs" ? tabBar : null);
const CSS = { escape: (s) => s };
const document = {
  querySelectorAll: (sel) => {
    if (sel !== "#session-list li, #session-grid .sg-cell") throw new Error("unexpected selector " + sel);
    return rail.concat(cells.filter((c) => !hiddenInFold.has(c.dataset.name)));
  },
};

/* setTimeout, driven by hand: the mark's expiry is a claim about time, and a
   test that actually waited 1.6s for it would be a test nobody runs. */
let timers = new Map();
let nextTimer = 1;
const setTimeout_ = (fn, ms) => { timers.set(nextTimer, { fn, ms }); return nextTimer++; };
const clearTimeout_ = (id) => { timers.delete(id); };
const fire = (id) => { const t = timers.get(id); timers.delete(id); t.fn(); };
/* A press leaves two timers: the mark's and the one that hands the rail back
   to the layout skip (claunch-ayaig). They are told apart by their delay, the
   way the page does. */
const ofMs = (ms) => [...timers.keys()].filter((id) => timers.get(id).ms === ms);
const only = () => ofMs(FLASH_MS)[0];
const laidOnly = () => ofMs(LAID_MS)[0];
const flashTimers = () => ofMs(FLASH_MS);
const laidTimers = () => ofMs(LAID_MS);
let FLASH_MS = 0;
let LAID_MS = 0;
/* Ends whatever a press left pending, so the next block starts from a rail
   that is neither marked nor laid out. */
const settle = () => { for (const id of [...timers.keys()]) fire(id); };

const ctx = {};
const stubs = {
  $, CSS, setSessionGroupTab, refreshSessions,
  sessionGridLayout, sessionGridPerLine, SESSION_GRID_FOLD_LINES, sessionGridVisible,
  sessionGridUnfolded, renderSessionGrid: (f) => renderSessionGrid(f),
};
new Function(
  "exports", "document", "setTimeout", "clearTimeout", "getView", ...Object.keys(stubs),
  // The module-level state the functions share, taken from app.js itself so
  // the window this checks is the shipped one.
  `let currentName = "s193";\n` +
  /const GOTO_FLASH_MS = \d+;/.exec(src)[0] + "\n" +
  /const RAIL_LAID_OUT_MS = \d+;/.exec(src)[0] + "\n" +
  "let gotoFlashName = null;\nlet gotoFlashTimer = null;\n" +
  "let railLaidOutTimer = null;\n" +
  [slice("applyGotoFlash"), slice("layRailOut"), slice("revealSessionListRow"),
   slice("revealSessionGridCell"),
   slice("revealSessionCard"), slice("gotoSessionCard"),
   /const BOOT_GOTO_POLLS = \d+;/.exec(src)[0],
   "let bootGotoName = null;\nlet bootGotoTries = 0;",
   slice("armBootGoto"), slice("revealBootSessionCard")].join("\n")
    .replace(/\bsessionView\b/g, "getView()") +
  `
Object.assign(exports, {
  applyGotoFlash, revealSessionCard, gotoSessionCard,
  armBootGoto, revealBootSessionCard, bootPending: () => bootGotoName,
  bootPolls: () => BOOT_GOTO_POLLS,
  flashMs: () => ${/const GOTO_FLASH_MS = (\d+);/.exec(src)[1]},
  laidMs: () => ${/const RAIL_LAID_OUT_MS = (\d+);/.exec(src)[1]},
  setCur: (n) => { currentName = n; },
});`
)(ctx, document, setTimeout_, clearTimeout_, () => sessionView, ...Object.values(stubs));
FLASH_MS = ctx.flashMs();
LAID_MS = ctx.laidMs();

const flashed = () => rail.filter((r) => r.classes.has("goto-flash")).map((r) => r.dataset.name);
const scrolledTo = () => rail.filter((r) => r.scrolls.length).map((r) => r.dataset.name);

/* ---- it goes to the attached session's row, not to the lit one ---- */
rail = [row("s127"), row("s191", ["active"]), row("s193"), row("s192")];
check("the press answers that it found a row", ctx.gotoSessionCard() === true);
check("it scrolls the row whose name is the attached session",
      JSON.stringify(scrolledTo()) === JSON.stringify(["s193"]), scrolledTo());
check("centred, so a row brought to the rail's edge is not called found",
      rail[2].scrolls[0] && rail[2].scrolls[0].block === "center", rail[2].scrolls[0]);
check("and the mark lands on that same row",
      JSON.stringify(flashed()) === JSON.stringify(["s193"]), flashed());
check("the row the rail had lit is left alone",
      rail[1].classes.has("active") && !rail[1].classes.has("goto-flash"));

/* ---- the rail is laid out for real before it is scrolled (claunch-ayaig) ----
   A row below the fold is not laid out: it answers with the
   contain-intrinsic-size fallback until it renders, and a smooth scroll
   commits to the position it measured. On a 200-row rail of 136px rows that
   put the row 5,928px below the top -- out of view -- while the same press
   with the rows laid out landed exactly. */
check("the press lays the rail out before it measures it",
      list.classList.contains("rail-laid-out"));
check("...on a timer of its own, so the skip comes back",
      laidTimers().length === 1 && LAID_MS > 0 && LAID_MS <= 2000,
      laidTimers().map((id) => timers.get(id).ms));
check("...and the mark's timer is a separate one",
      flashTimers().length === 1 && FLASH_MS !== LAID_MS, [FLASH_MS, LAID_MS]);
if (laidOnly()) fire(laidOnly());
check("...the skip is back once that timer fires",
      !list.classList.contains("rail-laid-out"));

/* ---- the mark survives a changed poll that rebuilds the rail ----
   A class written only onto the old node could be gone before a smooth
   scroll finished. */
const marked = only();
rail = [row("s127"), row("s191"), row("s193"), row("s192")];   // as a rebuild leaves it
check("a rebuilt rail has no mark until it is repainted", flashed().length === 0);
ctx.applyGotoFlash();
check("refreshSessions' repaint puts it back on the right row",
      JSON.stringify(flashed()) === JSON.stringify(["s193"]), flashed());

/* ---- and it is dropped when its time is up ---- */
check("the mark is held for a moment, not forever", ctx.flashMs() > 0 && ctx.flashMs() <= 5000,
      ctx.flashMs());
fire(marked);
check("the row is clean once the timer fires", flashed().length === 0, flashed());
ctx.applyGotoFlash();
check("and a later rebuild does not resurrect it", flashed().length === 0, flashed());

/* ---- a second press moves the mark, and does not leave the first timer to
        wipe the second row early ---- */
rail = [row("s127"), row("s193")];
ctx.gotoSessionCard();
const first = only();
ctx.setCur("s127");
ctx.gotoSessionCard();
check("the mark moved to the newly attached session",
      JSON.stringify(flashed()) === JSON.stringify(["s127"]), flashed());
check("the first press's expiry was cancelled, not left pending",
      flashTimers().length === 1 && !timers.has(first), [...timers.keys()]);
fire(only());
check("the surviving timer clears the mark it belongs to", flashed().length === 0);

/* ---- the two nothing-to-do cases are no-ops, not throws ---- */
settle();
rail = [row("s127"), row("s191")];
ctx.setCur("s193");                       // attached, but the rail is a poll behind
check("no row for this session yet: says so", ctx.gotoSessionCard() === false);
check("...and scrolls nothing", scrolledTo().length === 0, scrolledTo());
check("...and marks nothing", flashed().length === 0, flashed());
ctx.setCur(null);                         // no terminal up at all
check("no attached session: says so", ctx.gotoSessionCard() === false);
check("...and still no timer is left running", timers.size === 0, [...timers.keys()]);
check("...and the rail was not laid out for a scroll that never happens",
      !list.classList.contains("rail-laid-out"));

/* ---- a row that predates scrollIntoView (jsdom, an old view) ---- */
rail = [Object.assign(row("s193"), { scrollIntoView: undefined })];
ctx.setCur("s193");
check("a row that cannot scroll is still marked rather than throwing",
      ctx.gotoSessionCard() === true && flashed().length === 1, flashed());

/* ---- list view: a row in a closed tab is opened, then scrolled ----
   Grouped, the list holds the open tab's rows alone; a session in another
   group has no row to scroll to until its tab is open. */
{
  tabs = [tab("mesh", "mesh-1", ["s127"]), tab("mesh", "mesh-2", ["s050", "s193"])];
  opened = [["mesh", "mesh-1"]];
  redraws = [];
  rail = [row("s127")];
  ctx.setCur("s193");
  check("a row in a closed tab is still found", ctx.gotoSessionCard() === true);
  check("the tab holding it is opened",
        JSON.stringify(opened.at(-1)) === JSON.stringify(["mesh", "mesh-2"]), opened);
  check("the rail is redrawn from the records in hand, with no request",
        redraws.length === 1 && redraws[0] && redraws[0].cached === true, redraws);
  check("then the row is scrolled and marked",
        JSON.stringify(scrolledTo()) === JSON.stringify(["s193"])
        && JSON.stringify(flashed()) === JSON.stringify(["s193"]), [scrolledTo(), flashed()]);
  fire(only());
  // A row already in the list opens nothing and redraws nothing.
  redraws = [];
  ctx.gotoSessionCard();
  check("a row already drawn redraws nothing", redraws.length === 0, redraws);
  fire(only());
  // A session in no tab at all (not in the poll yet) is a no-op.
  ctx.setCur("s999");
  check("a session no tab holds is not found", ctx.gotoSessionCard() === false);
  check("and nothing was opened for it", redraws.length === 0, redraws);
  tabs = [];
  ctx.setCur("s193");
}

/* ---- grid view: the cell is scrolled to and marked, not the hidden list ---- */
{
  settle();
  sessionView = "grid";
  rail = [row("s193")];                     // the list is still built behind the grid
  layout.place = { s193: { row: 0, col: 2 }, s050: { row: 1, col: 5 } };
  cells = [cell("s127"), cell("s193")];
  hiddenInFold = new Set();
  renders = 0;
  ctx.setCur("s193");
  check("grid: the press finds the cell", ctx.gotoSessionCard() === true);
  check("grid: the cell is scrolled to, centred",
        cells[1].scrolls.length === 1 && cells[1].scrolls[0].block === "center", cells[1].scrolls);
  check("grid: the hidden list row is not scrolled", rail[0].scrolls.length === 0);
  check("grid: the cell is highlighted", cells[1].classes.has("goto-flash")
        && !cells[0].classes.has("goto-flash"));
  check("grid: a drawn cell needs no redraw", renders === 0, renders);
  fire(only());
  check("grid: the highlight expires with the timer", !cells[1].classes.has("goto-flash"));

  // A session whose cell sits in a folded stretch: open it, redraw, then find it.
  const hidden = cell("s050");
  cells = [cell("s193"), hidden];
  hiddenInFold = new Set(["s050"]);
  sessionGridUnfolded.clear();
  ctx.setCur("s050");
  check("grid: a cell in a folded stretch is found", ctx.gotoSessionCard() === true);
  check("grid: the stretch holding it is opened",
        sessionGridUnfolded.has("r1:0"), [...sessionGridUnfolded]);
  check("grid: the grid is redrawn once", renders === 1, renders);
  check("grid: then scrolled to and highlighted",
        hidden.scrolls.length === 1 && hidden.classes.has("goto-flash"));
  fire(only());

  // Not placed in the grid at all: a no-op, like a list with no row.
  ctx.setCur("s999");
  check("grid: a session with no cell says so", ctx.gotoSessionCard() === false);
  check("grid: ...and leaves no timer", timers.size === 0, [...timers.keys()]);
  sessionView = "list";
}

/* ---- arrival: the page opened on #/s/<name> goes to that card once ----
   (claunch-wyrsh) boot() arms it after its route(); a full poll retries while
   the card is not drawn yet, a bounded number of times, and only for the
   session the address named. */
{
  settle();
  sessionView = "list";
  tabs = [];
  rail = [row("s127"), row("s193")];
  ctx.setCur("s193");
  ctx.armBootGoto();
  check("arrival: the card already drawn is scrolled to at once",
        JSON.stringify(scrolledTo()) === JSON.stringify(["s193"]), scrolledTo());
  check("arrival: centred, as the button does",
        rail[1].scrolls[0] && rail[1].scrolls[0].block === "center", rail[1].scrolls[0]);
  check("arrival: marked, as the button does",
        JSON.stringify(flashed()) === JSON.stringify(["s193"]), flashed());
  check("arrival: done, nothing left armed", ctx.bootPending() === null, ctx.bootPending());
  fire(only());
  rail = [row("s127"), row("s193")];
  ctx.revealBootSessionCard();
  check("arrival: a later poll does not scroll again", scrolledTo().length === 0, scrolledTo());

  // The record is a poll behind: armed, then found by the poll that draws it.
  rail = [row("s127")];
  ctx.armBootGoto();
  check("arrival: no card yet, nothing scrolled", scrolledTo().length === 0, scrolledTo());
  check("arrival: and it stays armed for the next poll", ctx.bootPending() === "s193");
  rail = [row("s127"), row("s193")];
  ctx.revealBootSessionCard();
  check("arrival: the poll that draws the card scrolls to it",
        JSON.stringify(scrolledTo()) === JSON.stringify(["s193"]), scrolledTo());
  check("arrival: then disarmed", ctx.bootPending() === null);
  fire(only());

  // Never drawn: gives up after a few polls, no timer left behind.
  rail = [row("s127")];
  ctx.armBootGoto();
  for (let i = 0; i < 10; i++) ctx.revealBootSessionCard();
  check("arrival: gives up on a card that never comes", ctx.bootPending() === null);
  check("arrival: the limit is a few polls", ctx.bootPolls() >= 2 && ctx.bootPolls() <= 10,
        ctx.bootPolls());
  check("arrival: and leaves no mark or timer",
        flashTimers().length === 0 && flashed().length === 0);

  // The reader walks to another session before the card arrives: not chased.
  rail = [row("s127")];
  ctx.armBootGoto();
  ctx.setCur("s127");
  ctx.revealBootSessionCard();
  check("arrival: walking elsewhere first disarms it",
        scrolledTo().length === 0 && ctx.bootPending() === null, scrolledTo());

  // No session on the address (home, another page): nothing armed.
  ctx.setCur(null);
  ctx.armBootGoto();
  check("arrival: a page with no session arms nothing", ctx.bootPending() === null);

  // The card is in a closed tab: opening it redraws through refreshSessions,
  // which calls back in. That re-entry must not reveal twice.
  tabs = [tab("mesh", "mesh-1", ["s127"]), tab("mesh", "mesh-2", ["s193"])];
  opened = [["mesh", "mesh-1"]];
  redraws = [];
  rail = [row("s127")];
  let reentered = 0;
  onRedraw = () => { reentered++; ctx.revealBootSessionCard(); };
  ctx.setCur("s193");
  ctx.armBootGoto();
  onRedraw = null;
  check("arrival: the redraw's call back in happened", reentered === 1, reentered);
  check("arrival: and the card was scrolled exactly once",
        rail.length === 1 && rail[0].scrolls.length === 1, rail.map((r) => r.scrolls.length));
  fire(only());
  tabs = [];
}

/* ---- the markup and the stylesheet ---- */
const headStart = html.indexOf('id="term-header"');
const headEnd = html.indexOf('id="term-queued"');
const head = html.slice(headStart, headEnd);
check("the button is in the session header", head.includes('id="term-goto"'), headStart);
check("it sits with the name it acts on, before the buttons that change the session",
      head.indexOf('id="term-goto"') > head.indexOf('id="term-title"')
      && head.indexOf('id="term-goto"') < head.indexOf('class="term-actions"'));
check("it is a button, not a link", /<button id="term-goto"[^>]*type="button"/.test(head), head.slice(0, 200));
check("it says what it does on hover", /<button id="term-goto"[\s\S]{0,240}?title="[^"]+"/.test(head));
check("app.js wires the press to it",
      /\$\("term-goto"\)\.addEventListener\("click"/.test(src));
check("refreshSessions repaints the mark after every rebuild",
      /applyBriefingCards\(\);\s*(\/\/[^\n]*\n\s*)*applyGotoFlash\(\);/.test(src));
check("the stylesheet draws the marked row", /#session-list li\.goto-flash\s*\{/.test(css));
check("and the marked grid cell, after the cell states it must win over",
      /#session-grid \.sg-cell\.goto-flash\s*\{/.test(css)
      && css.indexOf("#session-grid .sg-cell.goto-flash") > css.indexOf("#session-grid .sg-cell.gated {")
      && css.indexOf("#session-grid .sg-cell.goto-flash") > css.indexOf("#session-grid .sg-cell:focus"));
check("a grid redraw repaints the mark on the cell",
      /name === gotoFlashName\) cell\.classList\.add\("goto-flash"\)/.test(slice("sessionGridCell")));
check("the mark's state is declared before the load-time grid draw reads it",
      src.indexOf("let gotoFlashName") >= 0
      && src.indexOf("let gotoFlashName") < src.indexOf("\nsyncSessionView();"));
check("boot arms the arrival reveal right after its one route()",
      /\n    route\(\);\s*(\/\/[^\n]*\n\s*)*if \(typeof armBootGoto === "function"\) armBootGoto\(\);/.test(slice("boot")));
check("every full poll gives the arrival reveal its retry, a cached redraw does not",
      /if \(!cached && typeof revealBootSessionCard === "function"\) revealBootSessionCard\(\);/
        .test(slice("refreshSessions")));
check("the chip has a rest and a hover state", /\.goto-chip:hover\s*\{/.test(css));
check("the stylesheet lets a revealed rail render every row for real",
      /#session-list\.rail-laid-out li\[data-name\]\s*\{[^}]*content-visibility:\s*visible/
        .test(css));
check("the reveal is what lays the rail out, and only in the list view",
      /function layRailOut\(\)/.test(src)
      && /const laidOut = sessionView === "grid" \? null : layRailOut\(\);/.test(src));

/* The press and the ancestor band above the list (railancestors_check.js owns
   the band itself): a jump has to show the landed card's chain, and the band
   changes the list's box, so it is pointed and painted before the scroll is
   measured. */
const revealSrc = slice("revealSessionCard");
const pinAt = revealSrc.indexOf("pinRailAncestors(name, GOTO_FLASH_MS)");
check("the press pins the ancestor band to the row it lands on",
      pinAt >= 0, revealSrc.slice(0, 0));
check("...before the scroll that has to measure the list with the band in place",
      pinAt >= 0 && pinAt < revealSrc.indexOf("scrollIntoView"),
      { pinAt, scrollAt: revealSrc.indexOf("scrollIntoView") });
check("the pin call is guarded, so a sliced harness still runs the press",
      /typeof pinRailAncestors === "function"/.test(revealSrc));
check("every rebuild repaints the band from the rows it just drew",
      /renderRailAncestors\(\)/.test(slice("refreshSessions")));
check("the band is in the rail, above the list it describes",
      html.indexOf('id="rail-ancestors"') > 0
      && html.indexOf('id="rail-ancestors"') < html.indexOf('id="session-list"'));

if (failures) {
  console.log(`${failures} failure(s)`);
  process.exit(1);
}
console.log("ok");
