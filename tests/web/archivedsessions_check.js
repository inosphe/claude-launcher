/* The rail keeps archived session records available without letting historical
   rows occupy the live fleet by default. Exercise the real visibility helper
   and pin the HTML/CSS contract that makes its state visible. */
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "app.js"), "utf8");
const html = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "index.html"), "utf8");
const css = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");

const a = src.indexOf("let archivedSessionsVisible = false;");
const b = src.indexOf("async function refreshSessions(", a);
if (a < 0 || b <= a) throw new Error("cannot locate archived-session helper");

function node() {
  const classes = new Set();
  return {
    classes, textContent: "", attrs: {},
    classList: {
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
    },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
}
const button = node(), list = node();
const ctx = {};
new Function("exports", "$", "currentName", src.slice(a, b) +
  "\nexports.sync = syncArchivedSessions;" +
  "\nexports.open = () => { archivedSessionsVisible = true; };")(
    ctx, (id) => id === "archived-sessions-toggle" ? button : list, null);

let failures = 0;
function check(what, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}` +
                  `\n  want ${JSON.stringify(want)}`);
    failures++;
  }
}

ctx.sync([{ name: "live", status: "idle" }, { name: "dead", status: "exited" }]);
check("the control is absent without archived records", button.classes.has("hidden"), true);
check("the list does not expose an empty group", list.classes.has("show-archived"), false);

const mixed = [
  { name: "live", status: "busy" },
  { name: "old-1", status: "exited", archived_at: "2026-08-27T00:00:00Z" },
  { name: "old-2", status: "exited", archived_at: "2026-08-28T00:00:00Z" },
];
ctx.sync(mixed);
check("the archived count is explicit", button.textContent, "Show archived sessions (2)");
check("the control reports its collapsed state", button.attrs["aria-expanded"], "false");
check("archived rows start collapsed", list.classes.has("show-archived"), false);

ctx.open();
ctx.sync(mixed);
check("the open control names its next action", button.textContent, "Hide archived sessions (2)");
check("the control reports its expanded state", button.attrs["aria-expanded"], "true");
check("the list exposes archived rows", list.classes.has("show-archived"), true);

check("the shipped page has the visibility control",
      /id="archived-sessions-toggle"[^>]*aria-controls="session-list"/.test(html), true);
check("collapsed archived rows are removed from layout",
      /#session-list:not\(\.show-archived\)\s*>\s*li\.archived-record\s*\{[^}]*display:\s*none/.test(css), true);
check("archived records are marked for the visibility rule",
      /s\.archived_at\) li\.classList\.add\("archived-record"\)/.test(src), true);
check("the normal web controls expose archive without permanent removal",
      /id="term-archive"/.test(html) && /id="archive-exited"/.test(html) &&
      !/id="term-remove"|id="clear-exited"|id="delete-all"/.test(html), true);

if (failures) process.exit(1);
console.log("archivedsessions_check: ok");
