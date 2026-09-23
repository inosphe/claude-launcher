// A settings poll must preserve an unsaved FAQ edit and its eventual PUT.
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(path.join(__dirname,
  "../../src/claude_launcher/web/static/app.js"), "utf8");
function el(tag, cls, text = "") {
  return { tag, cls, text, children: [], handlers: {},
    append(...nodes) { this.children.push(...nodes); },
    appendChild(node) { this.children.push(node); return node; },
    addEventListener(event, handler) { this.handlers[event] = handler; } };
}
const all = node => [node, ...node.children.flatMap(all)];
const find = (node, predicate) => all(node).find(predicate);
let request;
const context = vm.createContext({ el, document: { createElement: el },
  faqCache: [{ id: "merge", question: "Merged?", answer: "", enabled: true }],
  faqDraft: { question: "", answer: "" }, faqEdit: null, faqEditDraft: "",
  faqError: "", renderWorkspaces() {},
  api: async (url, options) => {
    request = { url, body: JSON.parse(options.body) };
    return { ok: true, json: async () => ({ faq: [request.body] }) };
  },
});
vm.runInContext(src.slice(src.indexOf("function faqCard() {"),
  src.indexOf("let prompterSettings =")), context);
(async () => {
  let card = context.faqCard();
  find(card, n => n.text === "Edit").handlers.click();
  card = context.faqCard();
  let edit = find(card, n => n.id === "briefing-faq-edit-question");
  edit.value = "수정 내용이 master에 머지됐나요?";
  edit.handlers.input();
  // An unrelated settings request completes and rebuilds the whole page.
  card = context.faqCard();
  edit = find(card, n => n.id === "briefing-faq-edit-question");
  assert.equal(edit.value, "수정 내용이 master에 머지됐나요?");
  await find(card, n => n.text === "Save").handlers.click();
  assert.equal(request.url, "/api/briefing/faq/merge");
  assert.equal(request.body.question, edit.value);
  assert.equal(context.faqCache[0].question, edit.value);
  card = context.faqCard();
  const add = find(card, n => n.id === "briefing-faq-question");
  add.value = "Tests passed?";
  add.handlers.input();
  assert.equal(find(context.faqCard(), n => n.id === add.id).value, add.value);
  console.log("FAQ drafts survive settings rebuilds and save the edited question");
})().catch(error => { console.error(error); process.exitCode = 1; });
