/* The throughput reading, as the dashboard draws it, run against the real
   functions from app.js.

   The daemon hangs a `tps` block on a session that goes through the metering
   shim — the last call's tokens/s, its time to first token, the model, a
   short rolling median — or nothing at all for a session that never does
   (the OAuth routes). Three places draw it: the rail row's own line, the
   open briefing card's chip, and the attached session's header badge with
   its twin overlay on the PTY. The rules these checks hold:

   - Absence is drawn as nothing. No `tps` block means no line, no chip, a
     hidden badge and a hidden overlay — never "0 tok/s".
   - A call that was not counted (an error answer) still shows that the
     session called out: its HTTP status stands where the number would.
   - A reading older than ten minutes dims (`stale`) instead of vanishing.
   - The overlay is drawn only while the terminal is on screen, and it is
     pointer-transparent by CSS (railayout_check's sibling in test_metering
     pins that), so it can never take a click from the PTY. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

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

/* A DOM just deep enough: elements with a class list, children and a
   title; `$` hands out the three header/terminal nodes by id. */
function node(tag, cls, text) {
  const n = {
    tag, className: cls || "", textContent: text ?? "", title: "", kids: [],
    classList: {
      toggle(c, on) {
        const has = n.className.split(/\s+/).filter(Boolean);
        const i = has.indexOf(c);
        if (on === undefined) on = i < 0;
        if (on && i < 0) has.push(c);
        if (!on && i >= 0) has.splice(i, 1);
        n.className = has.join(" ");
      },
      contains(c) { return n.className.split(/\s+/).includes(c); },
    },
    append(...cs) { n.kids.push(...cs); },
    appendChild(c) { n.kids.push(c); return c; },
    replaceChildren(...cs) { n.kids = cs; },
  };
  return n;
}
const dom = {
  "term-tps": node("span", "badge tps hidden"),
  "term-tps-overlay": node("div", "hidden"),
  "terminal": node("div", ""),
};

const ctx = {};
new Function(
  "exports", "el", "$", "sessionsCache", "currentName", "railOpen",
  ["fmtAge", "modelShort", "terminalOnScreen", "tpsFmt", "tpsAgeSecs", "ttftFmt",
   "tpsText", "tpsTooltip", "tpsStaleClass", "tpsRailLine", "tpsChip",
   "renderTermTps"].map(slice).join("\n") + `
exports.text = tpsText;
exports.tooltip = tpsTooltip;
exports.rail = tpsRailLine;
exports.chip = tpsChip;
exports.render = renderTermTps;
exports.setSessions = (s) => { sessionsCache = s; };
exports.setCurrent = (n) => { currentName = n; };
exports.setRailOpen = (v) => { railOpen = v; };
`)(ctx, node, (id) => dom[id], [], null, false);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const AT = new Date(Date.now() - 45_000).toISOString();          // 45s ago
const OLD = new Date(Date.now() - 3_600_000).toISOString();      // 1h ago
/* 38.06, not 38.05: toFixed(1) on the binary neighbour of 38.05 gives 38.0,
   and the check is about the format, not about float rounding. */
const FAST = {
  ts: AT, model: "deepseek-flash", status: 200, counted: true, tps: 38.06,
  ttft_ms: 715, output_tokens: 16, input_tokens: 36, cache_read: 0,
  window: 4, tps_median: 35.2, ttft_ms_median: 900, upstream: "https://api.deepseek.com/v1",
};
const BIG = { ...FAST, tps: 248.75, ttft_ms: 1054 };
const ERR = { ts: AT, model: null, status: 502, counted: false, tps: null, ttft_ms: null,
              window: 1, tps_median: null, ttft_ms_median: null };
const STALE = { ...FAST, ts: OLD, window: 1, tps_median: 38.06 };

const PI = { name: "pi", harness: "pi", tps: FAST };
const CLAUDE = { name: "ds4", harness: "claude", tps: BIG };
const BROKEN = { name: "broken", harness: "claude", tps: ERR };
const QUIET = { name: "quiet", harness: "claude", tps: STALE };
const OAUTH = { name: "nc", harness: "claude" };                 // never metered

/* ---- the glance ---- */
check("tokens/s with one decimal, and the time to first token",
      ctx.text(FAST), "38.1 tok/s · ttft 715ms");
