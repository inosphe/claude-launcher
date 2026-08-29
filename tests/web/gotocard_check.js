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
   its dataset, plus whatever classes the other painters put on it. */
function row(name, cls) {
  const n = { dataset: { name }, classes: new Set(cls || []), scrolls: [] };
  n.classList = {
    add: (c) => n.classes.add(c),
    remove: (c) => n.classes.delete(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
  };
  n.scrollIntoView = (opts) => n.scrolls.push(opts);
  return n;
}
let rail = [];
const document = {
  querySelectorAll: (sel) => {
    if (sel !== "#session-list li") throw new Error("unexpected selector " + sel);
    return rail;
  },
};

/* setTimeout, driven by hand: the mark's expiry is a claim about time, and a
   test that actually waited 1.6s for it would be a test nobody runs. */
let timers = new Map();
let nextTimer = 1;
const setTimeout_ = (fn, ms) => { timers.set(nextTimer, { fn, ms }); return nextTimer++; };
const clearTimeout_ = (id) => { timers.delete(id); };
const fire = (id) => { const t = timers.get(id); timers.delete(id); t.fn(); };
const only = () => [...timers.keys()][0];

const ctx = {};
new Function(
  "exports", "document", "setTimeout", "clearTimeout",
  // The module-level state the three functions share, taken from app.js
  // itself so the window this checks is the shipped one.
  `let currentName = "s193";\n` +
  /const GOTO_FLASH_MS = \d+;/.exec(src)[0] + "\n" +
  "let gotoFlashName = null;\nlet gotoFlashTimer = null;\n" +
  [slice("applyGotoFlash"), slice("revealSessionCard"),
   slice("gotoSessionCard")].join("\n") +
  `
Object.assign(exports, {
  applyGotoFlash, revealSessionCard, gotoSessionCard,
  flashMs: () => ${/const GOTO_FLASH_MS = (\d+);/.exec(src)[1]},
  setCur: (n) => { currentName = n; },
});`
)(ctx, document, setTimeout_, clearTimeout_);

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
      timers.size === 1 && !timers.has(first), [...timers.keys()]);
fire(only());
check("the surviving timer clears the mark it belongs to", flashed().length === 0);

/* ---- the two nothing-to-do cases are no-ops, not throws ---- */
rail = [row("s127"), row("s191")];
ctx.setCur("s193");                       // attached, but the rail is a poll behind
check("no row for this session yet: says so", ctx.gotoSessionCard() === false);
check("...and scrolls nothing", scrolledTo().length === 0, scrolledTo());
check("...and marks nothing", flashed().length === 0, flashed());
ctx.setCur(null);                         // no terminal up at all
check("no attached session: says so", ctx.gotoSessionCard() === false);
check("...and still no timer is left running", timers.size === 0, [...timers.keys()]);

/* ---- a row that predates scrollIntoView (jsdom, an old view) ---- */
rail = [Object.assign(row("s193"), { scrollIntoView: undefined })];
ctx.setCur("s193");
check("a row that cannot scroll is still marked rather than throwing",
      ctx.gotoSessionCard() === true && flashed().length === 1, flashed());

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
check("the chip has a rest and a hover state", /\.goto-chip:hover\s*\{/.test(css));

if (failures) {
  console.log(`${failures} failure(s)`);
  process.exit(1);
}
console.log("ok");
