/* Prompt-preset insertion belongs to the session footer: a click must keep
   text on either side of the current selection, replace that selection, and
   leave the field ready for the operator's normal send action. */
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
const ctx = {};
new Function("exports", "$", `${slice("insertPromptPreset")}; exports.insertPromptPreset = insertPromptPreset;`)(
  ctx, (id) => id === "term-input-field" ? field : null
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

console.log(failures ? `\n${failures} failure(s)` : "all prompt-preset checks passed");
process.exit(failures ? 1 : 0);
