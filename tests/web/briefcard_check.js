/* The rail's briefing card, run against the real functions from app.js.

   Each session row carries a toggle that folds open a card summarising the
   session — goal / now / state / progress, produced by the daemon's LLM.
   What has to hold: opening the card asks the daemon once and shows what
   came back; the ⟳ re-asks with refresh=1 while keeping the stale text
   readable underneath; the card tells apart the daemon's four answers
   (a briefing, prose that missed the shape, "no LLM configured", "no
   record"); blocked/waiting wear the loud classes while an unrecognised
   state stays neutral; and the open card survives the row rebuild every
   poll performs, because the open-set and the cache live outside the rows. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

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
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
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
const flush = () => new Promise((r) => setImmediate(r));  // let a fetch settle

const ctx = {};
/* The head also carries a context chip; that fact has its own harness
   (ctxsize_check), so here it is stubbed out to keep this one about the
   briefing. */
const noChip = () => null;
new Function(
  "exports", "$", "document", "api", "ctxChip",
  [slice("el"), slice("fmtAge"), slice("briefingStateClass"),
   slice("fetchBriefing"), slice("toggleBriefing"),
   slice("renderBriefingCard"), slice("applyBriefingTop"),
   slice("applyBriefingCards"), slice("syncRowRefresh")].join("\n") + `
const briefingOpen = new Set();
const briefingCache = new Map();
let briefingLLM = true;
exports.apply = applyBriefingCards;
exports.setLLM = (v) => { briefingLLM = v; };
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

const toggle = (li) => li.querySelector(".sess-brief-toggle");
const card = (li) => li.querySelector(".sess-brief");
const note = (li) => li.querySelector(".sess-brief-note");
const click = (n) => n.listeners.click({ stopPropagation() {} });
const kv = (li) => {
  const out = {};
  for (const r of card(li).children) {
    if (r.className !== "sess-brief-row") continue;
    out[r.children[0].textContent] = r.children[1].textContent;
  }
  return out;
};

(async () => {
  const rows = { s1: row("s1"), s2: row("s2") };

  /* Every row grows a closed toggle; nobody grows a card unasked. */
  ctx.apply();
  check("each row carries a toggle, closed",
        [toggle(rows.s1).textContent, toggle(rows.s2).textContent], ["▸", "▸"]);
  check("no card before anyone asks", card(rows.s1), null);

  /* Opening asks the daemon for this session's briefing, says so while it
     is out, and renders the four fields when it lands. */
  const iso = new Date(Date.now() - 90_000).toISOString();
  answer = { status: 200, body: {
    session: "s1", generated_at: iso, cached: false,
    source: { jsonl: true, cflow: true },
    briefing: { goal: "ship the card", now: "wiring CSS",
                state: "working", progress: "2 of 3 pieces done" },
    raw: null,
  } };
  click(toggle(rows.s1));
  check("opening asks for this session's briefing",
        calls, ["/api/sessions/s1/briefing"]);
  check("the card says it is summarising while the answer is out",
        note(rows.s1).textContent, "summarising…");
  await flush();
  check("goal, now and progress land as labelled rows", kv(rows.s1),
        { goal: "ship the card", now: "wiring CSS",
          progress: "2 of 3 pieces done" });
  const pill = card(rows.s1).querySelector(".sess-brief-state");
  check("the state rides the head as a working-coloured pill",
        [pill.textContent, pill.className],
        ["working", "sess-brief-state st-working"]);
  check("the answer's age is said in words",
        card(rows.s1).querySelector(".sess-brief-age").textContent, "1m ago");
  check("the toggle now reads open", toggle(rows.s1).textContent, "▾");
  check("the neighbour is untouched", card(rows.s2), null);

  /* ⟳ re-asks with refresh=1; the stale text stays readable, dimmed, and
     the button spins so a second click can't stack a second request. */
  answer = { status: 200, body: {
    session: "s1", generated_at: new Date().toISOString(), cached: true,
    source: { jsonl: true, cflow: false },
    briefing: { goal: "ship the card", now: "verifying",
                state: "blocked", progress: "waiting on review" },
    raw: null,
  } };
  click(card(rows.s1).querySelector(".sess-brief-refresh"));
  check("the refresh asks again, uncached",
        calls[1], "/api/sessions/s1/briefing?refresh=1");
  check("the stale text stays readable under the refresh",
        [card(rows.s1).className, kv(rows.s1).goal],
        ["sess-brief refreshing", "ship the card"]);
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

  /* The open card survives the poll's row rebuild — and does NOT re-ask. */
  list.children.length = 0;
  rows.s1 = row("s1"); rows.s2 = row("s2");
  ctx.apply();
  check("a rebuilt row gets its open card back from cache",
        [toggle(rows.s1).textContent, kv(rows.s1).now], ["▾", "verifying"]);
  check("the rebuild asked the daemon nothing", calls.length, 2);

  /* Closing takes the card with it. */
  click(toggle(rows.s1));
  check("closing removes the card and flips the glyph",
        [card(rows.s1), toggle(rows.s1).textContent], [null, "▸"]);

  /* The daemon's other answers, each told apart.  Prose that missed the
     agreed shape is still shown, as it came. */
  answer = { status: 200, body: {
    session: "s2", generated_at: new Date().toISOString(), cached: false,
    source: { jsonl: true, cflow: false },
    briefing: null, raw: "three lines of\nunshaped prose",
  } };
  click(toggle(rows.s2));
  await flush();
  const raw = card(rows.s2).querySelector(".sess-brief-raw");
  check("unshaped prose is shown as it came",
        [raw.tag, raw.textContent], ["pre", "three lines of\nunshaped prose"]);
  check("prose carries no state pill",
        card(rows.s2).querySelector(".sess-brief-state"), null);

  const s3 = row("s3");
  answer = { status: 400, body: { error: "llm not configured" } };
  ctx.apply();
  click(toggle(s3));
  await flush();
  check("no LLM points at the config to write",
        note(s3).textContent,
        "no LLM configured — set the llm section (endpoint, model, api_key) in ~/.claunch.yaml");

  const s4 = row("s4");
  answer = { status: 404, body: { error: "no record" } };
  ctx.apply();
  click(toggle(s4));
  await flush();
  check("a session with no record says that, not an error",
        note(s4).textContent, "no session record to summarise");

  const s5 = row("s5");
  answer = { status: 500, body: { error: "summariser fell over" } };
  ctx.apply();
  click(toggle(s5));
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
    briefing: { goal: "g", now: "n", state: "puzzled", progress: "" },
    raw: null,
  } };
  ctx.apply();
  click(toggle(s6));
  await flush();
  check("an unrecognised state stays neutral",
        card(s6).querySelector(".sess-brief-state").className,
        "sess-brief-state st-other");
  check("an empty progress grows no row", kv(s6), { goal: "g", now: "n" });

  /* No llm: block — the session poll says so and every toggle goes inert:
     disabled, its tooltip pointing at the config to write, and any open
     card folded away. Writing the config brings it all back on the next
     poll, the open-set intact, without anyone reloading. */
  ctx.setLLM(false);
  ctx.apply();
  check("without an llm the toggle is disabled, closed",
        [toggle(s6).disabled, toggle(s6).textContent], [true, "▸"]);
  check("its tooltip points at the config to write",
        toggle(s6).title,
        "briefing off — set the llm section (endpoint, model, api_key)"
        + " in ~/.claunch.yaml to enable");
  check("the open card is folded away while off", card(s6), null);
  ctx.setLLM(true);
  ctx.apply();
  check("configuring brings the toggle and the open card back",
        [toggle(s6).disabled, toggle(s6).textContent, kv(s6).goal],
        [false, "▾", "g"]);
  check("and the tooltip reads as the feature again",
        toggle(s6).title, "briefing: goal, current work, state — summarised");

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("briefcard_check: ok");
})();
