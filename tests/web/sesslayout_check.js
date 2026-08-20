/* The per-session layout: which panel the right rail shows (Details or
   Workflow, the radio), whether the run page is halved into the terminal's
   column (the ⬒ toggle and the bar), and where the bar was left. All of it
   remembered per SESSION in one localStorage key, so walking between
   terminals never resets a choice — and all of it derived, never toggled:
   the header button writes the choice down and syncSplitPane reads it back.
   Slice the real code out of the shipped app.js and check the writing, the
   reading, the junk-tolerance and the reconcile against a stub DOM. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");

function sliceTo(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}
function sliceLine(decl) {
  const start = src.indexOf(decl);
  if (start < 0) throw new Error("missing " + decl);
  return src.slice(start, src.indexOf("\n", start) + 1);
}
function sliceFn(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", classes: new Set(), handlers: {},
    title: "",
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k) { (this.handlers[k] || []).forEach((fn) => fn()); },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

/* ---- harness: the sliced code with everything it leans on stubbed ------ */
/* The split section's own state and doers, replaced by spies: what this file
   tests is that syncSplitPane calls them at the right moments, not what they
   then do to the network. currentPage/currentName live here too, mutable, so
   a scenario can walk between pages the way the router does. */
const prelude = `
let splitFor = null;
let currentPage = "terminal";
let currentName = "s15";
function applySplitRatio(r) { calls.ratios.push(r); }
function openSplit(name) { splitFor = name; calls.opened.push(name); }
function closeSplit() { splitFor = null; calls.closed++; }
`;
const expose = `
Object.assign(exports, {
  clampSplitRatio, sessLayoutFor, setSessLayout, syncSplitPane, sessRailTabs,
  SPLIT_DEFAULT, SPLIT_MIN, SPLIT_MAX,
  splitFor: () => splitFor,
  setPage: (p) => { currentPage = p; },
  setName: (n) => { currentName = n; },
});`;

function build(o = {}) {
  const store = new Map();
  if (o.stored !== undefined) store.set("claunch_sesslayout:/", o.stored);
  const storage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  };
  const nodes = {};
  for (const id of ["term-split", "term-wf", "term-splitbtn"]) nodes[id] = node(id);
  const body = node("body");
  const calls = { opened: [], closed: 0, ratios: [], refits: 0, refreshed: 0 };
  const ctx = {};
  new Function(
    "exports", "$", "document", "BASE", "localStorage", "MOBILE_MQ",
    "el", "refitSoon", "refreshSession", "calls",
    prelude +
    sliceTo("const SESSLAYOUT_KEY =", "/* Bind the terminal to a session") +
    sliceLine("let splitWasUp") +
    sliceFn("syncSplitPane") + "\n" + sliceFn("sessRailTabs") +
    expose
  )(ctx, (id) => nodes[id], { body }, "/", storage,
    { matches: !!o.narrow }, el, () => calls.refits++,
    () => calls.refreshed++, calls);
  return { ctx, nodes, body, store, calls };
}

/* --- the ratio is clamped, whatever the key holds ----------------------- */
{
  const { ctx } = build({});
  check("not-a-number falls back to the default",
        ctx.clampSplitRatio(NaN) === ctx.SPLIT_DEFAULT
        && ctx.clampSplitRatio(Infinity) === ctx.SPLIT_DEFAULT);
  check("below the floor lands on it", ctx.clampSplitRatio(0.02) === ctx.SPLIT_MIN);
  check("above the ceiling lands on it", ctx.clampSplitRatio(7) === ctx.SPLIT_MAX);
  check("a sane ratio passes through", ctx.clampSplitRatio(0.5) === 0.5);
  check("but not with a pointer-event's worth of digits",
        ctx.clampSplitRatio(1 / 3) === 0.333, ctx.clampSplitRatio(1 / 3));
}

/* --- reading: whole and sane, for any session, from any key ------------- */
{
  const { ctx } = build({});
  const lay = ctx.sessLayoutFor("never-seen");
  check("an unknown session gets the defaults",
        lay.rail === "detail" && lay.split === false
        && lay.ratio === ctx.SPLIT_DEFAULT, lay);
}
{
  const { ctx } = build({ stored: "not json at all" });
  check("junk in the key reads as the defaults, not as a crash",
        ctx.sessLayoutFor("s15").rail === "detail");
}
{
  const { ctx } = build({ stored: "[1,2,3]" });
  check("a key holding the wrong shape likewise",
        ctx.sessLayoutFor("s15").split === false);
}
{
  // Field-by-field: one broken field must not cost the others their values.
  const { ctx } = build({
    stored: JSON.stringify({ s15: { rail: "banana", split: 1, ratio: 99 } }),
  });
  const lay = ctx.sessLayoutFor("s15");
  check("an unknown rail falls back", lay.rail === "detail", lay);
  check("...without taking the truthy split with it", lay.split === true, lay);
  check("...and the wild ratio is clamped, not honoured",
        lay.ratio === ctx.SPLIT_MAX, lay);
}

/* --- writing: per session, merged, and through the reader ---------------- */
{
  const w = build({});
  w.ctx.setSessLayout("s15", { rail: "wf" });
  w.ctx.setSessLayout("s16", { split: true });
  check("each session keeps its own row",
        w.ctx.sessLayoutFor("s15").rail === "wf"
        && w.ctx.sessLayoutFor("s16").rail === "detail"
        && w.ctx.sessLayoutFor("s16").split === true);
  w.ctx.setSessLayout("s15", { split: true, ratio: 0.7 });
  const lay = w.ctx.sessLayoutFor("s15");
  check("a later patch merges rather than replaces",
        lay.rail === "wf" && lay.split === true && lay.ratio === 0.7, lay);
  check("what is stored is already whole",
        JSON.parse(w.store.get("claunch_sesslayout:/")).s15.rail === "wf");
  w.ctx.setSessLayout(null, { split: true });
  check("no session, no write", !("null" in JSON.parse(
    w.store.get("claunch_sesslayout:/"))));
}
/* Scoped like the token and the font size: several daemons share one relay
   origin, and an unscoped key would let each tunnel restyle its siblings. */
