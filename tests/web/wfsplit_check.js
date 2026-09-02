/* The run page's diagram column (.wf-diagram) is draggable against the
   reports beside it (.wf-side), same contract as the rail's and the
   detail's bars — except .wf-cols is rebuilt from scratch by renderWfInto on
   every poll, which would cut a drag short the instant its own node is
   replaced under the pointer. wfColDragHost is the fix: renderWfInto skips
   its rebuild for as long as it names the pane being dragged, so the nodes
   a drag closed over stay attached for the whole gesture. The clamp and the
   width-apply are real functions sliced out of app.js and driven here; the
   rebuild guard, the mobile opt-out and the CSS wiring are proved by
   reading the shipped files, because a stub DOM cannot host a media query
   or a live drag. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
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

/* the constants the clamp reads, taken from the shipped file so the test
   cannot drift from it */
const constSrc = ["WF_DIA_W_DEFAULT", "WF_DIA_W_MIN", "wfDiaWMax"]
  .map((n) => {
    const m = src.match(new RegExp(`const ${n} = [^\\n]+`));
    if (!m) throw new Error("missing const " + n);
    return m[0];
  })
  .join("\n");

/* ---- stub DOM: just enough for clampWfDiaW / setWfColW ---- */
let winW = 1400;
const windowStub = { get innerWidth() { return winW; } };

function styleStub() {
  return { flex: "", maxWidth: "" };
}

const ctx = {};
new Function(
  "exports", "window",
  constSrc + "\n" + [slice("clampWfDiaW"), slice("setWfColW")].join("\n") +
  `\nObject.assign(exports, { clampWfDiaW, setWfColW });`
)(ctx, windowStub);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

/* ---- the clamp ---- */
check("garbage falls back to the default", ctx.clampWfDiaW(NaN) === 380,
      ctx.clampWfDiaW(NaN));
check("too narrow pins at the floor", ctx.clampWfDiaW(50) === 260,
      ctx.clampWfDiaW(50));
check("too wide pins at the window-minus-reports ceiling",
      ctx.clampWfDiaW(5000) === 1400 - 420, ctx.clampWfDiaW(5000));
check("fractions land on whole pixels", ctx.clampWfDiaW(300.4) === 300);
winW = 500;   // a window so narrow the reports-column reserve eats past the floor
check("the floor beats a ceiling the reserve pushed under it",
      ctx.clampWfDiaW(5000) === 260, ctx.clampWfDiaW(5000));
winW = 1400;

/* ---- the apply ---- */
{
  const dia = styleStub(), side = styleStub();
  ctx.setWfColW({ style: dia }, { style: side }, 420);
  check("a width pins the diagram's flex-basis", dia.flex === "0 0 420px", dia);
  check("the same width lifts the stylesheet's max-width ceiling",
        dia.maxWidth === "420px", dia);
  check("the reports column takes what is left", side.flex === "1 1 0", side);

  ctx.setWfColW({ style: dia }, { style: side }, null);
  check("null clears back to the stylesheet's elastic default",
        dia.flex === "" && dia.maxWidth === "" && side.flex === "", { dia, side });
}

/* ---- wiring: a drag survives the poll that rebuilds .wf-cols ---- */
check("renderWfInto skips its rebuild while this pane's bar is held",
      /if \(wfColDragHost === ui\.host\) return;/.test(src));
check("the drag release replays the data the skipped poll(s) held",
      /onUp = \(\) => \{[\s\S]{0,600}renderSplit\(splitLastData\)[\s\S]{0,200}renderWf\(wfLastData\)/.test(src));
check("release writes the width down under a host-scoped key",
      /onUp = \(\) => \{[\s\S]{0,400}localStorage\.setItem\(wfDiaWKey\(host\)/.test(src));
check("double-click resets and forgets the stored width",
      /dblclick[\s\S]{0,100}localStorage\.removeItem\(wfDiaWKey\(host\)\)/.test(src));
check("boot reads the width back through the clamp",
      /loadWfDiaW[\s\S]{0,200}clampWfDiaW\(Number\(raw\)\)/.test(src));

/* ---- wiring: no bar, no forced width, on a phone ---- */
check("the split bar is skipped on a phone",
      /if \(!MOBILE_MQ\.matches\) \{\s*\n\s*cols\.appendChild\(wfSplitBar\(ui\.host, dia, side\)\)/.test(src));

/* ---- wiring: the two homes (full page / terminal split pane) keep separate widths ---- */
check("the storage key is scoped by host, not shared between the two homes",
      src.includes("`claunch_wfdiaw:${host}:${BASE}`"));

/* ---- wiring: the stylesheet has the bar, and it drags with col-resize ---- */
check(".wf-split exists beside .wf-diagram/.wf-side",
      /\.wf-split \{[^}]*col-resize/.test(css));
check(".wf-diagram keeps its stylesheet ceiling for the undragged default",
      /\.wf-diagram \{[^}]*max-width: 640px/.test(css));

console.log(failures ? `\n${failures} failure(s)` : "all wf-split checks passed");
process.exit(failures ? 1 : 0);
