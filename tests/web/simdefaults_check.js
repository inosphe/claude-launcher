/* The Settings page's "Simulation models" card (app.js simDefaultsCard),
   run against the real functions: it offers only the pi-capable profiles
   and the models of the chosen profile, a judge without a profile offers the
   interpreter's models, picking another profile drops the model it had
   (a model id belongs to the profile that offers it), and Save sends the
   four keys PUT /api/sims/defaults checks (claunch-n4pyw). */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("node:assert/strict");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
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

function node(tag, cls, text) {
  return {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], handlers: {}, value: "", id: "", disabled: false, type: "",
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
  };
}
const find = (n, pred) => pred(n) ? n : n.children.map((c) => find(c, pred)).find(Boolean);
const options = (sel) => sel.children.map((o) => o.value);

let renders = 0;
let sent = null;
const ctx = {
  el: node,
  document: { createElement: (tag) => node(tag) },
  Option: function (text, value) { return { tag: "option", text, value, children: [] }; },
  renderWorkspaces: () => { renders++; },
  api: async (url, opts) => {
    sent = { url, method: opts.method, body: JSON.parse(opts.body) };
    return { ok: true, json: async () => ({ defaults: sent.body }) };
  },
  JSON,
};
vm.createContext(ctx);
vm.runInContext(`
  let simDefaults = null, simDefaultsDraft = null, simDefaultsError = "", simDefaultsNotice = "", simDefaultsBusy = false;
  async ${slice("saveSimDefaults")}
  ${slice("simDefaultsCard")}
  this.card = simDefaultsCard;
  this.save = saveSimDefaults;
  this.load = (d) => { simDefaults = d; simDefaultsDraft = null; };
  this.notice = () => simDefaultsNotice;
`, ctx);

// Before the listing arrives the card says so.
assert.ok(find(ctx.card(), (n) => n.text === "reading…"));

ctx.load({
  profiles: ["work", "judge"], judges: ["judge"],
  models: { work: ["w-big", "w-small"], judge: ["j-1"] },
  defaults: { interpreter_profile: "work", interpreter_model: "w-big", judge_profile: "", judge_model: "" },
});
let card = ctx.card();
const sel = (id) => find(card, (n) => n.id === id);
assert.deepEqual(options(sel("sim-default-interpreter-profile")), ["", "work", "judge"]);
assert.deepEqual(options(sel("sim-default-interpreter-model")), ["", "w-big", "w-small"]);
assert.equal(sel("sim-default-interpreter-model").value, "w-big");
// Only profiles that can judge; with none chosen the judge runs on the interpreter's models.
assert.deepEqual(options(sel("sim-default-judge-profile")), ["", "judge"]);
assert.deepEqual(options(sel("sim-default-judge-model")), ["", "w-big", "w-small"]);

// Same model on both sides: the card warns.
sel("sim-default-judge-model").value = "w-big";
sel("sim-default-judge-model").handlers.change[0]();
card = ctx.card();
assert.ok(find(card, (n) => /not independent/.test(n.text) && n.tag === "p"));

// Another interpreter profile drops both models that belonged to "work".
sel("sim-default-interpreter-profile").value = "judge";
sel("sim-default-interpreter-profile").handlers.change[0]();
card = ctx.card();
assert.equal(sel("sim-default-interpreter-model").value, "");
assert.equal(sel("sim-default-judge-model").value, "");
assert.deepEqual(options(sel("sim-default-interpreter-model")), ["", "j-1"]);

// A saved value this machine no longer offers is shown, not silently dropped.
ctx.load({ profiles: ["work"], judges: [], models: { work: ["w-big"] },
           defaults: { interpreter_profile: "gone", interpreter_model: "", judge_profile: "", judge_model: "" } });
card = ctx.card();
assert.deepEqual(options(sel("sim-default-interpreter-profile")), ["", "work", "gone"]);

// Save sends the four keys to the defaults route.
ctx.load({ profiles: ["work"], judges: [], models: { work: ["w-big"] },
           defaults: { interpreter_profile: "work", interpreter_model: "w-big", judge_profile: "", judge_model: "" } });
card = ctx.card();
const form = find(card, (n) => n.tag === "form");
form.handlers.submit[0]({ preventDefault() {} });
setImmediate(() => {
  assert.equal(sent.url, "/api/sims/defaults");
  assert.equal(sent.method, "PUT");
  assert.deepEqual(sent.body, { interpreter_profile: "work", interpreter_model: "w-big", judge_profile: "", judge_model: "" });
  assert.match(ctx.notice(), /Saved/);
  console.log("simdefaults_check ok");
});
