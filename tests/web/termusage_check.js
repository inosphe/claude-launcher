/* The account usage on the PTY overlay (claunch-f5w4x.2, .4), run against
   the real functions from app.js.

   What has to hold:
     harnesses    -- claude, codex and kimi sessions ask /api/usage
                     (refresh=auto, out of the batch) with the session's
                     harness on the selector; another harness never asks.
     one read     -- one read per selector per five minutes, whichever of its
                     sessions is on screen; a read in flight is not repeated.
     drawn        -- the 5h, 7d and wk windows of any source, rounded,
                     coloured by level, under the throughput; alone when the
                     session has no throughput reading. The tooltip carries
                     the resets the way `claunch usage` writes them.
     borrow       -- a borrowed session draws two named rows, its own
                     profile's and the lender's (→name); a side that cannot
                     be read is left out.
     nothing      -- a reading with no labelled window draws nothing; neither
                     does a failed read. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8");

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
const constStart = src.indexOf("const TERM_USAGE_TTL_MS");
const constEnd = src.indexOf("const termUsageReads = new Map();");
if (constStart < 0 || constEnd < 0) throw new Error("cannot locate the usage tables");
const consts = src.slice(constStart, constEnd) + "let termUsageReads = new Map();\n";

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
const calls = [];
let reply = null;
const api = async (p, opts) => { calls.push([p, opts]); return reply; };
const tpsSparkline = () => node("svg", "term-tps-chart");

const ctx = {};
new Function(
  "exports", "el", "$", "api", "sessionsCache", "currentName", "railOpen", "tpsSparkline",
  consts + ["fmtAge", "modelShort", "terminalOnScreen", "tpsFmt", "tpsAgeSecs", "ttftFmt",
   "tpsLatencyText", "tpsAvgLatencyText", "tpsSecs", "tpsLiveText", "tpsCacheText", "tpsText",
   "tpsTooltip", "tpsStaleClass", "termUsageSelectors", "termUsageRead", "termUsageLine",
   "termUsageLabel", "termUsageParts",
   "renderTermTps"].map(slice).join("\n") + `
exports.line = termUsageLine;
exports.parts = termUsageParts;
exports.render = renderTermTps;
exports.reads = () => termUsageReads;
exports.setSessions = (s) => { sessionsCache = s; };
exports.setCurrent = (n) => { currentName = n; };
`)(ctx, node, (id) => dom[id], api, [], null, false, tpsSparkline);

let failures = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL ${label}\n  got      ${a}\n  expected ${e}`); }
}
const settle = () => new Promise((r) => setTimeout(r, 0));
const answer = (data, ok = true) => ({ ok, status: ok ? 200 : 400, json: async () => data });
const now = Date.now();
const iso = (mins) => new Date(now + mins * 60000 + 30000).toISOString();
const anthropic = {
  profile: "nc", provider: "anthropic", harness: "claude", source: "ratelimit-headers",
  windows: [
    { name: "five_hour", utilization: 2.0, resets_at: iso(136), status: "allowed" },
    { name: "seven_day", utilization: 98.0, resets_at: iso(5336), status: "allowed_warning" },
  ],
  cached: true, updated_at: now / 1000,
};

(async () => {
  // A harness without a usage reader never asks.
  check("an opencode session is not asked about",
        [ctx.line({ name: "o", profile: "fw", harness: "opencode" }), calls.length], [null, 0]);

  // A claude session asks once, out of the batch, and draws on the answer.
  reply = answer(anthropic);
  const s = { name: "s1", profile: "nc", harness: "claude", status: "running" };
  ctx.setSessions([s]);
  ctx.setCurrent("s1");
  ctx.render();
  check("the first look asks /api/usage with refresh=auto, out of the batch",
        calls.map(([p, o]) => [p, o.noBatch]), [["/api/usage?profile=nc%3Aclaude&refresh=auto", true]]);
  ctx.render();
  check("a read in flight is not asked again", calls.length, 1);
  await settle(); await settle();
  const overlay = dom["term-tps-overlay"];
  const rows = () => overlay.kids.find((k) => k.className === "term-usage").kids
    .map((r) => r.kids.map((k) => [k.className, k.textContent]));
  check("with no throughput the overlay carries the usage alone",
        [overlay.classList.contains("hidden"), overlay.kids.map((k) => k.className), rows()],
        [false, ["term-usage"],
         [[["term-usage-item low", "5h 2%"], ["term-usage-item high", "7d 98%"]]]]);
  check("its tooltip reads like claunch usage",
        overlay.title,
        "nc usage\n5h 2.0% (resets in 2h16m)\n7d 98.0% (resets in 88h56m) [allowed_warning]");

  // Under the throughput when there is one.
  s.tps = { tps: 41.3, counted: true, model: "claude-opus", age_s: 3 };
  ctx.render();
  check("with a throughput reading the usage is the last area",
        overlay.kids.map((k) => k.className), ["term-tps-last", "term-usage"]);

  // One read per selector per TTL; a second session of the profile shares it.
  ctx.line({ name: "s2", profile: "nc", harness: "claude" });
  check("another session of the profile shares the read", calls.length, 1);
  ctx.reads().get("nc:claude").at = now - 5 * 60 * 1000 - 1;
  ctx.line(s);
  check("past five minutes the profile is asked again", calls.length, 2);
  await settle(); await settle();

  // Codex and kimi: asked with their harness, labelled by window name.
  reply = answer({ profile: "codex", harness: "codex", source: "codex-app-server",
    windows: [{ name: "seven_day", utilization: 87, resets_at: iso(60), status: null }] });
  ctx.line({ name: "c", profile: "codex", harness: "codex" });
  await settle(); await settle();
  check("a codex session asks with its harness and draws its 7d window",
        [calls[calls.length - 1][0], ctx.line({ name: "c", profile: "codex", harness: "codex" }).rows],
        ["/api/usage?profile=codex%3Acodex&refresh=auto",
         [{ who: "", items: [{ label: "7d", pct: 87, level: "mid" }] }]]);
  const kimi = { profile: "kimi", harness: "claude", source: "kimi-managed-usage",
    windows: [{ name: "weekly", utilization: 100, resets_at: null },
              { name: "five_hour", utilization: 0, resets_at: null }] };
  check("a kimi reading draws 5h before wk",
        ctx.parts(kimi).items.map((i) => `${i.label} ${i.pct}`), ["5h 0", "wk 100"]);
  check("a kimi-harness reading draws the same windows",
        ctx.parts({ ...kimi, source: "kimi-web-server", harness: "kimi" }).items.length, 2);
  check("a codex bucket keeps its prefix in front of the label",
        ctx.parts({ profile: "c", windows: [{ name: "gpt.five_hour", utilization: 5 }] }).items[0].label,
        "gpt 5h");

  // Borrow: the own profile and the lender, two named rows.
  reply = answer({ ...anthropic, profile: "sr", windows: [{ name: "five_hour", utilization: 10 }] });
  const b = { name: "b", profile: "sr", harness: "claude", borrow: "nc", status: "running" };
  const before = calls.length;
  ctx.line(b);
  await settle(); await settle();
  check("a borrowed session asks its own profile; the lender's read is shared",
        calls.slice(before).map(([p]) => p), ["/api/usage?profile=sr%3Aclaude&refresh=auto"]);
  const both = ctx.line(b);
  check("a borrowed session draws its own row and the lender's",
        both.rows.map((r) => [r.who, r.items.map((i) => `${i.label} ${i.pct}%`)]),
        [["sr", ["5h 10%"]], ["→nc", ["5h 2%", "7d 98%"]]]);
  check("the tooltip names the lender", both.title.includes("\n\nborrowed: nc usage"), true);
  ctx.setSessions([b]);
  ctx.setCurrent("b");
  ctx.render();
  check("the overlay draws the borrowed rows with their names",
        rows().map((r) => r.map(([, t]) => t)), [["sr", "5h 10%"], ["→nc", "5h 2%", "7d 98%"]]);
  ctx.reads().get("sr:claude").data = null;
  check("a side that cannot be read is left out, the other keeps its name",
        ctx.line(b).rows.map((r) => r.who), ["→nc"]);

  // What draws nothing.
  check("a reading without a labelled window draws nothing",
        ctx.parts({ ...anthropic, windows: [{ name: "30_minute", utilization: 4 }] }), null);
  reply = answer({ error: "no reader" }, false);
  ctx.line({ name: "k", profile: "kimi-small", harness: "claude" });
  await settle(); await settle();
  check("a failed read draws nothing",
        ctx.line({ name: "k", profile: "kimi-small", harness: "claude" }), null);

  if (failures) { console.error(`termusage_check: ${failures} failure(s)`); process.exit(1); }
  console.log("termusage_check: ok");
})().catch((err) => { console.error(err); process.exit(1); });
