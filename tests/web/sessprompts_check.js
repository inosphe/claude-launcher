/* The session detail panel's Prompt packs section.

   `SessionDef.prompt_packs` records every pack that went into a session's
   opening, text included, and rides the /meta payload as
   `session.prompt_packs`. The section draws them as a row of labels -- one
   chip per pack, in opening order -- and pressing a chip opens that pack's
   detail under the row. What has to hold:

     labels        -- every recorded pack gets a chip, and no pack text is on
                      the page until one is pressed: a session usually has
                      three or four packs of a page each, and drawn in full
                      they would push the briefing and send box off the rail.
     detail        -- the pressed chip lights, and the detail says where the
                      text came from (the opening header's words), why a
                      package copy is there, and the text as recorded, in a
                      <pre>: no markdown pass.
     one at a time -- pressing another chip moves the detail; pressing the
                      open one shuts it.
     the fold      -- which chip is open lives in a map OUTSIDE the panel,
                      per session, because the panel is rebuilt every 2s.
     empty         -- drawn, not hidden, like the Opening task.

   Placement in renderSession is checked on the source, for the reason
   sesstask_check gives. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), title: "", listeners: {},
    attrs: {},
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    click() { if (this.listeners.click) this.listeners.click(); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((k) => k.classes.has(cls)); },
    words() { return this.all().map((k) => k.text).join(" "); },
  };
  return n;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const ctx = {};
let redraws = 0;
new Function("exports", "el", "refreshSession",
  slice("packOrigin") + slice("sessPromptPacks") + `
const sessPackOpen = new Map();
Object.assign(exports, { packs: sessPromptPacks, open: sessPackOpen });`
)(ctx, el, () => { redraws++; });

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const PACKS = [
  { id: "common", text: "## 공통 규칙\n- 작업 트리에 임시 파일을 남기지 않는다", source: "package", ticked: true },
  { id: "role-worker", text: "## Worker\nYou are a PRODUCER", source: "package",
    required_by: ["worker"] },
  { id: "house", text: "- **do not** touch *.js\n# not a heading" },
  { id: "@daemon", text: "daemon words", source: "daemon" },
];
const chips = (box) => box.find("sess-pack-chip");
const detail = (box) => box.find("sess-pack-detail");
const draw = (name = "s246") => ctx.packs({ name, prompt_packs: PACKS });

/* ---- labels ------------------------------------------------------------ */
let box = draw();
check("the section is drawn", box.classes.has("sess-packs"), true);
check("under a heading with the count", box.kids[0].tag === "h3" && box.kids[0].text,
      "Prompt packs (4)");
check("one chip per pack, in opening order, labelled by id",
      chips(box).map((c) => c.text), ["common", "role-worker", "house", "@daemon"]);
check("chips are buttons", chips(box).every((c) => c.tag === "button"), true);
check("nothing is open at first", detail(box).length, 0);
check("...so no pack text is on the page",
      box.words().includes("PRODUCER") || box.words().includes("공통 규칙"), false);
check("a chip's hover says where its text came from",
      chips(box)[1].title.includes("required by role worker"), true);

/* ---- detail ------------------------------------------------------------ */
const before = redraws;
chips(box)[1].click();
check("the press asks for a redraw", redraws - before, 1);
check("...and is remembered outside the panel", ctx.open.get("s246"), 1);
box = draw();
check("the rebuilt panel lights the pressed chip alone",
      chips(box).map((c) => c.classes.has("on")), [false, true, false, false]);
check("...marked selected for assistive tech",
      chips(box).map((c) => c.attrs["aria-selected"]), ["false", "true", "false", "false"]);
check("one detail is open", detail(box).length, 1);
const text = (b) => b.find("sess-pack-text").map((n) => [n.tag, n.text]);
check("the pack's text in full, in a pre", text(box),
      [["pre", "## Worker\nYou are a PRODUCER"]]);
check("the detail says why a package copy is there",
      detail(box)[0].words().includes("required by role worker"), true);
check("...and that it is not installed in this workspace",
      detail(box)[0].words().includes("not installed in this workspace"), true);

/* ---- one at a time ----------------------------------------------------- */
chips(box)[2].click();
box = draw();
check("pressing another chip moves the detail",
      chips(box).map((c) => c.classes.has("on")), [false, false, true, false]);
check("markdown characters survive as characters", text(box),
      [["pre", "- **do not** touch *.js\n# not a heading"]]);
check("a workspace copy names its file",
      detail(box)[0].words().includes(".claunch/prompts/house.md"), true);
chips(box)[2].click();
box = draw();
check("pressing the open chip shuts it", detail(box).length, 0);
check("...and forgets it", ctx.open.has("s246"), false);

chips(box)[0].click();
box = draw();
check("a ticked package copy says so",
      detail(box)[0].words().includes("ticked at session creation"), true);
chips(box)[3].click();
check("the daemon's prompt says what it is",
      detail(draw())[0].words().includes("this daemon's own prompt"), true);

/* Per session: one open chip must not open another session's. */
check("another session's packs are still shut", detail(draw("s247")).length, 0);

/* An index past the end (the record shrank between polls) opens nothing. */
ctx.open.set("s1", 9);
check("a stale index draws no detail",
      detail(ctx.packs({ name: "s1", prompt_packs: PACKS.slice(0, 1) })).length, 0);

/* ---- prompter snapshot ------------------------------------------------- */
ctx.open.set("s2", 0);
box = ctx.packs({ name: "s2", prompt_packs: [
  { id: "p-17", text: "x", source: "prompter", title: "Review guide",
    url: "https://prompter.example/p/17" }] });
check("a prompter snapshot is labelled by its title", chips(box)[0].text, "Review guide");
check("...and its detail carries the url",
      detail(box)[0].words().includes("https://prompter.example/p/17"), true);

/* ---- no record --------------------------------------------------------- */
for (const [what, s] of [["no field", { name: "s3" }],
                         ["an empty list", { name: "s4", prompt_packs: [] }]]) {
  box = ctx.packs(s);
  check(`${what} still draws the section`, box.classes.has("sess-packs"), true);
  check(`-- ${what} draws no chips`, chips(box).length, 0);
  check(`-- ${what} says so in words`,
        box.words().includes("no prompt packs recorded"), true);
}

/* ---- where it sits in the panel ---------------------------------------- */
const body = slice("renderSession");
check("renderSession draws it", body.includes("sessPromptPacks(s)"), true);
check("after the opening task",
      body.indexOf("sessTask(s)") < body.indexOf("sessPromptPacks(s)"), true);
check("and before the briefing",
      body.indexOf("sessPromptPacks(s)") < body.indexOf("sessBriefSection("), true);

if (failures) process.exit(1);
console.log("sessprompts_check ok");
