/* cflow step reports are written in markdown and rendered as markdown on the
   dashboard. Before this, `.wf-report-summary` was a <p> with no pre-wrap:
   every newline an agent wrote collapsed, and a report came out as one wall
   of prose. So the checks here hold three lines at once — the grammar does
   what markdown says (lists, fences, tables, emphasis), the line breaks an
   agent typed SURVIVE, and nothing in a report can turn into an element the
   report did not write (no innerHTML anywhere on this path). The renderer is
   sliced out of app.js and driven against a stub DOM, and wfReports is
   sliced with it so the wiring is checked and not just the parser. */
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
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
/* The constants come from the source too: a grammar rule tightened in app.js
   and not here would leave these checks testing a copy nobody runs. */
function sliceTo(decl, end) {
  const start = src.indexOf(decl);
  if (start < 0) throw new Error("missing " + decl);
  const stop = src.indexOf(end, start);
  if (stop < 0) throw new Error("unterminated " + decl);
  return src.slice(start, stop + end.length);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", classes: new Set(), handlers: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
  };
  n.classList = { add: (...cs) => cs.forEach((c) => n.classes.add(c)) };
  return n;
}
function textNode(v) {
  return { tag: "#text", kids: [], text: String(v), classes: new Set(), attrs: {} };
}
const document = {
  createElement: (t) => node(t),
  createTextNode: (v) => textNode(v),
};

const scope = { document };
new Function("document", "exports", `
  ${slice("el")}
  ${sliceTo("const MD_BULLET", ";")}
  ${sliceTo("const MD_RULE", ";")}
  ${sliceTo("const MD_FENCE", ";")}
  ${sliceTo("const MD_BLOCK_START", ";")}
  ${sliceTo("const MD_SAFE_HREF", ";")}
  ${sliceTo("const MD_INLINE = [", "\n];")}
  ${sliceTo("const mdCells =", ";")}
  ${slice("mdWrap")}
  ${slice("mdLink")}
  ${slice("mdInline")}
  ${slice("mdList")}
  ${slice("mdTable")}
  ${slice("mdBlocks")}
  ${slice("mdInto")}
  ${slice("mdText")}
  ${slice("mdPlain")}
  ${slice("wfReports")}
  Object.assign(exports, {
    el, mdBlocks, mdInto, mdText, mdPlain, wfReports,
  });
`)(document, scope);
const { el, mdBlocks, mdInto, mdText, mdPlain, wfReports } = scope;

/* ---- helpers ----------------------------------------------------------- */
let failures = 0;
function ok(cond, what) {
  if (!cond) { failures++; console.error("FAIL:", what); }
}
function eq(got, want, what) {
  const same = JSON.stringify(got) === JSON.stringify(want);
  if (!same) {
    failures++;
    console.error("FAIL:", what, "\n  got  ", JSON.stringify(got),
                  "\n  want ", JSON.stringify(want));
  }
}
/* A <br> is a line break, which is the whole point of the paragraph rule —
   so the readback has to turn one back into "\n" or the checks below could
   not tell "a\nb" from "ab". */
function textOf(n) {
  if (n.tag === "#text") return n.text;
  if (n.tag === "br") return "\n";
  return (n.text || "") + n.kids.map(textOf).join("");
}
const walk = (n, out = []) => {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
};
const tags = (n) => walk(n).map((k) => k.tag);
const hasClass = (n, c) => String(n.className || "").split(" ").includes(c);
const find = (n, c) => walk(n).filter((k) => hasClass(k, c));
const box = (blocks) => { const b = node("div"); blocks.forEach((x) => b.appendChild(x)); return b; };

/* ---- 1. the bug that started this: newlines survive ---------------------- */
{
  const blocks = mdBlocks("첫 줄\n둘째 줄\n셋째 줄");
  eq(blocks.length, 1, "three lines with no blank between them are ONE paragraph");
  ok(hasClass(blocks[0], "md-p"), "and it is a md-p");
  eq(tags(blocks[0]).filter((t) => t === "br").length, 2,
     "two <br> for the two newlines — a report's line breaks are not prose");
  eq(textOf(blocks[0]), "첫 줄\n둘째 줄\n셋째 줄", "and the text reads back as typed");
}
{
  const blocks = mdBlocks("one\n\ntwo");
  eq(blocks.length, 2, "a blank line starts a new paragraph");
  eq(blocks.map(textOf), ["one", "two"], "…and neither swallows the other");
}
{
  // Pasted output that nobody fenced still keeps its columns: the renderer
  // does not trim leading whitespace, and .md-p is pre-wrap in the stylesheet.
  const p = mdBlocks("head\n    aligned    value")[0];
  ok(textOf(p).includes("    aligned    value"),
     "leading and inner spacing of an unfenced line is kept");
}

