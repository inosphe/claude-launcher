/* The measurement window strip (claunch-bwzkt): the attached session's test
   grant or its place in the queue, drawn over the terminal. What it must get
   right — it exists only while the session holds or waits (and a terminal
   is on screen), it counts the run's time by the browser's clock and turns
   the history's median into "~left" or "over the usual", it says nothing
   about time when there is no estimate, it names the rule a waiting request
   is blocked by, it ignores a read that belongs to the previous session,
   and it refits the terminal only when its shape changes. Slice the real
   functions out of app.js and drive them against a stub DOM. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {},
    dataset: {}, title: "", type: "",
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    set innerHTML(v) { if (v === "") this.kids = []; },
    classList: {
      add: (...cs) => cs.forEach((c) => n.classes.add(c)),
      remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
      contains: (c) => n.classes.has(c),
      toggle: (c, on) => {
        const want = on === undefined ? !n.classes.has(c) : !!on;
        if (want) n.classes.add(c); else n.classes.delete(c);
        return want;
      },
    },
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const texts = (n) => walk(n).map((k) => k.text).join(" | ");
const byClass = (n, cls) => walk(n).filter((k) => k.classes.has(cls));

const strip = node("div");
strip.classes.add("hidden");
const $ = (id) => {
  if (id !== "term-window") throw new Error("unexpected $: " + id);
  return strip;
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

const stubs = `
let currentPage = "terminal";
let currentName = "s16";
let refits = 0;
function refitSoon() { refits++; }
const location = { hash: "" };
let intervals = 0;
function setInterval() { intervals++; return intervals; }
function clearInterval() {}
`;

const ctx = {};
new Function(
  "exports", "$", "el",
  stubs
  + slice("fmtAge") + slice("windowResultText") + slice("fmtSpan")
  + slice("termWindowSince") + slice("termWindowEstimateText")
  + slice("termWindowRows") + slice("termWindowLoad")
  + "let termWindowView = null; let termWindowTick = null;\n"
  + slice("renderTermWindow") + `
Object.assign(exports, {
  renderTermWindow, termWindowRows, fmtSpan,
  refits: () => refits,
  hash: () => location.hash,
  setPage: (p) => { currentPage = p; },
});`
)(ctx, $, el);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

const ago = (secs) => new Date(Date.now() - secs * 1000).toISOString();
const EST = { basis: "label", samples: 5, p50: 300, p90: 420 };
const HOLD = (over) => ({
  grant_id: "g1", cls: "targeted", session: "s16", label: "pytest tests/test_a.py",
  workers: 4, forced: false, acquired_at: ago(100), elapsed_seconds: 100,
  estimate: EST, remaining_seconds: 200, result: null, ...over,
});
const WINDOW = (holders, more) => ({
  holders, queued: 0, sweep_queued: false, caps: { sweep: 1, targeted: 3 },
  workers_in_use: 4, worker_budget: 12, ...more,
});
const VIEW = (over) => ({
  session: "s16", holding: [], waiting: [], last: null,
  window: WINDOW([]), ...over,
});

check("fmtSpan keeps the seconds under an hour", ctx.fmtSpan(125) === "2m05s",
      ctx.fmtSpan(125));

/* ---- neither holding nor waiting: no strip ---- */
ctx.renderTermWindow(VIEW());
check("an idle session has no strip", strip.classes.has("hidden"));
ctx.renderTermWindow(null);
check("no payload (old daemon, page swap) leaves it hidden",
      strip.classes.has("hidden"));

/* ---- holding: elapsed and the estimate ---- */
const before = ctx.refits();
ctx.renderTermWindow(VIEW({ holding: [HOLD()], window: WINDOW([HOLD()]) }));
check("a held grant shows the strip", !strip.classes.has("hidden"));
check("appearing refits the terminal", ctx.refits() === before + 1);
let t = texts(strip);
check("names the class and width", t.includes("targeted ×4"), t);
check("counts the run's time", /running 1m4\ds/.test(t), t);
check("turns the median into a remainder", /~3m1\ds left/.test(t), t);
check("shows the command it runs", t.includes("pytest tests/test_a.py"), t);
check("states the machine's load",
      t.includes("targeted 1/3") && t.includes("workers 4/12") && t.includes("queue 0"), t);
