/* Claude's auto-compact switch and threshold, as the create form, the spawn
   form and the detail panel speak them (claunch-5iq22).

   Both answers are two keys of claude's env — DISABLE_AUTO_COMPACT and
   CLAUDE_CODE_AUTO_COMPACT_WINDOW — so what is pinned here is the
   translation each surface makes:

   - the env helpers read the switch the way claude does (1/true/yes/on, and
     DISABLE_COMPACT as well) and only a positive integer as a threshold;
   - a standalone Create sends nothing while the rows say "on, the profile's
     threshold", the switch alone when off, the threshold when typed;
   - a spawn's rows start on the PARENT's env, including a parent that runs
     with auto-compact off;
   - the detail panel's form shows the session's own answer, posts
     {enabled, window} to /auto-compact and reattaches the relaunched PTY,
     and says plainly that another harness has no such switch. */
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
const constLine = (name) => src.match(new RegExp(`const ${name} = [^\\n]*;`))[0];

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
    value: "", checked: false, disabled: false, title: "", type: "",
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

const posts = [];
let reply = { ok: true, status: 200, doc: {} };
const events = [];
const ctx = {};
const boxes = { "new-claude-compact-hint": node("p") };
new Function(
  "exports", "document", "$", "api", "detach", "refreshSessions", "attach",
  [
    "let sessCompactBox = null;",
    "let currentName = 's1';",
    constLine("AUTO_COMPACT_OFF_ENV"), constLine("COMPACT_OFF_ENV"),
    constLine("COMPACT_WINDOW_ENV"),
    slice("el"), slice("envAutoCompactOff"), slice("envCompactWindow"),
    slice("newCompactWindow"), slice("newClaudeCompactEnv"),
    slice("renderNewClaudeCompact"), slice("newClaudeCompactOverride"),
    slice("sessCompact"),
  ].join("\n") + `
exports.off = envAutoCompactOff;
exports.window = envCompactWindow;
exports.createEnv = newClaudeCompactEnv;
exports.render = renderNewClaudeCompact;
exports.override = newClaudeCompactOverride;
exports.panel = sessCompact;
`)(
  ctx,
  { createElement: node },
  (id) => boxes[id] || null,
  async (url, init) => {
    posts.push({ url, body: JSON.parse(init.body) });
    return { ok: reply.ok, status: reply.status, json: async () => reply.doc };
  },
  () => events.push("detach"),
  async () => events.push("refresh"),
  (name) => events.push(`attach:${name}`),
);

/* ---- the env, read the way claude reads it ---- */
check("the switch reads claude's truthy spellings, and DISABLE_COMPACT too",
      [ctx.off({ DISABLE_AUTO_COMPACT: "1" }), ctx.off({ DISABLE_AUTO_COMPACT: "TRUE" }),
       ctx.off({ DISABLE_COMPACT: "yes" }), ctx.off({ DISABLE_AUTO_COMPACT: "0" }),
       ctx.off({}), ctx.off(undefined)],
      [true, true, true, false, false, false]);
check("a threshold is a positive whole number, otherwise none",
      [ctx.window({ CLAUDE_CODE_AUTO_COMPACT_WINDOW: "200000" }),
       ctx.window({ CLAUDE_CODE_AUTO_COMPACT_WINDOW: "-5" }),
       ctx.window({ CLAUDE_CODE_AUTO_COMPACT_WINDOW: "1.5" }), ctx.window({})],
      [200000, 0, 0, 0]);

/* ---- a standalone Create ---- */
const form = () => ({
  auto_compact: node("input"), auto_compact_window: node("input"),
});
const f = form();
f.auto_compact.checked = true;
check("on, the profile's threshold: nothing is sent", ctx.createEnv(f), {});
f.auto_compact_window.value = "300000";
check("a typed threshold is pinned",
      ctx.createEnv(f), { CLAUDE_CODE_AUTO_COMPACT_WINDOW: "300000" });
f.auto_compact.checked = false;
check("off sends the switch alone — a threshold nothing compacts at is dropped",
      ctx.createEnv(f), { DISABLE_AUTO_COMPACT: "1" });
