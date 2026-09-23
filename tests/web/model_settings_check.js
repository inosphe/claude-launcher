/* Exercise the Settings card's draft, validation and save/reset requests. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
const block = source.slice(source.indexOf('let modelChoicesHarness ='), source.indexOf('let llmSettings ='));
class Element {
  constructor(tag, cls, text) { this.tag = tag; this.text = text; this.children = []; this.events = {}; }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, fn) { this.events[name] = fn; }
}
const requests = [];
let fail = false;
let refreshes = 0;
const context = vm.createContext({
  el: (tag, cls, text) => new Element(tag, cls, text),
  Option: class { constructor(text, value) { this.text = text; this.value = value; } },
  harnessDetails: {
    codex: { models: ["old"], model_aliases: { old: "gpt-old" } },
    claude: { models: ["opus"], model_aliases: {} },
  },
  renderWorkspaces() {}, syncForkAvailability() {}, syncSpawnMode() {},
  refreshProfiles: async () => { refreshes++; },
  api: async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return { ok: !fail, json: async () => fail ? { error: "Save failed" } : {
      harness: { models: ["new"], model_aliases: { new: "gpt-new" } },
    } };
  },
});
vm.runInContext(block, context);
const run = (code) => vm.runInContext(code, context);
function find(element, id) {
  if (element.id === id) return element;
  for (const child of element.children || []) { const hit = find(child, id); if (hit) return hit; }
}
(async () => {
  let card = run("modelChoicesCard()");
  let input = find(card, "model-choices-text");
  assert.equal(input.value, "old = gpt-old");
  input.value = "new = gpt-new";
  input.events.input();
  assert.equal(find(run("modelChoicesCard()"), "model-choices-text").value, input.value);
  assert.throws(() => run('parseModelChoices("a = b\\na = c")'), /Duplicate/);
  assert.throws(() => run('parseModelChoices("missing-id")'), /one choice/);
  assert.equal(JSON.stringify(run('parseModelChoices(" ")')), "{}");
  await run('saveModelChoices("codex")');
  assert.deepEqual(requests[0], { url: "/api/harnesses/codex/models", body: { models: { new: "gpt-new" } } });
  assert.equal(refreshes, 1);
  assert.equal(run('Object.hasOwn(modelChoicesDrafts, "codex")'), false);
  await run('saveModelChoices("codex", true)');
  assert.deepEqual(requests[1].body, { models: null });
  fail = true;
  run('modelChoicesDrafts.codex = "retry = gpt-retry"');
  await run('saveModelChoices("codex")');
  assert.match(run("modelChoicesError"), /Save failed/);
  assert.equal(run("modelChoicesDrafts.codex"), "retry = gpt-retry");
  console.log("model settings: draft preservation, validation, save/reset and error recovery passed");
})().catch((err) => { console.error(err); process.exitCode = 1; });
