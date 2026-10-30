/* The board page's search box: two engines, one interaction.
   With the rag configured the box asks /api/search (ranked by meaning);
   without it — the state this file exercises first — it falls back to
   /api/beads/text-search (br's substring search), so the box works on a
   daemon with nothing configured beyond br. Typing debounces into a
   search, Enter runs one now or opens the walked row when the box matches
   what is shown, Arrow keys wrap the results, and every result highlights
   the query inside its id and title.
   Slice the real functions out of app.js and drive them against a stub
   DOM. */
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

/* ---- stub DOM: elements + text nodes, so highlight is exercised ---- */
function textNode(value) {
  return { nodeType: 3, nodeValue: value, parentNode: null };
}
function node(tag) {
  const n = {
    tag, nodeType: 1, kids: [], classes: new Set(), handlers: {},
    value: "", href: "", title: "", placeholder: "", type: "", parentNode: null,
    scrolls: 0, blurred: false, focused: false, caret: null,
    blur() { this.blurred = true; this.focused = false; },
    focus() { this.focused = true; this.blurred = false; },
    setSelectionRange(a, b) { this.caret = [a, b]; },
    appendChild(c) { this.kids.push(c); c.parentNode = this; return c; },
    insertBefore(c, ref) {
      const i = this.kids.indexOf(ref);
      if (i < 0) this.kids.push(c); else this.kids.splice(i, 0, c);
      c.parentNode = this;
      return c;
    },
    removeChild(c) {
      const i = this.kids.indexOf(c);
      if (i >= 0) this.kids.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev = {}) { for (const fn of this.handlers[k] || []) fn({ preventDefault() {}, ...ev }); },
    scrollIntoView() { this.scrolls++; },
    get childNodes() { return this.kids.slice(); },
    _text() {
      return this.kids.map((k) => k.nodeType === 3 ? k.nodeValue : k._text()).join("");
    },
    get textContent() { return this._text(); },
    set textContent(v) {
      for (const k of this.kids) k.parentNode = null;
      const t = textNode(String(v));
      t.parentNode = this;
      this.kids = [t];
    },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get classList() {
      const self = this;
      return {
        add: (...cs) => cs.forEach((c) => self.classes.add(c)),
        contains: (c) => self.classes.has(c),
      };
    },
    all() {
      const out = [this];
      for (const k of this.kids) if (k.all) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((n) => n.classes.has(cls)); },
  };
  return n;
}
const document = {
  createElement: (t) => node(t),
  createTextNode: (v) => textNode(v),
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* ---- injectable pieces ---- */
const calls = [];
let apiImpl = async () => { throw new Error("api not stubbed"); };
const location = { hash: "" };

const stubs = `
let ragConfigured = false;
let beadsCache = { boards: [{ root: "/repo" }] };
let beadsWorkspace = "/repo";
let beadsSearch = { q: "", results: null, pending: false, error: "", index: null,
                    reranked: false, mode: "", active: 0, draft: "" };
let beadsSearchTimer = null;
let beadsSearchFocus = false;
const BEADS_SEARCH_DEBOUNCE = 300;
let beadsFocus = "";
function renderBeads() {}
function beadsNameOfRoot(root) { return root; }
function setSearch(s) { beadsSearch = s; }
function getSearch() { return beadsSearch; }
function setRag(on) { ragConfigured = on; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "location",
  stubs
  + slice("beadsStatusBadge") + slice("beadsPriBadge") + slice("beadsIssueRow")
  + slice("beadsSearchBox") + slice("clearBeadsSearch") + slice("beadsSearchRoots")
  + slice("beadsSearchMove") + slice("beadsSearchOpen")
  + slice("beadsAskRag") + slice("beadsAskText") + slice("runBeadsSearch")
  + slice("beadsMergeSearch") + slice("beadsMergeTextSearch")
  + slice("beadsMarkHits") + slice("ragScoreChip") + slice("ragCoverageLine")
  + slice("beadsSearchSection")
  + `
Object.assign(exports, {
  box: beadsSearchBox, clear: clearBeadsSearch, roots: beadsSearchRoots,
  move: beadsSearchMove, open: beadsSearchOpen, run: runBeadsSearch,
  mergeRag: beadsMergeSearch, mergeText: beadsMergeTextSearch,
  mark: beadsMarkHits, section: beadsSearchSection,
  setSearch, getSearch, setRag,
});`
)(ctx, document, el, async (p) => { calls.push(String(p)); return apiImpl(p); }, location);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
const tick = () => new Promise((r) => setTimeout(r, 0));

/* ---- text mode: the no-rag fallback ----------------------------------- */
(async () => {
  const results = [
    { id: "a-1", title: "pagination is slow", status: "open", priority: 2 },
    { id: "b-2", title: "second PAGINATION case", status: "in_progress", priority: 1 },
  ];
  apiImpl = async () => ({ ok: true, json: async () => ({ issues: results, root: "/repo" }) });

  // Two engines behind one box: the rag asks /api/search, the fallback
  // asks /api/beads/text-search, each with the query and the board root.
  calls.length = 0;
  await ctx.run("pagination");
  check("text search asks the fallback endpoint", calls,
    ["/api/beads/text-search?q=pagination&limit=30&cwd=%2Frepo"]);
  let s = ctx.getSearch();
  check("text mode recorded", s.mode, "text");
  check("text results kept in br's order", s.results.map((r) => r.id), ["a-1", "b-2"]);
  check("rows carry their board root", s.results.map((r) => r.root), ["/repo", "/repo"]);

  // The section renders the rows, names the engine, and marks both hits.
  calls.length = 0;
  const sec = ctx.section();
  check("header names the substring engine",
    sec.find("beads-bits").map((n) => n.textContent),
    ["substring match — titles, descriptions, ids"]);
  check("one highlighted hit per matching row", sec.find("beads-hit").length, 2);
  check("hit text preserves the matched casing",
    sec.find("beads-hit").map((n) => n.textContent).sort(), ["PAGINATION", "pagination"]);

  // Walking wraps in both directions and Enter opens the walked row.
  ctx.move(-1);
  s = ctx.getSearch();
  check("walking up from the first row wraps to the last", s.active, 1);
  const walked = ctx.section().find("rag-hit").find((h) => h.classes.has("on"));
  check("the walked row is drawn as on", walked && walked.find("beads-id")[0].textContent, "b-2");
  ctx.open();
  check("Enter opens the walked issue", location.hash, "#/beads/b-2");

  // A board error is reported, not fatal: the other board's rows still show.
  apiImpl = async () => ({ ok: false, status: 500, json: async () => ({ error: "board gone" }) });
  calls.length = 0;
  await ctx.run("anything");
  s = ctx.getSearch();
  check("the failure surfaces with its board named", s.error, "/repo: board gone");
  check("and no rows are invented", s.results, []);

  // Dedup across boards: the same issue on two boards appears twice, the
  // same issue twice on one board does not.
  const merged = ctx.mergeText("q", [
    { root: "/repo", data: { issues: [{ id: "x", title: "x" }, { id: "x", title: "x dup" }] } },
    { root: "/repo", data: { issues: [{ id: "x", title: "x again" }] } },
    { root: "/other", data: { issues: [{ id: "x", title: "x elsewhere" }] } },
  ]);
  check("dup ids collapse on one board", merged.results.length, 2);
  check("the first spelling wins", merged.results[0].title, "x");

  /* ---- rag mode: the ranked path keeps its shape ---------------------- */
  ctx.setRag(true);
  apiImpl = async () => ({
    ok: true,
    json: async () => ({
      results: [
        { id: "r-1", title: "ranked first", score: 0.9, rerank_score: 0.8 },
        { id: "r-2", title: "ranked second", score: 0.7 },
      ],
      index: { total: 10, indexed: 10, syncing: false },
      reranked: true,
    }),
  });
  calls.length = 0;
  await ctx.run("meaning");
  s = ctx.getSearch();
  check("rag mode asks the rag endpoint", calls,
    ["/api/search?kind=beads&q=meaning&limit=30&cwd=%2Frepo"]);
  check("rag mode recorded", s.mode, "rag");
  check("rag rows ranked by rerank score", s.results.map((r) => r.id), ["r-1", "r-2"]);
  const ragSec = ctx.section();
  check("rag keeps its coverage line",
    ragSec.find("beads-bits").map((n) => n.textContent), ["10/10 indexed · reranked"]);
  check("rag shows a score chip per row", ragSec.find("rag-score").length, 2);
  // A stale answer (the query changed mid-flight) must not land.
  apiImpl = async () => ({ ok: true, json: async () => ({ results: [], index: null }) });
  const stale = ctx.run("first");
  ctx.setSearch({ ...ctx.getSearch(), q: "second", mode: "rag" });
  await stale;
  check("a stale answer is dropped", ctx.getSearch().q, "second");

  /* ---- the box: debounce, Enter, Escape ------------------------------- */
  ctx.setRag(false);
  ctx.setSearch({ q: "", results: null, pending: false, error: "", index: null,
                  reranked: false, mode: "", active: 0, draft: "" });
  const seen = [];
  apiImpl = async (p) => { seen.push(p); return { ok: true, json: async () => ({ issues: [], root: "/repo" }) }; };
  const box = ctx.box();
  box.value = "pa";
  box.fire("input");
  await new Promise((r) => setTimeout(r, 400));
  check("typing debounces into one search", seen.length, 1);
  check("the box's draft is the executed query", ctx.getSearch().q, "pa");
  // Enter with the box matching what is shown opens; here results are
  // empty so nothing opens — a second Enter re-searches instead.
  box.value = "pa";
  box.fire("keydown", { key: "Enter" });
  check("Enter on matching empty results does not navigate", location.hash, "#/beads/b-2");
  box.fire("keydown", { key: "Escape" });
  check("Escape clears the search state", ctx.getSearch().q, "");
  box.fire("keydown", { key: "ArrowDown" });
  check("arrows with no results are a no-op", ctx.getSearch().active ?? 0, 0);

  // A render rebuilds the filter bar mid-burst; the box must take its
  // focus back or the next keystroke goes nowhere.
  box.fire("focus");
  const rebuilt = ctx.box();
  await new Promise((r) => setTimeout(r, 5));
  check("the rebuilt box takes the focus back", rebuilt.focused, true);
  check("the caret sits at the end of the draft", rebuilt.caret, [rebuilt.value.length, rebuilt.value.length]);

  if (failures) {
    console.error(`${failures} failure(s)`);
    process.exit(1);
  }
  console.log("beadssearch_check: all checks passed");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
