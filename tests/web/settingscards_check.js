/* The rest of the Settings cards' shape (claunch-61vj3.1.1).

   Five cards on #/settings had no rule of their own in style.css: delegated
   child session, post-merge sweep batching, key switch, web debug and sim
   defaults. Their controls therefore rendered with the browser's own face —
   a label and its input ran together on one line, an input took the width of
   its value rather than of the card, and nothing lined up down the column.
   The Model tiers card got the same treatment one round earlier
   (modeltierscard_check.js); this file covers the five that did not.

   What is checked is the shape app.js and style.css have to agree on:

     - each card carries its own class on the section, so a rule written for
       one card cannot style another (webdebug wore sweep-batch-settings);
     - every field is a label that draws its name above its control (delegate,
       sim defaults) or a row whose label, control and Reset sit on one line
       with the explanation under it (sweep batching, key switch, web debug);
     - every Reset is a secondary button, and the only primary (green) button
       in the group is sim defaults' Save — the other four save on change, so
       a green button there would mark nothing;
     - style.css defines the rules that layout depends on, including the
       narrow block where the grids collapse to one column. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                          "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

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

function node(tag) {
  return {
    tagName: String(tag).toUpperCase(), className: "", textContent: "",
    children: [], attrs: {}, handlers: {}, disabled: false, value: "",
    checked: false, type: "", title: "", placeholder: "", name: "", id: "",
    min: "", max: "", step: "",
    append(...kids) { for (const k of kids) this.children.push(k); },
    appendChild(k) { this.children.push(k); return k; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
  };
}

const ctx = vm.createContext({
  document: { createElement: (t) => node(t) },
  Option: function Option(text, value) {
    const o = node("option"); o.textContent = text; o.value = value; return o;
  },
  delegateSetting: {
    profile: "codex", model: "small",
    defaults: { profile: "codex", model: "small" },
    profiles: ["codex", "ds4"], presets: ["default", "small"],
    loaded: true, error: "", busy: false,
  },
  sweepBatchSetting: {
    size: 5, settle: 300, defaults: { size: 5, settle: 300 },
    loaded: true, error: "", busy: false,
  },
  keySwitchSetting: {
    retries: 1, defaults: { retries: 1 }, loaded: true, error: "", busy: false,
  },
  webdebugSetting: {
    values: { max_windows: 1, no_sandbox: false, insecure_tls: false },
    effective: { max_windows: 1, no_sandbox: false, insecure_tls: false },
    defaults: { max_windows: 1, no_sandbox: false, insecure_tls: false },
    env: {}, loaded: true, error: "", busy: false,
  },
  simDefaults: {
    profiles: ["ds4"], judges: ["ds4"], models: { ds4: ["default", "small"] },
    defaults: { interpreter_profile: "ds4" },
  },
  simDefaultsDraft: null, simDefaultsError: "", simDefaultsNotice: "",
  simDefaultsBusy: false,
});
vm.runInContext(
  ["el", "delegateSelect", "delegateSettingCard", "sweepBatchField",
   "sweepBatchSettingCard", "keySwitchSettingCard", "webdebugEnvNote",
   "webdebugSwitchRow", "webdebugSettingCard", "simDefaultsCard"]
    .map(slice).join("\n") +
  "\nthis.cards = {delegate: delegateSettingCard()," +
  " sweep: sweepBatchSettingCard(), keyswitch: keySwitchSettingCard()," +
  " webdebug: webdebugSettingCard(), sim: simDefaultsCard()};",
  ctx);
const cards = ctx.cards;

function buttons(n, out = []) {
  if (n.tagName === "BUTTON") out.push(n);
  for (const c of n.children) buttons(c, out);
  return out;
}
const byClass = (nodes, cls) => nodes.filter((n) => n.className === cls);
function walk(n, out = []) {
  out.push(n);
  for (const c of n.children) walk(c, out);
  return out;
}

/* ---- every card says which card it is ---------------------------------- */
assert.deepEqual(
  Object.fromEntries(Object.entries(cards).map(([k, c]) => [k, c.className])),
  {
    delegate: "ws-add delegate-settings",
    sweep: "ws-add sweep-batch-settings",
    keyswitch: "ws-add key-switch-settings",
    // It drew sweep-batch-settings until claunch-61vj3.1.1: the two cards
    // shared a class, so neither could be styled on its own.
    webdebug: "ws-add webdebug-settings",
    sim: "ws-add sim-defaults-settings",
  });
for (const [name, card] of Object.entries(cards)) {
  const h3 = card.children.filter((c) => c.tagName === "H3");
  assert.equal(h3.length, 1, `${name} has one heading`);
}

/* ---- delegate: two labelled fields ------------------------------------- */
const delegateFields = byClass(cards.delegate.children, "delegate-field");
assert.equal(delegateFields.length, 2, "profile and model preset");
const names = delegateFields.map((f) => f.textContent.trim());
assert.deepEqual(names, ["Profile", "Model preset"],
  "each field draws its own name beside the select");
for (const field of delegateFields) {
  const selects = field.children.filter((c) => c.tagName === "SELECT");
  assert.equal(selects.length, 1, `${field.textContent.trim()} holds one select`);
}

