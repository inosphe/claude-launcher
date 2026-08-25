/* The briefing's two other homes, run against the real functions from app.js.

   briefcard_check holds the rail's ▸ toggle to its contract; this holds the
   two new surfaces to the same rules. The header button owns the current
   session's card — it reads and writes the same open-set the row ▸ uses, so
   opening it here opens the row too, and the card it folds into the pane
   between the header and the terminal is drawn by the same renderer. Without
   an llm: block it goes inert, its tooltip pointing at the config to write
   (a louder echo of the disabled row toggle) and its pane folded away; the
   detail panel's section fetches on first open and, off, becomes a static
   pointer at the config instead of a dead card. */
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

/* ---- stub DOM ---- */
function mkel(tag) {
  const node = {
    tag, className: "", title: "", dataset: {}, children: [],
    listeners: {}, parent: null, disabled: false, attrs: {},
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    append(...cs) { for (const c of cs) this.appendChild(c); },
    remove() {
      if (this.parent) {
        this.parent.children.splice(this.parent.children.indexOf(this), 1);
      }
    },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    setAttribute(k, v) { this.attrs[k] = v; },
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
  // Aggregating, like a real node: a node either holds text or holds
  // children (the setter clears them), and applyBriefingTop measures the
  // pane by the text under it — a card that grows from "summarising…" to a
  // filled summary is a taller pane, and the stub has to be able to say so.
  Object.defineProperty(node, "textContent", {
    get() { return text + node.children.map((c) => c.textContent).join(""); },
    set(v) { text = v; node.children.length = 0; node.attrs.textContent = v; },
  });
  Object.defineProperty(node, "innerHTML", {
    get() { return node.attrs.innerHTML || ""; },
    set(v) { node.attrs.innerHTML = v; node.children.length = 0; },
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

/* The three nodes applyBriefingTop + sessBriefSection touch, found by id. */
const byId = {};
function mount(id, tag) {
  const n = mkel(tag);
  byId[id] = n;
  return n;
}
const list = mount("session-list", "ul");
const btn = mount("term-brief", "button");
const pane = mount("term-brief-pane", "div");

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
/* The pane and the terminal share one flex column, so every change to the
   pane's height has to be followed by a refit — otherwise the session keeps
   the rows it had and the ones the card pushed past the bottom edge are
   clipped away by #terminal's overflow:hidden, with no scrollbar and no
   wheel (it browses the daemon's history) to reach them. Counted here so the
   checks can hold both halves of the rule: refit when the height changed,
   and NOT on the 2s poll that re-renders the same card. */
let refits = 0;
const refitSoon = () => { refits += 1; };
new Function(
  "exports", "$", "document", "api", "ctxChip", "refitSoon",
  [slice("el"), slice("fmtAge"), slice("briefingStateClass"),
   slice("fetchBriefing"), slice("toggleBriefing"),
   slice("renderBriefingCard"), slice("applyBriefingTop"),
   slice("applyBriefingCards"), slice("sessBriefSection")].join("\n") + `
const briefingOpen = new Set();
const briefingCache = new Map();
let briefingLLM = true;
let currentName = null;
// The header button's click lives in app.js's top-level wiring, not in any
// sliced function — wire it here the same way the shipped page does, so the
// click path is exercised rather than assumed.
const topBtn = $("term-brief");
if (topBtn) topBtn.addEventListener("click", () => {
  if (currentName) toggleBriefing(currentName);
});
exports.apply = applyBriefingTop;
exports.section = sessBriefSection;
exports.setCurrent = (n) => { currentName = n; };
exports.setLLM = (v) => { briefingLLM = v; };
`)(ctx, (id) => byId[id] || null, { createElement: mkel }, api, noChip, refitSoon);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const card = () => pane.querySelector(".sess-brief");
const noteOf = (box) => box.querySelector(".sess-brief-note");
const clickButton = () => btn.listeners.click({ stopPropagation() {} });
/* findTag: the stub's querySelector is class-only (like briefcard_check's);
   the section head is found by tag, so walk children for it. */
function findTag(node, tag) {
  for (const c of node.children) {
    if (c.tag === tag) return c;
    const hit = findTag(c, tag);
    if (hit) return hit;
  }
  return null;
}

(async () => {
  /* No current session: the button exists but is inert, nothing to open. */
  ctx.apply();
  check("with no session the button is disabled, closed",
        [btn.disabled, btn.textContent, pane.className.includes("hidden")],
        [true, "▸ briefing", true]);

  /* With a session and nothing open: enabled, closed, pane folded. */
  ctx.setCurrent("s1");
  ctx.apply();
  check("with a session the button is enabled, closed",
        [btn.disabled, btn.attrs["aria-pressed"], btn.textContent],
        [false, "false", "▸ briefing"]);
  check("and the pane says so", pane.className.includes("hidden"), true);

  /* Clicking opens the card: it asks the daemon once and the pane shows the
     fetch in flight, then the summary when it lands. */
  const iso = new Date(Date.now() - 60_000).toISOString();
  answer = { status: 200, body: {
    session: "s1", generated_at: iso, cached: false,
    source: { jsonl: true, cflow: true },
    briefing: { goal: "ship the top toggle", now: "slicing functions",
                state: "working", progress: "2 of 4 pieces done" },
    raw: null,
  } };
  clickButton();
  check("opening asks for the current session's briefing",
        calls, ["/api/sessions/s1/briefing"]);
  check("the button reads open and the pane holds a card",
        [btn.attrs["aria-pressed"], btn.textContent],
        ["true", "▾ briefing"]);
  check("the card says it is summarising while the answer is out",
        noteOf(pane).textContent, "summarising…");
  await flush();
  const rows = {};
  for (const r of card().children) {
    if (r.className !== "sess-brief-row") continue;
    rows[r.children[0].textContent] = r.children[1].textContent;
  }
  check("goal, now and progress land as labelled rows", rows,
        { goal: "ship the top toggle", now: "slicing functions",
          progress: "2 of 4 pieces done" });
  check("the pane is no longer folded", pane.className.includes("hidden"), false);
  check("the card's sandwich is the row renderer's", card().className, "sess-brief");

  /* The refit rule. The card is drawn between the header and the terminal in
     one flex column, so its height comes straight out of the grid: opening it
     without a refit leaves the session at the rows it had and the stylesheet
     clips the bottom ones away — no scrollbar, and the wheel is the daemon's
     history rather than that overflow, so the foot of the screen is simply
     gone until some unrelated event happens to refit. The summariser landing
     is the same event again: "summarising…" is one line and the answer is
     several, and the pane grows under a grid that has not been told. */
  check("opening the card refits, and the summary landing refits again",
        refits >= 2, true);

  /* ...and the other half of the rule: applyBriefingCards runs on every 2s
     poll, so an unchanged card must cost nothing. A refit is a session
     resize; firing it twice a second would churn the program's grid. */
  const quiet = refits;
  ctx.apply();
  ctx.apply();
  check("a poll that re-renders the same card does not refit", refits, quiet);

  /* The card's own ⟳ refreshes with refresh=1. */
  answer = { status: 200, body: {
    session: "s1", generated_at: new Date().toISOString(), cached: true,
    source: { jsonl: true, cflow: false },
    briefing: { goal: "ship the top toggle", now: "running checks",
                state: "idle", progress: "" },
    raw: null,
  } };
  card().querySelector(".sess-brief-refresh").listeners.click({ stopPropagation() {} });
  check("the refresh re-asks, uncached", calls[1], "/api/sessions/s1/briefing?refresh=1");
  await flush();

  /* Closing folds the card and flips the button back. */
  clickButton();
  check("closing removes the card and flips the glyph",
        [pane.className.includes("hidden"), btn.textContent],
        [true, "▸ briefing"]);
  check("and closing refits too — the column just got its height back",
        refits > quiet, true);

  /* No llm: block — the button goes inert, its tooltip pointing at the
     config to write, and any open card folded away. Configuring brings it
     back, the open-set intact. */
  ctx.setLLM(false);
  ctx.apply();
  check("off, the button is disabled and its tooltip points at the config",
        [btn.disabled, btn.title, btn.textContent],
        [true,
         "briefing off — set the llm section (endpoint, model, api_key)"
         + " in ~/.claunch.yaml to enable",
         "▸ briefing"]);
  ctx.setLLM(true);
  ctx.apply();
  check("configuring brings the button back",
        [btn.disabled, btn.attrs["aria-pressed"]], [false, "false"]);

  /* The detail panel section: a session the ▸ never opened asks the daemon
     on first draw, says the same thing the card does, under a "Briefing"
     head. */
  answer = { status: 200, body: {
    session: "dt1", generated_at: new Date().toISOString(), cached: false,
    source: { jsonl: true, cflow: false },
    briefing: { goal: "surface the briefing", now: "in the detail panel",
                state: "working", progress: "" },
    raw: null,
  } };
  const before = calls.length;
  const box = ctx.section("dt1");
  check("the section renders a head and pulls the briefing",
        [findTag(box, "h3").textContent, calls.length > before,
         box.className], ["Briefing", true, "sess-brief-section"]);
  check("it asked for the panel's own session, not the terminal's",
        calls[calls.length - 1], "/api/sessions/dt1/briefing");
  await flush();
  check("the section's card carries the fetched summary",
        ctx.section("dt1").querySelector(".sess-brief-k").textContent, "goal");

  // exercise the off-state through a fresh section: no fetch, a static note
  const c0 = calls.length;
  ctx.setLLM(false);
  const off = ctx.section("s2");
  const note = noteOf(off);
  check("off, the section is a static pointer at the config, no fetch",
        [note.className, note.textContent, calls.length === c0],
        ["sess-brief-note",
         "briefing off — set the llm section (endpoint, model, api_key)"
         + " in ~/.claunch.yaml to enable",
         true]);
  ctx.setLLM(true);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("briefingtop_check: ok");
})();
