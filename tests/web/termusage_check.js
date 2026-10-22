/* The account usage on the PTY overlay (claunch-f5w4x.2), run against the
   real functions from app.js.

   What has to hold:
     claude only  -- a claude-harness session asks /api/usage (refresh=auto,
                     out of the batch); another harness never asks.
     one read     -- one read per profile per five minutes, whichever of its
                     sessions is on screen; a read in flight is not repeated.
     drawn        -- the 5h and 7d windows of an Anthropic reading, rounded,
                     coloured by level, under the throughput; alone when the
                     session has no throughput reading. The tooltip carries
                     the resets the way `claunch usage` writes them.
     other source -- a reading from a source the table does not map draws
                     nothing; neither does a failed read. */
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
   "tpsTooltip", "tpsStaleClass", "termUsageSelector", "termUsageLine", "termUsageParts",
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
  // Another harness never asks.
  check("a codex session is not asked about",
        [ctx.line({ name: "c", profile: "work:codex", harness: "codex" }), calls.length], [null, 0]);

  // A claude session asks once, out of the batch, and draws on the answer.
  reply = answer(anthropic);
  const s = { name: "s1", profile: "nc", harness: "claude", status: "running" };
  ctx.setSessions([s]);
  ctx.setCurrent("s1");
  ctx.render();
  check("the first look asks /api/usage with refresh=auto, out of the batch",
        calls.map(([p, o]) => [p, o.noBatch]), [["/api/usage?profile=nc&refresh=auto", true]]);
  ctx.render();
  check("a read in flight is not asked again", calls.length, 1);
  await settle(); await settle();
  const overlay = dom["term-tps-overlay"];
  check("with no throughput the overlay carries the usage alone",
        [overlay.classList.contains("hidden"), overlay.kids.map((k) => k.className),
         overlay.kids[0].kids.map((k) => [k.className, k.textContent])],
        [false, ["term-usage"],
         [["term-usage-item low", "5h 2%"], ["term-usage-item high", "7d 98%"]]]);
  check("its tooltip reads like claunch usage",
        overlay.title,
        "nc usage\n5h 2.0% (resets in 2h16m)\n7d 98.0% (resets in 88h56m) [allowed_warning]");

  // Under the throughput when there is one.
  s.tps = { tps: 41.3, counted: true, model: "claude-opus", age_s: 3 };
  ctx.render();
  check("with a throughput reading the usage is the last area",
        overlay.kids.map((k) => k.className), ["term-tps-last", "term-usage"]);

  // One read per profile per TTL; a second session of the profile shares it.
  ctx.line({ name: "s2", profile: "nc", harness: "claude" });
  check("another session of the profile shares the read", calls.length, 1);
  ctx.reads().get("nc").at = now - 5 * 60 * 1000 - 1;
  ctx.line(s);
  check("past five minutes the profile is asked again", calls.length, 2);
  await settle(); await settle();

  // What draws nothing.
  check("a source without a table row draws nothing",
        ctx.parts({ ...anthropic, source: "kimi-managed-usage" }), null);
  reply = answer({ error: "Claude harness usage reporting requires an Anthropic service provider" }, false);
  ctx.line({ name: "k", profile: "kimi-small", harness: "claude" });
  await settle(); await settle();
  check("a failed read draws nothing",
        ctx.line({ name: "k", profile: "kimi-small", harness: "claude" }), null);

  if (failures) { console.error(`termusage_check: ${failures} failure(s)`); process.exit(1); }
  console.log("termusage_check: ok");
})().catch((err) => { console.error(err); process.exit(1); });
