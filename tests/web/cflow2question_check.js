/* cflow v2 brief asks in the run view (docs/cflow-v2.md 2.10), run against
   the real cf2QuestionView from app.js with a stub DOM.

   The question comes from GET /api/cflow2/run asks[].question
   (cflow2/question.to_json). Pinned here: sections render in their order,
   text as heading + markdown, the options table as one card per option
   (short columns as tags, prose ones as labelled markdown), and the
   recommendation as data -- data-value / data-recommended on the cards and
   the 권장 chip drawn from that -- with no code per question type. */
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

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ""; this.textContent = ""; this.dataset = {}; }
  get childNodes() { return this.children; }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { for (const c of cs) this.children.push(c); }
}
const document = { createElement: (tag) => new Node(tag) };
function mdInto(node, text) { node.md = text; return node; }

const cf2QuestionView = new Function("document", "mdInto",
  slice("el") + "\n" + slice("cf2QuestionView") + "\nreturn cf2QuestionView;")(document, mdInto);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const Q = {
  format: "decision",
  options: ["narrow", "full"],
  recommended: "narrow",
  sections: [
    { field: "problem", label: "문제", kind: "text", value: "범위를 정한다" },
    { field: "options", label: "선택지", kind: "table", value: {
      columns: [{ field: "summary", label: "내용" }, { field: "risk", label: "위험" }],
      rows: [
        { name: "narrow", recommended: true, cells: { summary: "좁게", risk: "작음" } },
        { name: "full", recommended: false, cells: { summary: "넓게", risk: null } },
      ] } },
    { field: "recommendation", label: "권장안", kind: "choice", value: "narrow" },
    { field: "why", label: "권장 이유", kind: "text", value: "되돌릴 수 있다" },
  ],
};

const view = cf2QuestionView(Q);
const text = (n) => n.textContent || n.children.map(text).join("");
check("the wrapper", [view.tag, view.className], ["div", "cf2-question"]);
check("sections keep their order: problem, options, the recommendation card",
      view.children.map((c) => c.className), ["cf2-sec", "cf2-sec", "cf2-rec"]);
check("a text section is its label then markdown",
      [view.children[0].children[0].textContent, view.children[0].children[1].md],
      ["문제", "범위를 정한다"]);
const cards = view.children[1].children[1].children;
check("one card per option, the option's name as data",
      cards.map((c) => [c.className, c.dataset.value, c.dataset.recommended || ""]),
      [["cf2-opt-card", "narrow", "true"], ["cf2-opt-card", "full", ""]]);
check("the 권장 chip comes from the data, beside the name",
      cards.map((c) => c.children[0].children.map((x) => x.className)),
      [["cf2-opt rec", "cf2-rec-chip", "cf2-tag", "cf2-tag"], ["cf2-opt", "cf2-tag"]]);
check("short cells are tags, a missing cell left out",
      cards.map((c) => c.children[0].children.slice(-2).map(text)),
      [["내용좁게", "위험작음"], ["full", "내용넓게"]]);
const long = cf2QuestionView({ sections: [{ label: "선택지", kind: "table", value: {
  columns: [{ field: "risk", label: "위험" }],
  rows: [{ name: "a", cells: { risk: "x".repeat(30) } }] } }] });
const body = long.children[0].children[1].children[0].children[1];
check("a prose column is a labelled markdown block under the name",
      [body.className, body.children[0].textContent, body.children[1].md],
      ["cf2-opt-body", "위험", "x".repeat(30)]);
const rec = view.children[2];
check("the recommendation is a card carrying its value, with the reason that follows",
      [rec.dataset.value, rec.children[0].children.map(text), rec.children[1].textContent,
       rec.children[2].md],
      ["narrow", ["권장안", "narrow"], "권장 이유", "되돌릴 수 있다"]);
check("an empty question draws nothing", cf2QuestionView({}).children.length, 0);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("cflow2question: ok");
