/* The Settings page's "Briefing model" card: which backend writes the
   session briefings.

   The setting used to exist only as the llm: block of ~/.claunch.yaml, so
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
    profile: "ds4", model: "deepseek-flash", max_tokens: 4096,
    endpoint: "", api_key: null,
  });
  root = card();
  assert.equal(field(root, "llm-endpoint").value,
               "https://ds.example/v1/chat/completions");

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

  console.log("briefmodel ok: profile list, direct save, a profile carries "
    + "endpoint and model with it, save clears the block's pair, typed "
    + "endpoint survives an abandoned pick, unknown profile kept, error");
})().catch((err) => { console.error(err); process.exit(1); });
