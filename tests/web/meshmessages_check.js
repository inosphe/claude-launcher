/* The mesh log's tabs and pager must keep an archived conversation off the
   default view, without losing the operator's route to older records. */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(
  __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"
), "utf8");
const a = src.indexOf("function renderMeshMessageLog(");
const b = src.indexOf("function fmtAge(", a);
if (a < 0 || b <= a) throw new Error("cannot locate mesh message log");

function node(tag, cls, text) {
  const n = { tag, cls: new Set(String(cls || "").split(/\s+/).filter(Boolean)),
    text: text === undefined ? "" : String(text), kids: [], handlers: {} };
  n.appendChild = (child) => { n.kids.push(child); return child; };
  n.addEventListener = (event, handler) => { n.handlers[event] = handler; };
  n.fire = (event) => n.handlers[event] && n.handlers[event]();
  return n;
}
const el = (tag, cls, text) => node(tag, cls, text);
const refreshed = [];
const ctx = {};
new Function("exports", "el", "refreshMeshView", "MESH_MESSAGE_PAGE_SIZE",
  "let meshMessageFilter = 'current'; let meshMessageOffset = 0;\n" +
  src.slice(a, b) +
  "\nexports.render = renderMeshMessageLog;" +
  "\nexports.state = () => ({ filter: meshMessageFilter, offset: meshMessageOffset });"
)(ctx, el, (force) => refreshed.push(force), 25);

function all(root, pred, out = []) {
  if (pred(root)) out.push(root);
  for (const child of root.kids) all(child, pred, out);
  return out;
}
const byClass = (root, cls) => all(root, (n) => n.cls.has(cls));
let failures = 0;
function check(name, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error(`FAIL ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
    failures++;
  }
}

const page = {
  total: 2, offset: 0, has_newer: false, has_older: true,
  counts: { all: 3, current: 2, archived: 1 },
};
const log = ctx.render({ messages: 3 }, [{ from: "lead", to: "worker", body: "recent" }], page);
check("filter labels include exact counts", byClass(log, "mesh-message-filter").map((n) => n.text),
  ["Current (2)", "All (3)", "Archived (1)"]);
check("the default filter is current", byClass(log, "mesh-message-filter").map((n) => n.cls.has("on")),
  [true, false, false]);
check("the page range is visible", byClass(log, "mesh-message-range").map((n) => n.text),
  ["Showing 2–2 of 2"]);
check("only the current page is rendered", byClass(log, "mesh-msg").length, 1);

byClass(log, "mesh-message-filter")[2].fire("click");
check("archived filter resets to its newest page", ctx.state(), { filter: "archived", offset: 0 });
check("filter change redraws immediately", refreshed, [true]);

byClass(log, "mesh-message-pager")[0].kids[2].fire("click");
check("older moves back one bounded page", ctx.state(), { filter: "archived", offset: 25 });
check("page change redraws immediately", refreshed, [true, true]);

if (failures) process.exit(1);
console.log("meshmessages_check: ok");
