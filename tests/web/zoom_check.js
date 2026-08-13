/* The terminal header's text-size knob. A step is not a private zoom: the
   grid is however many cells fit the box, so changing the glyph changes the
   cols and rows the session is told it has. That makes the interesting rules
   the ones a stylesheet cannot show — that a step refits, that the size is
   clamped and remembered, and that the readout and the two buttons agree with
   the size actually in force. Slice them out of the shipped app.js and drive
   them against a stub. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to);
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
  for (const id of ["term-zoom-out", "term-zoom-in", "term-zoom-level",
                    "m-zoom-out", "m-zoom-in"]) {
    nodes[id] = {
      id, textContent: "", disabled: false, on: {},
      addEventListener: (ev, fn) => { nodes[id].on[ev] = fn; },
      // as a browser does it: a disabled control does not activate, so a
      // mirror pressing a spent header button gets nothing, not an extra step
      click: () => { if (!nodes[id].disabled) nodes[id].on.click(); },
    };
  }
  const store = new Map();
  if (o.stored !== undefined) store.set("claunch_fontsize:/", String(o.stored));
  const storage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  };
  const fits = [];
  const term = o.attached ? { options: { fontSize: 0 } } : null;
  const fitAddon = { fit: () => fits.push(term ? term.options.fontSize : null) };
  // Through the button wiring and the load-time sync, so the harness starts
  // where a freshly loaded page does.
  const code = slice("const FONT_KEY =", "/* Bind the terminal to a session");
  const api = new Function(
    "$", "BASE", "localStorage", "term", "fitAddon", "canFit",
    code + "\nreturn {clampFont, setFontSize, syncZoomControls," +
    " get fontSize() { return fontSize; }," +
    " FONT_DEFAULT, FONT_MIN, FONT_MAX};"
  )((id) => nodes[id], "/", storage, term, fitAddon,
    () => o.attached && !o.offscreen);
  return { api, nodes, store, fits, term };
}

/* --- a step scales the session, not just this tab ---------------------- */
{
  const w = build({ attached: true });
  const start = w.api.fontSize;
  w.api.setFontSize(start + 2);
  check("the glyph the terminal draws with follows",
        w.term.options.fontSize === start + 2, w.term.options.fontSize);
  check("and a fit turns that into a new grid for the daemon",
        w.fits.length === 1 && w.fits[0] === start + 2, w.fits);
}
/* Off screen the box measures zero, and fitting to it would hand the session
   a garbage size — the same guard the viewport-driven fits use. */
{
  const w = build({ attached: true, offscreen: true });
  w.api.setFontSize(w.api.fontSize + 1);
  check("no fit while the terminal is not on screen", w.fits.length === 0, w.fits);
  check("but the size is still applied for when it is",
        w.term.options.fontSize === w.api.fontSize, w.term.options.fontSize);
}
/* Pressed from the home view, with no terminal open at all. */
{
  const w = build({ attached: false });
  w.api.setFontSize(20);
  check("a step with nothing attached is remembered, not dropped",
        w.api.fontSize === 20 && w.store.get("claunch_fontsize:/") === "20",
        w.store.get("claunch_fontsize:/"));
}

/* --- what the three buttons do ----------------------------------------- */
{
  const w = build({ attached: true });
  const start = w.api.fontSize;
  w.nodes["term-zoom-in"].click();
  check("+ steps up one", w.api.fontSize === start + 1, w.api.fontSize);
  w.nodes["term-zoom-out"].click();
  w.nodes["term-zoom-out"].click();
  check("− steps down one each press", w.api.fontSize === start - 1, w.api.fontSize);
  check("every press refits", w.fits.length === 3, w.fits);
  w.nodes["term-zoom-level"].click();
  check("the readout is the way back to the default",
        w.api.fontSize === w.api.FONT_DEFAULT, w.api.fontSize);
}

