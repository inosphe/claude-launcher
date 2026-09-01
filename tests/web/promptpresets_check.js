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

console.log(failures ? `\n${failures} failure(s)` : "all prompt-preset checks passed");
process.exit(failures ? 1 : 0);
