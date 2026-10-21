/* The Settings card for daemon.test_output_dir (claunch-pyv3d.1), run
   against stub nodes.

   GET /api/test-output-dir answers the saved setting ("" when unset) and
   the root it resolves to now; there is no PUT — the card is read-only.
   What is checked: the card reads "reading…" before the first answer; an
   empty setting shows the default-root note, a set value shows the saved
   line, both show the resolved "Root now:" line; a failed GET shows the
   error; the card never writes. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

function node(tag, cls, text) {
  return {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], on: {}, checked: false, disabled: false, id: "", type: "",
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { for (const c of cs) this.children.push(c); },
    addEventListener(ev, fn) { (this.on[ev] = this.on[ev] || []).push(fn); },
  };
}

function texts(card) {
  return card.children.filter((c) => c.tag === "p").map((c) => c.text);
}

function build(answers) {
  const calls = [];
  let renders = 0;
  const api = async (url, opts) => {
    calls.push({ url, method: (opts && opts.method) || "GET",
                 body: opts && opts.body ? JSON.parse(opts.body) : null });
    const next = answers.shift();
    return { ok: next.status === 200, status: next.status, json: async () => next.body };
  };
  const code = slice("/* Test output directory (claunch-pyv3d.1)",
                     "/* Delegated child session (claunch-yl0to)");
  const w = new Function(
    "el", "document", "api", "renderWorkspaces", "wsOpen",
    code + "\nreturn { testOutputDir, testOutputDirCard, refreshTestOutputDir };")(
    node, { createElement: (tag) => node(tag) }, api, () => { renders += 1; }, true);
  return { w, calls, renders: () => renders };
}

(async () => {
  {
    const { w } = build([]);
    const card = w.testOutputDirCard();
    check("before the first answer the card is reading", texts(card).includes("reading…"), texts(card));
    check("before the first answer no root line", !texts(card).some((t) => t.startsWith("Root now:")), texts(card));
  }

  {
    const { w, calls } = build([
      { status: 200, body: { configured: "", resolved: "/home/u/.claude-launcher/basetemp" } },
    ]);
    await w.refreshTestOutputDir();
    check("refresh is a GET of the setting", calls[0].url === "/api/test-output-dir" && calls[0].method === "GET", calls);
    const card = w.testOutputDirCard();
    check("unset: the card says the default root is in use",
          texts(card).includes("daemon.test_output_dir: (not set — the built-in default root)"), texts(card));
    check("unset: the Root now line shows the resolved default",
          texts(card).includes("Root now: /home/u/.claude-launcher/basetemp"), texts(card));
  }

  {
    const { w, calls } = build([
      { status: 200, body: { configured: "/data/test-output", resolved: "/data/test-output" } },
    ]);
    await w.refreshTestOutputDir();
    const card = w.testOutputDirCard();
    check("set: the card shows the saved value",
          texts(card).includes("daemon.test_output_dir: /data/test-output"), texts(card));
    check("set: the Root now line shows the resolved root",
          texts(card).includes("Root now: /data/test-output"), texts(card));
    check("the card never writes", calls.every((c) => c.method === "GET"), calls);
  }

  {
    const { w } = build([{ status: 500, body: { error: "config unreadable" } }]);
    await w.refreshTestOutputDir();
    const card = w.testOutputDirCard();
    check("a failed GET shows the error",
          card.children.some((c) => c.cls === "error" && c.text.includes("config unreadable")),
          card.children.map((c) => c.text));
    check("a failed GET shows no root line", !texts(card).some((t) => t.startsWith("Root now:")), texts(card));
  }

  if (failures) {
    console.log(`testoutputdir_check: ${failures} failure(s)`);
    process.exit(1);
  }
  console.log("testoutputdir_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
