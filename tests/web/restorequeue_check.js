/* The boot's restore queue on the rail (claunch-v4uyt): a delayed record is
   drawn as its own state — its place in the queue, a dot of its own — and
   never as a hold, which a person read as "failed to come back". */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.join(__dirname, "..", "..");
// A checkout with core.autocrlf=true has CRLF in the working tree, and the
// "\n}\n" a function is cut at would never match (claunch-oo8au.2).
const src = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "app.js"), "utf8").replace(/\r\n/g, "\n");
const css = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");
const html = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "index.html"), "utf8");

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

const banner = { classList: { values: new Set(["hidden"]),
  toggle(name, on) { if (on) this.values.add(name); else this.values.delete(name); } },
  textContent: "", title: "" };
const ctx = {
  document: { getElementById: (id) => id === "restore-queue-banner" ? banner : null },
  profileHarnessLabel: (profile, harness) => harness,
  dotGrade: () => "",
};
vm.createContext(ctx);
vm.runInContext([
  slice("function restoreEtaText(", "function railMetaText("),
  slice("function railMetaText(", "\n}\n") + "\n}\n",
  slice("function dotClassOf(", "\n}\n") + "\n}\n",
  slice("function dotTitle(", "\n}\n") + "\n}\n",
].join("\n"), ctx);

const delayed = {
  name: "s498", harness: "claude", status: "exited",
  held_at: "2026-10-14T08:52:56Z", delayed_at: "2026-10-14T08:52:56Z",
  restore_queue: { rank: 48, total: 84, ahead: 12, eta_seconds: 95 },
};
const held = { name: "h", harness: "claude", status: "exited", held_at: "2026-10-14T08:00:00Z" };

check("the rail line gives the place in the queue, not 'hold'",
      ctx.railMetaText(delayed), "claude · queued 48/84 · 12 ahead · ~2m");
check("the next one says so", ctx.restoreQueueText({ restore_queue: { rank: 3, total: 9, ahead: 0, eta_seconds: 4 } }),
      "queued 3/9 · next · ~4s");
check("a delayed row with no place yet still reads as queued",
      ctx.restoreQueueText({ delayed_at: "x" }), "queued to start");
check("a hold still reads as hold", ctx.railMetaText(held), "claude · hold");
check("the delayed dot is its own class, without hold",
      ctx.dotClassOf(delayed), "dot exited delayed");
check("the hold dot is unchanged", ctx.dotClassOf(held), "dot exited hold");
check("the dot title says it starts by itself",
      ctx.dotTitle(delayed).includes("starts by itself"), true);
check("the delayed dot has a colour apart from hold's violet",
      /\.dot\.exited\.delayed\s*\{[^}]*#39c5cf/.test(css) && !/\.dot\.exited\.hold\.delayed/.test(css), true);

ctx.renderRestoreQueueBanner({ total: 84, done: 36, remaining: 48, next: "s498", eta_seconds: 400 });
check("the banner shows progress while sessions wait",
      [banner.classList.values.has("hidden"), banner.textContent],
      [false, "Restoring after restart: 36/84 started · 48 queued · next s498 · ~7m left"]);
ctx.renderRestoreQueueBanner(undefined);
check("the banner hides when the queue is done", banner.classList.values.has("hidden"), true);
check("the page has the banner", html.includes('id="restore-queue-banner"'), true);

if (failed) process.exit(1);
console.log("restorequeue_check: ok");