check("a big number drops the decimal, a slow first token reads in seconds",
      ctx.text(BIG), "249 tok/s · ttft 1.1s");
check("an uncounted error answer shows its status where the number would be",
      ctx.text(ERR), "HTTP 502");
check("no block at all is an empty string, never a zero", ctx.text(null), "");

/* ---- the story ---- */
const tip = ctx.tooltip(FAST);
check("the tooltip dates the call and names the model",
      tip.split("\n")[0], "last call 45s ago on deepseek-flash (HTTP 200)");
check("...gives the token counts behind the rate",
      tip.includes("38.1 tokens/s over the generation, 16 out, 36 in"), true);
check("...and the rolling median with its window",
      tip.includes("median over the last 4 calls: 35.2 tok/s, ttft 900ms"), true);
check("...and where the call went", tip.includes("via https://api.deepseek.com/v1"), true);
check("an error answer's tooltip has no rate lines",
      ctx.tooltip(ERR).includes("tokens/s"), false);

/* ---- the rail row's line ---- */
const line = ctx.rail(PI);
check("the rail line is a bolt, the glance and the age, with the story on hover",
      [line.className, line.kids.map((k) => k.textContent), line.title === tip],
      ["rail-tps-line", ["⚡", "38.1 tok/s · ttft 715ms", "45s ago"], true]);
check("a reading an hour old dims", ctx.rail(QUIET).className, "rail-tps-line stale");
check("an error answer still gets a line", ctx.rail(BROKEN).kids[1].textContent, "HTTP 502");
check("a session that never went through the shim gets no line at all",
      ctx.rail(OAUTH), null);

/* ---- the card chip ---- */
ctx.setSessions([PI, CLAUDE, BROKEN, QUIET, OAUTH]);
check("the chip is the glance with the story on hover",
      [ctx.chip("pi").textContent, ctx.chip("pi").className, ctx.chip("pi").title === tip],
      ["38.1 tok/s · ttft 715ms", "sess-brief-tps", true]);
check("and dims when stale", ctx.chip("quiet").className, "sess-brief-tps stale");
check("no chip for an unmetered session, nor for one the rail no longer knows",
      [ctx.chip("nc"), ctx.chip("gone")], [null, null]);

/* ---- the header badge and the PTY overlay ---- */
ctx.setCurrent("ds4");
ctx.render();
check("the attached session's badge shows its last rate",
      [dom["term-tps"].className, dom["term-tps"].textContent],
      ["badge tps", "249 tok/s · ttft 1.1s"]);
check("and the overlay over the PTY draws the number big and the rest small",
      [dom["term-tps-overlay"].className,
       dom["term-tps-overlay"].kids.map((k) => [k.className, k.textContent])],
      ["term-tps-overlay",
       [["term-tps-big", "249 tok/s"], ["term-tps-small", "ttft 1.1s · deepseek flash · 45s ago"]]]);
// (modelShort spells the id the way the rail does: hyphens as spaces)

ctx.setRailOpen(true);                       // the rail covers the terminal (phone)
ctx.render();
check("the overlay leaves with the terminal while the badge stays",
      [dom["term-tps-overlay"].classList.contains("hidden"),
       dom["term-tps"].classList.contains("hidden")],
      [true, false]);
ctx.setRailOpen(false);

ctx.setCurrent("broken");
ctx.render();
check("an error answer's badge and overlay say so instead of a rate",
      [dom["term-tps"].textContent, dom["term-tps-overlay"].kids[0].textContent],
      ["HTTP 502", "HTTP 502"]);

ctx.setCurrent("quiet");
ctx.render();
check("a stale reading dims both", [dom["term-tps"].className, dom["term-tps-overlay"].className],
      ["badge tps stale", "term-tps-overlay stale"]);

ctx.setCurrent("nc");
ctx.render();
check("a session with no record hides both — nothing is drawn as zero",
      [dom["term-tps"].classList.contains("hidden"),
       dom["term-tps-overlay"].classList.contains("hidden")],
      [true, true]);

ctx.setCurrent(null);
ctx.render();
check("and so does no session at all",
      dom["term-tps"].classList.contains("hidden"), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("tps_check: ok");
