/* The session's timer line (claunch-7dzae): every countdown the attached
   session waits on, from GET /api/sessions/<name>/timers, drawn over the
   terminal. What it must get right — it exists only while there is a timer
   (and a terminal is on screen), it counts down by the browser's clock, it
   marks a held pick apart from a plain wait, it drops a closed window whose
   time has passed but keeps a held pick past its time as "due", it draws an
   unknown kind from a future provider with the default glyph, it ignores a
   read that belongs to the previous session, and it refits the terminal
   only when the set of chips changes. Slice the real functions out of app.js
   and drive them against a stub DOM. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

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

function sliceConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error("missing const " + name);
  const end = src.slice(start).search(/;\r?\n/);   // a checkout may be CRLF
  if (end < 0) throw new Error("unterminated const " + name);
  return src.slice(start, start + end + 1) + "\n";
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {},
    dataset: {}, title: "", type: "", disabled: false, props: {},
    style: { setProperty(k, v) { n.props[k] = v; } },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    set innerHTML(v) { if (v === "") this.kids = []; },
    classList: {
      add: (...cs) => cs.forEach((c) => n.classes.add(c)),
      remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
      contains: (c) => n.classes.has(c),
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
  if (id !== "term-timers") throw new Error("unexpected $: " + id);
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
let transcriptName = null;
let transcriptMachine = null;
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
  + slice("fmtAge") + slice("fmtSpan")
  + sliceConst("TERM_TIMER_GLYPH") + sliceConst("TERM_TIMER_GLYPH_DEFAULT")
  + slice("termTimerClock") + slice("termTimerRows")
  + "let termTimersView = null; let termTimersTick = null;\n"
  + slice("sessionLineOnPage") + slice("renderTermTimers") + `
Object.assign(exports, {
  renderTermTimers, termTimerRows,
  refits: () => refits,
  hash: () => location.hash,
  setPage: (p) => { currentPage = p; },
  setName: (n) => { currentName = n; },
  setTranscript: (n, m = null) => { transcriptName = n; transcriptMachine = m; },
});`
)(ctx, $, el);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

const NOW = Date.now();
Date.now = () => NOW;
const T = NOW / 1000;
const HOLD = (over) => ({
  source: "cflow2", kind: "hold", id: "s16:round/land/await-landing@103:nudge",
  label: "await-landing → nudge", due: T + 372, since: T - 228, state: "held",
  detail: "'nudge' is held until its window opens (interval 600s)",
  scope: "s16", link: "#/wf/s16%7CF%3A%5Crepo", ...over,
});
const VIEW = (timers, over) => ({ session: "s16", now: T, timers, errors: [], ...over });

/* ---- nothing to count: no strip ---- */
ctx.renderTermTimers(VIEW([]));
check("no timer, no strip", strip.classes.has("hidden"));
ctx.renderTermTimers(null);
check("no payload (old daemon, page swap) leaves it hidden", strip.classes.has("hidden"));

/* ---- the held nudge ---- */
const before = ctx.refits();
ctx.renderTermTimers(VIEW([HOLD()]));
check("a held pick shows the strip", !strip.classes.has("hidden"));
check("appearing refits the terminal", ctx.refits() === before + 1);
let t = texts(strip);
check("names the step and option", t.includes("await-landing → nudge"), t);
check("counts down by the browser's clock", t.includes("6m12s"), t);
let chip = byClass(strip, "tt-chip")[0];
check("a held pick is marked held", chip.classes.has("tt-state-held"));
check("the elapsed share is drawn", chip.props["--tt-pct"] === "38%", chip.props);
check("the tooltip says what happens when it fires",
      chip.title.includes("held pick is applied then") && chip.title.includes("run: s16"),
      chip.title);
chip.fire("click");
check("a chip opens its run page", ctx.hash() === "#/wf/s16%7CF%3A%5Crepo", ctx.hash());

ctx.renderTermTimers(VIEW([HOLD({ due: T + 300 })]));
check("a same-set repaint does not refit", ctx.refits() === before + 1);