f.auto_compact_window.value = "abc";
f.auto_compact.checked = true;
check("a threshold that is not a number is not sent", ctx.createEnv(f), {});

/* ---- a spawn starts on the parent's env ---- */
const child = form();
const parent = { name: "lead", harness: "claude",
                 env: { DISABLE_AUTO_COMPACT: "1" } };
ctx.render(child, "claude", parent);
check("a child of a parent running with auto-compact off starts off",
      [child.auto_compact.checked, child.auto_compact_window.disabled,
       ctx.override(child)], [false, true, undefined]);
check("...and its hint says whose answer it is",
      boxes["new-claude-compact-hint"].textContent,
      "auto-compact disabled (DISABLE_AUTO_COMPACT=1) — /compact still works" +
      " · inherited from lead");
child.auto_compact.checked = true;
ctx.render(child, "claude", parent);
check("turning it back on is the child's own answer",
      ctx.override(child), { enabled: true, window: null });
const pinned = form();
ctx.render(pinned, "claude", { name: "p2", harness: "claude",
  env: { CLAUDE_CODE_AUTO_COMPACT_WINDOW: "120000" } });
check("a parent's pinned threshold is the child's starting value",
      [pinned.auto_compact.checked, pinned.auto_compact_window.value,
       ctx.override(pinned)], [true, "120000", undefined]);
const other = form();
ctx.render(other, "claude", { name: "p3", harness: "codex",
  env: { DISABLE_AUTO_COMPACT: "1" } });
check("another harness's env is not a claude child's answer",
      other.auto_compact.checked, true);
ctx.render(other, "claude", null);
check("leaving the spawn puts the standalone default back",
      [other.auto_compact.checked, other.auto_compact_window.value], [true, ""]);

/* ---- the detail panel ---- */
async function main() {
  const off = ctx.panel({ session: { name: "s1", harness: "claude",
    env: { DISABLE_AUTO_COMPACT: "1" } } });
  check("the panel names the session's own answer",
        text(off).includes("auto-compact is off (DISABLE_AUTO_COMPACT=1)"), true);
  const inputs = all(off).filter((n) => n.tag === "input");
  const [toggle, threshold] = inputs;
  check("...with the switch unticked and the threshold greyed",
        [toggle.checked, threshold.disabled], [false, true]);
  check("the panel is kept across a poll that changes nothing",
        ctx.panel({ session: { name: "s1", harness: "claude",
          env: { DISABLE_AUTO_COMPACT: "1" } } }).kids[1], off.kids[1]);

  toggle.checked = true;
  toggle.handlers.change.forEach((fn) => fn());
  threshold.value = "250000";
  const btn = all(off).find((n) => n.tag === "button");
  reply = { ok: true, status: 200,
            doc: { env: { CLAUDE_CODE_AUTO_COMPACT_WINDOW: "250000" } } };
  await btn.handlers.click[0]();
  check("applying posts the pair to the restart route",
        posts, [{ url: "/api/sessions/s1/auto-compact",
                  body: { enabled: true, window: 250000 } }]);
  check("...and reattaches the relaunched session",
        events, ["detach", "refresh", "attach:s1"]);
  check("...and says what it restarted into",
        text(off).includes("restarted — compacting at 250000 tokens"), true);

  threshold.value = "-3";
  await btn.handlers.click[0]();
  check("a threshold that is not a positive whole number posts nothing",
        [posts.length, text(off).includes("positive whole number")], [1, true]);

  threshold.value = "";
  reply = { ok: false, status: 400,
            doc: { error: "session 's1' already runs with that auto-compact setting" } };
  await btn.handlers.click[0]();
  check("a refusal is shown as the daemon worded it",
        text(off).includes("already runs with that auto-compact setting"), true);
  check("...with the threshold left empty sent as the profile's",
        posts[1].body, { enabled: true, window: null });

  const codex = ctx.panel({ session: { name: "c1", harness: "codex", env: {} } });
  check("another harness is told it has no such switch",
        [text(codex).includes("auto-compact is claude's switch"),
         all(codex).some((n) => n.tag === "button")], [true, false]);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("autocompact_check: ok");
}
main();
