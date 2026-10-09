/* Prompt-preset delivery belongs to the session footer: a click must keep
   text on either side of the current selection, replace that selection, and
   send the resulting message through the normal footer path. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
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

const field = {
  value: "before after", disabled: false, selectionStart: 7, selectionEnd: 7,
  focus() { this.focused = true; },
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
};
const send = {};
const note = {};
let sent = [];
const ctx = {};
new Function(
  "exports", "$", "sendKeyLine",
  `${slice("insertPromptPreset")}; ${slice("sendPromptPreset")}; ` +
  "exports.insertPromptPreset = insertPromptPreset; exports.sendPromptPreset = sendPromptPreset;"
)(
  ctx,
  (id) => id === "term-input-field" ? field : (id === "term-input-send" ? send : note),
  (input, button, message) => {
    sent.push({ value: input.value, button, message });
    return true;
  }
);

let failures = 0;
const check = (name, value) => {
  if (value) return;
  failures++;
  console.log("FAIL " + name);
};

ctx.insertPromptPreset("prompt ");
check("inserts at the caret", field.value === "before prompt after");
check("focuses the footer field", field.focused === true);
check("moves the caret after inserted text", field.selectionStart === 14 && field.selectionEnd === 14);

field.value = "before selected after";
field.selectionStart = 7;
field.selectionEnd = 15;
ctx.insertPromptPreset("message");
check("replaces the selected text", field.value === "before message after");

field.disabled = true;
ctx.insertPromptPreset("ignored");
check("does not change a disabled footer", field.value === "before message after");

field.disabled = false;
field.value = "";
field.selectionStart = 0;
field.selectionEnd = 0;
const delivered = ctx.sendPromptPreset("send now");
check("submits after inserting the preset", delivered === true && sent.length === 1);
check("sends the inserted preset text", sent[0] && sent[0].value === "send now");
check("uses the footer send controls", sent[0] && sent[0].button === send && sent[0].message === note);

/* Which strip shows, and where a click goes. The attached local session
   (`currentName`) outlives a navigation, so the strip must follow the page:
   on the shadow page (#/r/<machine>/<name>) the strip that shows is the one
   above #shadow-input, and a click reaches the remote session's line -- not
   the local session still attached behind it. */
function fakeBox() {
  return {
    children: [], hidden: true, innerHTML: "",
    classList: { toggle(name, on) { if (name === "hidden") this.owner.hidden = !!on; } },
    appendChild(child) { this.children.push(child); },
  };
}
const boxes = {
  "term-preset-buttons": fakeBox(),
  "shadow-preset-buttons": fakeBox(),
  "term-input-field": {
    value: "", disabled: false, selectionStart: 0, selectionEnd: 0,
    focus() {}, setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
  },
  "shadow-input-field": {
    value: "", disabled: false, selectionStart: 0, selectionEnd: 0,
    focus() {}, setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
  },
};
for (const b of Object.values(boxes)) if (b.classList) b.classList.owner = b;
const page = { current: "terminal" };
const localSent = [];
const shadowSent = [];
const strips = {};
new Function(
  "exports", "$", "el", "sendKeyLine", "sendShadowLine", "promptPresetCache", "page",
  "let currentName = 'local1'; let sessionEnded = false;" +
  "Object.defineProperty(exports, 'page', { set(v) { currentPage = v; } });" +
  "let currentPage = page.current;" +
  `${slice("renderTermPresetButtons")}; ${slice("fillPresetStrip")}; ` +
  `${slice("sessionLineOnPage")}; ` +
  `${slice("insertPromptPreset")}; ${slice("sendPromptPreset")}; ${slice("sendShadowPreset")}; ` +
  "exports.render = renderTermPresetButtons;"
)(
  strips,
  (id) => boxes[id] || null,
  (tag, cls, text) => {
    const node = { tag, cls, text, handlers: {} };
    node.addEventListener = (ev, fn) => { node.handlers[ev] = fn; };
    return node;
  },
  (input) => { localSent.push(input.value); input.value = ""; return true; },
  () => { shadowSent.push(boxes["shadow-input-field"].value); boxes["shadow-input-field"].value = ""; return true; },
  [{ name: "go", text: "continue", enabled: true }, { name: "off", text: "x", enabled: false }],
  page
);

strips.render();
check("terminal page: its strip shows", boxes["term-preset-buttons"].hidden === false);
check("terminal page: disabled presets are left out", boxes["term-preset-buttons"].children.length === 1);
check("terminal page: the shadow strip stays hidden", boxes["shadow-preset-buttons"].hidden === true);

strips.page = "shadow";
strips.render();
check("shadow page: the terminal strip hides though a local session is attached",
  boxes["term-preset-buttons"].hidden === true);
check("shadow page: the strip above #shadow-input shows", boxes["shadow-preset-buttons"].hidden === false);
const shadowButton = boxes["shadow-preset-buttons"].children[0];
shadowButton && shadowButton.handlers.click();
check("shadow page: a click sends to the remote session's line",
  shadowSent.length === 1 && shadowSent[0] === "continue");
check("shadow page: a click sends nothing to the local session", localSent.length === 0);

strips.page = "home";
strips.render();
check("other pages: neither strip shows",
  boxes["term-preset-buttons"].hidden === true && boxes["shadow-preset-buttons"].hidden === true);

console.log(failures ? `\n${failures} failure(s)` : "all prompt-preset checks passed");
process.exit(failures ? 1 : 0);
