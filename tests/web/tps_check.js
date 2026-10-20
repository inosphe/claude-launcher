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
   - A session that has gone quiet shows "none", not its last number. The
     daemon's window is bounded in time, and past that bound it sends `idle`
     with no rate at all; the row keeps saying when the session last called
     out and on what. That is a different state from a session that was never
     measured, which is drawn as nothing.
   - A reading older than the daemon's bound dims (`stale`) instead of
     vanishing. This is now only reachable from a daemon that has not been
     restarted since the bound was added, and the fallback is the old ten
     minutes.
   - The rate is tokens over the whole call, one definition for every row.
     The latency beside it is the first token when the answer streamed and the
     first byte when it arrived in one piece, since there is no first token to
     have timed for the latter.
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
   "tpsLatencyText", "tpsSecs", "tpsLiveText", "tpsParallelText", "tpsParallelTooltip", "tpsText", "tpsTooltip", "tpsStaleClass", "tpsRailLine",
   "tpsChip", "renderTermTps"].map(slice).join("\n") + `
exports.text = tpsText;
exports.parallel = tpsParallelText;
exports.parallelTip = tpsParallelTooltip;
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
  idle: false, age_s: 45, max_age_s: 600,
  ttft_ms: 715, ttfb_ms: 733, output_tokens: 16, input_tokens: 36, cache_read: 0,
  window: 4, tps_median: 35.2, tps_median_n: 4, ttft_ms_median: 900,
  upstream: "https://api.deepseek.com/v1",
};
/* What the daemon sends for a session that has been quiet past its window:
   the last call is still dated and named, and there is no rate anywhere. */
const IDLE = {
  ts: OLD, model: "deepseek-flash", status: 200, counted: true,
  idle: true, age_s: 3600, max_age_s: 600,
  tps: null, ttft_ms: null, ttfb_ms: null,
  output_tokens: null, input_tokens: null, cache_read: null,
  window: 0, tps_median: null, tps_median_n: 0, ttft_ms_median: null,
  upstream: "https://api.deepseek.com/v1",
};
const BIG = { ...FAST, tps: 248.75, ttft_ms: 1054, ttfb_ms: 1067 };
const ERR = { ts: AT, model: null, status: 502, counted: false, tps: null, ttft_ms: null,
              window: 1, tps_median: null, ttft_ms_median: null };
/* A reading from a daemon that predates the window bound: it still carries a
   number for a call an hour old, and the fallback threshold must still dim
   it. A restarted daemon sends IDLE for this session instead. */
const STALE = { ...FAST, ts: OLD, age_s: 3600, max_age_s: undefined,
                window: 1, tps_median: 38.06 };
/* A whole-body answer. Its rate is the call's own, and it has no first token
   to time, so the latency drawn beside it is the first byte. 2.41 against a
   3158ms wait is the shape of the record that started this: the same call,
   divided by the sliver of network time the finished body took to cross,
   read as five digits. */
const WHOLE = { ...FAST, tps: 2.41, ttft_ms: null, ttfb_ms: 3158 };

const PI = { name: "pi", harness: "pi", tps: FAST };
const CLAUDE = { name: "ds4", harness: "claude", tps: BIG };
const BROKEN = { name: "broken", harness: "claude", tps: ERR };
const QUIET = { name: "quiet", harness: "claude", tps: STALE };
const GONE = { name: "gone-quiet", harness: "claude", tps: IDLE };
const WHOLEBODY = { name: "whole", harness: "claude", tps: WHOLE };
const OAUTH = { name: "nc", harness: "claude" };                 // never metered

/* ---- the glance ---- */
check("tokens/s with one decimal, and the time to first token",
      ctx.text(FAST), "38.1 tok/s · ttft 715ms");
check("a big number drops the decimal, a slow first token reads in seconds",
      ctx.text(BIG), "249 tok/s · ttft 1.1s");
check("an uncounted error answer shows its status where the number would be",
      ctx.text(ERR), "HTTP 502");
check("no block at all is an empty string, never a zero", ctx.text(null), "");
check("a session that has gone quiet reads none, not its last number",
      ctx.text(IDLE), "idle");

/* ---- the story ---- */
const tip = ctx.tooltip(FAST);
check("the tooltip dates the call and names the model",
      tip.split("\n")[0], "last call 45s ago on deepseek-flash (HTTP 200)");
check("...gives the token counts behind the rate",
      tip.includes("38.1 tokens/s over the whole call, 16 out, 36 in"), true);
check("...and the rolling median with both bounds of its window",
      tip.includes("median over 4 calls in the last 10m: 35.2 tok/s, ttft 900ms"), true);
check("...and where the call went", tip.includes("via https://api.deepseek.com/v1"), true);
check("an error answer's tooltip has no rate lines",
      ctx.tooltip(ERR).includes("tokens/s"), false);

/* ---- a session that went quiet ---- */
const quiet = ctx.tooltip(IDLE);
check("the quiet row still dates the last call and names the model",
      quiet.split("\n")[0], "last call 1h00m ago on deepseek-flash (HTTP 200)");
check("...and says why there is no number",
      quiet.includes("no call in the last 10m"), true);
check("...and carries no rate line at all", quiet.includes("tokens/s"), false);
check("...and no median either", quiet.includes("median over"), false);

/* ---- a whole-body answer: a rate, with the first byte for its latency ---- */
check("a whole-body answer is drawn like any other, on its own rate",
      ctx.text(WHOLE), "2.4 tok/s · first byte 3.2s");
check("...and its tooltip names the same window as everyone else's",
      ctx.tooltip(WHOLE).includes("2.4 tokens/s over the whole call, 16 out, 36 in"), true);
check("...and gives the first byte where a streamed call would give a ttft",
      ctx.tooltip(WHOLE).includes("first byte 3.2s"), true);

/* ---- the rail row's line ---- */
const line = ctx.rail(PI);
check("the rail line is a bolt, the glance and the age, with the story on hover",
      [line.className, line.kids.map((k) => k.textContent), line.title === tip],
      ["rail-tps-line", ["⚡", "38.1 tok/s · ttft 715ms", "45s ago"], true]);
check("a reading an hour old from a daemon with no bound still dims",
      ctx.rail(QUIET).className, "rail-tps-line stale");
check("a quiet session's line is the word none and the age, dimmed and marked idle",
      [ctx.rail(GONE).className, ctx.rail(GONE).kids.map((k) => k.textContent)],
      ["rail-tps-line stale idle", ["⚡", "idle", "1h00m ago"]]);
check("an error answer still gets a line", ctx.rail(BROKEN).kids[1].textContent, "HTTP 502");
check("a whole-body answer's rate reaches the rail line too",
      ctx.rail(WHOLEBODY).kids[1].textContent, "2.4 tok/s · first byte 3.2s");
check("a session that never went through the shim gets no line at all",
      ctx.rail(OAUTH), null);
// Quiet and never-measured are two states: one draws "none", the other nothing.

/* ---- the card chip ---- */
ctx.setSessions([PI, CLAUDE, BROKEN, QUIET, GONE, WHOLEBODY, OAUTH]);
check("the chip is the glance with the story on hover",
      [ctx.chip("pi").textContent, ctx.chip("pi").className, ctx.chip("pi").title === tip],
      ["38.1 tok/s · ttft 715ms", "sess-brief-tps", true]);
check("and dims when stale", ctx.chip("quiet").className, "sess-brief-tps stale");
check("and reads none, marked idle, for a session that went quiet",
      [ctx.chip("gone-quiet").textContent, ctx.chip("gone-quiet").className],
      ["idle", "sess-brief-tps stale idle"]);
check("and a whole-body answer's rate is drawn on the chip too",
      ctx.chip("whole").textContent, "2.4 tok/s · first byte 3.2s");
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

ctx.setCurrent("gone-quiet");
ctx.render();
check("a quiet session's badge and overlay say none instead of the last rate",
      [dom["term-tps"].className, dom["term-tps"].textContent,
       dom["term-tps-overlay"].className,
       dom["term-tps-overlay"].kids.map((k) => k.textContent)],
      ["badge tps stale idle", "idle", "term-tps-overlay stale idle",
       ["idle", "deepseek flash · 1h00m ago"]]);

ctx.setCurrent("whole");
ctx.render();
check("a whole-body answer reaches the badge and the overlay, with its first byte",
      [dom["term-tps"].textContent,
       dom["term-tps-overlay"].kids.map((k) => k.textContent)],
      ["2.4 tok/s · first byte 3.2s",
       ["2.4 tok/s", "first byte 3.2s · deepseek flash · 45s ago"]]);

/* The live poll repaints twice a second, and most of those answers read the
   same (claunch-i3es6): an unchanged overlay keeps its nodes, and one that
   reads differently is rebuilt. */
{
  const overlay = dom["term-tps-overlay"];
  const kept = overlay.kids;
  let rebuilt = 0;
  const replace = overlay.replaceChildren;
  overlay.replaceChildren = (...cs) => { rebuilt++; replace(...cs); };
  ctx.render();
  ctx.render();
  check("an overlay that reads the same is not rebuilt",
        [rebuilt, overlay.kids === kept], [0, true]);
  ctx.setCurrent("ds4");
  ctx.render();
  check("and one that reads differently is",
        [rebuilt, overlay.kids[0].textContent], [1, "249 tok/s"]);
  ctx.setCurrent("whole");
  ctx.render();
  overlay.replaceChildren = replace;
}

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

// Live (claunch-2fut4): a call in flight is what the glance shows, and a
// session with nothing in flight says idle outright -- even when its last
// finished call is recent enough to still carry a number.
const LIVE = (live) => ({ ...FAST, live: { window_s: 2, bin_s: 0.5, inflight: 1, ...live } });
check("a call that is streaming reads its live estimate",
      ctx.text(LIVE({ state: "streaming", rate: 41.25 })), "≈41.3 tok/s");
check("a call with nothing back yet reads waiting and for how long",
      [ctx.text(LIVE({ state: "waiting", rate: 0, since_s: 3.21 })),
       ctx.text(LIVE({ state: "waiting", rate: 0, since_s: 42.6 }))],
      ["waiting 3.2s", "waiting 43s"]);
check("nothing in flight reads idle, not the last call's rate",
      ctx.text(LIVE({ state: "idle", inflight: 0, rate: 0 })), "idle");
check("the live states are classed apart",
      [ctx.rail({ name: "a", tps: LIVE({ state: "streaming", rate: 5 }) }).className,
       ctx.rail({ name: "b", tps: LIVE({ state: "idle", inflight: 0, rate: 0 }) }).className],
      ["rail-tps-line live", "rail-tps-line stale idle"]);
check("the tooltip says what is in flight before the last call",
      ctx.tooltip(LIVE({ state: "waiting", rate: 0, since_s: 1 })).split("\n")[0],
      "waiting for the first token (1 call in flight)");
check("a session whose first call is still in flight has only the live part",
      ctx.text({ live: { state: "waiting", inflight: 1, since_s: 0.5 } }), "waiting 0.5s");
/* Parallel sessions on one shim (claunch-b28pp): the shim's total, the mean
   per session and the session count ride on the live glance and spell out in
   the tooltip; a session alone on its shim carries no `parallel` and shows
   only its own rate. The in-flight calls per session (claunch-v4jgh) say how
   the total is split -- "1+2+5" -- because the mean alone reads as an even
   split that one session running five calls does not have. */
const PAR = {
  sessions: 3, total: 120.4, mean: 40.13, calls: 8, counts: [1, 2, 5],
  by_session: [
    { session: "me", calls: 1, rate: 42, self: true },
    { session: "other-a", calls: 2, rate: 40, self: false },
    { session: "other-b", calls: 5, rate: 38.4, self: false },
  ],
  estimated: true, window_s: 2,
};
check("a session sharing its shim appends its calls, the total and the mean",
      ctx.text(LIVE({ state: "streaming", rate: 42, inflight: 1, parallel: PAR })),
      "≈42.0 tok/s · ×3 (1+2+5) Σ≈120 avg≈40.1");
check("the tooltip lists each session's calls and rate",
      ctx.tooltip(LIVE({ state: "streaming", rate: 42, parallel: PAR })).split("\n").slice(1, 3),
      ["shared proxy: 3 sessions, 8 calls — this 1 call ≈42.0 tok/s, other-a 2 calls ≈40.0 tok/s, other-b 5 calls ≈38.4 tok/s",
       "total ≈120 tok/s, avg ≈40.1 tok/s per session (last 2s)"]);
check("the overlay form spells the split out before the total",
      ctx.parallel(PAR, false),
      "3 sessions · calls 1+2+5 · total ≈120 tok/s · avg ≈40.1 tok/s");
check("the compact form carries the same counts",
      ctx.parallel(PAR, true), "×3 (1+2+5) Σ≈120 avg≈40.1");
/* A payload from a daemon that predates the call counts keeps the old text. */
const OLD_PAR = { sessions: 3, total: 120.4, mean: 40.13, estimated: true, window_s: 2 };
check("a payload without counts reads as before",
      ctx.text(LIVE({ state: "streaming", rate: 42, inflight: 1, parallel: OLD_PAR })),
      "≈42.0 tok/s · ×3 Σ≈120 avg≈40.1");
check("...and its tooltip keeps the one-line form",
      ctx.tooltip(LIVE({ state: "streaming", rate: 42, parallel: OLD_PAR })).split("\n")[1],
      "shared proxy: 3 sessions active now, total ≈120 tokens/s, avg ≈40.1 tokens/s per session (last 2s)");
check("a session alone on its shim shows only its own rate",
      ctx.text(LIVE({ state: "streaming", rate: 42 })), "≈42.0 tok/s");

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("tps_check: ok");