/* ---- several, from more than one kind and source ---- */
ctx.renderTermTimers(VIEW([
  HOLD(),
  { source: "cflow2", kind: "wait", id: "s16:w@4", label: "settle-wait", due: T + 30,
    since: null, state: "waiting", detail: "" },
  { source: "reminder", kind: "nudge-clock", id: "r1", label: "reminder", due: T + 90,
    since: null, state: "waiting", detail: "" },
]));
check("a new chip refits", ctx.refits() === before + 2);
const chips = byClass(strip, "tt-chip");
check("one chip per timer", chips.length === 3, chips.length);
check("a wait without a start draws no fill", !("--tt-pct" in chips[1].props), chips[1].props);
check("an unknown kind gets the default glyph",
      byClass(chips[2], "tt-glyph")[0].text === "⏱", byClass(chips[2], "tt-glyph")[0].text);
check("a chip without a link is not a button to nowhere", chips[1].disabled === true);

/* ---- the integration batch (claunch-1o6az) ---- */
const batchRows = ctx.termTimerRows(VIEW([
  { source: "cflow2", kind: "batch", id: "b", label: "integrate batch 2/5",
    due: T + 200, since: T - 100, state: "closed" },
]), NOW);
check("a batch chip carries its n/size label and a countdown",
      batchRows.length === 1 && batchRows[0].label === "integrate batch 2/5" &&
      batchRows[0].glyph === "▤" && batchRows[0].clock !== "due", batchRows);

/* ---- past due ---- */
const rows = ctx.termTimerRows(VIEW([
  HOLD({ due: T - 5 }),
  { source: "cflow2", kind: "window", id: "c", label: "await-landing · nudge",
    due: T - 1, since: T - 601, state: "closed" },
]), NOW);
check("a closed window past its time is dropped", rows.length === 1, rows);
check("a held pick past its time stays as due",
      rows[0].clock === "due" && rows[0].overdue === true, rows[0]);
ctx.renderTermTimers(VIEW([HOLD({ due: T - 5 })]));
check("and is marked for the amber rule", byClass(strip, "tt-chip")[0].classes.has("tt-due"));

/* ---- a read for another session, and leaving the terminal ---- */
ctx.renderTermTimers(VIEW([HOLD()], { session: "s99" }));
check("a previous session's read is ignored", strip.classes.has("hidden"));
ctx.renderTermTimers(VIEW([HOLD()]));
ctx.setPage("meshes");
ctx.renderTermTimers(VIEW([HOLD()]));
check("off the terminal page it hides", strip.classes.has("hidden"));
// The session's HTML view (claunch-hjux5): the transcript page of the
// attached local session keeps the line; another session's, or a remote
// shadow's, does not.
ctx.setPage("log");
ctx.setTranscript("s16");
ctx.renderTermTimers(VIEW([HOLD()]));
check("on the session's HTML view it shows", !strip.classes.has("hidden"));
ctx.setTranscript("s99");
ctx.renderTermTimers(VIEW([HOLD()]));
check("on another session's transcript it hides", strip.classes.has("hidden"));
ctx.setTranscript("s16", "box2");
ctx.renderTermTimers(VIEW([HOLD()]));
check("on a remote shadow's transcript it hides", strip.classes.has("hidden"));
ctx.setTranscript(null);
ctx.setPage("terminal");

/* ---- wiring: markup, style, poll, page switch, route ---- */
(function wiring() {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");
  const at = (id) => html.indexOf(`id="${id}"`);
  check("the strip sits under the header and above the terminal",
        at("term-header") > 0 && at("term-header") < at("term-timers")
        && at("term-timers") < at("terminal"));
  check("it starts hidden", /<div id="term-timers" class="hidden"/.test(html));
  check("its height is taken from the terminal (flex: none)",
        /#term-timers \{[^}]*flex: none/.test(css));
  check("the dashboard poll refreshes it",
        /const refreshes = \[[^\]]*refreshTermTimers\(\)/.test(src));
  check("the page switch refreshes or hides it",
        /if \(!sessionLineOnPage\(name\)\) renderTermTimers\(null\);\s*else refreshTermTimers\(\);/.test(src));
  check("opening the session's HTML view re-asks once the name is set",
        /transcriptName = name;[\s\S]{0,600}syncSessionStrips\(\);/.test(src)
        && /function syncSessionStrips\(\) \{[^}]*refreshTermTimers\(\)/.test(src));
  check("it reads the session's own route",
        src.includes("/api/sessions/${encodeURIComponent(name)}/timers"));

  if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
  console.log("termtimers_check ok");
})();
