/* The rail's grouping as tabs (claunch-xy0nx).

   Grouped by mesh or by workspace, the rail draws one tab per group above
   the list and only the open tab's sessions in it. What has to hold:
   - one grouping at a time, and a browser that saved the former nested
     order keeps its outer grouping;
   - the rows split by tab: each tab carries the rows its group holds, the
     open tab's rows reach the list without a heading, and the tab counts
     its sessions;
   - the open tab is remembered per grouping, and a saved tab that is not
     among the groups right now leaves the first one open without being
     forgotten;
   - pressing the open tab scrolls the list to its top; pressing another
     opens it and redraws from the records in hand;
   - under the strip, one header row for the open group alone (claunch-b7326):
     its kind, its full name, its session count and, for a mesh, the ⊕ that
     opens the create form with that mesh picked; no tab carries a ⊕;
   - pressing the header row scrolls the list to its top;
   - a mesh header carries a both | local | remote pick (claunch-no9dy):
     local drops the remote-shadow rows, remote drops this daemon's rows,
     tab headings stay so every tab survives and a nested heading the pick
     emptied goes; the pick is remembered per
     browser, redraws from the records in hand and is in the signature;
     a workspace header has no such pick;
   - the fold, its caret and the jump button are gone from the page. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
/* A top-level `let x = (() => { ... })();` initialiser, whole. */
function letBlock(name) {
  const start = src.indexOf(`let ${name} = (() => {`);
  if (start < 0) throw new Error("missing " + name);
  const end = src.indexOf("\n})();", start);
  return src.slice(start, end + 6);
}
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .+$`, "m"));
  if (!m) throw new Error(`cannot locate ${name} in app.js`);
  return m[0] + "\n";
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- stub DOM ---- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, attrs: {},
    title: "", type: "", handlers: {}, tabIndex: undefined, scrolled: [],
    appendChild(c) { n.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener(name, fn) { n.handlers[name] = fn; },
    setAttribute(k, v) { n.attrs[k] = v; },
    querySelector(sel) { return n.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) {
      const cls = sel.slice(1);
      return walk(n).filter((k) => k.classes.has(cls));
    },
    scrollIntoView(opts) { n.scrolled.push(opts); },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); n.kids = []; },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  };
  n.classList = {
    add: (c) => n.classes.add(c),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => {
      const want = on === undefined ? !n.classes.has(c) : !!on;
      if (want) n.classes.add(c); else n.classes.delete(c);
      return want;
    },
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const document = { createElement: node };
const bar = node("div");
bar.classes.add("hidden");
const list = node("ul");
list.scrollTop = 0;
const head = node("div");
head.classes.add("hidden");
const $ = (id) => (id === "session-group-tabs" ? bar : id === "session-group-header" ? head
  : id === "session-list" ? list : null);

function storage(seed) {
  const store = new Map(Object.entries(seed));
  const writes = [];
  return {
    writes,
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, v); writes.push([k, v]); },
  };
}

/* The state block and the tab helpers, sliced from app.js and bound to a
   browser storage of the harness's choosing. */
function load(seed) {
  const localStorage = storage(seed);
  const ctx = { redraws: [], went: [] };
  new Function(
    "exports", "document", "$", "localStorage", "BASE", "refreshSessions", "go",
    constLine("SESSION_GROUP_KEY") + constLine("SESSION_GROUP_ORDER_KEY") +
    constLine("SESSION_GROUPS") + letBlock("sessionGroupOrder") + "\n" +
    constLine("SESSION_GROUP_TAB_KEY") + letBlock("sessionGroupTab") + "\n" +
    constLine("SESSION_ORIGIN_FILTERS") + constLine("SESSION_ORIGIN_KEY") +
    letBlock("sessionOriginFilter") + "\n" +
    "let sessionGroupTabScrollReset = false;\n" +
    "let sessionFilter = 'current', sessionSearch = {q: ''};\n" +
    "function sessionMatchesSearch(s) { return !sessionSearch.q || s.name.includes(sessionSearch.q); }\n" +
    "function shadowMatchesSearch(s) { return !sessionSearch.q || s.session.includes(sessionSearch.q); }\n" +
    // The drag on the strip needs real pointer input: tabdragscroll_browser.cjs.
    "function enableTabDragScroll() {}\n" +
    [slice("sessionWorkspaceLabel"), slice("sessionGroupActiveTab"),
     slice("setSessionGroupTab"), slice("sessionGroupTabRows"),
     slice("scrollSessionListTop"), slice("selectSessionGroupTab"),
     slice("renderSessionGroupTabs"), slice("renderSessionGroupHeader"),
     slice("filterSessionOrigin"), slice("setSessionOriginFilter"),
     slice("sessionGroupRowVisible"), slice("sessionCategory"), slice("sessionMatchesFilter"),
     slice("shadowCategory"), slice("shadowMatchesFilter"),
     slice("syncSessionGroupTabSearch")].join("\n") + `
