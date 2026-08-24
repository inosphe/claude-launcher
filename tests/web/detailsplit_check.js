/* The docked detail rail's width is draggable on a wide screen and
   meaningless on a phone (where the detail is a page, not a column). The
   clamp and the apply are real functions sliced out of app.js and driven
   here; the parts that are wiring — the bar in the markup, the stylesheet
   reading the variable, the phone hiding the bar, the width surviving a
   reload through localStorage — are proved by reading the shipped files,
   because a stub DOM cannot host a media query. */
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
const constSrc = ["DETAIL_W_DEFAULT", "DETAIL_W_MIN", "detailWMax"]
  .map((n) => {
    const m = src.match(new RegExp(`const ${n} = [^\\n]+`));
    if (!m) throw new Error("missing const " + n);
    return m[0];
  })
  .join("\n");

/* ---- stub DOM: just enough for applyDetailW ---- */
const styles = {};
const ids = { layout: { style: { setProperty: (k, v) => { styles[k] = v; } } } };
const $ = (id) => ids[id];
let winW = 1400;
const windowStub = { get innerWidth() { return winW; } };

const ctx = {};
new Function(
  "exports", "$", "window",
  constSrc + "\n" + [slice("clampDetailW"), slice("applyDetailW")].join("\n") +
  `\nObject.assign(exports, { clampDetailW, applyDetailW });`
)(ctx, $, windowStub);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

/* ---- the clamp ---- */
check("garbage falls back to the default", ctx.clampDetailW(NaN) === 300,
      ctx.clampDetailW(NaN));
check("too narrow pins at the floor", ctx.clampDetailW(50) === 220,
      ctx.clampDetailW(50));
check("too wide pins at half the window", ctx.clampDetailW(5000) === 700,
      ctx.clampDetailW(5000));
check("fractions land on whole pixels", ctx.clampDetailW(300.4) === 300);
winW = 300;   // a window so narrow half of it is under the floor
check("the floor beats the half-window ceiling", ctx.clampDetailW(5000) === 220,
      ctx.clampDetailW(5000));
winW = 1400;

/* ---- the apply ---- */
ctx.applyDetailW(321);
check("the width lands on #layout as --detail-w", styles["--detail-w"] === "321px",
      styles);

/* ---- wiring: the width survives a reload ---- */
check("release writes the width down",
      /endDetailDrag[\s\S]{0,200}localStorage\.setItem\(DETAIL_W_KEY/.test(src));
check("boot reads it back through the clamp",
      src.includes(
        "applyDetailW(clampDetailW(Number(localStorage.getItem(DETAIL_W_KEY))"));

/* ---- wiring: the bar sits between #main and the detail rail ---- */
const sess = html.indexOf('id="sess-view"');
const bar = html.indexOf('id="detail-split"');
check("the bar is in the markup, before the detail rail it resizes",
      sess >= 0 && bar >= 0 && bar < sess, { bar, sess });
check("the layout move carries the bar with the rail",
      /insertBefore\(split, view\)/.test(src));
check("the bar hides whenever the rail is not the right column",
      src.includes('split.classList.toggle("hidden", !(up && !narrow))'));

/* ---- wiring: the stylesheet reads the variable, the phone drops the bar ---- */
check("the docked rail's width is the variable",
      css.includes("var(--detail-w"));
const mq = css.indexOf("@media (max-width: 820px)");
check("the phone breakpoint exists", mq >= 0);
check("the phone hides the bar",
      css.indexOf("#detail-split { display: none; }", mq) > mq);
check("the bar drags with col-resize, not the split bar's row-resize",
      /#detail-split \{[^}]*col-resize/.test(css));

console.log(failures ? `\n${failures} failure(s)` : "all detail-split checks passed");
process.exit(failures ? 1 : 0);
