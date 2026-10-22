/* The v2 start inputs ("Workflow variables") on the two creation paths that
   are not the new-session form (claunch-fqkqt): the run page's start panel
   (buildStartPanel) and the quick-fork dialog. Run against the real
   functions from app.js with a stub DOM: the box is drawn for a v2 pick
   with inputs and hidden otherwise, what it holds rides the start / fork
   body as `cflow_inputs`, and a refused start marks its fields. The box's
   own rows and values are cflowInputsForm / cflowInputsValues, checked by
   cflow2inputs_check.js; they are stubbed here. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

let failures = 0;
function check(label, got, want) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a === b) return;
  failures++;
  console.error(`FAIL ${label}\n  got:  ${a}\n  want: ${b}`);
}

/* A DOM node with the little buildStartPanel and the box helpers touch. */
function classList(node) {
  const set = new Set();
  return {
    add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c),
    toggle: (c, on) => { if (on === undefined ? !set.has(c) : on) set.add(c); else set.delete(c); },
    _sync: (text) => { set.clear(); for (const c of String(text || "").split(/\s+/)) if (c) set.add(c); },
  };
}
function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], dataset: {}, listeners: {},
    textContent: "", title: "", value: "", disabled: false, placeholder: "", type: "",
    appendChild(c) { this.children.push(c); if (this.tagName === "SELECT" && c.tagName === "OPTION" && !this.value) this.value = c.value; return c; },
    append(...cs) { for (const c of cs) this.appendChild(c); },
    replaceChildren() { this.children = []; },
    addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
    fire(ev) { return Promise.all((this.listeners[ev] || []).map((fn) => fn({}))); },
    find(pred) {
      for (const c of this.children) {
        if (pred(c)) return c;
        const deep = c.find && c.find(pred);
        if (deep) return deep;
      }
      return null;
    },
  };
  node.classList = classList(node);
  Object.defineProperty(node, "className", {
    get() { return this._cls || ""; },
    set(v) { this._cls = v; this.classList._sync(v); },
  });
  Object.defineProperty(node, "innerHTML", { set() { this.children = []; } });
  return node;
}
const document = { createElement: makeNode };
function el(tag, cls, text) {
  const n = makeNode(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const calls = [];
let reply = null;
const flows = [
  { name: "asks", version: 2, path: "/w/asks.yaml",
    inputs: [{ name: "goal", type: "text" }], input_constraints: [] },
  { name: "plain2", version: 2, path: "/w/plain2.yaml", inputs: [] },
  { name: "old", version: 1, path: "/w/old.yaml", deprecated: true },
];
async function api(url, options) {
  calls.push({ url, body: options && options.body ? JSON.parse(options.body) : null });
  if (url.startsWith("/api/cflow/workflows")) {
    return { ok: true, json: async () => ({ workflows: flows }) };
  }
  const r = reply || { status: 200, doc: { nudged_sessions: ["s1"] } };
  return { ok: r.status < 400, status: r.status, json: async () => r.doc };
}
const drawn = [], live = [], marks = [];
const ctx = {};
new Function("ctx", "document", "el", "api", "drawn", "live", "marks",
  `const CFLOW_V1_NOTE = "v1";
   let workflowsCache = [];
   const alerts = [], notes = [];
   function alert(m) { alerts.push(m); }
   function confirm() { return true; }
   function notify(t, m) { notes.push(t); }
   function wfOptionLabel(w) { return w.name; }
   function cflowInputsForm(box, fields) { drawn.push(fields.map((f) => f.name)); box.appendChild(el("p", "cf2-input")); }
   function cflowInputsLive(box, picked, cwdOf, profileEl, list) { live.push({ picked, cwd: cwdOf(), profileEl, list: list.length }); }
   function cflowInputsValues(box) { return box._values || {}; }
   function cflowInputsErrors(box, errors) { marks.push(errors); }
   let sessionView = {};
   function sessionFormView() { return sessionView; }
   const form = { quick_fork_workflow: { value: "-" }, quick_fork_mesh: { value: "-" },
     quick_fork_place: { value: "child" }, quick_fork_self_merge: { checked: false },
     quick_fork_role: { value: "" } };
   const forkBox = el("div", "cf2-inputs hidden");
   function $(id) { return id === "new-session" ? form : id === "new-quick-fork-inputs" ? forkBox : null; }
   function quickForkMeshName() { return ""; }
   function quickForkExclusiveHolders() { return {}; }
   const sessionsCache = [];
   const location = { hash: "" };
   async function refreshSessions() {}
   ${slice("cflowPickValue")} ${slice("cflowPickBody")} ${slice("cflowPickEntry")}
   ${slice("cflowPost")} ${slice("cflowInputsMount")} ${slice("buildStartPanel")}
   ${slice("quickForkWorkflowName")} ${slice("syncQuickForkInputs")}
   ${slice("quickForkRequestOptions")} ${slice("sessionFormConfig")}
   ${slice("sessionFormPayload")} ${slice("quickForkSession")}
   Object.assign(ctx, { buildStartPanel, quickForkWorkflowName, syncQuickForkInputs,
     quickForkRequestOptions, quickForkSession, alerts, form, forkBox,
     setView: (v) => { sessionView = v; } });`)(ctx, document, el, api, drawn, live, marks);

const inputsOf = (box) => box.find((n) => n.classList.contains("wf-start-inputs"));
const buttonOf = (box, text) => box.find((n) => n.tagName === "BUTTON" && n.textContent === text);
const selectOf = (box) => box.find((n) => n.tagName === "SELECT");
const posted = (url) => calls.filter((c) => c.url === url).map((c) => c.body);

(async () => {
  // ---- the run page's start panel ----
  const box = el("div", "wf-start");
  await ctx.buildStartPanel(box, { cwd: "/repo", scope: "s1", sessions: ["s1"] });
  const inputs = inputsOf(box);
  const direct = buttonOf(box, "Start directly");
  const ask = buttonOf(box, "Ask the agent to start");
  const again = (b) => { b.disabled = false; return b; };
  check("the panel carries a Workflow variables box", !!inputs, true);
  check("a v2 pick with inputs draws its fields", drawn, [["goal"]]);
  check("the box is shown for it", inputs.classList.contains("hidden"), false);
  check("the live check reads the panel's own list and directory, no profile picker",
        live, [{ picked: "asks", cwd: "/repo", profileEl: null, list: 3 }]);

  inputs._values = { goal: "ship it" };
  await direct.fire("click");
  check("Start directly sends the box's values as cflow_inputs",
        posted("/api/cflow/start"),
        [{ cwd: "/repo", scope: "s1", workflow: "asks", workflow_version: 2,
           context: "", cflow_inputs: { goal: "ship it" } }]);

  await ask.fire("click");
  check("Ask the agent sends them too (a v2 request is started at once)",
        posted("/api/cflow/request")[0].cflow_inputs, { goal: "ship it" });

  // A refused start marks the fields it names, then says why.
  reply = { status: 400, doc: { error: "bad inputs", errors: { goal: "Goal is required" } } };
  await again(direct).fire("click");
  check("a 400's errors are marked on the box", marks[marks.length - 1], { goal: "Goal is required" });
  check("and the message is still shown", ctx.alerts, ["bad inputs"]);
  reply = null;

  // A pick with no inputs, and a v1 pick: no box, no cflow_inputs.
  const sel = selectOf(box);
  for (const name of ["plain2", "old"]) {
    sel.value = name;
    await sel.fire("change");
    check(`${name}: the box is hidden`, inputs.classList.contains("hidden"), true);
    check(`${name}: the box is emptied`, inputs.children.length, 0);
    await again(direct).fire("click");
    check(`${name}: the start carries no cflow_inputs`,
          "cflow_inputs" in posted("/api/cflow/start").slice(-1)[0], false);
  }
  check("only the v2 pick with inputs drew fields", drawn, [["goal"]]);

  // ---- the quick-fork dialog ----
  const view = { source: { name: "a", cwd: "/origin" }, inheritance: { workflow: "asks" },
                 forkWorkflows: flows, forkInputsFor: null };
  ctx.setView(view);
  const f = ctx.form;
  check("'.' names the pair the origin hands a child",
        ctx.quickForkWorkflowName({ quick_fork_workflow: { value: "." } }, view), "asks");
  check("'-' names none", ctx.quickForkWorkflowName({ quick_fork_workflow: { value: "-" } }, view), "");

  f.quick_fork_workflow.value = "-";
  ctx.syncQuickForkInputs();
  check("no workflow: the fork's box stays hidden", ctx.forkBox.classList.contains("hidden"), true);
  check("no workflow: no cflow_inputs", "cflow_inputs" in ctx.quickForkRequestOptions(f, view).options, false);

  live.length = 0;
  f.quick_fork_workflow.value = ".";
  ctx.syncQuickForkInputs();
  check("the inherited v2 pair draws its fields", drawn.slice(-1)[0], ["goal"]);
  check("checked against the origin's directory, where the copy stands",
        live, [{ picked: "asks", cwd: "/origin", profileEl: null, list: 3 }]);
  ctx.syncQuickForkInputs();
  check("the same pick is not redrawn (typed values stay)", live.length, 1);

  ctx.forkBox._values = { goal: "the other way" };
  const { options } = ctx.quickForkRequestOptions(f, view);
  check("the fork request carries the box's values", options,
        { workflow: ".", cflow_inputs: { goal: "the other way" } });
  await ctx.quickForkSession("a", "", options, { onCreated() {}, onError() {} });
  check("and the fork route's body keeps them (the fork payload allows them)",
        posted("/api/sessions/a/quick-fork").slice(-1)[0],
        { workflow: ".", cflow_inputs: { goal: "the other way" } });

  f.quick_fork_workflow.value = "old";
  ctx.syncQuickForkInputs();
  check("a v1 workflow hides the box again", ctx.forkBox.classList.contains("hidden"), true);
  check("and sends no cflow_inputs",
        "cflow_inputs" in ctx.quickForkRequestOptions(f, view).options, false);

  if (failures) process.exit(1);
  console.log("startinputs_check: all assertions passed");
})().catch((e) => { console.error(e); process.exit(1); });
