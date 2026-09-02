/* Semantic search in the dashboard (daemon/rag.py behind it). Four things
   must hold. The rail's box narrows by substring over what a row already
   knows (name, identity, issue, branch, task, briefing one-liner) and, once
   the daemon has answered by meaning, shows only the sessions it named.
   The issue pickers put the daemon's ranking first and keep the substring
   matches it did not name, and ignore a ranking made for a different query.
   The Beads page's result list draws one row per hit with its score, marks
   a lexical hit, and says how much of the board the index covered. And the
   markup and stylesheet ship the rail box with the mobile font rule that
   keeps iOS from zooming on it.
   Slice the real functions out of app.js and drive them against a stub
   DOM. */
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "app.js"), "utf8");
const html = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "index.html"), "utf8");
const css = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");

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
    tag, kids: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    value: "", disabled: false, placeholder: "", title: "", href: "", type: "",
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.kids.push(c)); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get classList() {
      const self = this;
      return {
        add: (...cs) => cs.forEach((c) => self.classes.add(c)),
        contains: (c) => self.classes.has(c),
        toggle: (c, on) => { on ? self.classes.add(c) : self.classes.delete(c); },
      };
    },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((n) => n.classes.has(cls)); },
  };
  return n;
}
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) return;
  failures++;
  console.log(`FAIL ${label}\n  got  ${g}\n  want ${w}`);
}

/* ---- the rail's box ---------------------------------------------------- */
const rail = {};
new Function("exports",
  slice("sessionSearchText") + "\n" + slice("sessionMatchesSearch") + "\n" +
  slice("sessionSearchNote") + "\n" +
  "exports.matches = sessionMatchesSearch; exports.note = sessionSearchNote;")(rail);

const fleet = [
  { name: "s1", task: "wire the relay uplink", issue: "cl-1", branch: "s1-relay",
    briefing: { one_line: "reconnecting the relay" } },
  { name: "s2", identity: "worker w2", task: "kanban in_ready lane", role: null },
  { name: "s3", task: "" },
];
const sub = (q) => ({ q, hits: null, pending: false, error: "", index: null });
check("an empty box passes every session",
      fleet.map((s) => rail.matches(s, sub(""))), [true, true, true]);
check("substring reads the task", fleet.map((s) => rail.matches(s, sub("kanban"))), [false, true, false]);
check("...the issue id", fleet.map((s) => rail.matches(s, sub("cl-1"))), [true, false, false]);
check("...the branch and the briefing, case-insensitively",
      [rail.matches(fleet[0], sub("S1-RELAY")), rail.matches(fleet[0], sub("Reconnecting"))],
      [true, true]);
check("...the identity", fleet.map((s) => rail.matches(s, sub("w2"))), [false, true, false]);
check("every word must land (AND)",
      fleet.map((s) => rail.matches(s, sub("relay kanban"))), [false, false, false]);
const sem = { q: "who is on the relay", hits: new Map([["s3", 0.9]]), pending: false,
              error: "", index: { indexed: 3, total: 3 } };
check("a semantic answer shows only the sessions it named, whatever the words",
      fleet.map((s) => rail.matches(s, sem)), [false, false, true]);
check("the note counts the substring survivors and offers Enter",
      rail.note(sub("kanban"), fleet), "1 of 3 match — Enter searches by meaning");
check("...and after Enter says by meaning, with the coverage",
      rail.note(sem, fleet), "1 of 3 by meaning · 3/3 indexed — Esc clears");
check("...pending and failed states say so",
      [rail.note({ ...sub("x"), pending: true }, fleet), rail.note({ ...sub("x"), error: "HTTP 502" }, fleet)],
      ["searching by meaning…", "search failed: HTTP 502"]);
check("an empty box has no note", rail.note(sub(""), fleet), "");

/* ---- the pickers' order ------------------------------------------------ */
const pick = {};
new Function("exports",
  slice("issueSearchMatches") + "\n" + slice("issueSemanticOrder") + "\n" +
  slice("issuePickerLead") + "\n" +
  "exports.order = issueSemanticOrder; exports.lead = issuePickerLead;")(pick);
