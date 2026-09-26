/* The token-usage reading, as the dashboard draws it, run against the real
   functions from app.js.

   The daemon hangs a `token_usage` block on a session whose harness keeps a
   transcript it can total (daemon/tokenusage.py): four components, their
   total, the request count, and per harness `subagents` (claude),
   `reasoning` (codex) or `cost` (pi). Two places draw it: the rail row's own
   line and the detail panel's row. The rules these checks hold:

   - Absence is drawn as nothing: no block, no line and no row, never "Σ 0".
   - The glance is the whole session's spend (conversation plus subagents),
     with the output named beside it and the request count after it.
   - The four components stay apart. Every component appears in the
     breakdown under its own name with its share, and the bar has one
     segment per non-zero component sized by that share.
   - Codex's reasoning, pi's cost and claude's subagents are said where they
     exist and nowhere else.
   - A reading the daemon is still counting (`partial`) says so, on the line
     and in the breakdown. */
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
const partsLine = src.match(/^const USAGE_PARTS = \[[\s\S]*?\];/m);
if (!partsLine) throw new Error("cannot locate USAGE_PARTS in app.js");

function node(tag) {
  const n = {
    tag, kids: [], text: "", className: "", title: "", style: {},
    appendChild(c) { n.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
  };
  return n;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function descendants(n, out = []) {
  for (const k of n.kids) { out.push(k); descendants(k, out); }
  return out;
}

const ctx = {};
new Function("exports", "el",
  partsLine[0] + "\n"
  + slice("usageShort") + slice("usageTotalOf") + slice("usageRequestsOf")
  + slice("usageText") + slice("usageCost") + slice("usageShare")
  + slice("usageLines") + slice("usageTooltip") + slice("usageBar")
  + slice("usageRailLine") + slice("usageDetailRow")
  + `
Object.assign(exports, { usageShort, usageText, usageLines, usageTooltip,
  usageRailLine, usageDetailRow });`)(ctx, el);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* Measured on this machine's longest claude transcript (2026-09-27). */
const CLAUDE = {
  harness: "claude", input: 30336, cache_read: 3595935954,
  cache_write: 59010593, output: 6745165, total: 3661722048, requests: 15169,
  since: "2026-09-02T06:47:16.677Z", at: "2026-09-26T18:50:29.997Z",
  model: "claude-sonnet-5",
  subagents: { input: 15354, cache_read: 442278564, cache_write: 29782191,
               output: 357284, total: 472433393, requests: 7677 },
};
/* A real codex rollout (2026-08-27). */
const CODEX = {
  harness: "codex", input: 108686, cache_read: 8686336, cache_write: 0,
  output: 15800, total: 8810822, requests: 129, model: "gpt-5.6-sol",
  reasoning: 3198,
};
const PI = {
  harness: "pi", input: 24764, cache_read: 1246464, cache_write: 0,
  output: 39071, total: 1310299, requests: 44, model: "deepseek-flash",
  cost: 0.75,
};

/* ---- the short form ---- */
check("short form", [0, 999, 1234, 45_600, 1_234_567, 3_661_722_048].map(ctx.usageShort),
      ["0", "999", "1.23k", "45.6k", "1.23M", "3.66B"]);
check("garbage is a question mark, not a number",
      [NaN, -1, undefined].map(ctx.usageShort), ["?", "?", "?"]);

/* ---- the glance ---- */
check("claude: session total includes subagents; output and requests too",
      ctx.usageText(CLAUDE), "Σ 4.13B · out 7.10M · 22.8k req");
check("codex", ctx.usageText(CODEX), "Σ 8.81M · out 15.8k · 129 req");
check("pi shows the recorded cost", ctx.usageText(PI),
      "Σ 1.31M · out 39.1k · 44 req · $0.7500");
check("a partial reading says it is still counting",
      ctx.usageText({ ...CODEX, partial: true }).endsWith(" …"), true);
check("no reading, no text", ctx.usageText(undefined), "");
check("the compact form only drops the separator spaces",
      ctx.usageText(CODEX, true), "Σ 8.81M·out 15.8k·129 req");

/* ---- the breakdown ---- */
const claudeLines = ctx.usageLines(CLAUDE);
for (const label of ["fresh input", "cache read", "cache write", "output"]) {
  check(`claude breakdown names ${label}`,
        claudeLines.some((l) => l.trim().startsWith(label + " ")), true);
}
check("cache read is given with its share",
      claudeLines.some((l) => l.includes("cache read 3,595,935,954 (98.2%)")), true);
check("the rail tooltip leaves the shares out (a rail row claims no percentage)",
      /%/.test(ctx.usageTooltip(CLAUDE)), false);
check("subagents are their own section",
      claudeLines.some((l) => l.startsWith("subagents: 472,433,393 tokens over 7,677")), true);
check("the scope is said: this conversation",
      claudeLines.some((l) => l.includes("this conversation")), true);
check("codex says its reasoning share of the output",
      ctx.usageLines(CODEX).some((l) => l.includes("reasoning 3,198")), true);
check("codex has no subagent or cost line",
      ctx.usageLines(CODEX).some((l) => l.startsWith("subagents") || l.startsWith("cost")), false);
check("pi says its cost",
      ctx.usageLines(PI).some((l) => l.includes("$0.7500")), true);
check("partial is explained in the breakdown",
      ctx.usageLines({ ...PI, partial: true }).some((l) => l.includes("still counting")), true);

/* ---- the rail line ---- */
check("no reading, no rail line", ctx.usageRailLine({ name: "x" }), null);
const line = ctx.usageRailLine({ name: "c", token_usage: CLAUDE });
check("the rail line carries the compact glance",
      descendants(line).some((k) => k.textContent === "Σ 4.13B·out 7.10M·22.8k req"), true);
check("and the breakdown in its tooltip",
      line.title.includes(ctx.usageTooltip(CLAUDE)), true);
const segs = descendants(line).filter((k) => k.className.startsWith("usage-seg"));
check("one bar segment per non-zero component",
      segs.map((k) => k.className),
      ["usage-seg usage-in", "usage-seg usage-cr", "usage-seg usage-cw", "usage-seg usage-out"]);
const width = segs.reduce((a, k) => a + parseFloat(k.style.width), 0);
check("the segments add up to the whole bar", Math.round(width), 100);
const codexSegs = descendants(ctx.usageRailLine({ token_usage: CODEX }))
  .filter((k) => k.className.startsWith("usage-seg"));
check("a zero component gets no segment", codexSegs.length, 3);
check("a partial line is marked for the stylesheet",
      ctx.usageRailLine({ token_usage: { ...PI, partial: true } }).className,
      "rail-usage-line partial");

/* ---- the detail row ---- */
const dl = node("dl");
ctx.usageDetailRow(dl, { name: "x" });
check("no reading, no detail row", dl.kids.length, 0);
ctx.usageDetailRow(dl, { name: "c", token_usage: CLAUDE });
check("the detail row is a dt and a dd", dl.kids.map((k) => k.tag), ["dt", "dd"]);
check("labelled token usage", dl.kids[0].textContent, "token usage");
const dd = dl.kids[1];
check("the detail row shows the breakdown without a hover",
      descendants(dd).some((k) => k.tag === "pre"
        && k.textContent === ctx.usageLines(CLAUDE).join("\n")), true);
check("and a legend entry per component",
      descendants(dd).filter((k) => k.className === "usage-legend-item").length, 4);

if (failures) {
  console.error(`${failures} usage check(s) failed`);
  process.exit(1);
}
console.log("usage checks passed");
