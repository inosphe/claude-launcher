/* The light theme's colour rewrite (theme.js, claunch-xpe81), run under node.

   Pinned here:
     - a grey inverts its lightness exactly: the page background and the body
       text trade places, and a mid grey stays a mid grey;
     - muted grey text and borders are darkened past 1 - L (claunch-zguui),
       keeping their order, and black/white stay at the ends;
     - a dark surface used as a background is lifted past 1 - L toward white
       (claunch-zguui), keeps its order against the other surfaces, and the
       lift ends at SURFACE_L so mid-tone fills and borders still invert
       exactly;
     - hue and saturation survive the flip (red stays red);
     - translucent black and black inside a shadow are left alone, so a
       shadow does not become a glow;
     - saturated text is capped dark enough to read on a light page, while
       the same colour as a background is not capped;
     - every colour in a value is rewritten (gradients, shadow lists), the
       hex, rgb(), rgba(), hsl() and keyword spellings all parse, and a value
       with no colour in it comes back unchanged;
     - `color-scheme: dark` becomes light. */
const path = require("path");
const theme = require(path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "theme.js"));

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

const rgb = (s) => theme.parseColor(s);
const lum = ([r, g, b]) => (Math.max(r, g, b) + Math.min(r, g, b)) / 2 / 255;

// Greys: lightness 1 - L, exactly (to the rounding of a channel).
const bg = rgb(theme.lightColor("#14161a", "background-color"));
check("page background turns light", lum(bg) > 0.88, bg);
check("page background lifted past 1 - L", lum(bg) > 0.94, bg);
const plain = rgb(theme.lightColor("#14161a", "fill"));
check("same grey outside a background is not lifted", Math.abs(lum(plain) - (1 - lum(rgb("#14161a")))) < 0.01, plain);
const rail = rgb(theme.lightColor("#1b1e24", "background-color"));
check("surfaces keep their order", lum(rail) < lum(bg), [rail, bg]);
const midFill = rgb(theme.lightColor("#808080", "background-color"));
check("mid-grey fill not lifted", Math.abs(lum(midFill) - 0.5) < 0.02, midFill);
const text = rgb(theme.lightColor("#d8dbe0", "color"));
check("body text turns dark", lum(text) < 0.18, text);
check("pure white to pure black", theme.lightColor("#fff", "background-color") === "rgb(0, 0, 0)",
  theme.lightColor("#fff", "background-color"));
check("white keyword", theme.lightColor("white", "border-color") === "rgb(0, 0, 0)");
check("black keyword", theme.lightColor("black", "background-color") === "rgb(255, 255, 255)");
const mid = rgb(theme.lightColor("#808080", "fill"));
check("mid grey stays mid", Math.abs(lum(mid) - 0.5) < 0.02, mid);

// Muted grey text and borders are darkened past 1 - L, keeping their order.
const muted = rgb(theme.lightColor("#7d8590", "color"));
check("muted grey text darkened", lum(muted) < 0.36, muted);
const dimmer = rgb(theme.lightColor("#6b7280", "color"));
check("grey text keeps its order", lum(dimmer) > lum(muted), [dimmer, muted]);
const hair = rgb(theme.lightColor("#30363d", "border-color"));
check("hairline border darkened", lum(hair) < 0.75, hair);
check("black text stays white", theme.lightColor("#000", "color") === "rgb(255, 255, 255)");

// Hue survives.
const red = rgb(theme.lightColor("#ff7b72", "background-color"));
check("red stays red", red[0] > red[1] && red[0] > red[2], red);
const blue = rgb(theme.lightColor("#58a6ff", "border-color"));
check("blue stays blue", blue[2] > blue[0] && blue[2] > blue[1], blue);

// Shadows and scrims.
check("translucent black kept", theme.lightColor("rgba(0, 0, 0, 0.5)", "background-color") === "rgba(0, 0, 0, 0.5)");
check("black in a shadow kept", theme.lightColor("#000", "box-shadow") === "#000");
check("translucent white darkens", theme.lightColor("rgba(255, 255, 255, 0.08)", "background-color") === "rgba(0, 0, 0, 0.08)",
  theme.lightColor("rgba(255, 255, 255, 0.08)", "background-color"));
check("alpha kept", theme.parseColor(theme.lightColor("#58a6ff80", "background-color"))[3] === 0.502,
  theme.lightColor("#58a6ff80", "background-color"));

// Accent text is capped; the same colour as a fill is not.
const okText = rgb(theme.lightColor("#3fb950", "color"));
check("accent text capped", lum(okText) <= 0.401, okText);
const okFill = rgb(theme.lightColor("#3fb950", "background-color"));
check("accent fill not capped", lum(okFill) > 0.45, okFill);

// Whole values.
const grad = theme.lightValue("linear-gradient(rgb(20, 22, 26), #d8dbe0 60%)", "background-image");
check("gradient rewrites both stops", !/20, 22, 26|d8dbe0/.test(grad) && /linear-gradient\(rgb\(.*\), rgb\(.*\) 60%\)/.test(grad), grad);
const shadow = theme.lightValue("0 0 0 1px #58a6ff, 0 4px 12px rgba(0, 0, 0, 0.4)", "box-shadow");
check("shadow list: ring rewritten, black kept", !shadow.includes("#58a6ff") && shadow.endsWith("rgba(0, 0, 0, 0.4)"), shadow);
check("hsl parses", theme.parseColor("hsl(0, 100%, 50%)").slice(0, 3).map(Math.round).join() === "255,0,0");
check("space syntax parses", theme.parseColor("rgb(10 20 30 / 50%)").join() === "10,20,30,0.5");
check("no colour, no change", theme.lightValue("1px solid currentColor", "border-top") === "1px solid currentColor");
check("color-scheme", theme.lightValue("dark", "color-scheme") === "light");
check("unreadable token kept", theme.lightColor("#12", "color") === "#12");

if (failures) {
  console.log(`${failures} failure(s)`);
  process.exit(1);
}
console.log("theme_check: ok");