check("the key carries the base path",
      sliceLine("const SESSLAYOUT_KEY").includes("${BASE}"));

/* --- the reconcile: derived from (page, session, width, choice) ---------- */
{
  const w = build({ stored: JSON.stringify({ s15: { split: true, ratio: 0.7 } }) });
  w.ctx.syncSplitPane();
  check("split on: the bar and the pane come up",
        !w.nodes["term-split"].classes.has("hidden")
        && !w.nodes["term-wf"].classes.has("hidden"));
  check("...the column is marked", w.body.classes.has("term-split"));
  check("...the button reads pressed",
        w.nodes["term-splitbtn"].attrs["aria-pressed"] === "true");
  check("...the remembered ratio is applied", w.calls.ratios.includes(0.7),
        w.calls.ratios);
  check("...and the pane is pointed at the session",
        w.calls.opened.join() === "s15" && w.ctx.splitFor() === "s15");
  check("...with a refit for the height the terminal lost",
        w.calls.refits === 1, w.calls.refits);
  w.ctx.syncSplitPane();
  check("a second reconcile re-points and refits nothing",
        w.calls.opened.length === 1 && w.calls.refits === 1, w.calls);
  // Walking to another page takes the pane down; the choice stays written.
  w.ctx.setPage("home");
  w.ctx.syncSplitPane();
  check("leaving the terminal shuts the pane",
        w.nodes["term-wf"].classes.has("hidden") && w.calls.closed === 1
        && !w.body.classes.has("term-split"));
  check("...but not the choice", w.ctx.sessLayoutFor("s15").split === true);
}
{
  const w = build({ stored: JSON.stringify({ s15: { split: true } }),
                    narrow: true });
  w.ctx.syncSplitPane();
  check("below the breakpoint the pane stays down",
        w.nodes["term-wf"].classes.has("hidden") && w.calls.opened.length === 0);
  check("...while the button still tells the truth about the choice",
        w.nodes["term-splitbtn"].attrs["aria-pressed"] === "true");
}
{
  const w = build({});
  w.ctx.syncSplitPane();
  check("split off and never opened: nothing to close",
        w.calls.closed === 0 && w.nodes["term-wf"].classes.has("hidden"));
  // The setter is the button's whole handler: write, then reconcile.
  w.ctx.setSessLayout("s15", { split: true });
  check("writing the choice down IS what opens the pane",
        w.calls.opened.join() === "s15"
        && !w.nodes["term-wf"].classes.has("hidden"));
}
{
  // Switching terminals re-points the pane at the session on screen.
  const w = build({ stored: JSON.stringify({
    s15: { split: true }, s16: { split: true } }) });
  w.ctx.syncSplitPane();
  w.ctx.setName("s16");
  w.ctx.syncSplitPane();
  check("another terminal, the same choice: the pane follows",
        w.calls.opened.join() === "s15,s16", w.calls.opened);
}

/* --- the radio ----------------------------------------------------------- */
{
  const w = build({});
  const bar = w.ctx.sessRailTabs("s15");
  const tabs = walk(bar).filter((k) => k.classes.has("seq-tab"));
  check("two tabs, Details and Workflow",
        tabs.length === 2 && tabs[0].text === "Details"
        && tabs[1].text === "Workflow", tabs.map((t) => t.text));
  check("the current panel is lit and dead",
        tabs[0].classes.has("on") && !tabs[0].handlers.click
        && !tabs[1].classes.has("on") && !!tabs[1].handlers.click);
  tabs[1].fire("click");
  check("picking the other writes the choice down",
        w.ctx.sessLayoutFor("s15").rail === "wf");
  check("...and redraws now, not at the poll's leisure",
        w.calls.refreshed === 1, w.calls.refreshed);
  const bar2 = w.ctx.sessRailTabs("s15");
  const tabs2 = walk(bar2).filter((k) => k.classes.has("seq-tab"));
  check("the rebuild lights the new panel",
        tabs2[1].classes.has("on") && !tabs2[0].classes.has("on"));
}

/* --- the markup the code reaches for ------------------------------------- */
for (const id of ["term-splitbtn", "term-split", "term-wf"]) {
  check(`index.html declares #${id}`, html.includes(`id="${id}"`));
}
check("the bar and the pane sit under the terminal, before the pages",
      html.indexOf('id="term-split"') > html.indexOf('id="terminal"')
      && html.indexOf('id="term-wf"') > html.indexOf('id="term-split"')
      && html.indexOf('id="term-wf"') < html.indexOf('id="home-view"'));
check("the toggle sits in the terminal header",
      html.indexOf('id="term-splitbtn"') > html.indexOf('id="term-header"')
      && html.indexOf('id="term-splitbtn"') < html.indexOf('id="terminal"'));
check("both start hidden — syncSplitPane decides, not the markup",
      /id="term-split" class="hidden"/.test(html)
      && /id="term-wf" class="hidden"/.test(html));

if (failures) { console.log(`${failures} check(s) failed`); process.exit(1); }
console.log("all session-layout checks passed");
