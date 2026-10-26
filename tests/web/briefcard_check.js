/* The rail's briefing card, run against the real functions from app.js.

   An EXPANDED rail row carries a card summarising the session, produced by
   the daemon's LLM. There is no fold any more (app.js railCardMode): a card
   is expanded because the reader is on it — the terminal on screen, the
   detail panel's subject, a pinned session — and a card at another density
   carries no briefing at all.

   The rail's copy of the card is the SUMMARY alone: the one-line job
   description, with the goal / now / progress rows, the faq and the chips
   left off (renderBriefingCard's `rail` option). All of that is in the
   session's own body, which the reader is two inches from, and the chips are
   drawn on the row itself at every density. The full card still exists for
   the surfaces that have the room and exist to be READ — the detail panel and
   the terminal's top pane — and the second half of this file checks that the
   option did not take it away from them.

   What has to hold: opening an expanded card never requests a summary;
   its empty state offers an enabled ⟳ control; the ⟳ requests
   with refresh=1 while keeping the stale text readable underneath; the card
   tells apart the daemon's four answers (a briefing, prose that missed the
   shape, "no LLM configured", "no record"); blocked/waiting wear the loud
   classes while an unrecognised state stays neutral; and the card survives
   the row rebuild every poll performs, because the cache lives outside the
   rows. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

/* Module-level constants the sliced functions read. Taken from app.js instead
   of retyped, so what runs here is the string the page really carries; the
   expectations below still spell it out, which is what pins the wording. */
function sliceConst(name) {
  const match = src.match(new RegExp(`const ${name} =[\\s\\S]*?;`));
  if (!match) throw new Error(`cannot locate ${name} in app.js`);
  return match[0];
}

