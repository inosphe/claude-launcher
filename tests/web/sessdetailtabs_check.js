/* The Details panel's sub-tabs: All, or one group of the panel's sections.

   The panel used to be one column of some twenty sections. It is now four
   groups (Overview, Messages, Work, Settings) under a second radio, with All
   drawing the four in order under their names. This harness drives the real
   renderSession with every section stubbed to a named box, plus the real
   sub-tab bar, the real group head and the real per-session layout store,
   and checks:
   - which sections each sub-tab draws, and in what order;
   - that All draws a head per group and a single group's tab draws none;
   - that a group not on screen is not built (its sections are not called);
   - that the choice is the session's, stored with its layout, and junk in the
     store falls back to All;
   - that the stylesheet dresses the bar, the heads, and every section box as
     the same card. */
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
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1) + "\n"; }
  }
  throw new Error("unbalanced " + name);
}
function sliceTo(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b) + "\n";
}

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) return;
  failures++;
  console.error(`FAIL ${label}\n  got  ${g}\n  want ${w}`);
}

function node(tag = "div") {
  const n = {
    tag, kids: [], text: "", classes: new Set(), attrs: {}, handlers: {},
    title: "", scrollTop: 0,
    appendChild(c) { n.kids.push(c); return c; },
    setAttribute(k, v) { n.attrs[k] = String(v); },
    addEventListener(k, fn) { (n.handlers[k] ||= []).push(fn); },
    fire(k) { (n.handlers[k] || []).forEach((fn) => fn({})); },
    set innerHTML(v) { n.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    contains: (c) => n.classes.has(c),
  };
  return n;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined && text !== null) n.text = String(text);
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}

/* Every section is a box named for its builder, and every call is counted,
   so "not built" is checkable as "not called". */
const built = [];
const view = node("div");
const store = new Map();
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
};
let refreshed = 0;
const sections = [
  "sessFlags", "sessNote", "sessTask", "sessBriefSection", "sessInputJournal",
  "sessHandoff", "sessSend", "sessMeshJoin", "sessBeads", "sessCommits",
  "sessReborrow", "sessPerms", "sessMigrate",
];
const stubs = `
let sessName = "coder1", sessRunFold = null;
function $(id) { return view; }
function formInUse() { return false; }
function selectionInUse() { return false; }
function syncSplitPane() {}
function refreshSession() { bump(); }
function stopSessRun() {}
function metaRow() {}
function profileHarnessLabel() { return ""; }
function modelSentence() { return ""; }
function ctxSentence() { return ""; }
function ctxBreakdown() { return ""; }
function sessHead() { return el("div", "sess-head"); }
function sessRailTabs() { return el("div", "sess-tabs"); }
function sessWorkflow() { return el("div", "sess-wf"); }
function sessBeadsPanel() { return el("div", "sess-beads-panel"); }
function sessQueued() { built.push("sessQueued"); return el("div", "box-sessQueued"); }
function sessBackpressure() { built.push("sessBackpressure"); return null; }
function rolePanels() {
  built.push("rolePanels");
  return [() => el("div", "box-roleQuickJob")];
}
function sessHandles() { return []; }
function go() {}
${sections.map((f) =>
  `function ${f}() { built.push("${f}"); return el("div", "box-${f}"); }`
).join("\n")}
`;

const ctx = {};
new Function(
  "exports", "el", "view", "built", "localStorage", "BASE", "bump",
  stubs
  + sliceTo("const SESSLAYOUT_KEY =", "function clampSplitRatio(")
  + slice("clampSplitRatio") + slice("loadSessLayouts")
  + slice("sessLayoutFor") + slice("setSessLayout")
  + slice("sessDetailTabs") + slice("sessGroupHead")
  + slice("renderSession") + `
Object.assign(exports, {
  render: renderSession, tabs: sessDetailTabs, layout: sessLayoutFor,
  setLayout: setSessLayout, SUBTABS: DETAIL_SUBTABS,
});`
)(ctx, el, view, built, localStorage, "/", () => refreshed++);

const data = { session: { name: "coder1", cols: 80, rows: 24 }, meshes: [] };

/* The panel's own children, reduced to what each one is. */
function drawn() {
  return view.kids.map((k) => {
    if (k.classes.has("sess-group-head")) return "#" + k.text;
    if (k.classes.has("sess-subtabs")) return "sess-subtabs";
    const box = [...k.classes].find((c) => c.startsWith("box-"));
    if (box) return box.slice(4);
    return [...k.classes][0] || k.tag;
  });
}
function renderWith(sub) {
  if (sub === undefined) store.delete("claunch_sesslayout:/");
  else ctx.setLayout("coder1", { sub });
  built.length = 0;
  ctx.render(data);
  return drawn();
}