/* ---- 2. lists ----------------------------------------------------------- */
{
  const [list] = mdBlocks("- one\n- two\n- three");
  eq(list.tag, "ul", "a '- ' run is a <ul>");
  eq(walk(list).filter((k) => k.tag === "li").map(textOf), ["one", "two", "three"],
     "one <li> per bullet");
}
{
  const [list] = mdBlocks("* star\n+ plus");
  eq(walk(list).filter((k) => k.tag === "li").length, 2, "'*' and '+' bullet too");
}
{
  const [list] = mdBlocks("3. third\n4. fourth");
  eq(list.tag, "ol", "a numbered run is an <ol>");
  eq(list.getAttribute("start"), "3", "…starting where the report started it");
}
{
  const [list] = mdBlocks("- outer\n  - inner a\n  - inner b\n- outer two");
  const items = list.kids.filter((k) => k.tag === "li");
  eq(items.length, 2, "the deeper bullets do not become siblings");
  const sub = items[0].kids.filter((k) => k.tag === "ul");
  eq(sub.length, 1, "they nest under the item above them");
  eq(sub[0].kids.filter((k) => k.tag === "li").map(textOf), ["inner a", "inner b"],
     "…both of them");
}
{
  const [list] = mdBlocks("- item\n  continued here\n- next");
  const items = list.kids.filter((k) => k.tag === "li");
  eq(items.length, 2, "an indented non-bullet line is not a new item");
  eq(textOf(items[0]), "item\ncontinued here", "…it continues the item above");
}
{
  const blocks = mdBlocks("- a\n\nafter");
  eq(blocks.length, 2, "a blank line then prose ends the list");
  eq(blocks[1].tag, "p", "…and what follows is a paragraph");
}

/* ---- 3. fenced code: exact, and inert -------------------------------- */
{
  const [pre] = mdBlocks("```\n  keep   me\n**not bold**\n```");
  eq(pre.tag, "pre", "a fence is a <pre>");
  eq(pre.kids[0].tag, "code", "…wrapping a <code>");
  eq(textOf(pre), "  keep   me\n**not bold**",
     "everything inside a fence is literal — spacing kept, markdown NOT parsed");
}
{
  const [pre] = mdBlocks("```python\nx = 1\n```");
  eq(pre.kids[0].getAttribute("data-lang"), "python", "the info string is kept");
}
{
  const [pre] = mdBlocks("```\nunclosed at end of report");
  eq(textOf(pre), "unclosed at end of report",
     "a fence nobody closed still renders, rather than eating the rest");
}

/* ---- 4. inline ---------------------------------------------------------- */
const inline = (s) => mdBlocks(s)[0];
{
  const p = inline("a **bold** and *em* and `code` and ~~gone~~");
  eq(walk(p).filter((k) => k.tag === "strong").map(textOf), ["bold"], "**bold**");
  eq(walk(p).filter((k) => k.tag === "em").map(textOf), ["em"], "*em*");
  eq(walk(p).filter((k) => k.tag === "code").map(textOf), ["code"], "`code`");
  eq(walk(p).filter((k) => k.tag === "s").map(textOf), ["gone"], "~~strike~~");
  eq(textOf(p), "a bold and em and code and gone", "…and the text is the text");
}
{
  // The reason `_` is guarded: these reports are FULL of snake_case.
  const p = inline("tests/test_web_topology.py and run_the_thing()");
  eq(walk(p).filter((k) => k.tag === "em").length, 0,
     "mid-word underscores are part of the name, not emphasis");
  eq(textOf(p), "tests/test_web_topology.py and run_the_thing()", "…verbatim");
}
{
  const p = inline("_really_ and __both__");
  eq(walk(p).filter((k) => k.tag === "em").map(textOf), ["really"],
     "a standalone _em_ still works");
  eq(walk(p).filter((k) => k.tag === "strong").map(textOf), ["both"], "__strong__ too");
}
{
  const p = inline("`**not bold**`");
  eq(walk(p).filter((k) => k.tag === "strong").length, 0,
     "a code span swallows the markers inside it");
  eq(textOf(p), "**not bold**", "…and shows them");
}
{
  const p = inline("2 \\* 3 \\*not em\\*");
  eq(walk(p).filter((k) => k.tag === "em").length, 0, "a backslash escapes the marker");
  eq(textOf(p), "2 * 3 *not em*", "…and the backslash itself is gone");
}

/* ---- 5. links: only where the page may go ------------------------------- */
{
  const a = walk(inline("see [the run](https://example.test/run)"))
    .find((k) => k.tag === "a");
  ok(a, "an http link is an <a>");
  eq(a.getAttribute("href"), "https://example.test/run", "…with its href");
  eq(a.getAttribute("rel"), "noreferrer noopener", "…and not leaking the referrer");
  eq(textOf(a), "the run", "…labelled as written");
}
{
  const p = inline("[go](#/wf/x)");
  const a = walk(p).find((k) => k.tag === "a");
  eq(a.getAttribute("href"), "#/wf/x", "an in-page hash link is allowed");
  eq(a.getAttribute("target"), null, "…and is not shoved into a new tab");
}
{
  const p = inline("[click](javascript:alert(1))");
  eq(walk(p).filter((k) => k.tag === "a").length, 0,
     "a javascript: url never becomes a link");
  ok(textOf(p).includes("click"),
     "…but its text is still shown, so nothing is silently dropped");
}

