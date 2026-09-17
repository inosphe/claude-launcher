const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
function el(tag, cls, text = "") {
  return { tag, cls, text, children: [], handlers: {},
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(name, fn) { this.handlers[name] = fn; } };
}
function all(node) { return [node, ...node.children.flatMap(all)]; }
let response = { relays: [{id: "home", url: "wss://home", name: "pc", verify_tls: true, connected: true}] };
let ok = true;
const requests = [];
const context = vm.createContext({ el, document: { createTextNode: text => el("text", null, text) },
  wsOpen: true, renderWorkspaces() {}, renderRelayBadge() {},
  api: async (url, options) => { requests.push([url, options]); return { ok, json: async () => response }; },
});
vm.runInContext(src.slice(src.indexOf("let relaySettingsRows ="), src.indexOf("async function refreshRagStatus()")), context);
const card = () => context.relaySettingsCard();
const find = (root, predicate) => all(root).find(predicate);
(async () => {
  await context.refreshRelaySettings();
  let root = card();
  assert(find(root, n => n.text === "Connected"));
  find(root, n => n.text === "Edit").handlers.click();
  root = card();
  assert.equal(find(root, n => n.id === "relay-setting-id").value, "home");
  assert.equal(find(root, n => n.id === "relay-setting-token").type, "password");
  assert.equal(find(root, n => n.id === "relay-setting-token").value, "");
  const secret = find(root, n => n.id === "relay-setting-token");
  secret.value = "entered-secret";
  secret.handlers.input();
  await find(root, n => n.tag === "form").handlers.submit({ preventDefault() {} });
  assert.equal(JSON.parse(requests.at(-1)[1].body).token, "entered-secret");
  assert.equal(requests.at(-1)[0], "/api/relays");
  assert.equal(find(card(), n => n.id === "relay-setting-token").value, "");
  assert(find(card(), n => n.text.startsWith("Saved.")));
  ok = false;
  response = { error: "Token required" };
  await find(card(), n => n.tag === "form").handlers.submit({ preventDefault() {} });
  assert(find(card(), n => n.text.includes("Token required")));
  assert.equal(find(card(), n => n.text === "Save & connect").disabled, false);
  console.log("relaysettings ok: status, edit, masked token, submit, clearing, error recovery");
})().catch(err => { console.error(err); process.exit(1); });
