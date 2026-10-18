/* The split frame every pane in the terminal's split slot sits in
   (split-frame.js, claunch-039we).

   What has to hold:
     frame    -- mount draws one head row (lead with the pane's name, ⤢ and ×)
                 over a body; ⤢/× only ask (onMax/onClose); setMax draws
                 which way ⤢ points; of(host) finds it; unmount clears it.
     every    -- openSplit mounts that frame for every kind of pane (run,
                 sims, category, chart, and the conversation pane) and the
                 pane draws into the frame's body, so ⤢ and × are on each.
     buttons  -- every split toggle in the header carries .split-btn, and the
                 lit / pressed styles select that class rather than a list of
                 ids (the list once missed ⌘ category); every kind in
                 SPLIT_PANES names a button that exists; each toggle has a
                 glyph for the compact header (toolbar.js). */
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = path.join(__dirname, "../../src/claude_launcher/web/static");
const read = (f) => fs.readFileSync(path.join(STATIC, f), "utf8");

class N {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this._text = "";
    this.dataset = {};
    this.handlers = {};
    this.attrs = {};
    this.innerHTML = "";
  }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(...ns) { for (const n of ns) { n.parentNode = this; this.children.push(n); } }
  replaceChildren(...ns) { for (const c of this.children) c.parentNode = null; this.children = []; this.append(...ns); }
  addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); }
  fire(k) { for (const fn of this.handlers[k] || []) fn({preventDefault() {}}); }
  has(c) { return this.className.split(/\s+/).includes(c); }
  find(c) {
    for (const x of this.children) {
      if (x.has(c)) return x;
      const y = x.find(c);
      if (y) return y;
    }
    return null;
  }
}

const app = read("app.js");
const html = read("index.html");
const css = read("style.css");
const toolbar = read("toolbar.js");

function frameModule() {
  const ctx = vm.createContext({document: {createElement: (t) => new N(t)}, WeakMap});
  vm.runInContext(read("split-frame.js"), ctx);
  return ctx.SplitFrame;
}

/* --- frame ----------------------------------------------------------------- */
{
  const F = frameModule();
  const host = new N("div");
  const asked = [];
  const f = F.mount(host, {
    kind: "category", title: "⌘ category", max: false,
    onMax: (on) => asked.push(on ? "max" : "min"),
    onClose: () => asked.push("close"),
  });
  assert.equal(host.children.length, 1);
  const root = host.children[0];
  assert.ok(root.has("split-frame") && root.dataset.kind === "category");
  assert.deepEqual(root.children.map((c) => c.className), ["split-frame-head", "split-frame-body"]);
  assert.equal(f.body, root.children[1]);
  assert.equal(f.lead.textContent, "⌘ category", "the head names the pane");
  const big = root.find("split-frame-max");
  const shut = root.find("split-frame-close");
  assert.equal(big.textContent, "⤢");
  assert.equal(big.attrs["aria-pressed"], "false");
  big.fire("click");
  assert.deepEqual(asked, ["max"], "⤢ asks; the page decides");
  assert.equal(big.textContent, "⤢", "...and does not flip by itself when a page owns it");
  F.setMax(host, true);
  assert.equal(big.textContent, "⤡");
  assert.equal(big.attrs["aria-pressed"], "true");
  big.fire("click");
  assert.deepEqual(asked, ["max", "min"], "enlarged, ⤡ asks to shrink");
  shut.fire("click");
  assert.deepEqual(asked, ["max", "min", "close"]);
  assert.equal(F.of(host), f);
  F.unmount(host);
  assert.equal(F.of(host), null);
  assert.equal(host.children.length, 0);

  // Without an owner the buttons act on the frame itself.
  const solo = new N("div");
  F.mount(solo, {});
  solo.children[0].find("split-frame-max").fire("click");
  assert.equal(F.of(solo).max(), true);
  solo.children[0].find("split-frame-close").fire("click");
  assert.equal(solo.children.length, 0);
}

