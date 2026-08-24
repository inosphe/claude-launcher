/* The rail's width is draggable on a wide screen and meaningless on a phone
   (where the rail is a full-screen mode, not a column). The clamp and the
   apply are real functions sliced out of app.js and driven here; the parts
   that are wiring — the bar in the markup, the stylesheet reading the
   variable, the phone hiding the bar, the width surviving a reload through
   localStorage — are proved by reading the shipped files, because a stub DOM
   cannot host a media query. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");

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

/* the constants the clamp reads, taken from the shipped file so the test
   cannot drift from it */
const constSrc = ["RAIL_W_DEFAULT", "RAIL_W_MIN", "railWMax"]
  .map((n) => {
    const m = src.match(new RegExp(`const ${n} = [^\\n]+`));
    if (!m) throw new Error("missing const " + n);
    return m[0];
  })
  .join("\n");

/* ---- stub DOM: just enough for applyRailW ---- */
const styles = {};
const ids = { layout: { style: { setProperty: (k, v) => { styles[k] = v; } } } };
const $ = (id) => ids[id];
let winW = 1400;
const windowStub = { get innerWidth() { return winW; } };

const ctx = {};
new Function(
  "exports", "$", "window",
  constSrc + "\n" + [slice("clampRailW"), slice("applyRailW")].join("\n") +
  `\nObject.assign(exports, { clampRailW, applyRailW });`
)(ctx, $, windowStub);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

/* ---- the clamp ---- */
check("garbage falls back to the default", ctx.clampRailW(NaN) === 260,
      ctx.clampRailW(NaN));
check("too narrow pins at the floor", ctx.clampRailW(50) === 180,
      ctx.clampRailW(50));
check("too wide pins at half the window", ctx.clampRailW(5000) === 700,
      ctx.clampRailW(5000));
check("fractions land on whole pixels", ctx.clampRailW(300.4) === 300);
winW = 300;   // a window so narrow half of it is under the floor
check("the floor beats the half-window ceiling", ctx.clampRailW(5000) === 180,
      ctx.clampRailW(5000));
winW = 1400;

/* ---- the apply ---- */
ctx.applyRailW(321);
check("the width lands on #layout as --rail-w", styles["--rail-w"] === "321px",
      styles);

/* ---- wiring: the width survives a reload ---- */
check("release writes the width down",
      /endRailDrag[\s\S]{0,200}localStorage\.setItem\(RAIL_W_KEY/.test(src));
check("boot reads it back through the clamp",
      src.includes(
        "applyRailW(clampRailW(Number(localStorage.getItem(RAIL_W_KEY))"));

/* ---- wiring: the bar sits between the rail and #main ---- */
const aside = html.indexOf("</aside>");
const bar = html.indexOf('id="rail-split"');
const main = html.indexOf('<main id="main"');
check("the bar is in the markup, after the rail, before #main",
      aside >= 0 && aside < bar && bar < main, { aside, bar, main });

/* ---- wiring: the stylesheet reads the variable, the phone drops the bar ---- */
check("the rail's width is the variable", css.includes("var(--rail-w"));
const mq = css.indexOf("@media (max-width: 820px)");
check("the phone breakpoint exists", mq >= 0);
check("the phone hides the bar",
      css.indexOf("#rail-split { display: none; }", mq) > mq);
check("the phone frees the width too",
      css.indexOf("width: auto", mq) > mq);
check("the bar drags with col-resize, not the split bar's row-resize",
      /#rail-split \{[^}]*col-resize/.test(css));

console.log(failures ? `\n${failures} failure(s)` : "all rail-split checks passed");
process.exit(failures ? 1 : 0);