Object.assign(exports, {
  filter: (value) => { sessionFilter = value; syncSessionGroupTabSearch(); },
  search: (q) => { sessionSearch = {q}; syncSessionGroupTabSearch(); },
  order: () => sessionGroupOrder,
  tab: () => sessionGroupTab,
  scrollReset: () => sessionGroupTabScrollReset,
  active: sessionGroupActiveTab,
  split: sessionGroupTabRows,
  render: renderSessionGroupTabs,
  top: scrollSessionListTop,
  origin: () => sessionOriginFilter,
  byOrigin: filterSessionOrigin,
});`)(ctx, document, $, localStorage, "/t/local/",
      (opts) => ctx.redraws.push(opts), (hash) => ctx.went.push(hash));
  ctx.writes = localStorage.writes;
  return ctx;
}

/* ---- one grouping at a time ---- */
check("a saved nested order keeps its outer grouping",
      load({ "claunch_session_group_order:/t/local/": '["workspace","mesh"]' }).order(),
      ["workspace"]);
check("the former mesh-only checkbox still reads as mesh",
      load({ "claunch_session_group:/t/local/": "true" }).order(), ["mesh"]);
check("nothing saved is ungrouped", load({}).order(), []);
check("a saved tab pick is read back per grouping",
      load({ "claunch_session_group_tab:/t/local/": '{"mesh":"m2","workspace":7}' }).tab(),
      { mesh: "m2" });

/* ---- the split ---- */
const ctx = load({ "claunch_session_group_tab:/t/local/": '{"mesh":"m2"}' });
const session = (name) => ({ type: "session", session: { name }, depth: 0 });
const shadow = (name) => ({ type: "shadow", shadow: { session: name, machine: "x" }, depth: 0 });
const ROWS = [
  { type: "group", group: "mesh", value: "(no mesh)", level: 0 },
  session("s0"),
  { type: "group", group: "mesh", value: "m1", level: 0 },
  session("s1"), session("s2"),
  { type: "group", group: "mesh", value: "m2", level: 0 },
  session("s3"), shadow("r1"),
];
const split = ctx.split(ROWS, "mesh");
check("one tab per group, in the order the groups came",
      split.tabs.map((t) => t.value), ["(no mesh)", "m1", "m2"]);
check("each tab carries the rows its group holds",
      split.tabs.map((t) => t.rows.length), [1, 2, 2]);
check("the saved tab is the open one", split.active, "m2");
check("only the open tab's rows reach the list, without their heading",
      split.rows, [session("s3"), shadow("r1")]);
const gone = ctx.split(ROWS.slice(0, 5), "mesh");
check("a saved tab that is not among the groups leaves the first open",
      [gone.active, gone.rows], ["(no mesh)", [session("s0")]]);
check("...and is not forgotten for it", ctx.tab().mesh, "m2");
check("ungrouped rows pass through with no tabs",
      ctx.split([session("s0"), session("s1")], "mesh"),
      { tabs: [], active: null, rows: [session("s0"), session("s1")] });

/* ---- the strip ---- */
ctx.render("mesh", split.tabs, split.active);
const tabs = () => bar.kids;
check("the strip is shown when grouped",
      [bar.classes.has("hidden"), tabs().map((t) => t.dataset.value)],
      [false, ["(no mesh)", "m1", "m2"]]);
check("every tab is a tab to a reader, and says which is open",
      tabs().map((t) => [t.attrs.role, t.attrs["aria-selected"], t.tabIndex]),
      [["tab", "false", 0], ["tab", "false", 0], ["tab", "true", 0]]);
check("the open tab carries the active class alone",
      tabs().filter((t) => t.classes.has("active")).map((t) => t.dataset.value), ["m2"]);
check("each tab counts its sessions, local and remote",
      tabs().map((t) => t.querySelector(".session-group-count").textContent),
      ["1", "2", "2"]);
check("no tab carries a ⊕: the tabs are pickers alone",
      tabs().map((t) => t.querySelectorAll(".session-group-new").length), [0, 0, 0]);
check("the open tab is kept in view in a strip wider than the rail",
      tabs()[2].scrolled.length, 1);

/* ---- the open group's header row ---- */
check("one header row, for the open group, shown under the strip",
      [head.classes.has("hidden"), head.dataset.group, head.dataset.value],
      [false, "mesh", "m2"]);
check("it says the group's kind, full name and session count",
      [".session-group-kind", ".session-group-name", ".session-group-count"].map((sel) =>
        head.querySelector(sel).textContent), ["mesh", "m2", "2 sessions"]);
check("an open mesh's header carries the ⊕",
      head.querySelectorAll(".session-group-new").length, 1);
const add = head.querySelector(".session-group-new");
let stopped = 0;
add.handlers.click({ stopPropagation() { stopped++; } });
check("the ⊕ opens the create form with that mesh picked, and nothing else",
      [ctx.went, stopped, ctx.redraws.length], [["#/new/m2"], 1, 0]);

/* ---- the press ---- */
list.scrollTop = 300;
tabs()[2].handlers.click();
check("pressing the open tab takes the list to its top, with no redraw",
      [list.scrollTop, ctx.redraws.length], [0, 0]);
list.scrollTo = (opts) => { list.scrolledTo = opts; };
tabs()[2].handlers.click();
check("smoothly, where the browser can",
      list.scrolledTo, { top: 0, behavior: "smooth" });
delete list.scrollTo;
tabs()[1].handlers.click();
check("pressing another tab opens it and redraws from the records in hand",
      [ctx.tab().mesh, ctx.redraws, ctx.scrollReset()],
      ["m1", [{ cached: true }], true]);
check("the pick is remembered in this browser",
      ctx.writes.at(-1), ["claunch_session_group_tab:/t/local/", '{"mesh":"m1"}']);
let prevented = 0;
tabs()[0].handlers.keydown({ key: "Enter", preventDefault() { prevented++; } });
check("Enter on a tab presses it", [ctx.tab().mesh, prevented], ["(no mesh)", 1]);
tabs()[0].handlers.keydown({ key: "x", preventDefault() { prevented++; } });
check("other keys are left alone", prevented, 1);

/* ---- the header row's press ---- */
list.scrollTop = 300;
const redrawsBefore = ctx.redraws.length;
ctx.top();
check("pressing the header row takes the list to its top, with no redraw",
      [list.scrollTop, ctx.redraws.length - redrawsBefore], [0, 0]);

/* ---- (no mesh) and workspace tabs ---- */
ctx.render("mesh", split.tabs, "(no mesh)");
check("(no mesh) is not a mesh to create in: its header has no ⊕",
      [head.querySelector(".session-group-name").textContent,
       head.querySelector(".session-group-count").textContent,
       head.querySelectorAll(".session-group-new").length], ["(no mesh)", "1 session", 0]);
ctx.render("workspace", [{ value: "F:/a/b/repo", rows: [session("s1")] }], "F:/a/b/repo");
check("a workspace tab is labelled by its last two path parts",
      [tabs()[0].querySelector(".session-group-name").textContent,
       tabs()[0].querySelectorAll(".session-group-new").length], ["…/b/repo", 0]);
check("...and its header names the full path, with no ⊕",
      [head.querySelector(".session-group-kind").textContent,
       head.querySelector(".session-group-name").textContent,
       head.querySelectorAll(".session-group-new").length], ["workspace", "F:/a/b/repo", 0]);
ctx.render("", [], null);
check("ungrouped, the strip is hidden and the header keeps only the origin filter",
      [bar.classes.has("hidden"), tabs().length, head.classes.has("hidden"), head.kids.length],
      [true, 0, false, 1]);

/* ---- the mesh header's local/remote pick ---- */
check("nothing saved shows both sides", load({}).origin(), "both");
check("a saved pick is read back",
      load({ "claunch_session_origin:/t/local/": "remote" }).origin(), "remote");
check("an unknown saved pick falls back to both",
      load({ "claunch_session_origin:/t/local/": "far" }).origin(), "both");
check("both keeps every row", ctx.byOrigin(ROWS, "both"), ROWS);
const rowName = (r) => r.type === "group" ? r.value
  : r.type === "session" ? r.session.name : r.shadow.session;
check("local drops the remote-shadow rows and keeps every heading",
      ctx.byOrigin(ROWS, "local").map(rowName),
      ["(no mesh)", "s0", "m1", "s1", "s2", "m2", "s3"]);
check("remote drops this daemon's rows and keeps every heading",
      ctx.byOrigin(ROWS, "remote").map(rowName),
      ["(no mesh)", "m1", "m2", "r1"]);
const remoteSplit = ctx.split(ctx.byOrigin(ROWS, "remote"), "mesh");
check("...so every mesh keeps its tab, counted by what is shown",
      [remoteSplit.tabs.map((t) => t.value),
       remoteSplit.tabs.map((t) => t.rows.length)],
      [["(no mesh)", "m1", "m2"], [0, 0, 1]]);
const NESTED = [
  { type: "group", group: "mesh", value: "m1", level: 0 },
  { type: "group", group: "workspace", value: "F:/a", level: 1 },
  session("s1"),
  { type: "group", group: "workspace", value: "F:/b", level: 1 },
  session("s2"),
  { type: "group", group: "workspace", value: "@x", level: 1 },
  shadow("r1"),
  { type: "group", group: "mesh", value: "m2", level: 0 },
  { type: "group", group: "workspace", value: "@x", level: 1 },
  shadow("r2"),
];
check("nested, local drops the emptied @machine heading and keeps every tab",
      ctx.byOrigin(NESTED, "local").map(rowName),
      ["m1", "F:/a", "s1", "F:/b", "s2", "m2"]);
check("nested, remote drops the emptied local workspace headings",
      ctx.byOrigin(NESTED, "remote").map(rowName),
      ["m1", "@x", "r1", "m2", "@x", "r2"]);
check("nested, both keeps every row", ctx.byOrigin(NESTED, "both"), NESTED);
const UNGROUPED = [session("s1"),
  { type: "group", group: "remote", value: "other-daemon", level: 0 }, shadow("r1")];
check("ungrouped local removes the empty remote daemon heading",
      ctx.byOrigin(UNGROUPED, "local").map(rowName), ["s1"]);
check("ungrouped remote keeps the daemon heading with its remote row",
      ctx.byOrigin(UNGROUPED, "remote").map(rowName), ["other-daemon", "r1"]);
const octx = load({});
octx.render("mesh", split.tabs, "m2");
const opts = () => head.querySelectorAll(".session-origin-option");
check("a mesh header carries the three picks, both on",
      [opts().map((o) => o.dataset.origin), opts().map((o) => o.attrs["aria-checked"]),
       opts().filter((o) => o.classes.has("active")).map((o) => o.dataset.origin)],
      [["both", "local", "remote"], ["true", "false", "false"], ["both"]]);
check("...before the ⊕",
      head.kids.findIndex((k) => k.classes.has("session-origin-filter")) <
      head.kids.findIndex((k) => k.classes.has("session-group-new")), true);
let ostopped = 0;
opts()[1].handlers.click({ stopPropagation() { ostopped++; } });
check("picking local narrows the rail and redraws from the records in hand",
      [octx.origin(), octx.redraws, ostopped, octx.scrollReset()],
      ["local", [{ cached: true }], 1, true]);
check("the pick is remembered in this browser",
      octx.writes.at(-1), ["claunch_session_origin:/t/local/", "local"]);
octx.render("mesh", split.tabs, "m2");
opts()[1].handlers.click({ stopPropagation() {} });
check("picking the one already on does not redraw", octx.redraws.length, 1);
octx.render("mesh", split.tabs, "(no mesh)");
check("(no mesh) carries the pick too", opts().length, 3);
octx.render("workspace", [{ value: "F:/a/b/repo", rows: [session("s1")] }], "F:/a/b/repo");
check("a workspace header has no local/remote pick", opts().length, 0);
check("the pick is applied to mesh and ungrouped rows before the tabs split",
      /\(!groupOrder.length \|\| groupOrder\[0\] === "mesh"\) && typeof filterSessionOrigin === "function"\s*\? filterSessionOrigin\(allRows, sessionOriginFilter\)/.test(src) &&
      /sessionGroupTabRows\(originRows, groupOrder\[0\]\)/.test(src) &&
      /const rows = tabbed \? tabbed.rows : originRows;/.test(src), true);
check("the pick is part of the rail's rebuild signature",
      /typeof sessionOriginFilter === "string" \? sessionOriginFilter : "both"[,\]]/.test(src), true);
check("the pick has its own styling, on and off",
      [/\.session-origin-option\.active\s*\{/.test(css),
       /\.session-origin-filter\s*\{/.test(css)], [true, true]);
octx.render("", [], null);
check("ungrouped retains the selected origin and clears the old group metadata",
      [opts().map((o) => o.dataset.origin),
       opts().filter((o) => o.classes.has("active")).map((o) => o.dataset.origin),
       head.dataset.group, head.dataset.value],
      [["both", "local", "remote"], ["local"], "", ""]);

/* ---- counts follow status and search without a rail rebuild (claunch-3n6ml) ---- */
const counts = () => tabs().map((t) => t.querySelector(".session-group-count").textContent);
const headerCount = () => head.querySelector(".session-group-count").textContent;
const counted = load({ "claunch_session_group_tab:/t/local/": '{"mesh":"claunch"}' });
const record = (name, extra = {}) => ({ type: "session", session: { name, status: "busy", ...extra } });
const dead = (name) => record(name, { status: "exited" });
const screenTabs = [
  { value: "(no mesh)", rows: Array.from({length: 6}, (_, i) => dead(`none-${i}`)) },
  { value: "claunch", rows: [
    ...Array.from({length: 7}, (_, i) => record(`live-${i}`)),
    ...Array.from({length: 91}, (_, i) => dead(`dead-${i}`)),
  ] },
];
counted.filter(["running", "hold", "paused"]);
counted.render("mesh", screenTabs, "claunch");
check("104 records with seven live sessions render filtered counts immediately",
      [counts(), headerCount()], [["0", "7"], "7 sessions"]);
counted.filter("current");
check("current restores the screenshot totals without rebuilding",
      [counts(), headerCount()], [["6", "98"], "98 sessions"]);

const mixed = [{ value: "claunch", rows: [
  record("live"), dead("dead"),
  record("hold", {status: "exited", held_at: "now"}),
  record("paused", {status: "exited", paused_at: "now"}),
  record("archived", {status: "exited", archived_at: "now"}),
  {type: "group", group: "workspace", value: "nested", level: 1},
  {type: "shadow", shadow: {session: "remote-live", card: {exited: false}}},
  {type: "shadow", shadow: {session: "remote-dead", card: {exited: true}}},
] }, {value: "empty", rows: []}];
counted.render("mesh", mixed, "claunch");
for (const [filter, want] of [
  ["current", 6], [["running", "hold", "paused"], 4], ["running", 2],
  ["hold", 1], ["paused", 1], ["killed", 2], ["archived", 1], [[], 0],
]) {
  counted.filter(filter);
  check(`mixed local/remote counts for ${JSON.stringify(filter)}`,
        [counts(), headerCount()], [[String(want), "0"], `${want} session${want === 1 ? "" : "s"}`]);
}
counted.filter("current");
counted.search("remote-live");
check("search updates the remote match count and open header without rebuilding",
      [counts(), headerCount(), tabs()[0].classes.has("search-match")],
      [["1", "0"], "1 session", true]);
counted.filter("killed");
check("a search match excluded by status contributes nothing",
      [counts(), headerCount(), tabs()[0].classes.has("search-match")],
      [["0", "0"], "0 sessions", false]);
counted.search("");
check("clearing search restores status counts", counts(), ["2", "0"]);
counted.filter("running");
counted.search("live");
counted.render("workspace", mixed, "claunch");
check("workspace tabs use the same status and search count",
      [counts(), headerCount()], [["2", "0"], "2 sessions"]);
counted.render("mesh", [{value: "claunch", rows: counted.byOrigin(mixed[0].rows, "local")}], "claunch");
check("origin filtering and status/search compose", [counts(), headerCount()], [["1"], "1 session"]);

/* ---- the page ---- */
check("the fold and the jump are gone from the rail",
      ["sessionGroupCollapsed", "session-group-caret", "session-group-jump",
       "sessionGroupStickyLayout", "sessionGroupJumpOffset"].filter((name) =>
        src.includes(name) || css.includes(name)), []);
check("the tab strip sits between the filters and the list",
      html.indexOf('id="session-group-tabs"') > html.indexOf('id="session-filters"') &&
      html.indexOf('id="session-group-tabs"') < html.indexOf('id="session-list"'), true);
check("the header row sits right under the tab strip, above the list",
      html.indexOf('id="session-group-header"') > html.indexOf('id="session-group-tabs"') &&
      html.indexOf('id="session-group-header"') < html.indexOf('id="session-list"'), true);
check("the header row is wired to scroll the list to its top",
      /\$\("session-group-header"\)\?\.addEventListener\("click", scrollSessionListTop\)/.test(src),
      true);
check("the header row is hidden with the strip in grid view",
      /groupHeader\.classList\.toggle\("view-hidden", grid\)/.test(src) &&
      /\.session-group-header\.view-hidden\s*\{\s*display: none/.test(css), true);
check("the tabs have their own styling, open and matched",
      [/\.session-group-tab\.active\s*\{/.test(css),
       /\.session-group-tab\.search-match\s*\{/.test(css)], [true, true]);
check("the open tab is part of the rail's rebuild signature",
      /typeof sessionGroupTab === "object" \? sessionGroupTab : null,/.test(src), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("sessiongrouptabs_check: ok");
