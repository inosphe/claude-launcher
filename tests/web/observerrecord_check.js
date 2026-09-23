/* Observer's event cards, run against the real functions from observer.js.

   A record-origin event (search_records.capture, kind "briefing"/"checks")
   used to dump its payload as pretty-printed JSON straight into the card —
   the whole point of storing it structured was lost on the way to the
   screen. What has to hold now: a briefing record renders as the same
   .sess-brief box the session rail's live card uses (state pill, one-line,
   goal/now/progress rows, faq rows), a checks record renders as Y/N rows
   with the same icon/name the rail's status-check chip uses, the raw JSON
   is still reachable but demoted to a "원본 JSON 보기" fallback that needs
   no round trip (the payload is already on hand from e.text), and every
   OTHER event kind (daemon, agent, plain observation) is completely
   unaffected — still the flat text line, still "근거 · {source}" fetched
   lazily from the daemon. */
const fs = require("fs");
const path = require("path");
const observerSrc = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "observer.js"),
  "utf8"
);
const appSrc = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(source, name) {
  let start = source.indexOf(`async function ${name}(`);
  if (start < 0) start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name}`);
  let depth = 0;
  for (let j = source.indexOf("{", start); j < source.length; j++) {
    if (source[j] === "{") depth++;
    else if (source[j] === "}") { depth--; if (!depth) return source.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
// `node` and `request` are `const NAME = (...) => { ... };` in observer.js,
// not `function NAME(...)` -- same brace-balance walk, starting after `=`.
function sliceConst(source, name) {
  const marker = `const ${name} = `;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`cannot locate const ${name}`);
  const braceStart = source.indexOf("{", start);
  let depth = 0;
  for (let j = braceStart; j < source.length; j++) {
    if (source[j] === "{") depth++;
    else if (source[j] === "}") {
      depth--;
      if (!depth) {
        let end = j + 1;
        while (source[end] === ";") end++;
        return source.slice(start, end);
      }
    }
  }
  throw new Error(`unbalanced const ${name}`);
}

/* ---- stub DOM ---- */
function mkel(tag) {
  const el = {
    tag, className: "", title: "", dataset: {}, children: [],
    open: false, ontoggle: null,
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    append(...cs) { for (const c of cs) this.appendChild(c); },
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
      const cls = sel.slice(1);
      const out = [];
      const walk = (n) => {
        for (const c of n.children) {
          if (c.className.split(" ").includes(cls)) out.push(c);
          walk(c);
        }
      };
      walk(this);
      return out;
    },
  };
  let text = "";
  Object.defineProperty(el, "textContent", {
    get() { return text; },
    set(v) { text = v; el.children.length = 0; },
  });
  return el;
}
// A real <details> fires `toggle` (and calls .ontoggle) whenever .open
// flips, whichever code flips it -- the click was on the <summary>, not
// this node. This stub only needs the one-way "open it" case the harness
// below drives.
function openDetails(detail) {
  detail.open = true;
  if (detail.ontoggle) detail.ontoggle();
}

/* ---- stub API: every call is recorded ---- */
const calls = [];
let answer = { status: 200, body: null };
async function api(p) {
  calls.push(p);
  const a = answer;
  return { ok: a.status === 200, status: a.status, json: async () => a.body };
}
const flush = () => new Promise((r) => setImmediate(r));

const ctx = {};
new Function(
  "exports", "document", "briefingStateClass", "statusCheckIcon", "statusCheckText",
  "statusCheckName", "api",
  [sliceConst(observerSrc, "node"), slice(observerSrc, "request"),
   slice(observerSrc, "recordPayload"), slice(observerSrc, "briefingSnapshot"),
   slice(observerSrc, "checksSnapshot"), slice(observerSrc, "eventItem")].join("\n") + `
