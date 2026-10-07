/* The v2 start-inputs form (docs/cflow-v2.md 2.6), run against the real
   functions from app.js: what the box sends as `cflow_inputs`, the
   per-field marks a 400's `errors` puts on it, and the run view's helpers
   for undecided probes. A stub DOM carries only what those functions read. */
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

function classList() {
  const set = new Set();
  return { set, add: (c) => set.add(c), remove: (c) => set.delete(c),
           contains: (c) => set.has(c),
           toggle: (c, on) => { if (on === undefined ? !set.has(c) : on) set.add(c); else set.delete(c); } };
}
function node(sel, props) { return { sel, classList: classList(), ...props }; }
function row(name, type, parts) {
  const r = node(".cf2-input", { dataset: { input: name, type } });
  const err = node(".cf2-input-error", { textContent: "" });
  r.querySelector = (q) => (q === ".cf2-input-error" ? err : parts.value || null);
  r.querySelectorAll = (q) => (q === ".cf2-input-row" ? parts.rows || [] : []);
  r.err = err;
  return r;
}
function listRow(text, probe) {
  return { querySelector: (q) => (q === ".cf2-input-text" ? { value: text }
    : q === ".cf2-input-probe" && probe !== undefined ? { value: probe } : null) };
}

const ctx = {};
new Function("ctx",
  `const CFLOW_INPUT_CHECK_TYPES = ["invariants", "acceptance"];
   ${slice("cflowInputsValues")} ${slice("cflowInputsErrors")}
   ${slice("cflow2UndecidedCounts")} ${slice("cflow2Undecidable")}
   ${slice("cflowGhVerdict")}
   Object.assign(ctx, { cflowInputsValues, cflowInputsErrors,
                        cflow2UndecidedCounts, cflow2Undecidable, cflowGhVerdict });`)(ctx);

const rows = [
  row("goal", "text", { value: { value: "ship it" } }),
  row("issue", "string", { value: { value: "  " } }),
  row("rounds", "int", { value: { value: "3" } }),
  row("dry", "bool", { value: { checked: true } }),
  row("scope", "list", { rows: [listRow("src/a"), listRow(""), listRow("src/b")] }),
  row("invariants", "invariants", { rows: [listRow("master unmoved", "git diff --quiet master"),
                                           listRow("", ""), listRow("no deps", "")] }),
  row("acceptance", "acceptance", { rows: [listRow("", "")] }),
];
const box = { querySelectorAll: (q) => (q === ".cf2-input" ? rows : []) };
assert.deepStrictEqual(ctx.cflowInputsValues(box), {
  goal: "ship it", rounds: 3, dry: true, scope: ["src/a", "src/b"],
  invariants: [{ text: "master unmoved", probe: "git diff --quiet master" },
               { text: "no deps", probe: null }],
});

assert.strictEqual(ctx.cflowInputsErrors(box, { goal: "Goal is required" }), true);
assert.ok(rows[0].classList.contains("invalid"));
assert.strictEqual(rows[0].err.textContent, "Goal is required");
assert.ok(!rows[1].classList.contains("invalid"));
assert.strictEqual(ctx.cflowInputsErrors(box, {}), false);
assert.ok(!rows[0].classList.contains("invalid"));

const journal = [
  { event: "probe_undecided", instance: "w@4", count: 1 },
  { event: "probe_undecided", instance: "w@4", count: 2 },
  { event: "left", node: "cut-wait", outcome: "undecidable" },
  { event: "left", node: "x", outcome: "ok" },
];
assert.deepStrictEqual(ctx.cflow2UndecidedCounts(journal), { "w@4": 2 });
assert.deepStrictEqual(ctx.cflow2Undecidable(journal).map((e) => e.node), ["cut-wait"]);
console.log("cflow2inputs_check: ok");

// The gh readiness line under an input declared `precheck: gh`
// (claunch-yq80p.5): ok only when gh is installed and signed in to every
// host the directory pushes to; otherwise the guide's commands come along.
{
  const v = ctx.cflowGhVerdict;
  assert.strictEqual(v(null).ok, false);
  const missing = v({ client: { installed: false }, guide: [{ run: "sudo apt install gh" }] });
  assert.strictEqual(missing.ok, false);
  assert.match(missing.text, /not installed/);
  assert.deepStrictEqual(missing.guide.map((g) => g.run), ["sudo apt install gh"]);
  const noRemote = v({ client: { installed: true, version: "2.60" }, hosts: [] });
  assert.strictEqual(noRemote.ok, false);
  assert.match(noRemote.text, /no GitHub remote/);
  const out = v({ client: { installed: true, version: "2.60" },
                  hosts: [{ host: "ghe.example", authenticated: false }],
                  guide: [{ run: "gh auth login --hostname ghe.example" }] });
  assert.strictEqual(out.ok, false);
  assert.match(out.text, /not signed in to ghe\.example/);
  const ok = v({ client: { installed: true, version: "2.60" },
                 hosts: [{ host: "ghe.example", authenticated: true, account: "me" }],
                 guide: [{ run: "git config claunch.pr.remote <remote name>" }] });
  assert.strictEqual(ok.ok, true);
  assert.match(ok.text, /signed in to ghe\.example as me/);
  assert.deepStrictEqual(ok.guide, []);
}

