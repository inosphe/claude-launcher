/* VS Code's share of the dashboard (claunch-9omyu.1): the button every
   surface uses, the Workspaces row's controls and the session panel's block.

   What the checks hold:
     - the button is disabled, with the reason as its title, when the daemon
       cannot serve one (Windows) or the feature is turned off -- a click
       there would only fetch an error;
     - a workspace row offers its root and ONE picker over its worktrees,
       whatever their number, and marks the ones with a running server;
     - the session block opens by session name (the daemon resolves the
       directory), and says so when the session has none.

   Slice the real functions out of app.js and drive them against a stub DOM. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
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

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), title: "", disabled: false,
    listeners: {}, options: [], value: "", attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    appendChild(c) { if (this.tag === "select") this.options.push(c); this.kids.push(c); return c; },
    addEventListener(ev, fn) { this.listeners[ev] = fn; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((x) => x.classes.has(cls)); },
  };
  return n;
}
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function Option(text, value) { const o = node("option"); o.text = text; o.value = value; return o; }

const opened = [];
let refreshed = 0;
const ctx = {};
new Function("exports", "document", "el", "Option", "opened", "bump",
  "let vscodeState = null; let vscodeBusy = '';\n"
  + "function vscodeOpen(body) { opened.push(body); }\n"
  + "function refreshVscode() { bump(); }\n"
  + slice("vscodeButton") + slice("wsVscodeControls") + slice("sessVscodeButton")
  + "Object.assign(exports, { vscodeButton, wsVscodeControls, sessVscodeButton,"
  + " setState(s) { vscodeState = s; }, setBusy(b) { vscodeBusy = b; } });"
)(ctx, document, el, Option, opened, () => { refreshed++; });

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const ok = { supported: true, reason: "", settings: { enabled: true }, targets: [
  { key: "proj", kind: "workspace", workspace: "proj", worktree: "" },
  { key: "proj~a", kind: "worktree", workspace: "proj", worktree: "a",
    server: { state: "running" } },
  { key: "proj~b", kind: "worktree", workspace: "proj", worktree: "b", server: null },
  { key: "other~c", kind: "worktree", workspace: "other", worktree: "c" },
] };

/* ---- the button --------------------------------------------------------- */
ctx.setState(ok);
let b = ctx.vscodeButton("VS Code", { workspace: "proj" });
check("enabled when supported and on", b.disabled, false);
b.listeners.click();
check("a click opens that body", opened.pop(), { workspace: "proj" });
ctx.setState({ ...ok, supported: false, reason: "not on Windows" });
b = ctx.vscodeButton("VS Code", { workspace: "proj" });
check("unsupported: disabled", b.disabled, true);
check("...and says why", b.title, "not on Windows");
ctx.setState({ ...ok, settings: { enabled: false } });
check("turned off: disabled", ctx.vscodeButton("x", {}).disabled, true);
ctx.setState(ok);
ctx.setBusy(JSON.stringify({ workspace: "proj" }));
check("busy on the same body: disabled", ctx.vscodeButton("x", { workspace: "proj" }).disabled, true);
check("...another body is not", ctx.vscodeButton("x", { workspace: "q" }).disabled, false);
ctx.setBusy("");

/* ---- the Workspaces row ------------------------------------------------- */
ctx.setState(null);
check("nothing before the state arrives", ctx.wsVscodeControls({ name: "proj" }).kids.length, 0);
ctx.setState(ok);
const box = ctx.wsVscodeControls({ name: "proj" });
const pick = box.kids.find((k) => k.tag === "select");
check("root button, then one picker", box.kids.map((k) => k.tag), ["button", "select"]);
check("the picker lists this workspace's worktrees only, running marked",
      pick.options.map((o) => o.text), ["…or a worktree (2)", "a ●", "b"]);
pick.value = "b";
pick.listeners.change();
check("picking opens that worktree", opened.pop(), { workspace: "proj", worktree: "b" });
check("...and resets the picker", pick.value, "");
const lone = ctx.wsVscodeControls({ name: "solo" });
check("no worktrees, no picker", lone.kids.map((k) => k.tag), ["button"]);

/* ---- the session head's button (claunch-rvly4) ------------------------- */
let sb = ctx.sessVscodeButton({ name: "s7", cwd: "/w/proj" });
check("the head button is one button labelled VS Code", [sb.tag, sb.textContent],
      ["button", "VS Code"]);
check("...live for a session with a directory", sb.disabled, false);
sb.listeners.click();
check("opens by session name", opened.pop(), { session: "s7" });
sb = ctx.sessVscodeButton({ name: "s8", cwd: "" });
check("no directory: greyed", sb.disabled, true);
check("...and says why", sb.title, "this session has no directory");
ctx.setState(null);
refreshed = 0;
ctx.sessVscodeButton({ name: "s9", cwd: "/x" });
check("asks for the state when it has none", refreshed, 1);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("vscode_check ok");
