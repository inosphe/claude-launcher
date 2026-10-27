/* The rail's cflow v2 line, run against the real functions from app.js.

   Each session row whose session drives a v2 run grows one `.sess-cflow2`
   line from GET /api/cflow2/rail (keyed by session name). What has to hold:
   the line appears only on the row the entry is keyed by; it says which
   workflow and where the run stands; it is amber (`gated`) exactly when the
   server says a person owes the run something, and then the line names what;
   a finished or failed run reads by its status alone; the element survives
   re-application (its click listener walks to the run page and is attached
   once); and it is a path of its own -- the v1 badge (`.sess-cflow`) is not
   touched by it, and the stylesheet gives it its own line. */
const fs = require("fs");
const path = require("path");
const staticDir = path.join(__dirname, "..", "..", "src", "claude_launcher",
                            "web", "static");
const src = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const css = fs.readFileSync(path.join(staticDir, "style.css"), "utf8");

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

function wfGlyphTable() {
  const start = src.indexOf("const WF_GLYPH = {");
  if (start < 0) throw new Error("cannot locate WF_GLYPH in app.js");
  return src.slice(start, src.indexOf("};", start) + 2);
}

/* ---- stub DOM ---- */
function el(tag) {
  const node = {
    tag, className: "", title: "", dataset: {}, children: [],
    listeners: {}, parent: null,
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    append(...cs) { for (const c of cs) this.appendChild(c); },
    remove() {
      if (this.parent) {
        this.parent.children.splice(this.parent.children.indexOf(this), 1);
      }
    },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    querySelector(sel) {
      return this.children.find(
        (c) => c.className.split(" ").includes(sel.slice(1))
      ) || null;
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

const list = el("ul");
function row(name) {
  const li = el("li");
  li.dataset.name = name;
  list.appendChild(li);
  return li;
}

const location = { hash: "" };
const ctx = {};
new Function(
  "exports", "$", "document", "location", "cflow2RailCache",
  [wfGlyphTable(), slice("cflow2RailMarkState"), slice("cflow2RailLabel"),
   slice("applyCflow2Badges")].join("\n") + `
exports.apply = applyCflow2Badges;
exports.setRows = (rows) => { cflow2RailCache = rows; };
`)(ctx, (id) => (id === "session-list" ? list : null),
   { createElement: el }, location, {});

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const line = (li) => li.querySelector(".sess-cflow2");
const lineText = (li) => {
  const b = line(li);
  return b && b.children[2] ? b.children[2].textContent : null;
};

const rows = { s1: row("s1"), quiet: row("quiet") };
// A v1 badge already on the row: the v2 painter must leave it alone.
const v1 = el("span");
v1.className = "sess-cflow";
rows.s1.appendChild(v1);

const running = {
  cwd: "F:/repo", scope: "s1", workflow: "improv-worker-v2", status: "running",
  round: 2, current: ["목표 수립"], owed: [], gated: false,
};
ctx.setRows({ s1: running });
ctx.apply();
check("a running run is named on its session's row",
      lineText(rows.s1), "improv-worker-v2 · running · round 2 · 목표 수립");
check("the line carries a v2 tag", line(rows.s1).children[1].textContent, "v2");
check("the mark says running",
      [line(rows.s1).children[0].className, line(rows.s1).children[0].textContent],
      ["wf-mark wf-mark-running", "▸"]);
check("a running run is not the reader's move", line(rows.s1).className, "sess-cflow2");
check("a session without an entry grows no line", line(rows.quiet), null);
check("the v1 badge stays on the row", rows.s1.children.includes(v1), true);

check("run navigation is a native link", line(rows.s1).tag, "a");
check("run destination is explicit", line(rows.s1).children[3].textContent, "Run ↗");
check("link href targets this run", line(rows.s1).href, "#/wf/" + encodeURIComponent("s1|F:/repo"));
const first = line(rows.s1);
ctx.setRows({ s1: { ...running, status: "waiting_approval", gated: true,
                    owed: ["loop_limit: rounds", "choose: landing"] } });
ctx.apply();
check("an owed run is flagged as the reader's move",
      line(rows.s1).className, "sess-cflow2 gated");
check("the flagged line names what is owed",
      lineText(rows.s1), "improv-worker-v2 · ⚑ loop_limit: rounds · choose: landing");
check("the mark says it is yours", line(rows.s1).children[0].textContent, "◆");
check("the hover lists what is owed", line(rows.s1).title,
      "loop_limit: rounds\nchoose: landing");
check("the element is reused across polls", line(rows.s1) === first, true);

ctx.setRows({ s1: { ...running, status: "done", current: [] } });
ctx.apply();
check("a finished run reads by its status",
      [lineText(rows.s1), line(rows.s1).children[0].textContent],
      ["improv-worker-v2 · done", "✓"]);
ctx.setRows({ s1: { ...running, status: "failed" } });
ctx.apply();
check("a failed run is marked as an error",
      [lineText(rows.s1), line(rows.s1).children[0].className],
      ["improv-worker-v2 · failed", "wf-mark wf-mark-error"]);

line(rows.s1).listeners.click({ stopPropagation() {} });
check("the line walks to the run page",
      location.hash, "#/wf/" + encodeURIComponent("s1|F:/repo"));

ctx.setRows({});
ctx.apply();
check("a run that went away takes its line with it", line(rows.s1), null);
check("...and the v1 badge is still there", rows.s1.children.includes(v1), true);

check("the stylesheet gives the v2 line a full-width line of its own",
      /#session-list \.sess-cflow2 \{[^}]*flex-basis: 100%/.test(css), true);
check("...and the amber of a reader's move",
      /#session-list \.sess-cflow2\.gated \{[^}]*#d29922/.test(css), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railcflow2_check: ok");
