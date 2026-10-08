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
   - a mesh's open tab carries the ⊕ that opens the create form with that
     mesh picked, and no other tab does;
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
const $ = (id) => (id === "session-group-tabs" ? bar : id === "session-list" ? list : null);

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
    "let sessionGroupTabScrollReset = false;\n" +
    [slice("sessionWorkspaceLabel"), slice("sessionGroupActiveTab"),
     slice("setSessionGroupTab"), slice("sessionGroupTabRows"),
     slice("selectSessionGroupTab"), slice("renderSessionGroupTabs")].join("\n") + `
Object.assign(exports, {
  order: () => sessionGroupOrder,
  tab: () => sessionGroupTab,
  scrollReset: () => sessionGroupTabScrollReset,
  active: sessionGroupActiveTab,
  split: sessionGroupTabRows,
  render: renderSessionGroupTabs,
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
check("the ⊕ sits on the open mesh tab alone",
      tabs().map((t) => t.querySelectorAll(".session-group-new").length), [0, 0, 1]);
check("the open tab is kept in view in a strip wider than the rail",
      tabs()[2].scrolled.length, 1);
const add = tabs()[2].querySelector(".session-group-new");
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

/* ---- (no mesh) and workspace tabs ---- */
ctx.render("mesh", split.tabs, "(no mesh)");
check("(no mesh) is not a mesh to create in: its open tab has no ⊕",
      bar.querySelectorAll(".session-group-new").length, 0);
ctx.render("workspace", [{ value: "F:/a/b/repo", rows: [session("s1")] }], "F:/a/b/repo");
check("a workspace tab is labelled by its last two path parts",
      [tabs()[0].querySelector(".session-group-name").textContent,
       tabs()[0].querySelectorAll(".session-group-new").length], ["…/b/repo", 0]);
ctx.render("", [], null);
check("ungrouped, the strip is hidden and empty",
      [bar.classes.has("hidden"), tabs().length], [true, 0]);

/* ---- the page ---- */
check("the fold and the jump are gone from the rail",
      ["sessionGroupCollapsed", "session-group-caret", "session-group-jump",
       "sessionGroupStickyLayout", "sessionGroupJumpOffset"].filter((name) =>
        src.includes(name) || css.includes(name)), []);
check("the tab strip sits between the filters and the list",
      html.indexOf('id="session-group-tabs"') > html.indexOf('id="session-filters"') &&
      html.indexOf('id="session-group-tabs"') < html.indexOf('id="session-list"'), true);
check("the tabs have their own styling, open and matched",
      [/\.session-group-tab\.active\s*\{/.test(css),
       /\.session-group-tab\.search-match\s*\{/.test(css)], [true, true]);
check("the open tab is part of the rail's rebuild signature",
      /typeof sessionGroupTab === "object" \? sessionGroupTab : null\]/.test(src), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("sessiongrouptabs_check: ok");