/* ---- 6. headings, rules, quotes ---------------------------------------- */
{
  const [h] = mdBlocks("## What happened");
  ok(hasClass(h, "md-h") && hasClass(h, "md-h2"), "'## ' is a level-2 heading");
  eq(textOf(h), "What happened", "…without its markers");
}
{
  eq(mdBlocks("a\n\n---\n\nb").map((b) => b.tag), ["p", "hr", "p"], "'---' is a rule");
  eq(mdBlocks("- a\n- b").length, 1,
     "a '- ' bullet is not mistaken for a rule");
}
{
  const [q] = mdBlocks("> quoted line\n> second");
  eq(q.tag, "blockquote", "'> ' is a blockquote");
  eq(textOf(q), "quoted line\nsecond", "…keeping its own line breaks");
}

/* ---- 7. tables ---------------------------------------------------------- */
{
  const [t] = mdBlocks("| axis | value |\n| --- | ---: |\n| suite | 812/812 |");
  eq(t.tag, "table", "a pipe table with a rule row is a <table>");
  eq(walk(t).filter((k) => k.tag === "th").map(textOf), ["axis", "value"], "header cells");
  eq(walk(t).filter((k) => k.tag === "td").map(textOf), ["suite", "812/812"], "body cells");
  eq(walk(t).filter((k) => k.tag === "td")[1].getAttribute("style"),
     "text-align:right", "'---:' right-aligns its column");
}
{
  const [b] = mdBlocks("a | b was not a table");
  eq(b.tag, "p", "a stray pipe with no rule row stays a paragraph");
}

/* ---- 8. a report that was never markdown still reads as typed ---------- */
{
  const legacy = "ran the sweep; 812 passed, 0 failed.\n" +
    "touched src/claude_launcher/web/static/app.js (100% of it, 2+2=4).";
  eq(textOf(box(mdBlocks(legacy))), legacy,
     "plain prose survives the renderer character for character");
}

/* ---- 9. nothing in a report becomes an element ------------------------- */
{
  const nasty = '<img src=x onerror="boom()"> & <b>bold?</b>';
  const rendered = box(mdBlocks(nasty));
  eq(walk(rendered).filter((k) => k.tag === "img" || k.tag === "b").length, 0,
     "html in a report is text, not markup — the path builds nodes, never innerHTML");
  eq(textOf(rendered), nasty, "…and it is shown exactly as the agent wrote it");
}

/* ---- 10. the flat forms, for one-line rails and title tooltips --------- */
{
  const md = "## Head\n\n- **first** fact\n- `second` fact\n\nand [a link](https://x.test)";
  const flat = mdPlain(md);
  ok(!/[*`#\[]/.test(flat), "mdPlain leaves no markers behind: " + flat);
  ok(!flat.includes("\n"), "…and folds onto one line for an ellipsised row");
  ok(flat.includes("• first fact") && flat.includes("a link"),
     "…keeping the words and marking the bullets: " + flat);

  const kept = mdText(md);
  ok(kept.includes("\n"), "mdText keeps the line structure a tooltip can show");
  ok(!/\*\*|`|^#/m.test(kept), "…but still drops the markers: " + kept);
}
eq(mdPlain(undefined), "", "a missing summary flattens to empty, not 'undefined'");
eq(textOf(box(mdBlocks(null))), "", "…and renders to nothing");

/* ---- 11. the wiring: the report cards really go through markdown ------- */
{
  const ui = { getStep: () => null, select: () => {} };
  const data = {
    reports: [{
      step: "work", visit: 1, at: "2026-08-26T07:00:00",
      summary: "**done** — the sweep is green",
      details: "- touched `app.js`\n- 812/812",
    }],
  };
  const out = wfReports(data, ui);
  const summary = find(out, "wf-report-summary")[0];
  const details = find(out, "wf-report-details")[0];
  ok(summary, "the run page still renders a summary");
  ok(hasClass(summary, "md"), "…as a markdown block");
  eq(walk(summary).filter((k) => k.tag === "strong").map(textOf), ["done"],
     "…whose markdown is actually rendered");
  ok(details && hasClass(details, "md"), "and the details likewise");
  eq(walk(details).filter((k) => k.tag === "li").map(textOf),
     ["touched app.js", "812/812"],
     "…so evidence written as a list arrives as a list");
  ok(!walk(out).some((k) => k.tag === "pre" && hasClass(k, "wf-report-details")),
     "the old <pre> that showed raw markers is gone");
}

/* ---- 12. the source itself: no innerHTML on this path ------------------ */
{
  const start = src.indexOf("const MD_BULLET");
  const end = src.indexOf("function mdPlain");
  ok(start > 0 && end > start, "the markdown block is where the slices expect it");
  ok(!src.slice(start, src.indexOf("}", end)).includes("innerHTML"),
     "the renderer never touches innerHTML");
}

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("mdrender_check: ok");
