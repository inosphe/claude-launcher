/* The dropdown popup, which is the one part of a <select> the page does not
   paint.

   Every picker in this app is dark: the stylesheet gives the select box a
   light `color` on a near-black `background`. The CLOSED control obeys that
   pair. The OPEN popup does not — it is drawn by the platform, and Windows
   Chrome/Edge fall back to the system-white popup background while the
   options go on inheriting the select's light colour. The result is white
   text on a white popup: the rows are there, the highlight bar still tracks
   the mouse, and the list reads as EMPTY. That is a data bug's symptom worn
   by a styling bug, which is exactly why it is worth pinning here.

   So: options must carry their OWN colour pair, the pair must actually
   contrast, and the rule must be global — a per-form rule would leave the
   next dark select to rediscover this. Parsed out of the real stylesheet,
   because the stylesheet is the only thing that ships. */
const fs = require("fs");
const path = require("path");
const css = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "style.css"), "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* ---- a comment-blind rule reader ---------------------------------------
   Selectors are matched against the stylesheet with its block comments
   stripped: the stylesheet's own prose says "option" a dozen times, and a
   naive indexOf would happily accept a rule that exists only in a comment. */
const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");

/** Every declaration block whose selector list contains `sel` as a whole
    comma-separated selector (so `option` does not match `.wf-btn.option`). */
function rules(sel) {
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(bare))) {
    const selectors = m[1].split(",").map((s) => s.trim()).filter(Boolean);
    if (selectors.includes(sel)) out.push({ selectors, body: m[2] });
  }
  return out;
}

function decl(body, prop) {
  const m = new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`, "i").exec(body);
  return m ? m[1].trim().toLowerCase() : null;
}

/** #rgb / #rrggbb -> [r,g,b]; anything else -> null (the app only uses hex). */
function rgb(v) {
  if (!v) return null;
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v.trim());
  if (!m) return null;
  const h = m[1].length === 3 ? m[1].replace(/./g, (c) => c + c) : m[1];
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}

/** WCAG relative luminance, and the contrast ratio between two colours. */
function lum([r, g, b]) {
  const f = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
function contrast(a, b) {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

/* ---- the rule exists, globally ----------------------------------------- */
const optRules = rules("option");
check("the stylesheet declares a bare `option` rule", optRules.length >= 1,
  optRules.length);

const opt = optRules[0] || { selectors: [], body: "" };
check("...that is not scoped to one form (no descendant/id prefix)",
  opt.selectors.includes("option"), opt.selectors);
check("...and carries optgroup with it", opt.selectors.includes("optgroup"),
  opt.selectors);

/* ---- and it sets BOTH halves of the pair -------------------------------- */
const bg = decl(opt.body, "background-color") || decl(opt.body, "background");
const fg = decl(opt.body, "color");
check("options declare their own background", !!bg, opt.body);
check("options declare their own colour", !!fg, opt.body);

/* ---- which is the whole point: they must be readable against each other -- */
const bgc = rgb(bg);
const fgc = rgb(fg);
check("both are plain hex the check can compare", !!bgc && !!fgc, { bg, fg });
if (bgc && fgc) {
  const ratio = contrast(bgc, fgc);
  // 4.5:1 is WCAG AA for body text; a popup row is body text.
  check("option text contrasts with the option background (>= 4.5:1)",
    ratio >= 4.5, { bg, fg, ratio: Number(ratio.toFixed(2)) });
}

/* The failure this file exists for, stated as an assertion: the option
   background must not be left to the platform while a light colour is
   inherited from the select. If someone deletes the background-color and
   keeps the colour, the popup goes white again — and only this check
   notices, because every browser at the developer's desk may render it
   correctly. */
check("the option background is not `inherit`/`transparent`",
  !!bg && !["inherit", "transparent", "initial", "unset"].includes(bg), bg);

/* ---- a disabled option is offered, so it must still be legible ---------- */
const disRules = rules("option:disabled");
check("a disabled option gets its own colour", disRules.length >= 1);
if (disRules.length && bgc) {
  const dfg = rgb(decl(disRules[0].body, "color"));
  check("...that is still hex", !!dfg, disRules[0].body);
  if (dfg) {
    const dratio = contrast(bgc, dfg);
    // Deliberately dimmer than live text, but not invisible: 3:1 is the
    // WCAG floor for text that is large or non-essential, and a greyed
    // "(missing)" entry is read, not acted on.
    check("...and readable against the popup (>= 3:1)", dratio >= 3,
      { fg: decl(disRules[0].body, "color"), ratio: Number(dratio.toFixed(2)) });
    check("...and dimmer than a live option", lum(dfg) < lum(fgc || [255, 255, 255]));
  }
}

/* ---- every dark select is covered by it --------------------------------
   The rule is global, so this is really a check that nobody has since added
   a select rule that re-scopes options back out. Any rule that paints a
   select's own background dark is fine BECAUSE the global option rule
   stands; if the global rule ever loses its `background-color`, the checks
   above fail first and this one explains why it mattered. */
const selectRules = bare.match(/[^{}]*\bselect\b[^{}]*\{[^{}]*\}/g) || [];
const darkSelects = selectRules.filter((r) => {
  const b = decl(r.slice(r.indexOf("{")), "background") ||
    decl(r.slice(r.indexOf("{")), "background-color");
  const c = rgb(b);
  return c && lum(c) < 0.2;
});
check("the app really does have dark selects (the premise still holds)",
  darkSelects.length >= 3, darkSelects.length);

console.log("\nselectpopup_check: " + (failures ? failures + " failing" : "all ok"));
process.exitCode = failures ? 1 : 0;
