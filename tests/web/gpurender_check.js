/* The terminals draw on a WebGL canvas, not in DOM rows (claunch-i9oms).

   xterm's DOM renderer rewrites a row's spans for every line of output, and
   every rewrite is a mutation each observing content script walks the
   document for. A profile of a busy session's tab had a password manager's
   MutationObserver scanning for 5.2 s of 12 s, with typing queued behind it.
   On the WebGL renderer the output mutates nothing.

   Pinned here:
     - attachGpuRenderer (sliced from the shipped app.js) loads the addon,
       disposes it when the context is lost, and leaves the terminal on the
       DOM renderer -- without throwing -- where the addon is missing or
       refuses to load;
     - the session terminal and the CLI terminal both get it, after open();
     - index.html loads the vendored addon after xterm and before app.js,
       and the vendored file is the UMD build that defines WebglAddon. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

function sliceFunction(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate function ${name}`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth += 1;
    else if (src[j] === "}") { depth -= 1; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced function ${name}`);
}

function run(globals) {
  const ctx = { ...globals };
  vm.createContext(ctx);
  vm.runInContext(`${sliceFunction("attachGpuRenderer")}\nthis.attach = attachGpuRenderer;`, ctx);
  return ctx.attach;
}

function fakeTerminal({ refuse = false } = {}) {
  return {
    loaded: [],
    loadAddon(addon) {
      if (refuse) throw new Error("WebGL2 not supported");
      this.loaded.push(addon);
    },
  };
}

class FakeWebgl {
  constructor() { this.disposed = 0; this.lossHandlers = []; }
  onContextLoss(fn) { this.lossHandlers.push(fn); }
  dispose() { this.disposed += 1; }
}

{
  const attach = run({});
  const t = fakeTerminal();
  check("no addon loaded: stays on the DOM renderer", attach(t) === null && t.loaded.length === 0);
}

{
  const attach = run({ WebglAddon: { WebglAddon: FakeWebgl } });
  const t = fakeTerminal();
  const gl = attach(t);
  check("the addon is loaded into the terminal", gl && t.loaded.length === 1 && t.loaded[0] === gl);
  check("a context-loss handler is registered", gl && gl.lossHandlers.length === 1);
  gl.lossHandlers.forEach((fn) => fn());
  check("losing the context disposes the addon", gl.disposed === 1, gl.disposed);
}

{
  const attach = run({ WebglAddon: { WebglAddon: FakeWebgl } });
  const t = fakeTerminal({ refuse: true });
  let threw = false;
  let out;
  try { out = attach(t); } catch { threw = true; }
  check("a refused load does not throw", !threw);
  check("a refused load returns null", out === null, out);
}

/* Both terminals are opened, then get the renderer. */
for (const [label, re] of [
  ["session terminal", /term\.open\(\$\("terminal"\)\);\s*\n\s*if \(typeof attachGpuRenderer === "function"\) attachGpuRenderer\(term\);/],
  ["CLI terminal", /cliTerm\.open\(\$\("cli-term"\)\);\s*\n\s*attachGpuRenderer\(cliTerm\);/],
]) {
  check(`${label} gets the WebGL renderer after open()`, re.test(src));
}

const at = (needle) => html.indexOf(needle);
const xterm = at('<script src="static/vendor/xterm.js"></script>');
const webgl = at('<script src="static/vendor/addon-webgl.js"></script>');
const app = at('<script src="static/app.js"></script>');
check("index.html loads the WebGL addon", webgl >= 0);
check("…after xterm and before app.js", xterm >= 0 && xterm < webgl && webgl < app, { xterm, webgl, app });

const vendored = fs.readFileSync(path.join(STATIC, "vendor", "addon-webgl.js"), "utf8");
check("the vendored addon is the UMD build defining WebglAddon",
  /e\.WebglAddon=t\(\)/.test(vendored.slice(0, 400)));
const manifest = fs.readFileSync(path.join(STATIC, "vendor", "VENDORED"), "utf8");
check("VENDORED records the addon and its version", /addon-webgl 0\.19\.0/.test(manifest));

process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log("all gpu-renderer checks passed");
});