/* ---- the bar ------------------------------------------------------------ */
check("five sub-tabs, All first",
      ctx.SUBTABS.map(([, label]) => label),
      ["All", "Overview", "Messages", "Work", "Settings"]);
{
  store.clear();
  const bar = ctx.tabs("coder1");
  const tabs = bar.kids;
  check("the bar is a tablist of buttons", [
    bar.attrs.role, tabs.every((t) => t.tag === "button" && t.attrs.role === "tab"),
  ], ["tablist", true]);
  check("an unknown session starts on All, lit and dead",
        [tabs[0].classes.has("on"), tabs[0].attrs["aria-selected"],
         !!tabs[0].handlers.click],
        [true, "true", false]);
  check("...the rest live",
        tabs.slice(1).map((t) => !!t.handlers.click && !t.classes.has("on")),
        [true, true, true, true]);
  tabs[2].fire("click");
  check("picking one writes it down for this session", ctx.layout("coder1").sub,
        "messages");
  check("...and redraws now", refreshed, 1);
  check("...without touching another session's", ctx.layout("coder2").sub, "all");
  check("...or this one's panel radio", ctx.layout("coder1").rail, "detail");
  const again = ctx.tabs("coder1").kids;
  check("the rebuild lights the new choice alone",
        again.map((t) => t.classes.has("on")), [false, false, true, false, false]);
}
{
  store.set("claunch_sesslayout:/", JSON.stringify({ coder1: { sub: "banana" } }));
  check("junk in the store falls back to All", ctx.layout("coder1").sub, "all");
}

/* ---- what each sub-tab draws -------------------------------------------- */
const OVERVIEW = ["sess-meta", "sessFlags", "sessNote", "sessTask",
                  "sessBriefSection"];
const MESSAGES = ["sessInputJournal", "sessHandoff", "sessSend", "sessQueued",
                  "sess-meshes"];
const WORK = ["sessBeads", "sessCommits", "roleQuickJob"];
const SETTINGS = ["sessReborrow", "sessPerms", "sessMigrate"];
const TOP = ["sess-head", "sess-tabs", "sess-subtabs"];

check("All draws every group, in order, each under its name",
      renderWith("all"),
      [...TOP, "#Overview", ...OVERVIEW, "#Messages", ...MESSAGES,
       "#Work", ...WORK, "#Settings", ...SETTINGS]);
check("no stored choice is All", renderWith(undefined)[3], "#Overview");
check("Overview draws its group alone, with no head",
      renderWith("overview"), [...TOP, ...OVERVIEW]);
check("...and builds nothing else",
      built.filter((b) => !["sessFlags", "sessNote", "sessTask",
                            "sessBriefSection"].includes(b)), []);
check("Messages draws its group alone", renderWith("messages"),
      [...TOP, ...MESSAGES]);
check("Work draws its group alone", renderWith("work"), [...TOP, ...WORK]);
check("Settings draws its group alone", renderWith("settings"),
      [...TOP, ...SETTINGS]);
check("...and builds nothing else", built.slice().sort(),
      ["sessMigrate", "sessPerms", "sessReborrow"]);

/* The sub-tabs belong to Details: the Workflow and Beads panels keep the
   column to themselves. */
ctx.setLayout("coder1", { rail: "wf", sub: "work" });
ctx.render(data);
check("the Workflow panel draws no sub-tab bar", drawn(),
      ["sess-head", "sess-tabs", "sess-wf"]);
ctx.setLayout("coder1", { rail: "beads" });
ctx.render(data);
check("nor does the Beads panel", drawn(),
      ["sess-head", "sess-tabs", "sess-beads-panel"]);
check("...and leaving Details keeps the sub-tab for coming back",
      ctx.layout("coder1").sub, "work");

/* ---- the stylesheet ----------------------------------------------------- */
/* One row: the shared `.seq-tabs` rule wraps and sits later in the file, so
   the bar's own rule has to out-rank it to keep the row a row. */
check("the bar is one row, above the shared tab rule",
      /\.seq-tabs\.sess-subtabs \{[^}]*flex-wrap: nowrap/.test(css), true);
check("...its lit tab is underlined, not filled",
      /\.sess-subtabs \.seq-tab\.on \{[^}]*border-bottom-color/.test(css), true);
check("the group head has a rule", /\.sess-group-head \{/.test(css), true);
/* One card rule for every section box: a box missing from it is the one that
   looks out of place, which is how the metadata list, the input journal, the
   commits and the board summary each came to be drawn differently. */
const cardRule = (css.match(/((?:\.[\w-]+,\s*)*\.[\w-]+)\s*\{[^}]*border-radius: 8px[^}]*\}/g) || [])
  .find((r) => r.includes(".sess-flags")) || "";
for (const cls of ["sess-meta", "sess-flags", "sess-note", "sess-task",
                   "sess-brief-section", "sess-input-journal", "sess-send",
                   "sess-queued", "sess-bp", "sess-meshes", "sess-beads",
                   "sess-commits", "sess-quickjob", "sess-kids",
                   "sess-reborrow", "sess-perms", "sess-migrate"]) {
  check(`.${cls} is in the one card rule`,
        new RegExp(`\\.${cls}[,\\s]`).test(cardRule), true);
}

/* sessCommits returns its box empty when the daemon serves no `commits`;
   drawn as a card, that is a blank frame in the Work group. */
check("an empty commits box draws no card",
      /\.sess-commits:empty \{[^}]*display: none/.test(css), true);

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("sessdetailtabs_check: ok");
