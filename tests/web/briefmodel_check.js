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
     Two saved backends, the second one chosen. The picker must switch the
     fields to that preset's backend, and the save must send the whole list
     plus the pointer — this is the whole feature: choosing a preset is how
     the operator changes the model the briefing uses. */
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
  const presetPicker = field(root, "llm-preset");
  assert.deepEqual(presetPicker.children.map((o) => o.value), ["", "big", "cheap"]);
  assert.equal(presetPicker.value, "cheap");
  // the fields show the CHOSEN preset, not the block's own (empty) values
  assert.equal(field(root, "llm-endpoint").value,
               "https://cheap.example/v1/chat/completions");
  assert.equal(field(root, "llm-model").value, "glm-small");
  assert.equal(field(root, "llm-api-key").placeholder, "stored · blank keeps it");
  assert(find(root, (n) => n.text.includes("backend preset cheap")));

  /* Switching to the other preset carries it across, and the save sends both
     rows with the new pointer. The second preset's stored key is not sent
     back (the box is blank), and is not nulled either — one field saved must
     not wipe another preset's credential. */
  presetPicker.value = "big";
  presetPicker.handlers.change();
  root = card();
  assert.equal(field(root, "llm-profile").value, "ds4");
  assert.equal(field(root, "llm-endpoint").readOnly, true);
  await submit(root);
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.preset, "big");
  // "big" names a profile and no model, so it keeps following that profile's
  // default rather than being pinned to whatever the profile leads with today.
  assert.deepEqual(sent.presets, [
    { id: "big", profile: "ds4", model: "", endpoint: "",
      api_key: null },
    { id: "cheap", profile: "", model: "glm-small",
      endpoint: "https://cheap.example/v1/chat/completions" },
  ]);

  /* Deleting the chosen preset sends the list without it and the pointer
     cleared, so the daemon is not left pointing at a name that is gone. */
  response = { ...response, preset: "big" };
  await context.refreshLlmSettings();
  root = card();
  find(root, (n) => n.text === "Delete preset").handlers.click();
  await new Promise((r) => setTimeout(r, 0));
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.preset, "");
  assert.deepEqual(sent.presets.map((p) => p.id), ["cheap"]);

  /* "Add preset" seeds a row from the fields and saves it straight away, so
     the new row is selectable: the daemon refuses a pointer at a preset the
     same request does not also save. */
  requests.length = 0;
  response = {
    ...response, preset: "", presets: [],
    profile: "", model: "some-model",
    endpoint: "https://typed.example/v1/chat/completions", api_key_set: true,
  };
  await context.refreshLlmSettings();
  root = card();
  find(root, (n) => n.text === "Add preset").handlers.click();
  await new Promise((r) => setTimeout(r, 0));
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.preset, "preset-1");
  assert.deepEqual(sent.presets, [{
    id: "preset-1", profile: "", model: "some-model",
    endpoint: "https://typed.example/v1/chat/completions", api_key: null,
  }]);

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
  const nameInput = field(root, "llm-preset-name");
  assert.equal(nameInput.value, "cheap");
  assert.equal(nameInput.disabled, false);
  nameInput.value = "cheap-a";
  nameInput.handlers.input();
  await submit(root);
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.preset, "cheap-a");
  assert.deepEqual(sent.presets, [{
    id: "cheap-a", previous_id: "cheap", profile: "", model: "glm-small",
    endpoint: "https://cheap.example/v1/chat/completions",
  }]);
  // an unrenamed row sends no previous_id at all
  vm.runInContext("llmDraft = null;", context);
  await context.refreshLlmSettings();
  root = card();
  await submit(root);
  sent = JSON.parse(requests.at(-1)[1].body);
  assert.equal(sent.presets[0].previous_id, undefined);

  console.log("briefmodel ok: profile list, direct save, a profile carries "
    + "endpoint and model with it, save clears the block's pair, typed "
    + "endpoint survives an abandoned pick, unknown profile kept, error, "
    + "preset switch carries endpoint and model, save sends the list and "
    + "pointer, delete clears the pointer, add saves the new row, rename "
    + "sends the name the key is filed under, provider shown when resolved");
})().catch((err) => { console.error(err); process.exit(1); });
