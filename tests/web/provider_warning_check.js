/* The provider warning strip (#term-provider-warn), checked against the
   shipped markup and the shipped painters.

   A session running the claude harness on a provider that is not Claude's own
   still has its permission classifier call Claude, so the screen says so and
   offers two answers: restart it on yolo mode, or stop showing the warning.
   Three things can break silently here:

   - The decision. It is taken from two facts the daemon publishes on the
     session row (``provider``/``provider_service``) plus the record's own
     ``provider_warning_ignored`` and the session's own argv: a strip that
     shows for Anthropic's own service, for another harness, for a session
     that already dismissed it, or for one already running on skip
     permissions is a warning nobody can trust; one that stays quiet on a row
     whose service is unreadable is the safer direction of the same mistake,
     and is pinned as such.
   - The presses. "restart with yolo mode" must be the details panel's own
     restart against the same route and body (/skip-permissions, {skip:true})
     — a second path would be a second definition of yolo mode. "ignore" must
     write the record (POST /provider-warning, {ignore:true}) and then stop
     drawing, rather than merely hiding in this browser.
   - The markup. The strip belongs between the cflow answer strip and the
     briefing pane, inside the column that is refitted when a strip changes;
     a div placed outside it would sit over the terminal and never be
     measured.
*/
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

function sliceFrom(marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error(`cannot slice ${marker} from app.js`);
  // Braces from the first "{" after the marker: these are plain functions,
  // and the text after them ends at the matching close.
  const open = src.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) return src.slice(start, i + 1) + "\n";
    }
  }
  throw new Error(`unbalanced braces after ${marker}`);
}

/* ---- the markup ---- */
const gate = html.indexOf('id="term-cflow"');
const warn = html.indexOf('id="term-provider-warn"');
const brief = html.indexOf('id="term-brief-pane"');
check("#term-provider-warn is in the session column", warn > 0, true);
check("...after the cflow answer strip", warn > gate, true);
check("...before the briefing pane", brief > warn, true);
check("...declared hidden, like the other strips",
      /<div id="term-provider-warn" class="hidden"/.test(html), true);

