/* The rail row's age line (claunch-y8ibz): when the session was created, in
   the reader's own clock, and how long it has lived -- up to now while it
   runs, up to its exit once it ended.

   The failures worth a harness: the creation time printed in the daemon's
   UTC instead of the browser's zone, an exited row whose lifetime keeps
   counting, a running row whose lifetime is frozen at the age it was drawn
   with (the in-place refresh never swapping it), and the line dropping off
   the row builder without a trace. The zone is pinned to Asia/Seoul (no
   daylight saving) so a UTC rendering differs from the local one by nine
   hours on any machine; node reads TZ again when it changes. */
process.env.TZ = "Asia/Seoul";
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.RAILLIFE_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);
const css = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "style.css"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* ---- stub DOM ---- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, title: "",
    appendChild(c) { n.kids.push(c); c.parentNode = n; return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    querySelector(sel) {
      const cls = sel.startsWith(".") ? sel.slice(1) : null;
      return descendants(n).find((k) => cls && k.classes.has(cls)) || null;
    },
    querySelectorAll(sel) {
      if (sel !== "li[data-name]") return [];
      return descendants(n).filter((k) => k.tag === "li" && k.dataset.name);
    },
    replaceWith(other) {
      const p = n.parentNode;
      p.kids[p.kids.indexOf(n)] = other;
      other.parentNode = p;
    },
    get textContent() { return n.text + n.kids.map((k) => k.textContent).join(""); },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  };
  n.classList = { contains: (c) => n.classes.has(c) };
  return n;
}
function descendants(n, out = []) {
  for (const k of n.kids) { out.push(k); descendants(k, out); }
  return out;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const stubs = `
let sessionsCache = [];
const railStale = {};
function seenAgo() { return null; }
function railSeenLine() { return el("span", "rail-seen"); }
function decorateBriefingRow() {}
`;
const ctx = {};
new Function("exports", "el",
  stubs + slice("fmtAge") + slice("localStamp") + slice("lifeParse") + slice("lifeCreatedText")
  + slice("lifeSecs") + slice("lifeSpanText") + slice("railLifeLine")
  + slice("refreshRailSeen") + `
Object.assign(exports, {
  created: lifeCreatedText, secs: lifeSecs, span: lifeSpanText,
  line: railLifeLine, refresh: refreshRailSeen,
  setCache: (v) => { sessionsCache = v; },
});`)(ctx, el);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
const pairs = (line) => line.kids.map((p) => [p.kids[0].text, p.kids[1].text]);

/* ---- the creation stamp, in the browser's zone ---- */
const now = Date.parse("2026-10-19T09:30:00Z");   // 18:30 in Seoul
check("today: the local clock alone (UTC 09:07 is 18:07 in Seoul)",
      ctx.created("2026-10-19T09:07:53+00:00", now), "18:07");
check("a daemon stamp without a zone is read as UTC",
      ctx.created("2026-10-19T09:07:53", now), "18:07");
check("this year: month-day and clock",
      ctx.created("2026-10-17T23:00:00Z", now), "10-18 08:00");
check("UTC yesterday that is local today reads as today",
      ctx.created("2026-10-18T16:00:00Z", now), "01:00");
check("an earlier year: the date alone",
      ctx.created("2025-12-31T00:00:00Z", now), "2025-12-31");
check("no stamp, no text", ctx.created(null, now), "");

/* ---- the lifetime ---- */
const born = "2026-10-19T08:00:00Z";
check("running: created to now",
      ctx.secs({ created_at: born, status: "idle" }, now), 5400);
check("exited: created to exit, not to now",
      ctx.secs({ created_at: born, status: "exited", exited_at: "2026-10-19T08:20:00Z" }, now), 1200);
check("exited without an exit stamp counts to now",
      ctx.secs({ created_at: born, status: "exited" }, now), 5400);
check("no creation stamp: no lifetime", ctx.secs({ status: "idle" }, now), null);
check("seconds", ctx.span(42), "42s");
check("minutes", ctx.span(1200), "20m");
check("hours", ctx.span(5400), "1h30m");
check("days, where fmtAge would say 51h00m", ctx.span(51 * 3600), "2d03h");

/* ---- the line ---- */
const line = ctx.line({ created_at: born, status: "busy" }, now);
check("the line carries its class", line.className, "rail-life");
check("created and lifetime, in that order",
      pairs(line), [["created", "17:00"], ["lifetime", "1h30m"]]);
check("the created pair's title names the full local stamp",
      line.kids[0].title.split("\n")[1], "2026-10-19 17:00:00");
const gone = ctx.line({ created_at: born, status: "exited", exited_at: "2026-10-19T08:20:00Z" }, now);
check("an exited row's lifetime stops at the exit", pairs(gone)[1], ["lifetime", "20m"]);
check("an exited row's title names the exit",
      gone.kids[1].title.split("\n")[1], "exited 2026-10-19 17:20:00");
check("a record without created_at draws an empty line",
      ctx.line({ status: "idle" }, now).kids.length, 0);

/* ---- the in-place refresh moves a running row's lifetime ---- */
const list = node("ul");
const li = node("li");
li.dataset.name = "s1";
list.appendChild(li);
li.appendChild(el("span", "rail-seen"));
li.appendChild(ctx.line({ created_at: new Date(Date.now() - 600 * 1000).toISOString(), status: "idle" }));
check("drawn ten minutes old", pairs(li.querySelector(".rail-life"))[1], ["lifetime", "10m"]);
ctx.setCache([{ name: "s1", status: "idle",
                created_at: new Date(Date.now() - 1800 * 1000).toISOString() }]);
ctx.refresh(list);
check("the refresh redraws the lifetime from the record",
      pairs(li.querySelector(".rail-life"))[1], ["lifetime", "30m"]);
const kept = li.querySelector(".rail-life");
ctx.refresh(list);
check("an unchanged reading is not swapped", li.querySelector(".rail-life") === kept, true);

/* ---- the builder attaches it, guarded ---- */
check("the row builder draws the line", /const railLife = typeof railLifeLine === "function" \? railLifeLine\(s\) : null;/.test(src), true);
check("...and appends it after the attention line",
      /railSeen, \.\.\.\(railLife \? \[railLife\] : \[\]\)/.test(src), true);

/* ---- the stylesheet ---- */
const rule = /#session-list \.rail-life \{([^}]*)\}/.exec(css);
check("the line has a rule", !!rule, true);
check("...and is a full-width line breaker after the toggle",
      !!rule && /flex-basis: 100%/.test(rule[1]) && /order: 2/.test(rule[1]), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("raillife_check: ok");