const board = [
  { id: "cl-1", title: "wire the rail", status: "open" },
  { id: "cl-2", title: "relay reconnect", status: "open" },
  { id: "cl-3", title: "kanban lane", status: "closed" },
];
let o = pick.order(board, "relay", null);
check("no ranking: the substring filter alone", [o.shown.map((i) => i.id), o.semantic], [["cl-2"], false]);
o = pick.order(board, "relay", { q: "relay", order: ["cl-3", "cl-1"], scores: { "cl-3": 0.9, "cl-1": 0.4 } });
check("a ranking comes first in its order, then the substring leftovers",
      [o.shown.map((i) => i.id), o.semantic], [["cl-3", "cl-1", "cl-2"], true]);
o = pick.order(board, "relay", { q: "kanban", order: ["cl-3"] });
check("a ranking for another query is ignored", [o.shown.map((i) => i.id), o.semantic], [["cl-2"], false]);
o = pick.order(board, "relay", { q: "relay", order: ["cl-9", "cl-2"] });
check("an id the board did not offer cannot be picked", o.shown.map((i) => i.id), ["cl-2"]);
check("the lead row names the mode",
      [pick.lead("", [], 3, false), pick.lead("relay", [1], 3, false), pick.lead("relay", [1, 2], 3, true),
       pick.lead("zzz", [], 3, true)],
      ["(pick an issue)", "(1 of 3 match)", "(2 of 3 match, by meaning)", '(no issue matches "zzz")']);

/* ---- the Beads page's results ------------------------------------------ */
const page = {};
new Function("exports", "el", "document", "beadsFocus", "beadsSearch", "clearBeadsSearch",
  slice("beadsStatusBadge") + "\n" + slice("beadsPriBadge") + "\n" + slice("beadsIssueRow") + "\n" +
  slice("ragScoreChip") + "\n" + slice("ragCoverageLine") + "\n" +
  slice("beadsMergeSearch") + "\n" + slice("beadsSearchSection") + "\n" +
  "exports.section = beadsSearchSection; exports.merge = beadsMergeSearch;")(
  page, el, document, "", {
    q: "relay", pending: false, error: "", reranked: true,
    index: { total: 10, indexed: 8, syncing: true },
    results: [
      { id: "cl-2", title: "relay reconnect", status: "open", priority: 1, score: 0.71,
        rerank_score: 0.93, lexical: true, excerpt: "the uplink drops" },
      { id: "cl-1", title: "wire the rail", status: "closed", priority: 2, score: 0.55, lexical: false },
    ],
  }, () => {});
const sec = page.section();
check("one row per hit, the id linked to its detail",
      sec.find("beads-id").map((n) => [n.text, n.href]),
      [["cl-2", "#/beads/cl-2"], ["cl-1", "#/beads/cl-1"]]);
check("the chip shows the reranker's score when it ran, and marks a lexical hit",
      sec.find("rag-score").map((n) => [n.text, n.classes.has("lex")]),
      [["0.93", true], ["0.55", false]]);
check("the head counts the hits and says the coverage",
      [sec.find("rag-head-text")[0].text, sec.find("rag-head")[0].find("beads-bits")[0].text],
      ["2 results for “relay”", "8/10 indexed (sync in progress — search again later for the rest) · reranked"]);
check("the excerpt rides under the row", sec.find("rag-excerpt").map((n) => n.text), ["the uplink drops"]);
const merged = page.merge("q", [
  { root: "/a", data: { results: [{ id: "a-1", score: 0.2 }], reranked: false, index: { total: 3, indexed: 3 } } },
  { root: "/b", error: "HTTP 502" },
  { root: "/c", data: { results: [{ id: "c-1", score: 0.1, rerank_score: 0.8 }], reranked: true,
                        index: { total: 5, indexed: 1, syncing: true } } },
]);
check("several boards merge best-first, carrying their root, summing coverage, keeping the error",
      [merged.results.map((r) => [r.id, r.root]), merged.index, merged.reranked, merged.error],
      [[["c-1", "/c"], ["a-1", "/a"]], { total: 8, indexed: 4, syncing: true }, true, "/b: HTTP 502"]);

/* ---- the markup and the stylesheet ------------------------------------- */
check("the rail ships the search box before the state filters",
      html.indexOf('id="session-search"') > 0 && html.indexOf('id="session-search"') < html.indexOf('id="session-filters"'),
      true);
const mobile = css.slice(css.indexOf("iOS zooms the page"), css.indexOf("iOS zooms the page") + 400);
check("the mobile font rule covers both search boxes",
      [mobile.includes("#session-search"), mobile.includes(".beads-search")], [true, true]);

if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log("ragsearch ok");
