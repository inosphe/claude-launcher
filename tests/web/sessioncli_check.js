/* A session's own shell in the split pane (claunch-jg8pt).

   What is held here:
     1. the pane is a split pane like run/sims — registered in SPLIT_PANES,
        accepted by sessLayoutFor, toggled by splitAfterPress, and the header
        has its button;
     2. the CLI tab's sharing is rolled back — no #cli-suggest strip, no
        renderCliSuggestions/cliSuggestions, and nothing still calls the
        global /api/shell routes;
     3. cli-pane.js, driven against a stub DOM and socket: mounting opens a
        socket at the session's own address, the init frame's proposals draw
        as rows, run posts to the session's accept route (no body), and an
        empty suggestions frame clears the strip.

   No browser needed: the pane talks to its socket through WebSocket, which
   a stub supplies. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const pane = fs.readFileSync(path.join(STATIC, "cli-pane.js"), "utf8");

let failures = 0;
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${name}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
  }
}

/* ---- slice the real layout code out of app.js -------------------------- */
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
const panesStart = src.indexOf("const SPLIT_PANES = {");
const panesEnd = src.indexOf("\n};", panesStart) + 3;
if (panesStart < 0 || panesEnd < panesStart) throw new Error("cannot locate SPLIT_PANES");

const sandbox = {
  DETAIL_SUBTAB_IDS: new Set(["all"]),
  clampSplitRatio: (r) => (Number.isFinite(r) ? r : 0.6),
  loadSessLayouts: () => sandbox.__layouts,
  __layouts: {},
  console,
};
vm.createContext(sandbox);
vm.runInContext(
  src.slice(panesStart, panesEnd) + "\n" + slice("sessLayoutFor") + "\n" +
  slice("splitAfterPress") + "\nglobalThis.SPLIT_PANES = SPLIT_PANES;",
  sandbox,
);

/* ---- 1. the pane is a split pane --------------------------------------- */
check("SPLIT_PANES has a cli pane with its own header button",
      sandbox.SPLIT_PANES.cli && sandbox.SPLIT_PANES.cli.btn, "term-clibtn");
check("the header carries that button",
      /id="term-clibtn"/.test(html), true);
sandbox.__layouts = {s1: {split: true, pane: "cli"}};
check("a session's remembered cli pane is honoured",
      sandbox.sessLayoutFor("s1").pane, "cli");
sandbox.__layouts = {s1: {split: true, pane: "nonsense"}};
check("a junk pane still falls back to the run",
      sandbox.sessLayoutFor("s1").pane, "run");
check("pressing the cli button opens it",
      sandbox.splitAfterPress({split: false, pane: "run"}, "cli"),
      {split: true, pane: "cli"});
check("pressing it again closes the pane",
      sandbox.splitAfterPress({split: true, pane: "cli"}, "cli"),
      {split: false, max: false});
check("mounting it is wired into openSplit",
      /kind === "cli"[^\n]*CliPane\?\.mount/.test(src), true);
check("unmounting it is wired into closeSplit",
      /splitKind === "cli"[^\n]*CliPane\?\.unmount/.test(src), true);

/* ---- 2. the CLI tab's sharing is rolled back --------------------------- */
check("the CLI tab has no proposal strip", /id="cli-suggest"/.test(html), false);
check("no CLI-tab suggestion renderer is left in app.js",
      /renderCliSuggestions|decideCliSuggestion|cliSuggestions/.test(src), false);
check("nothing still calls the global /api/shell routes",
      /\/api\/shell/.test(src) || /\/api\/shell/.test(pane), false);
check("the CLI tab's socket handler no longer draws a suggestion frame",
      /msg\.type === "suggestions"/.test(src), false);

/* ---- 3. the pane itself, against a stub DOM and socket ------------------ */
function node(tag) {
  const n = {
    tag, kids: [], attrs: {}, handlers: {}, classes: new Set(), text: "",
    title: "", type: "",
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    append(...kids) { for (const k of kids) this.kids.push(k); return this; },
    appendChild(k) { this.kids.push(k); return k; },
    replaceChildren(...kids) { this.kids = kids; return this; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    click() { (this.handlers.click || []).forEach((fn) => fn()); },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((x) => x.classes.has(cls)); },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
  };
  return n;
}

const sockets = [];
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;   // OPEN
    this.sent = [];
    sockets.push(this);
  }
  send(data) { this.sent.push(data); }
  close() { this.readyState = 3; }
  frame(obj) { this.onmessage({data: JSON.stringify(obj)}); }
}
const calls = [];
const sandbox2 = {
  console,
  document: { createElement: (t) => node(t) },
  location: { protocol: "http:", host: "pr.test" },
  WebSocket: FakeSocket,
  ResizeObserver: class { observe() {} disconnect() {} },
  Terminal: class {
    constructor(opts) { this.opts = opts; this.cols = 80; this.rows = 24; this.written = []; }
    loadAddon() {}
    open(el) { this.el = el; }
    onData(fn) { this.dataFn = fn; }
    onResize(fn) { this.resizeFn = fn; }
    write(d) { this.written.push(d); }
    resize(c, r) { this.cols = c; this.rows = r; }
    dispose() {}
  },
  FitAddon: { FitAddon: class { fit() {} } },
  api: async (path, opts) => { calls.push([path, opts]); return {ok: true}; },
  url: (p) => p,
  fontSize: 13,
  TextEncoder,
  encodeURIComponent,
  setTimeout,
  clearTimeout,
};
sandbox2.globalThis = sandbox2;
vm.createContext(sandbox2);
vm.runInContext(pane, sandbox2);
const CliPane = sandbox2.CliPane;

const host = node("div");
CliPane.mount(host, "s1");
check("mounting opens one socket", sockets.length, 1);
check("the socket is the session's own shell", sockets[0].url,
      "ws://pr.test/api/sessions/s1/shell/ws");
check("the pane claims the frame's body so the terminal has a box",
      host.classList.contains("cli-pane-host"), true);

sockets[0].frame({
  type: "init", cols: 100, rows: 30, pid: 42, exited: false, session: "s1",
  suggestions: [{id: "sh-aaa111", session: "s1", text: "make test", why: "verify"}],
});
let rows = host.find("cli-suggest-row");
check("the agent's proposal draws as a row", rows.length, 1);
check("the row shows the exact text", rows[0].find("cli-suggest-text")[0].text, "make test");
check("the row shows why", rows[0].find("cli-suggest-why")[0].text, "verify");

const run = rows[0].find("cli-suggest-run")[0];
check("the run button is the session's accept route",
      run.handlers.click.length, 1);
run.click();
// The click is async (api is awaited); let the microtask run.
setTimeout(() => {
  check("run posts to the accept route with no session body",
        calls[0] && calls[0][0],
        "/api/sessions/s1/shell/suggestions/sh-aaa111/accept");
  check("the accept call carries an empty body",
        calls[0] && calls[0][1] && calls[0][1].body, "{}");
  check("the row goes once the user decided", host.find("cli-suggest-row").length, 0);

  sockets[0].frame({type: "suggestions", items: []});
  check("an empty queue hides the strip",
        host.find("cli-pane-suggest")[0].classList.contains("hidden"), true);

  // Keystrokes go as binary frames; a control frame stays JSON.
  const term = host.find("cli-pane-term")[0];
  check("the terminal is mounted in the pane", !!term, true);
  CliPane.unmount();
  check("unmounting closes the socket", sockets[0].readyState, 3);
  check("unmounting gives the frame's body back",
        host.classList.contains("cli-pane-host"), false);

  if (failures) process.exit(1);
  console.log("sessioncli_check ok");
}, 0);