/* --- every kind of pane gets the frame -------------------------------------- */
{
  const fn = (name) => {
    const start = app.search(new RegExp(`^(async )?function ${name}\\(`, "m"));
    assert.ok(start >= 0, name);
    let end = app.indexOf("{", app.indexOf(")", start));
    for (let depth = 0; ; end++) {
      if (app[end] === "{") depth++;
      else if (app[end] === "}" && !--depth) break;
    }
    return app.slice(start, end + 1);
  };
  const constant = (name) => {
    const start = app.indexOf(`const ${name} = {`);
    assert.ok(start >= 0, name);
    return app.slice(start, app.indexOf("\n};\n", start) + 3);
  };
  const F = frameModule();
  const wf = new N("div");
  const into = [];
  const layouts = [];
  const pane = (tag) => ({mount: (host) => into.push([tag, host]), unmount() {}});
  const ctx = {
    $: (id) => (id === "term-wf" ? wf : new N("div")),
    el: (tag, cls, text) => { const n = new N(tag); n.className = cls || ""; if (text) n.textContent = text; return n; },
    api: () => new Promise(() => {}),
    fetchCflow2Run: () => Promise.resolve(null),
    renderSplit() {}, renderCflow2Into() {},
    visiblePoll: (f) => f, setInterval: () => 1, clearInterval: () => {},
    threadsReadKey: () => "k",
    sessLayoutFor: () => ({max: false}),
    setSessLayout: (name, patch) => layouts.push([name, {...patch}]),
    refreshQaRail() {}, refreshThreadsRail() {},
    globalThis: {
      SplitFrame: F, SimPane: pane("sims"), CategoryPane: pane("category"),
      TalkPane: {mount: (host, name, o) => into.push(["talk", host, o]), unmount() {}, setMode() {}},
      DurableFlow: {mount: (host) => { into.push(["chart", host]); return {unmount() {}}; }},
    },
  };
  vm.createContext(ctx);
  vm.runInContext(`
    let splitFor = null, splitKind = null, splitCwd = null, splitScope = null;
    let splitPollTimer = null, splitSelectedStep = null, splitLastData = null;
    let splitDurable = null, splitGen = 0, splitBody = null;
    ${fn("isTalkPane")}
    ${constant("SPLIT_PANES")}
    ${fn("mountSplitFrame")}
    ${fn("openSplit")}
    ${fn("closeSplit")}
    ${fn("refreshSplit")}
    this.openSplit = openSplit;
    this.closeSplit = closeSplit;
    this.SPLIT_PANES = SPLIT_PANES;
  `, ctx);

  for (const kind of ["run", "sims", "category", "chart", "threads"]) {
    into.length = 0;
    layouts.length = 0;
    ctx.openSplit("s1", kind);
    const frame = F.of(wf);
    assert.ok(frame, `${kind}: a frame is mounted`);
    assert.equal(frame.root.dataset.kind, kind);
    assert.ok(frame.root.find("split-frame-max") && frame.root.find("split-frame-close"),
              `${kind}: ⤢ and × are on the pane`);
    if (kind === "run") {
      assert.equal(frame.body.innerHTML, "<p class='wf-note'>loading…</p>", "run: drawn into the body");
    } else {
      assert.equal(into.length, 1, kind);
      assert.equal(into[0][1], frame.body, `${kind}: drawn into the frame's body`);
    }
    if (kind === "threads") {
      assert.equal(into[0][2].lead, frame.lead, "the conversation pane gets the lead for its tabs");
      assert.ok(!("onClose" in into[0][2]) && !("onMax" in into[0][2]), "...and no controls of its own");
      assert.equal(frame.lead.textContent, "", "...so the frame names nothing there itself");
    } else {
      assert.equal(frame.lead.textContent, ctx.SPLIT_PANES[kind].title, `${kind}: the head names it`);
    }
    frame.root.find("split-frame-max").fire("click");
    frame.root.find("split-frame-close").fire("click");
    assert.deepEqual(layouts, [["s1", {max: true}], ["s1", {split: false, max: false}]],
                     `${kind}: ⤢ and × write the session's layout`);
    ctx.closeSplit();
    assert.equal(F.of(wf), null, `${kind}: closing takes the frame down`);
    assert.equal(wf.children.length, 0);
  }

  /* --- buttons -------------------------------------------------------------- */
  const toggles = ["term-splitbtn", "term-simsbtn", "term-categorybtn", "term-chartbtn", "term-qabtn", "term-threadsbtn"];
  for (const id of toggles) {
    assert.match(html, new RegExp(`<button id="${id}" class="term-btn split-btn[ "]`), `${id} carries .split-btn`);
    assert.ok(app.includes(`$("${id}").addEventListener("click", () => toggleSplitPane(`), `${id} toggles the pane`);
    assert.ok(new RegExp(`"${id}": "[^"]+"`).test(toolbar), `${id} has a compact-header glyph (toolbar.js)`);
  }
  for (const [kind, spec] of Object.entries(ctx.SPLIT_PANES)) {
    assert.ok(toggles.includes(spec.btn), `${kind}: ${spec.btn} is a split toggle`);
    assert.ok(html.includes(`>${spec.title}</button>`), `${kind}: the head reads like its button`);
    assert.ok(spec.on && spec.off, kind);
  }
  assert.ok(/\.term-btn\.split-btn\[aria-pressed="true"\] \{/.test(css), "pressed is styled by class");
  assert.ok(/\.term-btn\.split-btn\.has-resource \{/.test(css), "lit is styled by class");
  assert.ok(!/#term-(splitbtn|simsbtn|chartbtn|qabtn)\[aria-pressed/.test(css), "no id list left to fall behind");
  const at = (s) => html.indexOf(`src="static/${s}"`);
  assert.ok(at("split-frame.js") > 0 && at("split-frame.js") < at("app.js"));
}

console.log("split frame checks passed");
