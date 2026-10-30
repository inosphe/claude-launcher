/* A waiting gate's card body is markdown (claunch-ns4kd). The ask prompt is
   written with headings, lists and the plan's own line breaks, but the card
   set the whole thing as one `textContent` string: a request read as a wall
   of prose, with the option lines and the file references run into the
   argument. `noticeAskBody` is the one place both the corner card and the
   notification history row now go through, so the checks here hold three
   things at once — `prompt_md` is rendered as markdown (headings, lists,
   inline code), its line structure survives, and a daemon too old to send
   `prompt_md` still gets the plain one-line `prompt` drawn as it was. The
   renderer and `noticeAskBody` are sliced out of app.js, so a card rewritten
   without this path fails here rather than on a screenshot. */
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
    tag, attrs: {}, kids: [], text: undefined, className: "", classes: new Set(),
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener() {},
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get classList() {
      const self = this;
      return { add: (...cs) => cs.forEach((c) => self.classes.add(c)),
               toggle() {}, remove() {} };
    },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get className() { return [...this.classes].join(" "); },
  };
  return n;
}
function textNode(v) { return { tag: "#text", kids: [], text: String(v), classes: new Set() }; }
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
  ${slice("mdTableStart")}
  ${slice("mdBlocks")}
  ${slice("mdInto")}
  ${slice("mdText")}
  ${slice("mdPlain")}
  ${slice("noticeAskBody")}
  Object.assign(exports, { el, mdInto, noticeAskBody });
`)(document, scope);
const { noticeAskBody } = scope;

/* ---- helpers ----------------------------------------------------------- */
let failures = 0;
function ok(cond, what) {
  if (!cond) { failures++; console.error("FAIL:", what); }
}
function eq(got, want, what) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    failures++;
    console.error("FAIL:", what, "\n  got  ", JSON.stringify(got),
                  "\n  want ", JSON.stringify(want));
  }
}
function textOf(n) {
  if (!n) return "";
  if (n.tag === "#text") return n.text;
  if (n.tag === "br") return "\n";
  const child = (n.kids || []).map(textOf).join("");
  return (n.text === undefined ? "" : n.text) + child;
}
const walk = (n, out = []) => {
  for (const k of n.kids || []) { out.push(k); walk(k, out); }
  return out;
};
const tags = (n) => walk(n).map((k) => k.tag);
const classes = (n) => String(n.className || "").split(/\s+/).filter(Boolean);

/* The prompt the operator's screenshot showed: a heading, a list, an inline
   file reference, and paragraphs the ask wrote as separate lines. */
const PROMPT_MD = [
  "## 일감",
  "claunch-ns4kd — 알림 카드 본문",
  "",
  "## 변경",
  "- `app.js:63882` 를 고친다",
  "- `style.css` 에 규칙을 더한다",
  "",
  "리스크: 같은 파일을 **s6078** 이 잡고 있다.",
].join("\n");

/* ---- 1. the body is rendered as markdown, not set as one string -------- */
{
  const body = noticeAskBody("notice-sub gate-reason",
                             { prompt: "## 일감 claunch-ns4kd", prompt_md: PROMPT_MD });
  ok(classes(body).includes("gate-reason"), "the card's own class is kept");
  ok(classes(body).includes("md"), "and the markdown class is added to it");
  eq(body.text, undefined, "the body is not one textContent string");
  eq(walk(body).filter((k) => classes(k).includes("md-h2")).map(textOf),
     ["일감", "변경"], "both headings arrive as headings");
  eq(walk(body).filter((k) => k.tag === "li").map(textOf),
     ["app.js:63882 를 고친다", "style.css 에 규칙을 더한다"],
     "the option lines arrive as list items");
  eq(walk(body).filter((k) => k.tag === "code").map(textOf),
     ["app.js:63882", "style.css"], "and the file references as code spans");
  ok(walk(body).some((k) => k.tag === "strong" && textOf(k) === "s6078"),
     "emphasis is rendered too");
}

/* ---- 2. the ask's own line breaks survive ------------------------------ */
{
  const body = noticeAskBody("notice-sub gate-reason", { prompt_md: PROMPT_MD });
  const blocks = (body.kids || []).map((k) => k.tag);
  ok(blocks.filter((t) => t === "p").length >= 2,
     "the ask's blank lines start new paragraphs");
  ok(!/\n/.test(textOf(body.kids[0]) ), "…and the first line is its own block");
  const flat = textOf(body);
  ok(flat.includes("claunch-ns4kd — 알림 카드 본문"),
     "the argument is there, whole");
  ok(!/##\s/.test(flat) || !flat.startsWith("##"),
     "the heading marker is gone from the text that is drawn");
}

/* ---- 3. a daemon that sends no prompt_md still draws the plain line ---- */
{
  const body = noticeAskBody("notice-sub gate-reason",
                             { prompt: "may I a? — a single line" });
  ok(!classes(body).includes("md"),
     "no markdown class when there is no markdown to draw");
  eq(body.text, "may I a? — a single line",
     "the one-line prompt is set as text exactly as before");
  eq((body.kids || []).length, 0, "…and nothing is parsed out of it");
  const blank = noticeAskBody("notice-sub gate-reason",
                              { prompt: "should have a reason", prompt_md: "   " });
  eq(blank.text, "should have a reason",
     "a blank prompt_md falls back to the plain prompt");
}

/* ---- 4. nothing in a prompt becomes an element it did not write -------- */
{
  const body = noticeAskBody("notice-sub gate-reason",
    { prompt_md: "<img src=x onerror=alert(1)>\n\n<script>bad()</script>" });
  ok(!walk(body).some((k) => k.tag === "img" || k.tag === "script"),
     "an HTML tag in the prompt stays literal text");
  ok(textOf(body).includes("<img src=x onerror=alert(1)>"),
     "…and it is visible as the text the ask wrote");
}

/* ---- 5. the wiring: both cards go through this one body ---------------- */
{
  const calls = src.split('noticeAskBody("notice-sub gate-reason", rec)').length - 1;
  eq(calls, 2, "the corner card and the history row both call noticeAskBody");
  ok(!/el\("div", "notice-sub gate-reason", rec\.prompt\)/.test(src),
     "neither card sets the prompt as text directly any more");
  ok(src.includes('prompt_md: typeof a.prompt_md === "string" ? a.prompt_md : ""'),
     "the rail's prompt_md is carried onto the gate record");
  ok(src.includes('prompt_md: String((r.ask && r.ask.prompt) || "")'),
     "and a v1 run's ask prompt is carried too, so both versions draw alike");
}

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("noticeaskmd_check: ok");
