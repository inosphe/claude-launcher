/* The Workflow row's recent picks (claunch-pmw8u), run against the real
   functions from app.js.

   What has to hold: a launch moves its workflow to the front and drops a
   repeat of it (least recently used goes first past five); the list lives in
   localStorage under this dashboard's key and a corrupt or unreadable store
   reads as empty; only picks the select offers now get a button; the held
   value's button is lit; and a click sets the select and fires its change
   event, which is what marks the row as picked by hand. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

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

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { failures++; console.error(`FAIL ${label}: got ${g}, want ${w}`); }
}

/* ---- stubs ---- */
const store = new Map();
const localStorage = {
  broken: false,
  getItem(k) { if (this.broken) throw new Error("denied"); return store.has(k) ? store.get(k) : null; },
  setItem(k, v) { if (this.broken) throw new Error("denied"); store.set(k, String(v)); },
};
function node(tag) {
  const n = {
    tag, className: "", textContent: "", children: [], attrs: {}, listeners: {},
    classes: new Set(),
    classList: {
      toggle(c, on) { on ? n.classes.add(c) : n.classes.delete(c); },
      contains(c) { return n.classes.has(c); },
    },
    appendChild(c) { n.children.push(c); return c; },
    set innerHTML(v) { if (v === "") n.children.length = 0; },
    setAttribute(k, v) { n.attrs[k] = v; },
    addEventListener(t, f) { n.listeners[t] = f; },
    dispatchEvent(ev) { (n.events = n.events || []).push(ev.type); },
  };
  return n;
}
const row = node("div");
const select = node("select");
select.value = "";
select.options = [];
function offer(pairs) {
  select.options = [["(none)", ""], ...pairs].map(([t, v]) => ({ textContent: t, value: v }));
}
const form = { workflow: select };
const $ = (id) => ({ "new-workflow-recent": row, "new-session": form })[id] || null;
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
class Event { constructor(type) { this.type = type; } }

const api = new Function(
  "localStorage", "$", "el", "Event",
  `const BASE = "/";
   const WORKFLOW_RECENT_KEY = \`claunch_workflow_recent:\${BASE}\`;
   const WORKFLOW_RECENT_MAX = 5;
   ${slice("workflowRecentPush")}
   ${slice("workflowRecentLoad")}
   ${slice("workflowRecentRecord")}
   ${slice("renderWorkflowRecent")}
   return { workflowRecentPush, workflowRecentLoad, workflowRecentRecord,
            renderWorkflowRecent, KEY: WORKFLOW_RECENT_KEY };`
)(localStorage, $, el, Event);

// The constants the test re-declares must match app.js.
check("key spelling", /const WORKFLOW_RECENT_KEY = `claunch_workflow_recent:\$\{BASE\}`;/.test(src), true);
check("max is five", /const WORKFLOW_RECENT_MAX = 5;/.test(src), true);

/* ---- LRU order ---- */
const push = api.workflowRecentPush;
check("first use", push([], "a", 5), ["a"]);
check("newest first", push(["a"], "b", 5), ["b", "a"]);
check("a repeat moves to the front, once", push(["b", "a", "c"], "a", 5), ["a", "b", "c"]);
check("least recent drops past the cap",
      push(["e", "d", "c", "b", "a"], "f", 5), ["f", "e", "d", "c", "b"]);
check("junk entries are dropped", push(["a", 3, "", null, "b"], "", 5), ["a", "b"]);

/* ---- storage ---- */
check("empty store reads empty", api.workflowRecentLoad(), []);
for (const v of ["a", "b", "c", "d", "e", "f", "b"]) api.workflowRecentRecord(v);
check("stored under the dashboard key", JSON.parse(store.get(api.KEY)), ["b", "f", "e", "d", "c"]);
api.workflowRecentRecord("");
api.workflowRecentRecord("-");
check("no pick and an explicit refusal are not uses", api.workflowRecentLoad(), ["b", "f", "e", "d", "c"]);
store.set(api.KEY, "{not json");
check("a corrupt store reads empty", api.workflowRecentLoad(), []);
store.set(api.KEY, JSON.stringify({ a: 1 }));
check("a non-list reads empty", api.workflowRecentLoad(), []);
localStorage.broken = true;
check("an unreadable store reads empty", api.workflowRecentLoad(), []);
api.workflowRecentRecord("x"); // must not throw
localStorage.broken = false;

/* ---- the row ---- */
store.set(api.KEY, JSON.stringify(["v2:flow", "gone", "plain"]));
offer([["plain — default for worker", "plain"], ["flow · v2", "v2:flow"], ["other", "other"]]);
select.value = "plain";
api.renderWorkflowRecent();
const buttons = () => (row.children[1] || { children: [] }).children;
check("row shown", row.classes.has("hidden"), false);
check("only offered picks, in recent order",
      buttons().map((b) => b.textContent), ["flow · v2", "plain"]);
check("the held value is lit", buttons().map((b) => b.attrs["aria-pressed"]), ["false", "true"]);
check("buttons do not submit", buttons().map((b) => b.type), ["button", "button"]);
buttons()[0].listeners.click();
check("a click sets the select", select.value, "v2:flow");
check("...and fires its change event", select.events, ["change"]);

offer([["other", "other"]]);
api.renderWorkflowRecent();
check("nothing offered: row hidden and empty", [row.classes.has("hidden"), row.children.length], [true, 0]);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("wfrecent_check: ok");