function slice(name) {
  // an async function must keep its `async` — the body awaits
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

/* ---- stub DOM ---- */
function mkel(tag) {
  const node = {
    tag, className: "", title: "", dataset: {}, children: [],
    listeners: {}, parent: null, disabled: false,
    appendChild(c) { c.remove(); this.children.push(c); c.parent = this; return c; },
    get firstChild() { return this.children[0] || null; },
    insertBefore(c, before) {
      c.remove();
      const i = before ? this.children.indexOf(before) : this.children.length;
      this.children.splice(i, 0, c); c.parent = this; return c;
    },
    append(...cs) { for (const c of cs) this.appendChild(c); },
    remove() {
      if (this.parent) {
        this.parent.children.splice(this.parent.children.indexOf(this), 1);
      }
    },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    // class selectors only, and depth-first like the real thing — the card's
    // pieces sit two levels down
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
/* A rail row as refreshSessions builds one: the density is an attribute the
   stylesheet and applyBriefingCards both read (app.js railCardMode). `expanded`
   is the card the reader is on, which is the only one that draws a briefing. */
function row(name, mode = "expanded") {
  const li = mkel("li");
  li.dataset.name = name;
  li.dataset.railMode = mode;
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
const flush = () => new Promise((r) => setImmediate(r));  // let a fetch settle

const ctx = {};
/* The head also carries a context chip; that fact has its own harness
   (ctxsize_check), so here it is stubbed out to keep this one about the
   briefing. */
const noChip = () => null;
new Function(
  "exports", "$", "document", "api", "ctxChip",
  [sliceConst("BRIEFING_OFF_HINT"), slice("el"), slice("fmtAge"), slice("briefingStateClass"),
   slice("sessionStatusChecks"), slice("statusCheckText"), slice("statusCheckIcon"),
   slice("statusCheckName"), slice("statusCheckRefreshState"),
   slice("paintStatusCheckRefresh"), slice("appendStatusChecks"),
   slice("fetchBriefing"),
   slice("renderBriefingCard"), slice("applyBriefingTop"),
   slice("applyBriefingCards"), slice("syncRowRefresh")].join("\n") + `
const briefingOpen = new Set();
const briefingCache = new Map();
const sessionsCache = [];
const statusCheckRefreshes = new Map();
let briefingLLM = true;
exports.apply = applyBriefingCards;
exports.setLLM = (v) => { briefingLLM = v; };
exports.statusSessions = sessionsCache;
/* The same builder without the rail's trim — what the detail panel and the
   terminal's top pane draw. Exposed rather than rebuilt here so the checks
   below run the shipped function, not a copy of it. */
exports.railCard = (entry, rowSummary) => renderBriefingCard("s1", entry, {rail: true, rowSummary});
exports.fullCard = (name) => renderBriefingCard(name, briefingCache.get(name));
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


const card = (li) => li.querySelector(".sess-brief");
const note = (li) => li.querySelector(".sess-brief-note");
const one = (li) => card(li).querySelector(".sess-brief-one");
const click = (n) => n.listeners.click({ stopPropagation() {} });
/* A card's labelled rows, read off the card itself so the same helper works
   for the rail's copy and the full one. */
function kvOf(cardNode) {
  const out = {};
  if (!cardNode) return out;
  for (const r of cardNode.children) {
    if (r.className !== "sess-brief-row") continue;
    out[r.children[0].textContent] = r.children[1].textContent;
  }
  return out;
}
const kv = (li) => kvOf(card(li));

(async () => {
  const rows = { s1: row("s1"), s2: row("s2") };
  ctx.statusSessions.push({ name: "s1", status_checks: [
    { id: "tests", name: "Tests", question: "Did the targeted tests pass?", answer: "yes" },
  ] });

  /* Opening a card is a read action, including a never-summarised session. */
  answer = { status: 200, body: {
    session: "s1", generated_at: new Date(Date.now() - 90_000).toISOString(),
    cached: false, source: { jsonl: true, cflow: true },
    briefing: { "one-line-job-description": "착지를 관리한다",
                goal: "ship the card", now: "wiring CSS",
                state: "working", progress: "2 of 3 pieces done" },
    raw: null,
  } };
  for (const summary of ["", "   ", "existing job summary"]) {
    const rendered = ctx.railCard(null, summary);
    check("empty notice follows row summary presence: " + summary,
      !!rendered.querySelector(".sess-brief-note"), !summary.trim());
    check("refresh remains available", !!rendered.querySelector(".sess-brief-refresh"), true);
  }
  for (const [phase, expected] of [["loading", "summarising…"], ["error", "failed"]]) {
    check("row summary preserves " + phase,
      ctx.railCard({phase, error: "failed"}, "existing").querySelector(".sess-brief-note").textContent,
      expected);
  }
  check("full card still explains missing detail", !!ctx.fullCard("missing").querySelector(".sess-brief-note"), true);
  ctx.apply();
  check("opening uncached expanded rows makes no request", calls, []);
  check("the empty card explains the explicit action", note(rows.s1).textContent,
        "No summary yet");
  check("the empty card has an enabled refresh", card(rows.s1).querySelector(".sess-brief-refresh").disabled, false);
  ctx.apply();
  check("repainting empty rows still makes no request", calls, []);
  click(card(rows.s1).querySelector(".sess-brief-refresh"));
  check("an explicit request starts summarising", note(rows.s1).textContent, "summarising…");
  check("the explicit action uses refresh=1", calls, ["/api/sessions/s1/briefing?refresh=1"]);
  await flush();
  /* The rail's card is the summary. The rows the daemon sent back for goal /
     now / progress are dropped HERE and kept for the surfaces below — so the
     two assertions together say the option trims rather than that the data
     went missing. */
  check("the rail card carries the summary line", one(rows.s1).textContent,
        "착지를 관리한다");
  check("...and no goal / now / progress rows", kv(rows.s1), {});
  check("...and no chips — the row draws those at every density",
        card(rows.s1).querySelector(".status-check-icon"), null);
  const full = ctx.fullCard("s1");
  check("the same card outside the rail keeps goal, now and progress", kvOf(full),
        { goal: "ship the card", now: "wiring CSS",
          progress: "2 of 3 pieces done" });
  const statusIcon = full.querySelector(".status-check-icon");
  check("a briefing status check shows its named icon and keeps its sentence on hover",
        [statusIcon.textContent, statusIcon.className, statusIcon.title,
         statusIcon.parent.children[1].textContent],
        ["✓", "status-check-icon check-yes", "Did the targeted tests pass?", "Tests"]);
  const pill = card(rows.s1).querySelector(".sess-brief-state");
  check("the state still rides the rail card's head as a working-coloured pill",
        [pill.textContent, pill.className],
        ["working", "sess-brief-state st-working"]);
  check("the answer's age is said in words",
        card(rows.s1).querySelector(".sess-brief-age").textContent, "1m ago");

  // A row with no digest still needs the card title (checked above). Once
  // the digest exists, the same title is rendered once, on that row.
  const digest = mkel("div");
  digest.className = "rail-brief";
  digest.textContent = "착지를 관리한다";
  rows.s1.appendChild(digest);
  ctx.apply();
  check("a row digest suppresses the identical card title", one(rows.s1), null);
  check("the full card retains its title", ctx.fullCard("s1").querySelector(".sess-brief-one").textContent,
        "착지를 관리한다");
  digest.remove();
  ctx.apply();

  /* A card at another density carries none: the compact card says what the
     session is doing with the one-line it already has, and the one-line
     roles have a single line of their own. Moving the row's density is what
     opens and closes it, and that is the rail's attribute, not a button. */
  ctx.apply();
  rows.s1.dataset.railMode = "compact";
  ctx.apply();
  check("a compact row carries no card", card(rows.s1), null);
  rows.s1.dataset.railMode = "min";
  ctx.apply();
  check("a one-line row carries no card either", card(rows.s1), null);
  rows.s1.dataset.railMode = "expanded";
  const before = calls.length;
  ctx.apply();
  check("going back to expanded draws the card from cache, asking nothing new",
        [calls.length, one(rows.s1).textContent], [before, "착지를 관리한다"]);

  /* ⟳ re-asks with refresh=1; the stale text stays readable, dimmed, and
     the button spins so a second click can't stack a second request. */
  answer = { status: 200, body: {
    session: "s1", generated_at: new Date().toISOString(), cached: true,
    source: { jsonl: true, cflow: false },
    briefing: { "one-line-job-description": "착지 상태를 검토한다",
                goal: "ship the card", now: "verifying",
                state: "blocked", progress: "waiting on review" },
    raw: null,
  } };
  click(card(rows.s1).querySelector(".sess-brief-refresh"));
  check("the refresh asks again, uncached",
        calls.at(-1), "/api/sessions/s1/briefing?refresh=1");
  check("the stale text stays readable under the refresh",
        [card(rows.s1).className, one(rows.s1).textContent],
        ["sess-brief refreshing", "착지를 관리한다"]);
  const spin = card(rows.s1).querySelector(".sess-brief-refresh");
  check("the button spins and refuses a second click while out",
        [spin.className, spin.disabled],
        ["sess-brief-refresh spinning", true]);
  await flush();
  check("blocked wears the loud class",
        card(rows.s1).querySelector(".sess-brief-state").className,
        "sess-brief-state st-blocked");
  check("a cached answer says so, next to its age",
        card(rows.s1).querySelector(".sess-brief-age").textContent,
        "0s ago · cached");

  /* The card survives the poll's row rebuild — and does NOT re-ask. */
  list.children.length = 0;
  rows.s1 = row("s1"); rows.s2 = row("s2");
  const rebuiltFrom = calls.length;
  ctx.apply();
  check("a rebuilt row gets its card back from cache",
        one(rows.s1).textContent, "착지 상태를 검토한다");
  check("the rebuild asked the daemon nothing", calls.length, rebuiltFrom);

  /* The daemon's other answers, each told apart.  Prose that missed the
     agreed shape is still shown, as it came. */
  answer = { status: 200, body: {
    session: "s2", generated_at: new Date().toISOString(), cached: false,
    source: { jsonl: true, cflow: false },
    briefing: null, raw: "three lines of\nunshaped prose",
  } };
  const s2row = row("s2b");
  ctx.apply();
  click(card(s2row).querySelector(".sess-brief-refresh"));
  await flush();
  const raw = card(s2row).querySelector(".sess-brief-raw");
  check("unshaped prose is shown as it came",
        [raw.tag, raw.textContent], ["pre", "three lines of\nunshaped prose"]);
  check("prose carries no state pill",
        card(s2row).querySelector(".sess-brief-state"), null);

  const s3 = row("s3");
  answer = { status: 400, body: { error: "llm not configured" } };
  ctx.apply();
  click(card(s3).querySelector(".sess-brief-refresh"));
  await flush();
  check("no LLM points at the config to write",
        note(s3).title,
        "no LLM configured — briefing off — pick a profile and model in "
        + "Settings ▸ Briefing model (the llm: block of ~/.claude-launcher/.claunch.yaml)");

  const s4 = row("s4");
  answer = { status: 404, body: { error: "no record" } };
  ctx.apply();
  click(card(s4).querySelector(".sess-brief-refresh"));
  await flush();
  check("a session with no record says that, not an error",
        note(s4).textContent, "no session record to summarise");

  const s5 = row("s5");
  answer = { status: 500, body: { error: "summariser fell over" } };
  ctx.apply();
  click(card(s5).querySelector(".sess-brief-refresh"));
  await flush();
  check("a real failure is reported in the daemon's words",
        [note(s5).textContent, note(s5).className],
        ["summariser fell over", "sess-brief-note error"]);
  check("a failed card still offers the ⟳",
        card(s5).querySelector(".sess-brief-refresh").disabled, false);

  /* A state outside the agreed five stays neutral. */
  const s6 = row("s6");
  answer = { status: 200, body: {
    session: "s6", generated_at: new Date().toISOString(), cached: false,
    source: { jsonl: false, cflow: true },
    briefing: { "one-line-job-description": "여섯 번째 카드",
                goal: "g", now: "n", state: "puzzled", progress: "" },
    raw: null,
  } };
  ctx.apply();
  click(card(s6).querySelector(".sess-brief-refresh"));
  await flush();
  check("an unrecognised state stays neutral",
        card(s6).querySelector(".sess-brief-state").className,
        "sess-brief-state st-other");
  /* The empty-progress rule is the full card's, where the rows are: the rail's
     copy draws none of them at all, which the s1 checks above already pin. */
  check("an empty progress grows no row",
        kvOf(ctx.fullCard("s6")), { goal: "g", now: "n" });

  /* No llm: block. The card is still drawn — the expanded row is what the
     reader is on, and it is where the missing setting is explained — but it
     says the setting's how-to rather than a summary, and no request goes
     out. */
  ctx.setLLM(false);
  ctx.apply();
  check("without an llm the expanded card explains the setting instead",
        note(s6).title,
        "briefing off — pick a profile and model in Settings ▸ Briefing model "
        + "(the llm: block of ~/.claude-launcher/.claunch.yaml)");
  const quietFrom = calls.length;
  await flush();
  check("and nothing is asked of a summariser that is not configured",
        calls.length, quietFrom);
  ctx.setLLM(true);
  ctx.apply();
  check("configuring brings the summary back from cache without a reload",
        one(s6).textContent, "여섯 번째 카드");

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("briefcard_check: ok");
})();
