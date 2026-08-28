/* The rail keeps exited session records available without letting historical
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

const a = src.indexOf("let exitedSessionsVisible = false;");
const b = src.indexOf("async function refreshSessions(", a);
if (a < 0 || b <= a) throw new Error("cannot locate exited-session helper");

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
  "\nexports.sync = syncExitedSessions;" +
  "\nexports.open = () => { exitedSessionsVisible = true; };")(
    ctx, (id) => id === "exited-sessions-toggle" ? button : list, null);

let failures = 0;
function check(what, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}` +
                  `\n  want ${JSON.stringify(want)}`);
    failures++;
  }
}

ctx.sync([{ name: "live", status: "idle" }]);
check("the control is absent without exited records", button.classes.has("hidden"), true);
check("the list does not expose an empty group", list.classes.has("show-exited"), false);

const mixed = [
  { name: "live", status: "busy" },
  { name: "old-1", status: "exited" },
  { name: "old-2", status: "exited" },
];
ctx.sync(mixed);
check("the closed count is explicit", button.textContent, "Show exited sessions (2)");
check("the control reports its collapsed state", button.attrs["aria-expanded"], "false");
check("exited rows start collapsed", list.classes.has("show-exited"), false);

ctx.open();
ctx.sync(mixed);
check("the open control names its next action", button.textContent, "Hide exited sessions (2)");
check("the control reports its expanded state", button.attrs["aria-expanded"], "true");
check("the list exposes exited rows", list.classes.has("show-exited"), true);

check("the shipped page has the visibility control",
      /id="exited-sessions-toggle"[^>]*aria-controls="session-list"/.test(html), true);
check("collapsed exited rows are removed from layout",
      /#session-list:not\(\.show-exited\)\s*>\s*li\.exited-record\s*\{[^}]*display:\s*none/.test(css), true);
check("exited records are marked for the visibility rule",
      /s\.status === "exited"\) li\.classList\.add\("exited-record"\)/.test(src), true);

if (failures) process.exit(1);
console.log("exitedsessions_check: ok");
