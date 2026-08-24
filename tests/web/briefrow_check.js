/* The briefing's always-on rail surface: the one-line job description and
   the collapsed ⟳.

   briefcard_check holds the ▸ toggle + card and briefingtop_check the header
   toggle + detail section; this holds what a row says whether folded or not.
   Each row carries a persistent one-line (the digest that rides the
   /api/sessions poll — so a browser refresh repaints it from the daemon's
   session state instead of regenerating), falling back to the recorded
   opening task until a briefing exists. The row's ⟳ refreshes the summary
   from the collapsed state: it re-asks the daemon, bypassing the cache, and
   does not open the card. Off (no llm: block) the ⟳ goes inert, its tooltip
   pointing at the config, while the task-line still shows. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  let start = src.indexOf(`async function ${name}(`);
  if (start < 0) start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* ---- stub DOM (from briefcard_check) ---- */
function mkel(tag) {
  const node = {
    tag, className: "", title: "", dataset: {}, children: [],
    listeners: {}, parent: null, disabled: false,
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    append(...cs) { for (const c of cs) this.appendChild(c); },
    remove() {
      if (this.parent) {
        this.parent.children.splice(this.parent.children.indexOf(this), 1);
      }
    },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    querySelector(sel) {
      const cls = sel.slice(1);
      const walk = (n) => {
        for (const c of n.children) {
          if (c.className.split(" ").includes(cls)) return c;
          const hit = walk(c);
          if (hit) return hit;
        }
        return null;
      };
      return walk(this);
    },
    querySelectorAll(sel) {
      if (sel === "li[data-name]") {
        return this.children.filter((c) => c.tag === "li" && c.dataset.name);
      }
      throw new Error(`unexpected selector ${sel}`);
    },
  };
  let text = "";
  Object.defineProperty(node, "textContent", {
    get() { return text; },
    set(v) { text = v; node.children.length = 0; },
  });
  return node;
}

const list = mkel("ul");
function row(name) {
  const li = mkel("li");
  li.dataset.name = name;
  list.appendChild(li);
  return li;
}

/* ---- stub API: every call is recorded, the next answer is scripted ---- */
const calls = [];
let answer = { status: 200, body: null };
async function api(p) {
  calls.push(p);
  const a = answer;
  return { ok: a.status === 200, status: a.status, json: async () => a.body };
}
const flush = () => new Promise((r) => setImmediate(r));

const ctx = {};
const noChip = () => null;
new Function(
  "exports", "$", "document", "api", "ctxChip",
  [slice("el"), slice("fmtAge"), slice("briefingStateClass"),
   slice("fetchBriefing"), slice("refreshBriefingRow"),
   slice("toggleBriefing"), slice("renderBriefingCard"),
   slice("applyBriefingTop"), slice("applyBriefingCards"),
   slice("decorateBriefingRow")].join("\n") + `
const briefingOpen = new Set();
const briefingCache = new Map();
let briefingLLM = true;
exports.decorate = decorateBriefingRow;
exports.setLLM = (v) => { briefingLLM = v; };
exports.openSet = briefingOpen;
`)(ctx, (id) => (id === "session-list" ? list : null),
   { createElement: mkel }, api, noChip);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const oneLine = (li) => li.querySelector(".rail-brief");
const refresh = (li) => li.querySelector(".sess-brief-rowref");
const click = (n) => n.listeners.click({ stopPropagation() {} });

(async () => {
  /* A digest one-line goes on the row, folded; the task is the fallback. */
  const s1 = row("s1");
  ctx.decorate(s1, { name: "s1", briefing: { one_line: "한 줄", state: "working" }, task: "테스크" });
  check("the digest one-line is on the row, always visible",
        [oneLine(s1).textContent, oneLine(s1).children.length], ["한 줄", 0]);
  check("no card before anyone opens it", s1.querySelector(".sess-brief"), null);

  const s2 = row("s2");
  ctx.decorate(s2, { name: "s2", task: "개설 태스크" });
  check("no digest yet: the recorded task is the one-line",
        oneLine(s2).textContent, "개설 태스크");

  const s3 = row("s3");
  ctx.decorate(s3, { name: "s3" });
  check("neither digest nor task: no one-line at all", oneLine(s3), null);

  /* The collapsed ⟳ refreshes without opening: asks, does not fold in. */
  answer = { status: 200, body: {
    session: "s1", generated_at: new Date().toISOString(), cached: true,
    source: { jsonl: true, cflow: false },
    briefing: { goal: "g", now: "n", state: "working",
                "one-line-job-description": "새 한 줄" },
    raw: null,
  } };
  click(refresh(s1));
  check("the collapsed ⟳ re-asks, uncached", calls, ["/api/sessions/s1/briefing?refresh=1"]);
  check("it never opens the card", [ctx.openSet.has("s1"),
        s1.querySelector(".sess-brief")], [false, null]);
  await flush();

  /* Off (no llm: block): the ⟳ is inert with the config tooltip; the
     task-line (a local fact) still shows. */
  ctx.setLLM(false);
  const s4 = row("s4");
  ctx.decorate(s4, { name: "s4", task: "로컬 태스크" });
  check("off, the ⟳ is disabled and points at the config",
        [refresh(s4).disabled, refresh(s4).title],
        [true, "briefing off — set the llm section (endpoint, model, api_key)"
          + " in ~/.claunch.yaml to enable"]);
  check("the task-line is not gated on the llm", oneLine(s4).textContent, "로컬 태스크");
  ctx.setLLM(true);

  /* A row rebuilt by the poll keeps its one-line and ⟳ (decorate is
     stateless — re-running over the same li must not duplicate either). */
  ctx.decorate(s1, { name: "s1", briefing: { one_line: "한 줄", state: "working" }, task: "테스크" });
  ctx.decorate(s1, { name: "s1", briefing: { one_line: "한 줄", state: "working" }, task: "테스크" });
  const onLine = s1.children.filter((c) => c.className.split(" ").includes("rail-brief"));
  const refs = s1.children.filter((c) => c.className.split(" ").includes("sess-brief-rowref"));
  check("idempotent: one one-line and one ⟳ after repeated decoration",
        [onLine.length, refs.length], [1, 1]);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("briefrow_check: ok");
})();
