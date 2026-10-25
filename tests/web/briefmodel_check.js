/* The Settings page's "Briefing model" card: which backend writes the
   session briefings.

   The setting used to exist only as the llm: block of ~/.claude-launcher/.claunch.yaml, so
   what this harness holds is the part a form can get wrong. Choosing a
   profile moves three fields at once: the endpoint box shows that profile's
   URL read-only, the model switches to that profile's default, and the save
   deletes the block's own endpoint and key ("" and null) because the profile
   supplies both. Before that, picking a profile changed only which
   credential the call used while the two boxes went on showing the previous
   backend's values. The typed endpoint survives in the draft, so going back
   to "none" returns it. The password field arrives blank on every render and
   blank means "keep", which is why removing a key has its own button. */
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(
  path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");

function node(tag, cls, text = "") {
  return {
    tag, cls, text, children: [], handlers: {}, attrs: {},
    value: "", type: "", disabled: false, readOnly: false,
    title: "", placeholder: "",
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { for (const kid of kids) this.appendChild(kid); },
    addEventListener(name, fn) { this.handlers[name] = fn; },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
}
function el(tag, cls, text = "") { return node(tag, cls, text); }
function all(root) { return [root, ...root.children.flatMap(all)]; }
const find = (root, predicate) => all(root).find(predicate);

const requests = [];
let ok = true;
let response = {
  profile: "", model: "", endpoint: "", max_tokens: 4096,
  api_key_set: false, configured: false, error: "",
  resolved: { endpoint: "", model: "", has_key: false },
  profiles: [
    { name: "ds4", endpoint: "https://ds.example/v1/chat/completions",
      models: ["deepseek-flash", "glm-small"], has_key: true },
    { name: "local", endpoint: "http://127.0.0.1:8080/v1/chat/completions",
      models: ["mlx-model"], has_key: false },
  ],
};
const context = vm.createContext({
  el,
  document: { createElement: (tag) => node(tag, null, "") },
  wsOpen: true,
  renderWorkspaces() {},
  applyBriefingTop() {},
  briefingLLM: false,
  confirm: () => true,
  api: async (url, options) => {
    requests.push([url, options]);
    return { ok, status: ok ? 200 : 400, json: async () => response };
  },
});
vm.runInContext(
  src.slice(src.indexOf("let llmSettings = null;"),
            src.indexOf("function faqCard()")),
  context);

const card = () => context.llmSettingsCard();
const field = (root, id) => find(root, (n) => n.id === id);
const submit = (root) =>
  find(root, (n) => n.tag === "form").handlers.submit({ preventDefault() {} });

(async () => {
  await context.refreshLlmSettings();
  let root = card();
  assert.equal(requests.at(-1)[0], "/api/briefing/llm");

  /* Every profile that can serve is offered, with what it resolves to, and
     the direct fields are editable while none is chosen. */
  const select = field(root, "llm-profile");
  assert.deepEqual(select.children.map((o) => o.value), ["", "ds4", "local"]);
  assert(select.children[2].textContent.includes("(no key)"));
  assert.equal(field(root, "llm-endpoint").readOnly, false);
  assert.equal(field(root, "llm-api-key").type, "password");
  assert.equal(field(root, "llm-api-key").value, "");

  /* Direct form: the endpoint and key are part of the save. */
  const endpoint = field(root, "llm-endpoint");
  endpoint.value = "https://typed.example/v1/chat/completions";
  endpoint.handlers.input();
  const key = field(root, "llm-api-key");
  key.value = "sk-typed";
  key.handlers.input();
  const model = field(root, "llm-model");
  model.value = "m";
  model.handlers.input();
  response = { ...response, configured: true, api_key_set: true,
               model: "m", endpoint: "https://typed.example/v1/chat/completions",
               resolved: { endpoint: "https://typed.example/v1/chat/completions",
                           model: "m", has_key: true } };
  await submit(root);
  let sent = JSON.parse(requests.at(-1)[1].body);
  assert.deepEqual(sent, {
    presets: [], preset: "",
    profile: "", model: "m", max_tokens: 4096,
    endpoint: "https://typed.example/v1/chat/completions", api_key: "sk-typed",
  });
  assert.equal(requests.at(-1)[1].method, "PUT");
  // the rail's toggles are told at once rather than waiting for the poll
  assert.equal(context.briefingLLM, true);
  root = card();
  assert(find(root, (n) => n.text.startsWith("Saved.")));
  assert.equal(field(root, "llm-api-key").placeholder, "stored · blank keeps it");

  /* Choosing a profile moves the endpoint and the model with it: the box
     shows that profile's URL read-only, and the model becomes its default
     instead of the id typed for the backend before it. */
  field(root, "llm-profile").value = "ds4";
  field(root, "llm-profile").handlers.change();
  root = card();
  assert.equal(field(root, "llm-endpoint").readOnly, true);
  assert.equal(field(root, "llm-endpoint").value,
               "https://ds.example/v1/chat/completions");
  assert.equal(field(root, "llm-model").value, "deepseek-flash");
  assert.equal(field(root, "llm-api-key").disabled, true);
  assert.equal(
    find(root, (n) => n.tag === "datalist").children.map((o) => o.value).join(","),
    "deepseek-flash,glm-small");
  // the card does not offer to remove a key the profile supplies
  assert.equal(find(root, (n) => n.text === "Remove stored key"), undefined);

  /* Switching again carries both across a second time. */
  field(root, "llm-profile").value = "local";
  field(root, "llm-profile").handlers.change();
  root = card();
  assert.equal(field(root, "llm-endpoint").value,
               "http://127.0.0.1:8080/v1/chat/completions");
  assert.equal(field(root, "llm-model").value, "mlx-model");

  /* Back to none before saving: the typed endpoint is still in the draft and
     the box is writable again, so a profile tried out and abandoned costs
     nothing. */
  field(root, "llm-profile").value = "";
  field(root, "llm-profile").handlers.change();
  root = card();
  assert.equal(field(root, "llm-endpoint").readOnly, false);
  assert.equal(field(root, "llm-endpoint").value,
               "https://typed.example/v1/chat/completions");

  /* The save deletes the block's own pair rather than leaving a credential
     for a backend this no longer calls. */
  field(root, "llm-profile").value = "ds4";
  field(root, "llm-profile").handlers.change();
  root = card();
  response = { ...response, profile: "ds4", model: "deepseek-flash",
               endpoint: "", api_key_set: false, configured: true,
               resolved: { endpoint: "https://ds.example/v1/chat/completions",
                           model: "deepseek-flash", has_key: true } };
  await submit(root);
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.deepEqual(sent, {
    presets: [], preset: "",
    profile: "ds4", model: "deepseek-flash", max_tokens: 4096,
    endpoint: "", api_key: null,
  });
  root = card();
  assert.equal(field(root, "llm-endpoint").value,
               "https://ds.example/v1/chat/completions");

  /* The provider is on the card when one is resolved: it decides which
     request headers the call carries (session_header, anthropic_auth_header),
     so an upstream that rejects the call names something the card showed. */
  response = { ...response, profile: "ds4", resolved: {
    endpoint: "https://ds.example/v1/chat/completions",
    model: "deepseek-flash", has_key: true, provider: "ds4-provider" } };
  await submit(root);
  root = card();
  assert(find(root, (n) => n.text.includes("provider ds4-provider")));
  // the typed form belongs to no provider entry, so nothing is claimed there
  response = { ...response, resolved: {
    endpoint: "https://typed.example/v1/chat/completions",
    model: "m", has_key: true, provider: "" } };
  await submit(root);
  root = card();
  assert.equal(find(root, (n) => n.text.includes("provider ")), undefined);

  /* A profile the picker cannot offer (hand-edited, or its provider lost the
     endpoint) is still shown with the reason, instead of the form proposing
     "none" as if the operator had chosen it. A save is what hands the card
     that state: it answers with the block as written plus why it does not
     resolve, and the form takes its values from that answer again. */
  response = { ...response, profile: "gone", model: "m", configured: false,
               error: "profile 'gone' does not exist",
               resolved: { endpoint: "", model: "m", has_key: false } };
  await submit(root);
  root = card();
  assert.equal(field(root, "llm-profile").value, "gone");
  assert(find(root, (n) => n.text === "profile 'gone' does not exist"));
  assert(find(root, (n) => n.text.includes("NO key")));
  // nothing to show read-only, so the box stays empty and says why
  assert.equal(field(root, "llm-endpoint").value, "");
  assert.equal(field(root, "llm-endpoint").readOnly, true);
  assert.equal(field(root, "llm-endpoint").placeholder,
               "this profile resolves to no endpoint");

  /* A refused save says why and leaves the fields as typed. */
  ok = false;
  response = { error: "max_tokens must be between 1 and 1000000" };
  await submit(root);
  root = card();
  assert(find(root, (n) => n.text.includes("max_tokens must be between")));
  assert.equal(find(root, (n) => n.text === "Save").disabled, false);

  /* ── Presets ─────────────────────────────────────────────────────────────
     Two saved backends, the second one chosen. The presets are radio buttons
     — one row per saved backend plus "custom", which is the block's own
     backend and what a fresh install uses. Selecting is the whole switch, and
     a saved preset is read-only until Edit opens it: the fields must not let
     a stray keystroke rewrite the backend on the way past. */
  ok = true;
  response = {
    preset: "cheap", profile: "", model: "", endpoint: "", max_tokens: 4096,
    api_key_set: false, configured: true, error: "",
    resolved: { endpoint: "https://cheap.example/v1/chat/completions",
                model: "glm-small", has_key: true },
    profiles: [
      { name: "ds4", endpoint: "https://ds.example/v1/chat/completions",
        models: ["deepseek-flash", "glm-small"], has_key: true },
    ],
    presets: [
      { id: "big", profile: "ds4", model: "", endpoint: "", api_key_set: false,
        resolved: { endpoint: "https://ds.example/v1/chat/completions",
                    model: "deepseek-flash", has_key: true }, error: "" },
      { id: "cheap", profile: "", endpoint: "https://cheap.example/v1/chat/completions",
        model: "glm-small", api_key_set: true,
        resolved: { endpoint: "https://cheap.example/v1/chat/completions",
                    model: "glm-small", has_key: true }, error: "" },
    ],
  };
  // A fresh page. ``llmDraft`` is a ``let`` inside the context, so it is not
  // a property of ``context`` — it has to be cleared from inside.
  vm.runInContext("llmDraft = null;", context);
  await context.refreshLlmSettings();
  root = card();
  const radios = (r) => all(r).filter((n) => n.type === "radio");
  const checked = (r) => radios(r).find((n) => n.checked);
  assert.deepEqual(radios(root).map((n) => n.value), ["", "big", "cheap"]);
  assert.equal(checked(root).value, "cheap");
  // "custom" is a named row, not an empty slot: the card says which backend
  // the fields below stand for
  assert(find(root, (n) => n.text.includes("custom")));
  // the fields show the CHOSEN preset, not the block's own (empty) values
  assert.equal(field(root, "llm-endpoint").value,
               "https://cheap.example/v1/chat/completions");
  assert.equal(field(root, "llm-model").value, "glm-small");
  assert.equal(field(root, "llm-api-key").placeholder, "stored · blank keeps it");
  assert(find(root, (n) => n.text.includes("backend preset cheap")));

  /* Read-only by default, name included: every field refuses typing, and the
     card offers Edit rather than Save. */
  assert.equal(field(root, "llm-model").readOnly, true);
  assert.equal(field(root, "llm-endpoint").readOnly, true);
  assert.equal(field(root, "llm-profile").disabled, true);
  assert.equal(field(root, "llm-api-key").disabled, true);
  assert.equal(field(root, "llm-preset-name").disabled, true);
  assert(find(root, (n) => n.text === "Edit preset"));
  assert.equal(find(root, (n) => n.text === "Save preset"), undefined);
  // a keystroke that reaches a locked field changes nothing
  const lockedModel = field(root, "llm-model");
  lockedModel.value = "typed-anyway";
  lockedModel.handlers.input();
  assert.equal(card().children.length > 0, true);
  assert.equal(vm.runInContext("llmDraft.model", context), "glm-small");

  /* Edit opens that one preset — the name and the four value fields
     together — and the card swaps Edit for Save/Delete plus Cancel. Nothing
     is written by opening it. */
  requests.length = 0;
  find(root, (n) => n.text === "Edit preset").handlers.click();
  root = card();
  assert.equal(requests.length, 0);
  assert.equal(field(root, "llm-model").readOnly, false);
  assert.equal(field(root, "llm-endpoint").readOnly, false);
  assert.equal(field(root, "llm-profile").disabled, false);
  assert.equal(field(root, "llm-api-key").disabled, false);
  assert.equal(field(root, "llm-preset-name").disabled, false);
  assert(find(root, (n) => n.text === "Save preset"));
  assert(find(root, (n) => n.text === "Cancel"));

  /* Save sends the whole list with the edited values and the pointer. The
     other preset's stored key is neither sent back (its box is blank) nor
     nulled — one field saved must not wipe another preset's credential. */
  field(root, "llm-model").value = "glm-large";
  field(root, "llm-model").handlers.input();
  root = card();
  assert(find(root, (n) => n.text.includes("저장하지 않은 변경")));
  await find(root, (n) => n.text === "Save preset").handlers.click();
  await new Promise((r) => setTimeout(r, 0));
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.preset, "cheap");
  assert.equal(sent.presets.find((p) => p.id === "cheap").model, "glm-large");
  assert.equal(sent.presets.find((p) => p.id === "big").api_key, null);
  assert.equal(sent.presets.find((p) => p.id === "cheap").api_key, undefined);

  /* An unsaved edit is thrown away only with the operator's consent. A
     declined confirm leaves the card on the preset it was editing — a radio
     that moves anyway would show a backend the draft never went to. */
  vm.runInContext("llmDraft = null;", context);
  await context.refreshLlmSettings();
  root = card();
  find(root, (n) => n.text === "Edit preset").handlers.click();
  root = card();
  field(root, "llm-model").value = "never-saved";
  field(root, "llm-model").handlers.input();
  root = card();
  let asked = 0;
  context.confirm = () => { asked += 1; return false; };
  radios(root).find((n) => n.value === "big").handlers.change();
  root = card();
  assert.equal(asked, 1);
  assert.equal(checked(root).value, "cheap");
  // the declined switch did not carry the half-typed value anywhere either
  assert.equal(vm.runInContext("llmDraft.presets.find(p => p.key === 'big').model",
                               context), "");
  // ... and consenting does move, back to a read-only view of the other one
  context.confirm = () => { asked += 1; return true; };
  radios(root).find((n) => n.value === "big").handlers.change();
  root = card();
  assert.equal(asked, 2);
  assert.equal(checked(root).value, "big");
  assert.equal(field(root, "llm-profile").value, "ds4");
  assert.equal(field(root, "llm-model").readOnly, true);
  assert.equal(field(root, "llm-endpoint").readOnly, true);
  assert.equal(field(root, "llm-endpoint").value,
               "https://ds.example/v1/chat/completions");

  /* Cancel puts back what is stored, reading the row rather than the form. */
  find(root, (n) => n.text === "Edit preset").handlers.click();
  root = card();
  field(root, "llm-model").value = "throw-me-away";
  field(root, "llm-model").handlers.input();
  root = card();
  requests.length = 0;
  find(root, (n) => n.text === "Cancel").handlers.click();
  root = card();
  assert.equal(requests.length, 0);
  assert.equal(field(root, "llm-model").value, "");
  assert.equal(field(root, "llm-model").readOnly, true);

  /* "custom" is the block's own backend: always editable, and it has no name
     box because there is no row behind it to name. */
  context.confirm = () => true;
  radios(root).find((n) => n.value === "").handlers.change();
  root = card();
  assert.equal(checked(root).value, "");
  assert.equal(field(root, "llm-model").readOnly, false);
  assert.equal(field(root, "llm-endpoint").readOnly, false);
  assert.equal(field(root, "llm-profile").disabled, false);
  assert.equal(field(root, "llm-preset-name").disabled, true);
  assert.equal(find(root, (n) => n.text === "Edit preset"), undefined);

  /* Switching to custom carries the save's pointer to "": the briefing then
     follows the block's own fields. */
  field(root, "llm-endpoint").value = "https://typed.example/v1/chat/completions";
  field(root, "llm-endpoint").handlers.input();
  field(root, "llm-model").value = "typed-model";
  field(root, "llm-model").handlers.input();

  /* The unsaved-changes line must name a button that is on the card. ``custom``
     has no Save preset — its fields are the block's own — so the line has to
     point at the form's Save, or it sends the operator looking for a control
     that is not rendered. */
  root = card();
  assert.equal(find(root, (n) => n.text === "Save preset"), undefined);
  const dirtyLine = find(root, (n) => n.cls === "wf-warning"
                                  && n.text.includes("저장하지 않은 변경"));
  assert(dirtyLine, "custom shows the unsaved-changes line");
  assert(dirtyLine.text.includes("아래 Save"));
  assert(!dirtyLine.text.includes("Save preset"));
  requests.length = 0;
  await submit(card());
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.preset, "");
  assert.equal(sent.endpoint, "https://typed.example/v1/chat/completions");
  assert.equal(sent.model, "typed-model");
  assert.deepEqual(sent.presets.map((p) => p.id), ["big", "cheap"]);

  /* ── Adding a preset ─────────────────────────────────────────────────────
     The point of this round: a preset no longer has to be named before it
     exists. "Add preset" starts a nameless row from the fields and opens it,
     writes nothing, and the name is decided at Save — or the row is dropped
     by Cancel or by selecting something else. */
  vm.runInContext("llmDraft = null;", context);
  response = {
    ...response, preset: "", profile: "", model: "", endpoint: "",
    endpoint: "", api_key_set: false,
  };
  await context.refreshLlmSettings();
  root = card();
  field(root, "llm-endpoint").value = "https://fresh.example/v1/chat/completions";
  field(root, "llm-endpoint").handlers.input();
  field(root, "llm-model").value = "fresh-model";
  field(root, "llm-model").handlers.input();
  root = card();
  requests.length = 0;

  /* Add preset carries the typed values into the new row, so its question
     must not borrow the discard wording — an operator who reads "discard"
     and approves would get the opposite of what they agreed to. It asks
     once, and declining leaves everything exactly as it was. */
  let prompts = [];
  context.confirm = (msg) => { prompts.push(msg); return false; };
  find(root, (n) => n.text === "Add preset").handlers.click();
  root = card();
  assert.equal(prompts.length, 1);
  assert(prompts[0].includes("옮길까요"), prompts[0]);
  assert(!prompts[0].includes("버리고"), prompts[0]);
  assert.equal(radios(root).length, 3, "declining starts no row");
  assert.equal(field(root, "llm-model").value, "fresh-model");
  assert.equal(vm.runInContext("llmDraft.dirty", context), true);

  // approving the same question starts the row with those values
  context.confirm = () => true;
  find(root, (n) => n.text === "Add preset").handlers.click();
  root = card();
  assert.equal(requests.length, 0, "adding writes nothing");
  assert.equal(radios(root).length, 4);
  assert(find(root, (n) => n.text.includes("(name needed)")));
  // the draft key is only a handle inside the card; the name is what is saved
  assert.equal(checked(root).value, "preset-3");
  // it opens straight into edit mode, so the name box is ready
  assert.equal(field(root, "llm-preset-name").disabled, false);
  assert.equal(field(root, "llm-preset-name").value, "");
  assert.equal(field(root, "llm-model").value, "fresh-model");

  /* Saving before the row has a name is refused by the card with the reason
     on it, rather than by the daemon's 400 after a round-trip. */
  await submit(root);
  root = card();
  assert(find(root, (n) => n.text.includes("Name this preset before saving")));
  assert.equal(requests.length, 0);

  /* Naming it later and saving is the whole flow: the row goes into the file
     under that name, and it is the selected one. */
  field(root, "llm-preset-name").value = "fresh";
  field(root, "llm-preset-name").handlers.input();
  root = card();
  await find(root, (n) => n.text === "Save preset").handlers.click();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(requests.length, 1);
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.preset, "fresh");
  assert.deepEqual(sent.presets.find((p) => p.id === "fresh"), {
    id: "fresh", profile: "", model: "fresh-model",
    endpoint: "https://fresh.example/v1/chat/completions", api_key: null,
  });

  /* An added row that is abandoned goes away with no request: it was never in
     the file, so leaving it would hold a nameless row every later save has to
     refuse. */
  vm.runInContext("llmDraft = null;", context);
  await context.refreshLlmSettings();
  root = card();
  find(root, (n) => n.text === "Add preset").handlers.click();
  root = card();
  requests.length = 0;
  find(root, (n) => n.text === "Cancel").handlers.click();
  root = card();
  assert.equal(requests.length, 0);
  assert.equal(radios(root).length, 3);
  assert.equal(checked(root).value, "");

  /* A second "Add preset" while one is still unnamed opens that one instead
     of starting another — the card never holds two nameless rows. */
  find(root, (n) => n.text === "Add preset").handlers.click();
  root = card();
  requests.length = 0;
  find(root, (n) => n.text === "Add preset").handlers.click();
  root = card();
  assert.equal(radios(root).length, 4);
  assert.equal(requests.length, 0);

  /* Deleting a saved preset sends the list without it and the pointer
     cleared, so the daemon is not left pointing at a name that is gone. */
  vm.runInContext("llmDraft = null;", context);
  response = {
    ...response, preset: "big",
    presets: [
      { id: "big", profile: "ds4", model: "", endpoint: "", api_key_set: false,
        resolved: { endpoint: "https://ds.example/v1/chat/completions",
                    model: "deepseek-flash", has_key: true }, error: "" },
      { id: "cheap", profile: "", endpoint: "https://cheap.example/v1/chat/completions",
        model: "glm-small", api_key_set: true,
        resolved: { endpoint: "https://cheap.example/v1/chat/completions",
                    model: "glm-small", has_key: true }, error: "" },
    ],
  };
  await context.refreshLlmSettings();
  root = card();
  requests.length = 0;
  find(root, (n) => n.text === "Delete preset").handlers.click();
  await new Promise((r) => setTimeout(r, 0));
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.preset, "");
  assert.deepEqual(sent.presets.map((p) => p.id), ["cheap"]);

  /* Renaming a preset carries the name it is filed under. The Name box edits
     the id in place, and the daemon holds the secret under the old name — a
     save that sends only the new one would drop the key and turn the
     briefing off. ``previous_id`` is what lets it keep the credential. */
  requests.length = 0;
  response = {
    preset: "cheap", profile: "", model: "", endpoint: "", max_tokens: 4096,
    api_key_set: false, configured: true, error: "",
    resolved: { endpoint: "https://cheap.example/v1/chat/completions",
                model: "glm-small", has_key: true },
    profiles: [],
    presets: [
      { id: "cheap", profile: "", endpoint: "https://cheap.example/v1/chat/completions",
        model: "glm-small", api_key_set: true,
        resolved: { endpoint: "https://cheap.example/v1/chat/completions",
                    model: "glm-small", has_key: true }, error: "" },
    ],
  };
  vm.runInContext("llmDraft = null;", context);
  await context.refreshLlmSettings();
  root = card();
  // the name is locked with the rest until Edit
  assert.equal(field(root, "llm-preset-name").disabled, true);
  find(root, (n) => n.text === "Edit preset").handlers.click();
  root = card();
  const nameInput = field(root, "llm-preset-name");
  assert.equal(nameInput.value, "cheap");
  assert.equal(nameInput.disabled, false);
  nameInput.value = "cheap-a";
  nameInput.handlers.input();
  await find(card(), (n) => n.text === "Save preset").handlers.click();
  await new Promise((r) => setTimeout(r, 0));
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.preset, "cheap-a");
  assert.deepEqual(sent.presets, [{
    id: "cheap-a", previous_id: "cheap", profile: "", model: "glm-small",
    endpoint: "https://cheap.example/v1/chat/completions",
  }]);

  /* An unrenamed row sends no previous_id at all — an unchanging save must
     not look like a rename. */
  vm.runInContext("llmDraft = null;", context);
  await context.refreshLlmSettings();
  root = card();
  await submit(root);
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.presets[0].previous_id, undefined);

  /* A refused save must not have moved the row: the values typed into the
     form are adopted only once the daemon has accepted them, so Cancel after
     a failure still restores what is stored rather than the rejected edit. */
  find(card(), (n) => n.text === "Edit preset").handlers.click();
  root = card();
  field(root, "llm-model").value = "rejected-edit";
  field(root, "llm-model").handlers.input();
  ok = false;
  response = { error: "endpoint must be an HTTP(S) URL" };
  await submit(card());
  root = card();
  assert(find(root, (n) => n.text.includes("HTTP(S) URL")));
  ok = true;
  response = {
    preset: "cheap", profile: "", model: "", endpoint: "", max_tokens: 4096,
    api_key_set: false, configured: true, error: "",
    resolved: { endpoint: "https://cheap.example/v1/chat/completions",
                model: "glm-small", has_key: true },
    profiles: [],
    presets: [
      { id: "cheap", profile: "", endpoint: "https://cheap.example/v1/chat/completions",
        model: "glm-small", api_key_set: true,
        resolved: { endpoint: "https://cheap.example/v1/chat/completions",
                    model: "glm-small", has_key: true }, error: "" },
    ],
  };
  root = card();
  find(root, (n) => n.text === "Cancel").handlers.click();
  root = card();
  assert.equal(field(root, "llm-model").value, "glm-small");
  assert.equal(field(root, "llm-model").readOnly, true);

  /* ── Test connection ─────────────────────────────────────────────────────
     The button sends the fields as they stand, not the saved block: the
     backend an operator is checking is the one they just typed, and after
     picking a preset that backend is not in the file yet. It is a plain
     button, so it must not save. */
  requests.length = 0;
  response = {
    preset: "cheap", profile: "", model: "", endpoint: "", max_tokens: 4096,
    api_key_set: false, configured: true, error: "",
    resolved: { endpoint: "https://cheap.example/v1/chat/completions",
                model: "glm-small", has_key: true },
    profiles: [],
    presets: [
      { id: "cheap", profile: "", endpoint: "https://cheap.example/v1/chat/completions",
        model: "glm-small", api_key_set: true,
        resolved: { endpoint: "https://cheap.example/v1/chat/completions",
                    model: "glm-small", has_key: true }, error: "" },
    ],
  };
  vm.runInContext("llmDraft = null; llmTestResult = null;", context);
  await context.refreshLlmSettings();
  root = card();
  // the preset is read-only until Edit, and a typed value has to be the one
  // the probe sends — so open it first
  find(root, (n) => n.text === "Edit preset").handlers.click();
  root = card();
  const typed = field(root, "llm-endpoint");
  typed.value = "https://typed.example/v1/chat/completions";
  typed.handlers.input();
  field(root, "llm-model").value = "typed-model";
  field(root, "llm-model").handlers.input();
  response = {
    ok: true, status: 200, detail: "answered 200 in 0.31s — the backend replied",
    endpoint: "https://typed.example/v1/chat/completions", model: "typed-model",
    provider: "", seconds: 0.31,
  };
  const testBtn = find(root, (n) => n.text === "Test connection");
  assert(testBtn, "the card offers Test connection");
  assert.equal(testBtn.type, "button");
  await testBtn.handlers.click();
  const [testUrl, testOpts] = requests.at(-1);
  assert.equal(testUrl, "/api/briefing/llm/test");
  assert.equal(testOpts.method, "POST");
  const tested = JSON.parse(testOpts.body);
  // the typed fields, not the saved preset's — and the preset's own name,
  // which is what tells the daemon whose stored key a blank api_key means
  assert.deepEqual(tested, {
    preset: "cheap", profile: "", model: "typed-model",
    endpoint: "https://typed.example/v1/chat/completions", api_key: "",
  });
  // a test writes nothing: no PUT followed it
  assert.equal(requests.filter(([u, o]) => o && o.method === "PUT").length, 0);
  root = card();
  const okLine = find(root, (n) => n.text.startsWith("Connected —"));
  assert(okLine);
  assert(okLine.text.includes("the backend replied"));
  // the line names what was tested, so a later edit cannot read as covered
  assert(okLine.text.includes("https://typed.example/v1/chat/completions"));
  assert(okLine.text.includes("typed-model"));

  /* A refusal is shown the same way, with the upstream's own words. */
  response = {
    ok: false, status: 400, detail: "answered 400: {\"error\":\"MissingSessionID\"}",
    endpoint: "https://typed.example/v1/chat/completions", model: "typed-model",
    provider: "", seconds: 0.12,
  };
  await find(card(), (n) => n.text === "Test connection").handlers.click();
  root = card();
  const badLine = find(root, (n) => n.text.startsWith("Failed —"));
  assert(badLine);
  assert(badLine.text.includes("MissingSessionID"));
  assert.equal(badLine.cls, "wf-warning");

  /* The result outlives a re-render (the card is rebuilt on every poll), and
     a second test replaces it rather than stacking up. */
  root = card();
  assert.equal(all(root).filter((n) => n.text.startsWith("Connected —")
                                  || n.text.startsWith("Failed —")).length, 1);

  /* With a profile chosen the typed endpoint/key are not sent beside it —
     the same pairing rule the save follows. */
  response = {
    preset: "", profile: "", model: "", endpoint: "", max_tokens: 4096,
    api_key_set: true, configured: true, error: "",
    resolved: { endpoint: "https://ds.example/v1/chat/completions",
                model: "deepseek-flash", has_key: true },
    profiles: [
      { name: "ds4", endpoint: "https://ds.example/v1/chat/completions",
        models: ["deepseek-flash"], has_key: true },
    ],
    presets: [],
  };
  vm.runInContext("llmDraft = null; llmTestResult = null;", context);
  await context.refreshLlmSettings();
  root = card();
  field(root, "llm-profile").value = "ds4";
  field(root, "llm-profile").handlers.change();
  requests.length = 0;
  await find(card(), (n) => n.text === "Test connection").handlers.click();
  const withProfile = JSON.parse(requests.at(-1)[1].body);
  assert.deepEqual(withProfile,
                   { preset: "", profile: "ds4", model: "deepseek-flash" });

  console.log("briefmodel ok: profile list, direct save, a profile carries "
    + "endpoint and model with it, save clears the block's pair, typed "
    + "endpoint survives an abandoned pick, unknown profile kept, error, "
    + "presets as radios with a named custom row, preset read-only until "
    + "Edit (name included) and locked fields ignore keystrokes, Edit writes "
    + "nothing, Save preset sends the listed edit and pointer, a declined "
    + "discard keeps the card where it was, Cancel restores the stored "
    + "values, custom is always editable and saves the empty pointer, Add "
    + "preset starts a nameless row and writes nothing, saving unnamed is "
    + "refused by the card, naming later saves it, an abandoned new row "
    + "disappears, a saved preset deletes with the pointer cleared, rename "
    + "sends the name the key is filed under, provider shown when resolved, "
    + "Test connection sends the unsaved fields and reports the result");
})().catch((err) => { console.error(err); process.exit(1); });
