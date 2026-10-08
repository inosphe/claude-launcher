/* A Codex session's YOLO and Sandbox switches in the detail panel
   (claunch-w186v).

   Codex's packaged default (--dangerously-bypass-approvals-and-sandbox)
   already skips the asks, so what is pinned here is that the panel reads a
   session the way the daemon launches it, and that both restart buttons
   send the whole mode to /runtime-mode:

   - Permissions reads a default Codex session as skipping (it used to read
     "asks before it acts" and offer a skip the daemon refused), and its
     toggle sends {yolo: !yolo, sandbox: as it reads now};
   - Sandbox reads the same state, and its toggle sends {yolo: as it reads
     now, sandbox: !sandbox};
   - a harness without the sandbox pair keeps the old skip-permissions route
     and gets no Sandbox button. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let i = body; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (!depth) return src.slice(head, i + 1);
    }
  }
  throw new Error("unbalanced " + name);
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

function node(tag) {
  return {
    tag, kids: [], textContent: "", className: "", handlers: {}, dataset: {},
    disabled: false, title: "",
    appendChild(child) { this.kids.push(child); return child; },
    append(...children) { children.forEach((c) => this.appendChild(c)); },
    addEventListener(kind, fn) { (this.handlers[kind] ||= []).push(fn); },
  };
}
function all(n, out = []) {
  out.push(n);
  (n.kids || []).forEach((k) => all(k, out));
  return out;
}
const text = (n) => all(n).map((k) => k.textContent).join(" ");
const button = (n) => all(n).find((k) => k.tag === "button");

const CODEX = {
  name: "codex", args: ["--dangerously-bypass-approvals-and-sandbox"],
  skip_permissions_args: ["-a", "never"],
  full_access_args: ["--sandbox", "danger-full-access"],
  full_access_off_args: ["--sandbox", "workspace-write"],
  mode_conflict_args: ["--dangerously-bypass-approvals-and-sandbox"],
};
const CLAUDE = { name: "claude", skip_permissions_args: ["--dangerously-skip-permissions"] };

const posts = [];
let reply = { ok: true, status: 200, doc: {} };
const events = [];
const ctx = {};
new Function(
  "exports", "document", "api", "detach", "refreshSessions", "attach",
  [
    "let sessPermsBox = null, sessSandboxBox = null;",
    "let currentName = 'cx1';",
    `const harnessDetails = ${JSON.stringify({ codex: CODEX, claude: CLAUDE })};`,
    "function el(tag, cls, text) { const n = document.createElement(tag);" +
      " if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }",
    slice("argvHasGroup"), slice("codexModeGroups"), slice("codexRuntimeState"),
    slice("codexRuntimeDeclared"), slice("codexRuntimeText"),
    slice("sessPerms"), slice("sessSandbox"),
  ].join("\n") + `
exports.perms = sessPerms;
exports.sandbox = sessSandbox;
`)(
  ctx,
  { createElement: node },
  async (url, init) => {
    posts.push({ url, body: JSON.parse(init.body) });
    return { ok: reply.ok, status: reply.status, json: async () => reply.doc };
  },
  () => events.push("detach"),
  async () => events.push("refresh"),
  (name) => events.push(`attach:${name}`),
);

async function main() {
  const fresh = { session: { name: "cx1", harness: "codex", args: [] } };

  /* ---- Permissions on a default Codex session ---- */
  const perms = ctx.perms(fresh);
  check("a default Codex session reads as never asking",
        [text(perms).includes("never asks before it acts"),
         button(perms).textContent], [true, "Ask again"]);
  reply = { ok: true, status: 200,
            doc: { args: ["--sandbox", "danger-full-access"] } };
  await button(perms).handlers.click[0]();
  check("turning the asks back on sends the whole mode, sandbox kept",
        posts.pop(), { url: "/api/sessions/cx1/runtime-mode",
                       body: { yolo: false, sandbox: false } });
  check("...and reattaches the relaunched session",
        events.splice(0), ["detach", "refresh", "attach:cx1"]);
  check("...and says what it restarted into",
        text(perms).includes("asking before it acts again"), true);

  /* ---- Sandbox ---- */
  const box = ctx.sandbox(fresh);
  check("the Sandbox section names the state and offers the opposite",
        [text(box).includes("sandbox disabled"), button(box).textContent],
        [true, "Turn sandbox on"]);
  reply = { ok: true, status: 200,
            doc: { args: ["-a", "never", "--sandbox", "workspace-write"] } };
  await button(box).handlers.click[0]();
  check("turning the sandbox on keeps YOLO as it reads",
        posts.pop(), { url: "/api/sessions/cx1/runtime-mode",
                       body: { yolo: true, sandbox: true } });
  check("...and says it is in the sandbox now",
        text(box).includes("restarted — in the workspace-write sandbox"), true);
  events.splice(0);

  const sandboxed = { session: { name: "cx1", harness: "codex",
                                 args: ["-a", "never", "--sandbox", "workspace-write"] } };
  const box2 = ctx.sandbox(sandboxed);
  check("a sandboxed session offers to turn it off",
        button(box2).textContent, "Turn sandbox off");
  check("the Permissions form is rebuilt when only the sandbox changed",
        ctx.perms(sandboxed).kids[1] === perms.kids[1], false);
  reply = { ok: false, status: 400, doc: { error: "session 'cx1' already runs" } };
  await button(box2).handlers.click[0]();
  check("a refusal is shown as the daemon worded it",
        text(box2).includes("already runs"), true);

  /* ---- a session carrying the refused --approval-mode group ----
     Read the way the daemon launches it: not a mode group, so the bypass
     default stands beside it — the same answer as codex_mode.runtime_state,
     and the Sandbox button offers a write the daemon does not refuse as a
     no-op (s129-rev). */
  const legacy = { session: { name: "cx2", harness: "codex",
                              args: ["--approval-mode", "full-auto"] } };
  check("the refused group alone reads as YOLO without the sandbox",
        button(ctx.sandbox(legacy)).textContent, "Turn sandbox on");

  /* ---- a harness without the pair ---- */
  const claude = { session: { name: "cl1", harness: "claude", args: [] } };
  check("claude gets no Sandbox button",
        [text(ctx.sandbox(claude)).includes("declares no sandbox switch"),
         !!button(ctx.sandbox(claude))], [true, false]);
  currentName = "other";
  reply = { ok: true, status: 200, doc: { args: ["--dangerously-skip-permissions"] } };
  const cperms = ctx.perms(claude);
  await button(cperms).handlers.click[0]();
  check("claude's Permissions keeps the skip-permissions route",
        posts.pop(), { url: "/api/sessions/cl1/skip-permissions",
                       body: { skip: true } });

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("sandboxpanel_check: ok");
}
main();