/* ---- the value rows: label, control, Reset on one line, note under ----- */
function rowsOf(card, cls) {
  return walk(card).filter((n) => n.className === cls);
}
const sweepRows = rowsOf(cards.sweep, "sweep-batch-field");
assert.equal(sweepRows.length, 2, "batch size and settle time are separate rows");
const keyRow = rowsOf(cards.keyswitch, "sweep-batch-row");
assert.equal(keyRow.length, 1, "the key switch card has one value");
const debugRows = rowsOf(cards.webdebug, "sweep-batch-field");
assert.equal(debugRows.length, 3, "windows at once, sandbox, insecure TLS");

for (const [where, row] of [
  ...sweepRows.map((r) => ["sweep", r]),
  ["key switch", keyRow[0]],
  ...debugRows.map((r) => ["web debug", r]),
]) {
  const label = byClass(row.children, "sweep-batch-label");
  assert.equal(label.length, 1, `${where}: the row's copy is one label`);
  const controls = label[0].children.filter(
    (c) => c.tagName === "INPUT");
  assert.equal(controls.length, 1, `${where}: one control per row`);
  const reset = buttons(label[0]).filter((b) => b.textContent === "Reset");
  assert.equal(reset.length, 1, `${where}: the row can reset to the default`);
  assert.ok(/\bclear\b/.test(reset[0].className),
    `${where}: Reset is the secondary button, not the card's action`);
  assert.ok(!/\bapprove\b/.test(reset[0].className), `${where}: Reset is never primary`);
  // The explanation belongs to the field, so it hangs under the row rather
  // than becoming another child of the label.
  const notes = row.children.filter((c) => c.className === "wf-note");
  assert.ok(notes.length >= 1, `${where}: the row explains what the value does`);
}
// A number row sizes its input as a number; a switch row keeps the checkbox.
assert.equal(sweepRows[0].children[0].children[0].type, "number");
assert.equal(debugRows[1].children[0].children[0].type, "checkbox");
assert.equal(debugRows[1].children[0].children[0].attrs.value, undefined);

/* ---- one primary button in the group, and it is sim defaults' Save ----- */
const primaries = Object.entries(cards).flatMap(([name, card]) =>
  buttons(card).filter((b) => /\bapprove\b/.test(b.className)).map((b) => [name, b.textContent]));
assert.deepEqual(primaries, [["sim", "Save simulation models"]],
  "the four cards that save on change have no green button; sim defaults' Save is the group's only one");

/* ---- sim defaults keeps its two pickers in a labelled grid ------------- */
const simForm = walk(cards.sim).find((n) => n.className === "llm-settings-form");
assert.ok(simForm, "sim defaults still uses the shared settings form");
assert.equal(byClass(simForm.children, "wf-note").length >= 0, true);
assert.equal(buttons(simForm).length, 1, "the form has the one Save");

/* ---- style.css styles that shape --------------------------------------- */
// A rule's selector may run over several lines (one per comma-separated
// group), so the lookup stops at the brace, not at the end of the line.
function rule(selector) {
  const at = css.indexOf(`\n${selector}`);
  assert.ok(at >= 0, `style.css has a rule for ${selector}`);
  const open = css.indexOf("{", at);
  const end = css.indexOf("}", open);
  return css.slice(open, end);
}
// Delegate's two short fields share a row; anything narrower is one column.
assert.match(rule(".delegate-settings"), /grid-template-columns:/);
assert.match(rule(".delegate-field"), /flex-direction: column/,
  "the label sits above its select rather than beside it");
assert.match(rule(".delegate-field select"), /background: #0d1117/,
  "the select takes the card's surface, not the browser's");
// The value rows stack their line of controls over their explanation.
assert.match(rule(".sweep-batch-field, .sweep-batch-row"), /display: flex/);
assert.match(rule(".sweep-batch-label"), /display: flex/);
// Wide enough for the widest value these rows hold (a settle time of 86400):
// at 6ch the last digit was clipped by the box.
assert.match(rule('.sweep-batch-label input[type="number"]'), /width: 7ch/);
assert.match(rule(".sweep-batch-label .wf-btn"), /margin: 0 0 0 auto/,
  "Reset sits at the right edge so the values read down the card");
assert.match(
  rule(".sweep-batch-settings > .sweep-batch-field:first-of-type,"),
  /border-top: none/,
  "the first field follows the prose without a divider");
assert.match(rule(".sim-defaults-settings .llm-settings-form .wf-btn"),
  /grid-column: 1 \/ -1/, "Save spans the form instead of sitting in a cell");
assert.ok(css.includes("@media (max-width: 720px)"), "the cards have a narrow layout");
// The whole media block, brace-balanced (its rules have their own braces).
const narrowAt = css.indexOf("@media (max-width: 720px)", css.indexOf(".delegate-settings {"));
let depth = 0, narrowEnd = narrowAt;
for (let i = css.indexOf("{", narrowAt); i < css.length; i++) {
  if (css[i] === "{") depth++;
  else if (css[i] === "}" && !--depth) { narrowEnd = i + 1; break; }
}
const narrow = css.slice(narrowAt, narrowEnd);
assert.match(narrow, /\.delegate-settings \{ grid-template-columns: minmax\(0, 1fr\); \}/,
  "the delegate card is one column where two would squeeze the selects");
assert.match(narrow, /\.sweep-batch-label \.wf-btn \{ margin-left: 0; \}/,
  "on a narrow card Reset follows the value instead of the right edge");
// All five cards fold together: the shared form would otherwise keep two
// columns down to 600px while the card above it already went to one.
assert.match(narrow, /\.sim-defaults-settings \.llm-settings-form \{ grid-template-columns: minmax\(0, 1fr\); \}/);

console.log("settings cards layout passed");