exports.eventItem = eventItem;
`)(ctx, { createElement: mkel },
   new Function(slice(appSrc, "briefingStateClass") + "\nreturn briefingStateClass;")(),
   new Function(slice(appSrc, "statusCheckIcon") + "\nreturn statusCheckIcon;")(),
   new Function(slice(appSrc, "statusCheckText") + "\nreturn statusCheckText;")(),
   new Function(slice(appSrc, "statusCheckName") + "\nreturn statusCheckName;")(),
   api);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const s = { name: "s1" };
const iso = "2026-09-21T00:00:00.000Z";
const kv = (box) => {
  const out = {};
  for (const r of box.children) {
    if (r.className !== "sess-brief-row" && r.className !== "sess-brief-row sess-brief-faq") continue;
    out[r.children[0].textContent] = r.children[1].textContent;
  }
  return out;
};

(async () => {
  /* A parsed briefing record renders as the rail's .sess-brief box, not a
     JSON dump. */
  const briefing = {
    goal: "카드 재설계", now: "구조화 렌더 작성", state: "working",
    progress: "eventItem 절반", "one-line-job-description": "observer 카드를 구조화한다",
    faq: [{ question: "이 세션의 목표는?", answer: "observer UI 재설계" }],
  };
  const e1 = { id: "e1", kind: "briefing", origin: "record", source: "briefing",
               at: iso, text: JSON.stringify(briefing, null, 2) };
  const item1 = ctx.eventItem(s, e1);
  const box1 = item1.querySelector(".sess-brief");
  check("a briefing record grows the rail's box, not a raw <div>", !!box1, true);
  check("no plain JSON dump sits beside it",
        item1.children.some((c) => c.tag === "div" && c.textContent === e1.text), false);
  check("the state rides as a working-coloured pill",
        box1.querySelector(".sess-brief-state").className, "sess-brief-state st-working");
  check("the one-line job description heads the box",
        box1.querySelector(".sess-brief-one").textContent, "observer 카드를 구조화한다");
  check("goal/now/progress land as labelled rows", kv(box1),
        { "목표": "카드 재설계", "현재": "구조화 렌더 작성", "진행": "eventItem 절반",
          "이 세션의 목표는?": "observer UI 재설계" });

  /* The raw JSON is still reachable, but demoted to a no-round-trip fallback
     -- the payload came from e.text, so opening it asks the daemon nothing. */
  const summary1 = item1.children.find((c) => c.tag === "details");
  check("the fallback reads as a fallback, not primary evidence",
        summary1.children[0].textContent, "원본 JSON 보기");
  openDetails(summary1);
  check("it renders synchronously -- no fetch for data already on hand",
        [calls.length, summary1.children[1].textContent],
        [0, JSON.stringify(briefing, null, 2)]);

  /* A briefing that missed the agreed shape still shows the model's words,
     the same fallback app.js's rail card uses. */
  const e2 = { id: "e2", kind: "briefing", origin: "record", source: "briefing",
               at: iso, text: JSON.stringify({ raw: "shape 밖의 산문" }, null, 2) };
  const box2 = ctx.eventItem(s, e2).querySelector(".sess-brief");
  const raw = box2.querySelector(".sess-brief-raw");
  check("unshaped prose is shown as it came", [raw.tag, raw.textContent], ["pre", "shape 밖의 산문"]);
  check("prose carries no state pill", box2.querySelector(".sess-brief-state"), null);

  /* A checks record renders as Y/N rows with the rail's icon and name. */
  const checks = [
    { id: "tests", name: "Tests", question: "테스트가 통과했는가?", enabled: true,
      answer: "yes", reported_at: iso },
    { id: "merged", name: "Merged", question: "master에 머지됐는가?", enabled: true,
      answer: "no", reported_at: iso },
  ];
  const e3 = { id: "e3", kind: "checks", origin: "record", source: "checks",
               at: iso, text: JSON.stringify(checks, null, 2) };
  const box3 = ctx.eventItem(s, e3).querySelector(".sess-brief");
  const rows3 = box3.querySelectorAll(".sess-brief-check");
  check("each check is its own row", rows3.length, 2);
  const icon0 = rows3[0].querySelector(".status-check-icon");
  check("a yes answer shows the named check with its hover question",
        [icon0.textContent, icon0.className, icon0.title, rows3[0].children[1].textContent],
        ["✓", "status-check-icon check-yes", "테스트가 통과했는가?", "Tests"]);
  const icon1 = rows3[1].querySelector(".status-check-icon");
  check("a no answer shows the crossed icon",
        [icon1.textContent, icon1.className], ["×", "status-check-icon check-no"]);

  /* An empty checks record says so instead of showing an empty box. */
  const e4 = { id: "e4", kind: "checks", origin: "record", source: "checks",
               at: iso, text: "[]" };
  const box4 = ctx.eventItem(s, e4).querySelector(".sess-brief");
  check("an empty checks record names the absence",
        box4.querySelector(".sess-brief-note").textContent, "표시할 체크 항목이 없습니다.");

  /* Every other event kind is untouched: flat text, lazy-fetched evidence
     under the original "근거 · {source}" label. */
  const e5 = { id: "e5", kind: "cflow", origin: "observation", source: "commit-a1b2",
               at: iso, text: "커밋 a1b2가 목표를 만족한다" };
  const item5 = ctx.eventItem(s, e5);
  check("a plain observation keeps its flat text line",
        item5.children[1].textContent, "커밋 a1b2가 목표를 만족한다");
  check("no .sess-brief box grows for it", item5.querySelector(".sess-brief"), null);
  const summary5 = item5.children.find((c) => c.tag === "details");
  check("its fallback keeps the original source label",
        summary5.children[0].textContent, "근거 · commit-a1b2");
  answer = { status: 200, body: { evidence: "fetched on demand" } };
  openDetails(summary5);
  check("opening it DOES ask the daemon -- nothing was on hand for it",
        calls, ["api/observer/s1/events/e5"]);
  await flush();
  check("the fetched evidence lands once the request settles",
        summary5.children[1].textContent, JSON.stringify({ evidence: "fetched on demand" }, null, 2));

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("observerrecord_check: ok");
})();
