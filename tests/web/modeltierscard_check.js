/* The Model tiers card's layout (claunch-61vj3.1).

   The card is drawn from app.js and styled by style.css, and the two halves
   fail in different ways: markup with no rule renders as the browser's own
   inline forms (the card wrapped mid-row and no two selects lined up), and a
   rule whose markup changed underneath it styles nothing. What is checked
   here is the shape the two agree on, since neither is visible in a plain
   unit test of the other:

     - a tier is a panel with its name and entry count in a head, one row per
       entry under a single column header, and its own add-entry control;
     - the field names are drawn once, in that header, and each select carries
       the same name as its accessible name (the visible copy is no longer
       adjacent to the control);
     - the card keeps one primary button ("Add tier"): adding an entry and
       deleting one are secondary, so the green button marks the card's own
       action instead of repeating once per tier;
     - style.css defines a rule for every element of that shape. */
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

/* The least DOM modelTierSection/modelTiersCard touch: createElement, Option,
   append, attributes and listeners. */
function node(tag) {
  return {
    tagName: String(tag).toUpperCase(), className: "", textContent: "",
    children: [], attrs: {}, handlers: {}, disabled: false, value: "",
    checked: false, type: "", title: "", placeholder: "", name: "",
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
  modelTiers: {
    block: {
      version: 1,
      tiers: [
        { id: "misc", entries: [
          { profile: "ds4", harness: "claude", model: "small" },
          { profile: "codex" },
        ] },
        { id: "worker", entries: [{ profile: "codex" }] },
      ],
      active: { misc: 1, worker: 0 },
    },
    profiles: ["codex", "ds4"],
    harnesses: { ds4: ["claude"], codex: ["codex"] },
    models: { ds4: ["default", "small"], codex: ["default", "luna"] },
    models_by_harness: {
      ds4: { "": ["default", "small"], claude: ["default", "small"] },
      codex: { "": ["default", "luna"], codex: ["default", "luna"] },
    },
    presets: ["default", "small"],
    max_tiers: 5, loaded: true, error: "", busy: false,
  },
  modelTiersTierDraft: "",
  modelTiersAddError: "",
  MODEL_TIER_ID_RE: /^[a-z0-9][a-z0-9-]{0,31}$/,
});
vm.runInContext(
  ["el", "modelTierEntrySelect", "modelTiersEntryModelChoices",
   "modelTiersEntryHarnessChoices", "modelTierSection", "modelTiersCard"]
    .map(slice).join("\n") + "\nthis.card = modelTiersCard();",
  ctx);
const card = ctx.card;

const byClass = (nodes, cls) => nodes.filter((n) => n.className === cls);

/* ---- the card head ------------------------------------------------------ */
const head = card.children[0];
assert.equal(head.className, "model-tier-card-head");
assert.equal(head.children[0].textContent, "Model tiers");
assert.equal(head.children[1].textContent, "2 of 5 tiers",
  "the head says how much of the tier budget is used");
// The help is prose above the panels, not inside them.
const help = byClass(card.children, "wf-note model-tier-help");
assert.equal(help.length, 1);
assert.ok(!/\(profile, model, harness\) entries/.test(help[0].textContent),
  "the ordering sentence moved out of the help text, which now names the rule");

/* ---- a tier is a panel with a header and a column header ---------------- */
const tiers = byClass(card.children, "model-tier");
assert.equal(tiers.length, 2);
const misc = tiers[0];
const tierHead = misc.children[0];
assert.equal(tierHead.className, "model-tier-head");
assert.equal(tierHead.children[0].tagName, "H4");
assert.equal(tierHead.children[0].textContent, "misc");
assert.equal(tierHead.children[1].textContent, "2 entries");
assert.equal(tiers[1].children[0].children[1].textContent, "1 entry",
  "one entry is not written as 1 entries");

const columnRows = byClass(misc.children, "model-tier-row model-tier-row-head");
assert.equal(columnRows.length, 1, "one column header per tier");
assert.deepEqual(columnRows[0].children.map((c) => c.textContent),
  ["Active", "Profile", "Harness", "Model", ""]);
assert.deepEqual(columnRows[0].children.map((c) => c.className),
  Array(5).fill("model-tier-col"));

