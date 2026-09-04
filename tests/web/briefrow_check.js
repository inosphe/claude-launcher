/* The briefing's always-on rail surface: the one-line job description and
   the collapsed ⟳.

   briefcard_check holds the ▸ toggle + card and briefingtop_check the header
   toggle + detail section; this holds what a row says whether folded or not.
   Each row carries a persistent one-line (the digest that rides the
   /api/sessions poll — so a browser refresh repaints it from the daemon's
   session state instead of regenerating) and nothing else: the recorded
   opening task is drawn only in the detail panel (sesstask_check), never as
   the row's summary. The row's ⟳ refreshes the summary from the collapsed
   state: it re-asks the daemon, bypassing the cache, and does not open the
   card. Off (no llm: block) the ⟳ goes inert, its tooltip pointing at the
   config, while the digest one-line still shows. */
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
  Object.defineProperty(node, "classList", {
    value: {
      toggle(c, force) {
        const has = node.className.split(" ").includes(c);
        if (force === undefined) force = !has;
        if (force && !has) node.className = (node.className + " " + c).trim();
        else if (!force) node.className =
          node.className.split(" ").filter((x) => x !== c).join(" ");
        return force;
      },
    },
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
  [slice("el"), slice("fmtAge"), slice("seenAgo"), slice("briefingStateClass"),
   slice("sessionStatusChecks"), slice("statusCheckText"), slice("statusCheckIcon"),
   slice("statusCheckName"), slice("statusCheckRefreshState"),
   slice("paintStatusCheckRefresh"), slice("requestStatusChecksRefresh"),
   slice("fetchBriefing"), slice("refreshBriefingRow"),
   slice("toggleBriefing"), slice("renderBriefingCard"),
   slice("applyBriefingTop"), slice("applyBriefingCards"),
   slice("decorateBriefingRow"), slice("syncRowRefresh")].join("\n") + `
const briefingOpen = new Set();
const briefingCache = new Map();
const sessionsCache = [];
const statusCheckRefreshes = new Map();
const refreshSessions = async () => {};
let briefingLLM = true;
exports.decorate = decorateBriefingRow;
exports.setLLM = (v) => { briefingLLM = v; };
exports.openSet = briefingOpen;
exports.statusSessions = sessionsCache;
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
const ago = (secs) => new Date(Date.now() - secs * 1000).toISOString();

(async () => {
  /* A digest one-line goes on the row, folded; the recorded opening task is
     never the row's summary — it belongs to the detail panel. */
  const s1 = row("s1");
  ctx.decorate(s1, { name: "s1", briefing: { one_line: "한 줄", state: "working" }, task: "테스크" });
  check("the digest one-line is on the row, always visible",
        [oneLine(s1).textContent, oneLine(s1).children.length], ["한 줄", 0]);
  check("no card before anyone opens it", s1.querySelector(".sess-brief"), null);

  const s2 = row("s2");
  ctx.decorate(s2, { name: "s2", task: "개설 태스크" });
  check("no digest: no one-line at all, the task untold",
        oneLine(s2), null);

  const s3 = row("s3");
  ctx.decorate(s3, { name: "s3" });
  check("neither digest nor task recorded: no one-line at all", oneLine(s3), null);

  const sChecks = row("schecks");
  const testsReportedAt = ago(300);
  ctx.decorate(sChecks, { name: "schecks", status_checks: [
    { id: "tests", name: "Tests", question: "Tests passed?", answer: "yes", reported_at: testsReportedAt },
    { id: "merged", name: "Merged", question: "Merged?" },
  ] });
  const checkChips = sChecks.querySelector(".rail-status-checks").children;
  check("reported and unreported status checks are compact chips, the " +
        "reported one carrying how long ago that was",
        [checkChips[0].textContent, checkChips[0].className,
         checkChips[1].textContent, checkChips[1].className],
        ["✓ Tests · 5m", "rail-status-check check-yes",
         "• Merged", "rail-status-check check-unknown"]);
  check("a status-check chip keeps the agent question and the report's " +
        "absolute time on hover",
        checkChips[0].title,
        `Tests passed?\nreported ${new Date(Date.parse(testsReportedAt)).toLocaleString()}`);
  check("an unreported check has no age to show, so its hover stays the question alone",
        checkChips[1].title, "Merged?");
  check("status checks carry an independent agent refresh control",
        sChecks.querySelector(".sess-status-check-rowref").textContent, "checks ⟳");
  ctx.statusSessions.push({ name: "schecks", status_checks: [
    { id: "tests", name: "Tests", question: "Tests passed?", answer: "yes", reported_at: "2026-09-01T00:00:00+00:00" },
    { id: "merged", name: "Merged", question: "Merged?" },
  ] });
  answer = { status: 200, body: { delivered: true, checks: [] } };
  const statusRefresh = sChecks.querySelector(".sess-status-check-rowref");
  click(statusRefresh);
  check("a status-check refresh immediately spins and names the pending request",
        [statusRefresh.className, statusRefresh.textContent, statusRefresh.disabled],
        ["sess-status-check-rowref requesting", "checking…", true]);
  await flush();
  check("delivery leaves a visible waiting state until the agent reports",
        [statusRefresh.className, statusRefresh.textContent, statusRefresh.disabled],
        ["sess-status-check-rowref waiting", "checks · waiting", false]);
  ctx.statusSessions[0].status_checks[0].reported_at = "2026-09-01T00:01:00+00:00";
  ctx.statusSessions[0].status_checks[1].reported_at = "2026-09-01T00:01:00+00:00";
  ctx.decorate(sChecks, ctx.statusSessions[0]);
  check("a later report turns the refresh into a completed state",
        [statusRefresh.className, statusRefresh.textContent],
        ["sess-status-check-rowref updated", "checks ✓"]);
  calls.length = 0;

  /* The row carries the WHOLE digest, however long — the stylesheet wraps
     it (raillayout_check pins that) and nothing here may shorten it first.
     A cut made in JS would be the worse half of the same bug: invisible to
     the CSS check, and unrecoverable, because the element's title is a fixed
     label rather than the text. The digest is a briefing paragraph, newlines
     and all, so its own line breaks have to survive the trip too. The task
     is not a case at all: however long, it never reaches the row. */
  const long = "긴 요약: " + "여러 줄로 접혀야 하는 문장. ".repeat(12)
    + "\n두 번째 줄 — F:\\works\\claude-launcher\\.claude\\worktrees\\s82-railbrief-full";
  const s5 = row("s5");
  ctx.decorate(s5, { name: "s5", task: long });
  check("a long recorded task is still no one-line", oneLine(s5), null);
  const s6 = row("s6");
  ctx.decorate(s6, { name: "s6", briefing: { one_line: long, state: "working" }, task: "짧은 태스크" });
  check("a long digest one-line is not shortened either",
        oneLine(s6).textContent, long);

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
  /* With the card closed, the ⟳ itself is the only place the row can say
     the click was taken: it spins, goes inert against a double-click, and
     says so on its tooltip — until the answer lands. */
  const spinning = (n) => n.className.split(" ").includes("spinning");
  check("in flight, the collapsed ⟳ spins and is inert",
        [spinning(refresh(s1)), refresh(s1).disabled, refresh(s1).title],
        [true, true, "summarising…"]);
  check("the old one-line stays up meanwhile", oneLine(s1).textContent, "한 줄");
  await flush();
  check("landed: the ⟳ stops and is live again",
        [spinning(refresh(s1)), refresh(s1).disabled, refresh(s1).title],
        [false, false, "refresh the summary without opening it"]);
  /* ...and the fresh one-line is on the row at once, not after the next
     poll — the spin stopping and the text changing are one event. */
  check("the refreshed one-line is painted without waiting for the poll",
        oneLine(s1).textContent, "새 한 줄");

  /* A failed refresh: the glyph says so (class + reason on the tooltip) and
     stays clickable, because it is also the retry. The one-line keeps the
     last good text. */
  answer = { status: 500, body: { error: "llm timed out" } };
  click(refresh(s1));
  await flush();
  const failed = (n) => n.className.split(" ").includes("failed");
  check("a failed refresh marks the ⟳ and keeps it clickable",
        [failed(refresh(s1)), spinning(refresh(s1)), refresh(s1).disabled, refresh(s1).title],
        [true, false, false, "briefing failed: llm timed out — click to retry"]);
  check("the one-line keeps the last good text after a failure",
        oneLine(s1).textContent, "새 한 줄");
  /* The poll rebuilds the row; the failure mark must survive it (it is
     read off the cache, not the element). */
  ctx.decorate(s1, { name: "s1", briefing: { one_line: "새 한 줄", state: "working" }, task: "테스크" });
  check("the failure mark survives a poll rebuild", failed(refresh(s1)), true);
  answer = { status: 200, body: {
    session: "s1", generated_at: new Date().toISOString(), cached: false,
    source: { jsonl: true, cflow: false },
    briefing: { goal: "g", now: "n", state: "working",
                "one-line-job-description": "새 한 줄" },
    raw: null,
  } };
  click(refresh(s1));
  await flush();
  check("a retry that succeeds clears the mark", failed(refresh(s1)), false);

  /* Off (no llm: block): the ⟳ is inert with the config tooltip; the
     digest one-line (a fact poured by the /api/sessions poll, not the LLM)
     still shows — and a recorded task still never does. */
  ctx.setLLM(false);
  const s4 = row("s4");
  ctx.decorate(s4, { name: "s4", task: "로컬 태스크" });
  check("off, the ⟳ is disabled and points at the config",
        [refresh(s4).disabled, refresh(s4).title],
        [true, "briefing off — set the llm section (endpoint, model, api_key)"
          + " in ~/.claunch.yaml to enable"]);
  check("off, a task is still no one-line", oneLine(s4), null);
  const s7 = row("s7");
  ctx.decorate(s7, { name: "s7", briefing: { one_line: "뽑아온 줄", state: "working" } });
  check("the digest one-line is not gated on the llm",
        oneLine(s7).textContent, "뽑아온 줄");
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