const row = byClass(strip, "tw-row")[0];
check("the estimate's basis is in the tooltip",
      row.title.includes("p50 5m00s") && row.title.includes("5 runs of this command"),
      row.title);
check("not marked over while inside the median", !row.classes.has("tw-over"));

ctx.renderTermWindow(VIEW({ holding: [HOLD()], window: WINDOW([HOLD()]) }));
check("a same-shape repaint does not refit", ctx.refits() === before + 1);

/* ---- past the median: "over", amber, never a negative count ---- */
ctx.renderTermWindow(VIEW({ holding: [HOLD({ acquired_at: ago(400) })] }));
t = texts(strip);
check("a long run reads as over the usual time",
      /1m4\ds over the usual 5m00s/.test(t), t);
check("and is marked for the amber rule",
      byClass(strip, "tw-row")[0].classes.has("tw-over"));

/* ---- no estimate: say so, invent nothing ---- */
ctx.renderTermWindow(VIEW({ holding: [HOLD({ estimate: null, remaining_seconds: null })] }));
t = texts(strip);
check("no history means no countdown", t.includes("no estimate yet") && !t.includes("left"), t);

/* ---- a result reported while still holding ---- */
ctx.renderTermWindow(VIEW({
  holding: [HOLD({ result: { outcome: "failed", failed: 2, passed: 9 } })],
}));
t = texts(strip);
check("a reported result is shown", t.includes("result: failed (9 passed, 2 failed)"), t);

/* ---- waiting: place, wait, rule, and the soonest holder ---- */
const other = HOLD({ session: "x", cls: "sweep", acquired_at: ago(60),
                     estimate: { basis: "class", samples: 9, p50: 600, p90: 900 } });
const refitsBefore = ctx.refits();
ctx.renderTermWindow(VIEW({
  waiting: [{
    grant_id: "q1", cls: "targeted", label: "pytest tests/test_b.py", priority: 0,
    position: 2, ahead: 1, enqueued_at: ago(45), waited_seconds: 45,
    reason: "a sweep holds the window", estimate: null,
  }],
  window: WINDOW([other], { queued: 2, sweep_queued: true }),
}));
t = texts(strip);
check("a waiting request shows the strip", !strip.classes.has("hidden"));
check("held→waiting changes shape and refits", ctx.refits() === refitsBefore + 1);
check("names the queue place", t.includes("#2 in queue (1 ahead)"), t);
check("counts the wait", /waiting 4\ds/.test(t), t);
check("names the blocking rule", t.includes("a sweep holds the window"), t);
check("estimates the soonest holder", /first holder done in ~9m0\ds/.test(t), t);
check("says a sweep is waiting", t.includes("queue 2 (sweep waiting)"), t);

/* ---- a read for the previous session is not this session's ---- */
ctx.renderTermWindow(VIEW({ session: "other", holding: [HOLD()] }));
check("another session's read hides the strip", strip.classes.has("hidden"));

/* ---- off the terminal page: hidden ---- */
ctx.setPage("meshes");
ctx.renderTermWindow(VIEW({ holding: [HOLD()] }));
check("no terminal on screen, no strip", strip.classes.has("hidden"));
ctx.setPage("terminal");

(async () => {
  ctx.renderTermWindow(VIEW({ holding: [HOLD()] }));
  await byClass(strip, "tw-row")[0].fire("click");
  check("a row opens the Window page", ctx.hash() === "#/window", ctx.hash());

  /* ---- the markup, the stylesheet, the call sites ---- */
  const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");
  const at = (id) => html.indexOf(`id="${id}"`);
  check("the strip sits between the header and the terminal",
        at("term-header") > 0 && at("term-header") < at("term-window")
        && at("term-window") < at("terminal"));
  check("it starts hidden", /<div id="term-window" class="hidden"/.test(html));
  check("its height is taken from the terminal (flex: none)",
        /#term-window \{[^}]*flex: none/.test(css));
  check("the dashboard poll refreshes it",
        /const refreshes = \[[^\]]*refreshTermWindow\(\)/.test(src));
  check("the page switch refreshes or hides it",
        /if \(!showTerm\) renderTermWindow\(null\);\s*else refreshTermWindow\(\);/.test(src));
  check("it reads the session's own route",
        src.includes("/api/sessions/${encodeURIComponent(name)}/window"));

  if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
  console.log("termwindow_check ok");
})();