/* ---- the styles ---- */
check("the strip has a rule", /#term-provider-warn \{/.test(css), true);
check("...that hides it while it has nothing to say",
      /#term-provider-warn\.hidden \{ display: none; \}/.test(css), true);

/* ---- the painters, run against stubs ---- */
function cls() {
  const set = new Set();
  return {
    has: (c) => set.has(c),
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    toggle: (c, on) => (on === undefined
      ? (set.has(c) ? set.delete(c) : set.add(c))
      : (on ? set.add(c) : set.delete(c))),
  };
}
function node(tag, className, text) {
  const n = {
    tag, className: className || "", textContent: text || "", title: "",
    type: "", disabled: false, dataset: {}, children: [], listeners: {},
    classList: null,
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(ev, fn) { this.listeners[ev] = fn; },
  };
  // ``box.innerHTML = ""`` is how the painter clears itself before a rebuild;
  // a plain property would leave the old children in place and the next read
  // would see two generations of buttons at once.
  Object.defineProperty(n, "innerHTML", {
    get() { return ""; },
    set() { n.children.length = 0; },
  });
  n.classList = cls();
  for (const c of String(className || "").split(/\s+/).filter(Boolean)) {
    n.classList.add(c);
  }
  return n;
}

const box = node("div", "hidden");
box.id = "term-provider-warn";
const posts = [];
const calls = [];
let attached = [];

const ctx = {
  currentName: "s1",
  sessionsCache: [],
  harnessDetails: { claude: { skip_permissions_args: ["--dangerously-skip-permissions"] } },
  box,
};

const run = new Function(
  "exports", "ctx", "$", "el", "refitSoon", "api", "detach", "attach",
  "refreshSessions", "argvHasGroup",
  "let currentName = ctx.currentName;\n" +
  "let sessionsCache = ctx.sessionsCache;\n" +
  "const harnessDetails = ctx.harnessDetails;\n" +
  // The shipped page's own constant (app.js): the check compares against it
  // rather than against a second copy of the string.
  'const ANTHROPIC_SERVICE = "anthropic";\n' +
  sliceFrom("function termProviderWarnView(") +
  sliceFrom("function renderTermProviderWarn(") +
  sliceFrom("async function restartSessionYolo(") +
  sliceFrom("async function ignoreProviderWarning(") +
  "\nexports.render = () => { currentName = ctx.currentName; " +
  "sessionsCache = ctx.sessionsCache; renderTermProviderWarn(); };\n" +
  "exports.view = () => { currentName = ctx.currentName; " +
  "sessionsCache = ctx.sessionsCache; return termProviderWarnView(); };\n" +
  "exports.yolo = restartSessionYolo;\n" +
  "exports.ignore = ignoreProviderWarning;\n");

run(
  ctx,
  ctx,
  (id) => (id === "term-provider-warn" ? box : null),
  (tag, className, text) => node(tag, className, text),
  () => {},
  async (p, opts) => {
    posts.push({ path: p, body: opts && opts.body ? JSON.parse(opts.body) : null });
    return { ok: true, status: 200, json: async () => ({}) };
  },
  () => calls.push("detach"),
  (name) => calls.push("attach:" + name),
  async () => calls.push("refresh"),
  (argv, group) => group.every((arg) => argv.includes(arg)),
);

const hidden = () => box.classList.has("hidden");
function all(node, out = []) {
  for (const c of node.children || []) { out.push(c); all(c, out); }
  return out;
}
const buttons = () => all(box).filter((c) => c.tag === "button");
const labels = () => buttons().map((b) => b.textContent);
const shownText = () => {
  const t = box.children.find((c) => c.className === "tpw-text");
  return t ? t.textContent : "";
};

/* Nothing to say: no session, another harness, Claude's own service. */
ctx.currentName = null;
ctx.sessionsCache = [{ name: "s1", harness: "claude", provider: "fw",
                       provider_service: "custom" }];
ctx.render();
check("no session, no strip", hidden(), true);

ctx.currentName = "s1";
ctx.sessionsCache = [{ name: "s1", harness: "codex", provider: "fw",
                       provider_service: "custom" }];
ctx.render();
check("another harness is not this warning", hidden(), true);

ctx.sessionsCache = [{ name: "s1", harness: "claude", provider: "claude",
                       provider_service: "anthropic" }];
ctx.render();
check("Claude's own service is what the warning exists to leave alone",
      hidden(), true);

/* A row whose service could not be resolved says nothing: "nothing to
   compare" must not read as "Claude's own", and it must not accuse either. */
ctx.sessionsCache = [{ name: "s1", harness: "claude", provider: "",
                       provider_service: "" }];
ctx.render();
check("an unreadable provider stays silent", hidden(), true);

/* The case it exists for: claude harness, somebody else's provider. */
ctx.sessionsCache = [{ name: "s1", harness: "claude", provider: "fireworks",
                       provider_service: "custom", args: [] }];
ctx.render();
check("a third-party provider shows the strip", hidden(), false);
check("...names the provider", /fireworks/.test(shownText()), true);
check("...states the cost as a possibility, not a fact",
      /can add Claude usage/.test(shownText()), true);
check("...and offers both answers", labels(), ["restart with yolo mode", "ignore"]);

/* Already on yolo mode: the advice has been taken and the permission
   classifier is not called, so the strip is not drawn at all. It is not a
   hidden card with an inert press either — "ignore" is the one press that
   would record a permanent answer in the one state where it buys nothing,
   and with that record the warning could never come back for a session that
   later runs without the flag. Turning the flag off repaints from the same
   row (claunch-9mwba.1). */
ctx.sessionsCache = [{ name: "s1", harness: "claude", provider: "fireworks",
                       provider_service: "custom",
                       args: ["--dangerously-skip-permissions"] }];
ctx.render();
check("a session already on yolo mode draws nothing", hidden(), true);
check("...and leaves no press to record an ignore with", labels(), []);
ctx.sessionsCache = [{ name: "s1", harness: "claude", provider: "fireworks",
                       provider_service: "custom", args: [] }];
ctx.render();
check("...and the strip comes back once the flag is off", hidden(), false);
check("...with both answers again", labels(),
      ["restart with yolo mode", "ignore"]);

/* A dismissed record is the answer: nothing drawn at all. */
ctx.sessionsCache = [{ name: "s1", harness: "claude", provider: "fireworks",
                       provider_service: "custom", provider_warning_ignored: true }];
ctx.render();
check("a session that dismissed the warning draws nothing", hidden(), true);

/* ---- the presses ---- */
(async () => {
  ctx.sessionsCache = [{ name: "s1", harness: "claude", provider: "fireworks",
                         provider_service: "custom", args: [] }];
  ctx.render();
  const yolo = buttons().find((b) => b.textContent === "restart with yolo mode");
  const ignore = buttons().find((b) => b.textContent === "ignore");

  posts.length = 0;
  calls.length = 0;
  await yolo.listeners.click();
  check("the restart is the details panel's own route",
        posts.map((p) => [p.path, p.body]),
        [["/api/sessions/s1/skip-permissions", { skip: true }]]);
  check("...and it re-attaches the session it restarted",
        calls.includes("detach") && calls.includes("attach:s1")
        && calls.includes("refresh"), true);

  posts.length = 0;
  await ignore.listeners.click();
  check("ignore writes the record rather than hiding locally",
        posts.map((p) => [p.path, p.body]),
        [["/api/sessions/s1/provider-warning", { ignore: true }]]);
  ctx.render();
  check("...and the strip goes without waiting for the next poll", hidden(), true);

  if (failures) {
    console.error(`${failures} failure(s)`);
    process.exit(1);
  }
  console.log("provider warning: ok");
})();
