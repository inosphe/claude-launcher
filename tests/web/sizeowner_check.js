/* Who sizes the session, as the web terminal plays it (claunch-tre55).

   Two people looking at one session used to resize it to their own windows
   in turn: every focus and refit claimed the size, the other side redrew
   and claimed it back, and both screens redrew without end. The daemon now
   applies only the size owner's resizes (daemon/ws.py, "Who sizes the
   session"); this pins the client's half:

     - the owner fits the session to its box, as before;
     - a viewer without the size never refits its grid — it mirrors the
       owner's — and only asks for the size with its box's dimensions,
       which the daemon grants when nobody else is looking;
     - the header's `take size` button is up only while another viewer
       holds the size, and pressing it steals the size, then resizes the
       session to this window;
     - xterm's own resize event never reaches the daemon from a viewer
       without the size.

   The real functions are sliced out of the shipped app.js and driven
   against a stub world. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);
const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "index.html"),
  "utf8"
);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
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

function build(o) {
  const sent = [];
  const fits = [];
  const chip = {
    hidden: true, on: {},
    classList: { toggle: (c, on) => { if (c === "hidden") chip.hidden = !!on; } },
    addEventListener: (ev, fn) => { chip.on[ev] = fn; },
  };
  const term = { cols: 80, rows: 24, options: { fontSize: 14 } };
  const fitAddon = {
    fit: () => { fits.push(true); term.cols = 120; term.rows = 40; },
    proposeDimensions: () => ({ cols: 120, rows: 40 }),
  };
  const ws = { readyState: 1, send: (s) => sent.push(JSON.parse(s)) };
  const code = ["setRenderFont", "fitView", "localFit", "requestSize",
    "renderSizeChip", "stealSize"].map(slice).join("\n");
  const api = new Function(
    "$", "WebSocket", "term", "fitAddon", "ws", "canFit", "terminalOnScreen",
    `let sizeOwner = ${o.owner}, sizeHeld = ${o.held};\n` +
    "let sessionEnded = false;\n" +
    "const fontSize = 14;\nconst VIEW_FONT_MIN = 4;\n" +
    code +
    "\nreturn {localFit, renderSizeChip, stealSize," +
    " get owner() { return sizeOwner; } };"
  )((id) => (id === "term-size" ? chip : null), { OPEN: 1 }, term, fitAddon, ws,
    () => true, () => true);
  return { api, sent, fits, chip, term };
}

/* --- the owner fits the session to its box ----------------------------- */
{
  const w = build({ owner: true, held: true });
  w.api.localFit();
  check("the owner refits", w.fits.length === 1, w.fits);
  check("and asks nothing (xterm's resize event carries it)", w.sent.length === 0, w.sent);
  w.api.renderSizeChip();
  check("the owner is offered no take-size button", w.chip.hidden === true);
}

/* --- a viewer without the size mirrors, and only asks ------------------ */
{
  const w = build({ owner: false, held: true });
  w.api.localFit();
  check("a viewer without the size does not refit its grid", w.fits.length === 0, w.fits);
  check("its grid stays the owner's", w.term.cols === 80 && w.term.rows === 24,
    [w.term.cols, w.term.rows]);
  check("it asks for the size with its box's dimensions",
    JSON.stringify(w.sent) === JSON.stringify([{ type: "resize", cols: 120, rows: 40 }]),
    w.sent);
  w.api.renderSizeChip();
  check("and is offered the take-size button", w.chip.hidden === false);
}

/* --- nobody holds it: no button, the next fit is the claim ------------- */
{
  const w = build({ owner: false, held: false });
  w.api.renderSizeChip();
  check("no button while nobody holds the size", w.chip.hidden === true);
}

/* --- the steal ---------------------------------------------------------- */
{
  const w = build({ owner: false, held: true });
  w.api.stealSize();
  check("pressing it makes this viewer the owner", w.api.owner === true);
  check("the steal goes first, then this window's size, then a repaint",
    JSON.stringify(w.sent.map((m) => m.type)) === JSON.stringify(["steal", "resize", "repaint"]),
    w.sent);
  check("the size sent is this window's", w.sent[1].cols === 120 && w.sent[1].rows === 40,
    w.sent[1]);
  check("and the button goes away", w.chip.hidden === true);
}

/* --- xterm's resize event is gated on the size ------------------------- */
{
  const body = slice("freshAttach");
  const hook = body.slice(body.indexOf("term.onResize("));
  check("xterm's own resize reaches the daemon only from the owner",
    /if \(applyingRemoteResize\) return;[\s\S]*?if \(!sizeOwner\) return;[\s\S]*?ws\.send/.test(hook));
}

/* --- the button is in the session header ------------------------------- */
{
  const at = html.indexOf('id="term-size"');
  check("the take-size button sits in the terminal header",
    at > html.indexOf('id="term-header"') && at < html.indexOf('id="terminal"'));
  check("and its press is wired", /\$\("term-size"\)\.addEventListener\("click", stealSize\)/.test(src));
  const m = html.indexOf('id="m-size"');
  check("the phone's top bar carries a mirror of it",
    m > html.indexOf('id="mobile-top"') && m < html.indexOf('id="sidebar"'));
  check("and the mirror's press is wired too",
    /\$\("m-size"\)\.addEventListener\("click", stealSize\)/.test(src));
}

if (failures) {
  console.log(`${failures} size-owner check(s) failed`);
  process.exit(1);
}
console.log("all size-owner checks passed");
