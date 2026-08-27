/* Borrowed auth is a live validation, not merely a restart picker. Slice the
   shipped detail-pane function, drive it with daemon-shaped payloads, and pin
   the three contracts: API-key harnesses can borrow, OAuth harnesses cannot,
   and a missing lender credential is visibly invalid without exposing it. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let i = body; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (!depth) return src.slice(head, i + 1);
    }
  }
  throw new Error("unbalanced " + name);
}

function node(tag) {
  return {
    tag, kids: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    value: undefined, disabled: false, title: "",
    appendChild(child) {
      this.kids.push(child);
      if (this.tag === "select" && child.tag === "option" && this.value === undefined) {
        this.value = child.value;
      }
      return child;
    },
    append(...children) { children.forEach((c) => this.appendChild(c)); },
    addEventListener(kind, fn) { (this.handlers[kind] ||= []).push(fn); },
    fire(kind) { return Promise.all((this.handlers[kind] || []).map((fn) => fn())); },
    set innerHTML(value) { this.kids = []; this.value = undefined; },
    get innerHTML() { return ""; },
    get options() { return this.kids.filter((child) => child.tag === "option"); },
    get textContent() { return this.text; },
    set textContent(value) { this.text = String(value); },
    get className() { return [...this.classes].join(" "); },
    set className(value) {
      this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
    },
  };
}

function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const document = { createElement: (tag) => node(tag) };
function walk(n, out = []) {
  for (const child of n.kids) { out.push(child); walk(child, out); }
  return out;
}
const tags = (n, tag) => walk(n).filter((x) => x.tag === tag);
const text = (n) => walk(n).map((x) => x.text).join(" | ");
const settle = () => new Promise((resolve) => setImmediate(resolve));

const api = async (url) => ({
  ok: true, status: 200,
  json: async () => ({ options: [
    { name: "ds4", label: "ds4", selectable: true, valid: true, message: "ready" },
    { name: "codex", label: "codex — harness policy denied", selectable: false,
      valid: false, message: "harness policy denied" },
  ] }),
});
const stubs = `
let sessReborrowBox = null;
let currentName = null;
function detach() {}
async function refreshSessions() {}
function attach() {}
`;
const ctx = {};
new Function(
  "exports", "document", "el", "api",
  stubs + slice("readBorrowOptions") + slice("sessReborrow") + `
Object.assign(exports, {
  render: sessReborrow,
  drop: () => { sessReborrowBox = null; },
});`
)(ctx, document, el, api);

let failures = 0;
function check(label, condition, extra) {
  if (condition) return;
  failures++;
  console.error(`FAIL ${label}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
}

async function main() {
  const oauth = ctx.render({
    session: { name: "k", profile: "work:kimi", harness: "kimi" },
    harness: { auth: "oauth", borrowable: false, borrow_mode: "none" },
  });
  check("OAuth harness has no borrow form", tags(oauth, "select").length === 0);
  check("OAuth refusal explains namespaced auth", text(oauth).includes("own storage"), text(oauth));

  ctx.drop();
  const pi = ctx.render({
    session: { name: "p", profile: "work:pi", harness: "pi", borrow: "ds4" },
    harness: { auth: "api-key", borrowable: true, borrow_mode: "token" },
    borrowed_auth: {
      allowed: true, ready: true, valid: true, status: "ready",
      message: "'ds4's shared token will be exported as ANTHROPIC_API_KEY",
    },
  });
  await settle();
  const piSelect = tags(pi, "select")[0];
  check("API-key harness has the reborrow picker", !!piSelect);
  check("Pi picker has no Claude --null answer",
    tags(piSelect, "option").map((o) => o.value).join(",") === "own,b:ds4,b:codex",
    tags(piSelect, "option").map((o) => o.value));
  check("policy-denied lenders stay visible and disabled",
    tags(piSelect, "option").some((o) => o.value === "b:codex" && o.disabled));
  check("ready borrowed auth is visibly validated",
    text(pi).includes("✓ validation") && text(pi).includes("ANTHROPIC_API_KEY"), text(pi));

  ctx.drop();
  const missing = ctx.render({
    session: { name: "p", profile: "work:pi", harness: "pi", borrow: "ds4" },
    harness: { auth: "api-key", borrowable: true, borrow_mode: "token" },
    borrowed_auth: {
      allowed: true, ready: false, valid: false, status: "missing-token",
      message: "profile 'ds4' has no shared token for harness 'pi'",
    },
  });
  await settle();
  const warning = walk(missing).find((n) => n.classes.has("wf-warning"));
  check("missing lender credential is a validation warning",
    !!warning && warning.text.includes("⚠ validation") && warning.text.includes("no shared token"),
    warning && warning.text);

  ctx.drop();
  const claude = ctx.render({
    session: { name: "c", profile: "work:claude", harness: "claude" },
    harness: { auth: "claude", borrowable: true, borrow_mode: "provider-token" },
  });
  await settle();
  check("Claude retains the explicit no-token choice",
    tags(tags(claude, "select")[0], "option").some((o) => o.value === "null"));

  console.log("reborrow_check: " + (failures ? `${failures} failing` : "ok"));
  process.exitCode = failures ? 1 : 0;
}
main().catch((err) => { console.error(err); process.exitCode = 1; });