/* ---- one row per entry, the same cells in both ------------------------- */
const rows = byClass(misc.children, "model-tier-row");
assert.equal(rows.length, 2, "the column header is not one of the entry rows");
for (const row of rows) {
  assert.equal(row.children.length, 5, "radio, three selects, delete");
  assert.equal(row.children[0].tagName, "INPUT");
  assert.equal(row.children[0].type, "radio");
  assert.ok(row.children[0].attrs["aria-label"],
    "the radio says which entry it makes active");
  const labels = ["Profile", "Harness", "Model"];
  labels.forEach((label, i) => {
    const sel = row.children[i + 1];
    assert.equal(sel.tagName, "SELECT");
    // The visible copy of the name is in the header row, far from the control:
    // without an accessible name these three selects are "combobox" each.
    assert.equal(sel.attrs["aria-label"], label,
      `${label}'s select carries its column name`);
  });
  const del = row.children[4];
  assert.equal(del.tagName, "BUTTON");
  assert.ok(del.className.includes("wf-btn"),
    "Delete keeps the shared button face rather than the browser's");
}

// The active pointer is drawn where the data says it is.
assert.equal(rows[0].children[0].checked, false);
assert.equal(rows[1].children[0].checked, true);

/* ---- the card has one primary button ----------------------------------- */
function buttons(n, out = []) {
  if (n.tagName === "BUTTON") out.push(n);
  for (const c of n.children) buttons(c, out);
  return out;
}
const all = buttons(card);
const primary = all.filter((b) => /\bapprove\b/.test(b.className));
assert.deepEqual(primary.map((b) => b.textContent), ["Add tier"],
  "Add tier is the card's only green button; Add entry is secondary");
assert.ok(all.some((b) => b.textContent === "Add entry"),
  "the per-tier add action is still there, as a secondary button");
for (const del of all.filter((b) => b.textContent === "Delete")) {
  assert.ok(!/\bapprove\b/.test(del.className), "Delete is never a primary button");
}

/* ---- the add controls name what their select holds --------------------- */
for (const tier of tiers) {
  const addRow = byClass(tier.children, "model-tier-add")[0];
  assert.ok(addRow, "each tier can add an entry");
  const field = addRow.children[0];
  assert.equal(field.tagName, "LABEL");
  assert.equal(field.children[0].textContent, "New entry",
    "the add-entry select is labelled -- it used to be a bare select");
  assert.equal(field.children[1].tagName, "SELECT");
}
const tierForm = byClass(card.children, "model-tier-add-tier")[0];
assert.ok(tierForm, "the card can add a tier");
assert.equal(tierForm.children[0].tagName, "LABEL");
assert.equal(tierForm.children[0].children[0].textContent, "New tier");
assert.equal(tierForm.children[0].children[1].tagName, "INPUT");
assert.equal(tierForm.children[1].className, "wf-btn approve");

/* ---- style.css styles that shape --------------------------------------- */
function rule(selector) {
  const at = css.indexOf(`\n${selector} {`);
  assert.ok(at >= 0, `style.css has a rule for ${selector}`);
  const end = css.indexOf("}", at);
  return css.slice(at, end);
}
// The entries stand in columns because the row is a grid: the reader compares
// two entries by looking down one column.
assert.match(rule(".model-tier-row"), /grid-template-columns:/);
assert.match(rule(".model-tier-row-head"), /border-bottom:/,
  "the column header is divided from the rows it names");
assert.match(rule(".model-tier"), /border:/, "a tier is bounded from its neighbours");
assert.match(rule(".model-tier-add"), /border-top:/);
assert.match(rule(".model-tier-add-tier"), /border-top:/);
// Controls the card draws have the app's dark surface, not the browser's.
assert.match(rule(".model-tier-row select, .model-tier-add-field select, .model-tier-add-tier input"),
  /background: #0d1117/);
assert.ok(css.includes("@media (max-width: 820px)"),
  "the card has a narrow layout");
const narrow = css.slice(css.indexOf("@media (max-width: 820px)", css.indexOf(".model-tier {")),
  css.indexOf("/* Settings > Providers", css.indexOf(".model-tier {")));
assert.match(narrow, /\.model-tier-row-head \{ display: none; \}/,
  "the column header goes away where the row is no longer a grid");

console.log("model tiers card layout passed");