/* --- the phone's mirrors ----------------------------------------------- */
{
  // The header is display:none below the breakpoint, so these are the only
  // way to the size there — and they must mean exactly what the header means.
  const w = build({ attached: true });
  const start = w.api.fontSize;
  w.nodes["m-zoom-in"].click();
  w.nodes["m-zoom-in"].click();
  check("the bar's + steps the same size the header does",
        w.api.fontSize === start + 2, w.api.fontSize);
  check("and refits like any other press", w.fits.length === 2, w.fits);
  w.nodes["m-zoom-out"].click();
  check("the bar's − likewise", w.api.fontSize === start + 1, w.api.fontSize);
}
{
  const w = build({ stored: String(build({}).api.FONT_MIN) });
  check("a limit greys the mirror out too, not just the header",
        w.nodes["m-zoom-out"].disabled === true
        && w.nodes["term-zoom-out"].disabled === true
        && w.nodes["m-zoom-in"].disabled === false);
  w.nodes["m-zoom-out"].click();
  check("and pressing it anyway changes nothing",
        w.api.fontSize === w.api.FONT_MIN, w.api.fontSize);
}

/* --- clamping ---------------------------------------------------------- */
{
  const { api } = build({});
  check("below the floor lands on it", api.clampFont(1) === api.FONT_MIN);
  check("above the ceiling lands on it", api.clampFont(999) === api.FONT_MAX);
  check("a size that isn't a number falls back to the default",
        api.clampFont(NaN) === api.FONT_DEFAULT
        && api.clampFont(Infinity) === api.FONT_DEFAULT);
  check("fractions settle on a whole pixel", api.clampFont(13.4) === 13);
}
{
  const w = build({ attached: true });
  w.api.setFontSize(w.api.FONT_MAX + 50);
  check("a press cannot step past the ceiling",
        w.api.fontSize === w.api.FONT_MAX, w.api.fontSize);
  check("and the readout says the size in force, not the one asked for",
        w.nodes["term-zoom-level"].textContent === `${w.api.FONT_MAX}px`,
        w.nodes["term-zoom-level"].textContent);
  check("with nothing left to press on that side",
        w.nodes["term-zoom-in"].disabled === true
        && w.nodes["term-zoom-out"].disabled === false);
}

/* --- remembered across loads ------------------------------------------- */
{
  const w = build({ stored: "19" });
  check("a stored size is adopted at load", w.api.fontSize === 19, w.api.fontSize);
  check("and shown before anything is pressed",
        w.nodes["term-zoom-level"].textContent === "19px",
        w.nodes["term-zoom-level"].textContent);
}
{
  // Junk in localStorage (hand-edited, or a key another tool wrote) must not
  // open the terminal at a size xterm cannot draw.
  const w = build({ stored: "not-a-size" });
  check("junk falls back to the default", w.api.fontSize === w.api.FONT_DEFAULT,
        w.api.fontSize);
}
{
  const w = build({ stored: "900" });
  check("a stored size out of range is clamped, not honoured",
        w.api.fontSize === w.api.FONT_MAX, w.api.fontSize);
}

/* --- the key is scoped, like the token --------------------------------- */
{
  // Several daemons reach the browser through one relay origin and share its
  // localStorage; an unscoped key would let each tunnel resize its siblings.
  const key = slice("const FONT_KEY =", "const FONT_DEFAULT");
  check("the size key carries the base path", key.includes("${BASE}"), key.trim());
}

/* --- the markup the code reaches for ----------------------------------- */
for (const id of ["term-zoom-out", "term-zoom-level", "term-zoom-in",
                  "m-zoom", "m-zoom-out", "m-zoom-in"]) {
  check(`index.html declares #${id}`, html.includes(`id="${id}"`));
}
check("the knob sits in the terminal header",
      html.indexOf('class="term-zoom"') > html.indexOf('id="term-header"')
      && html.indexOf('class="term-zoom"') < html.indexOf('id="terminal"'));
check("and its mirrors on the bar that replaces that header",
      html.indexOf('id="m-zoom"') > html.indexOf('id="mobile-top"')
      && html.indexOf('id="m-zoom"') < html.indexOf('id="sidebar"'));
// It comes up with the terminal it sizes, so it starts hidden like the other
// two mirrors and syncMobileBars decides from there.
check("the mirrors start hidden", /id="m-zoom" class="hidden"/.test(html));

if (failures) { console.log(`${failures} check(s) failed`); process.exit(1); }
console.log("all zoom checks passed");
