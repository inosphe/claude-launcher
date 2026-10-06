/* cflow v2 brief asks in the run view (docs/cflow-v2.md 2.10), run against
   the real cf2QuestionView from app.js with a stub DOM.

   The question comes from GET /api/cflow2/run asks[].question
   (cflow2/question.to_json). Pinned here: sections render in their order,
   text as heading + markdown, the options table one row per option with the
   recommended row marked, and the recommendation in bold -- with no code per
   question type. */
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
  constructor(tag) { this.tag = tag; this.children = []; this.className = ""; this.textContent = ""; }
  appendChild(c) { this.children.push(c); return c; }
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
check("the wrapper", [view.tag, view.className], ["div", "cf2-question"]);
check("sections keep their order",
      view.children.map((c) => c.tag),
      ["h5", "div", "h5", "table", "p", "h5", "div"]);
check("a text section is its label then markdown",
      [view.children[0].textContent, view.children[1].md], ["문제", "범위를 정한다"]);
const table = view.children[3];
check("the table head is name + the columns",
      table.children[0].children.map((c) => c.textContent), ["name", "내용", "위험"]);
check("the recommended row is marked",
      table.children.slice(1).map((r) => r.className), ["recommended", ""]);
check("rows carry the cells, a missing cell empty",
      table.children.slice(1).map((r) => r.children.map((c) => c.textContent)),
      [["narrow (권장)", "좁게", "작음"], ["full", "넓게", ""]]);
check("the recommendation is bold",
      [view.children[4].className, view.children[4].children[0].tag,
       view.children[4].children[0].textContent],
      ["cf2-q-rec", "strong", "권장안: narrow"]);
check("an empty question draws nothing", cf2QuestionView({}).children.length, 0);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("cflow2question: ok");
