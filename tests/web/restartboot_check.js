/* The successor answers before it has restored the fleet (claunch-9akgh):
   `boot: {phase: "restoring", done, total}` from /api/health and /api/daemon
   becomes a line on the rail, and the rail is refreshed once more when the
   restore finishes, because the tail of it lands after the tick's session
   read. Without this the rail filled in over ten seconds with nothing
   saying why, which reads the same as sessions going missing. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "app.js"), "utf8").replace(/\r\n/g, "\n");

function slice(start, end) {
  const a = src.indexOf(start);
  const b = src.indexOf(end, a + start.length);
  if (a < 0 || b <= a) throw new Error(`cannot locate ${start}`);
  return src.slice(a, b);
}

let failed = 0;
function check(what, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    failed++;
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
  }
}

const banner = {
  values: new Set(["hidden"]),
  textContent: "",
  title: "",
  classList: {
    add(name) { banner.values.add(name); },
    remove(name) { banner.values.delete(name); },
    toggle(name, on) { if (on) banner.values.add(name); else banner.values.delete(name); },
  },
};
let refreshed = 0;
const ctx = {
  document: { getElementById: (id) => id === "restore-queue-banner" ? banner : null },
  refreshSessions: () => { refreshed += 1; },
};
vm.createContext(ctx);
vm.runInContext(slice("let daemonBootPhase", "function railMetaText("), ctx);

// `let` at a script's top level is not a property of the context object, so
// the state is read back by name.
check("nothing shown on a ready daemon",
      vm.runInContext("daemonBootPhase", ctx), null);

ctx.syncBootProgress({ phase: "restoring", done: 3, total: 10, elapsed: 4 });
check("restoring is announced with its count",
      banner.textContent, "Restarting the daemon — restoring sessions 3/10 (4s)");
check("and the line is visible", banner.values.has("hidden"), false);

ctx.syncBootProgress({ phase: "restoring", done: 0, total: 0 });
check("a restore with no total still says what it is doing",
      banner.textContent, "Restarting the daemon — restoring sessions…");

ctx.syncBootProgress({ phase: "ready", done: 10, total: 10 });
check("ready takes the line down", banner.values.has("hidden"), true);
check("and re-reads the rail once, for the tail the restore added",
      refreshed, 1);

ctx.syncBootProgress({ phase: "ready", done: 10, total: 10 });
check("a second ready tick refreshes nothing", refreshed, 1);

if (failed) process.exit(1);
console.log("restartboot: ok");
