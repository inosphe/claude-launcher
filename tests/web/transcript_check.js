/* The transcript pane: paging up a conversation the terminal cannot hold.

   The pane exists because a claude session repaints the alternate screen
   instead of scrolling it — nothing reaches any scrollback, so "what did this
   session say an hour ago" has no answer in the pipe. The daemon serves
   claude's own jsonl in pages, and this reads them into an ordinary overflow
   scroller so the BROWSER owns the wheel.

   Which is why the checks below are mostly about not disturbing the reader:
   prepending a page must leave the text they are looking at exactly where it
   was (the classic infinite-scroll trap — insert above someone and the
   browser slides them down by the height of what arrived), and the
   follow-forward must move only a reader who is already at the bottom.

   The real functions are sliced out of the shipped app.js and driven against
   a stub DOM whose scroll geometry is arithmetic we control. */
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

let failures = 0;
let ran = 0;
function check(what, cond, extra) {
  ran += 1;
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${what}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* ---- stub DOM ----------------------------------------------------------
   Every element is ROW_H tall, so scrollHeight is a count and the anchor
   arithmetic the pane does is checkable by hand. */
const ROW_H = 100;

function mkel(tag) {
  const node = {
    tag, className: "", title: "", dataset: {}, children: [],
    listeners: {}, parent: null, disabled: false,
    scrollTop: 0, clientHeight: 500,
    get scrollHeight() { return this.children.length * ROW_H; },
    classList: {
      add: (c) => { if (!node.className.split(" ").includes(c)) node.className = `${node.className} ${c}`.trim(); },
      remove: (c) => { node.className = node.className.split(" ").filter((x) => x && x !== c).join(" "); },
      contains: (c) => node.className.split(" ").includes(c),
      toggle: (c, on) => (on ? node.classList.add(c) : node.classList.remove(c)),
    },
    appendChild(c) {
      // A fragment splices its own children in, like the real thing.
      if (c.tag === "#fragment") { for (const k of c.children) this.appendChild(k); return c; }
      this.children.push(c); c.parent = this; return c;
    },
    insertBefore(c, ref) {
      if (c.tag === "#fragment") {
        const at = ref ? this.children.indexOf(ref) : this.children.length;
        this.children.splice(at < 0 ? this.children.length : at, 0, ...c.children);
        for (const k of c.children) k.parent = this;
        return c;
      }
      const at = ref ? this.children.indexOf(ref) : this.children.length;
      this.children.splice(at < 0 ? this.children.length : at, 0, c);
      c.parent = this;
      return c;
    },
    remove() {
      if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
    },
    setAttribute(k, v) { node[`attr:${k}`] = v; },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    get firstChild() { return this.children[0] || null; },
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
  };
  let text = "";
  Object.defineProperty(node, "textContent", {
    get() { return text; },
    set(v) { text = String(v); node.children.length = 0; },
  });
  Object.defineProperty(node, "innerHTML", {
    get() { return ""; },
    set() { node.children.length = 0; },
  });
  return node;
}

function build() {
  const nodes = {
    "term-log": mkel("button"),
    "term-log-pane": mkel("div"),
    terminal: mkel("div"),
  };
  const document_ = {
    createElement: mkel,
    createDocumentFragment: () => mkel("#fragment"),
  };

  const calls = [];
  let answers = [];
  async function api(p) {
    calls.push(p);
    const a = answers.shift() || { status: 200, body: { records: [], has_more: false } };
    return { ok: a.status === 200, status: a.status, json: async () => a.body };
  }

  const ctx = {};
  const code = [
    slice("el"), slice("ctxShort"),
    slice("transcriptIsOpen"), slice("toggleTranscript"), slice("applyTranscript"),
    slice("onTranscriptScroll"), slice("transcriptAtEnd"),
    slice("loadTranscriptPage"), slice("renderTranscriptPage"),
    slice("renderTranscriptRecord"), slice("renderTranscriptBlock"),
    slice("transcriptClipped"), slice("fmtLogTime"),
    slice("startTranscriptPoll"), slice("stopTranscriptPoll"), slice("pollTranscript"),
  ].join("\n");
  new Function(
    "exports", "$", "document", "api", "setInterval", "clearInterval",
    "Date", "Number",
    `
let currentName = null;
const TRANSCRIPT_PAGE = 40;
const TRANSCRIPT_NEAR_TOP = 400;
const TRANSCRIPT_NEAR_END = 40;
const TRANSCRIPT_POLL_MS = 4000;
let transcriptName = null, transcriptCursor = null, transcriptSeen = -1;
let transcriptMore = false, transcriptBusy = false, transcriptTimer = null;
` + code + `
exports.toggle = toggleTranscript;
exports.apply = applyTranscript;
exports.scroll = onTranscriptScroll;
exports.poll = pollTranscript;
exports.walkTo = (n) => { currentName = n; };
Object.defineProperty(exports, "open", { get: () => transcriptIsOpen() });
Object.defineProperty(exports, "cursor", { get: () => transcriptCursor });
Object.defineProperty(exports, "more", { get: () => transcriptMore });
Object.defineProperty(exports, "polling", { get: () => transcriptTimer !== null });
`
  )(ctx, (id) => nodes[id], document_, api,
    () => 1, () => {}, Date, Number);

  return {
    api: ctx, nodes, calls,
    pane: nodes["term-log-pane"],
    script: (list) => { answers = list.slice(); },
  };
}

const flush = () => new Promise((r) => setImmediate(r));

function recs(from, to) {
  const out = [];
  for (let i = from; i < to; i += 1) {
    out.push({ seq: i, role: i % 2 ? "assistant" : "user", ts: "",
               blocks: [{ type: "text", text: `m${i}` }] });
  }
  return out;
}

const texts = (pane) => pane.children
  .filter((c) => c.className.includes("log-rec"))
  .map((c) => c.dataset.seq);

/* --- opening reads the tail and lands at the bottom ---------------------- */
(async () => {
  const w = build();
  w.api.walkTo("s8");
  w.script([{ status: 200, body: { records: recs(60, 100), has_more: true, cursor: 60, total: 100 } }]);
  w.api.toggle();
  await flush(); await flush();

  check("the pane is open", w.api.open === true);
  check("it asked for the tail — no cursor on the first page",
        w.calls[0].includes("/transcript?limit=") && !w.calls[0].includes("before="),
        w.calls[0]);
  check("the records landed", texts(w.pane).length === 40, texts(w.pane).length);
  check("scrolled to the newest", w.pane.scrollTop === w.pane.scrollHeight,
        { top: w.pane.scrollTop, h: w.pane.scrollHeight });
  check("and the follow-forward is running", w.api.polling === true);
  check("the terminal gives up its space", w.nodes.terminal.className.includes("under-log"));
})();

/* --- scrolling up prepends, and holds the reader exactly still ----------- */
(async () => {
  const w = build();
  w.api.walkTo("s8");
  w.script([{ status: 200, body: { records: recs(60, 100), has_more: true, cursor: 60, total: 100 } }]);
  w.api.toggle();
  await flush(); await flush();

  // The reader flicks up to the top of what is loaded.
  w.pane.scrollTop = 100;
  const heightBefore = w.pane.scrollHeight;
  w.script([{ status: 200, body: { records: recs(20, 60), has_more: true, cursor: 20, total: 100 } }]);
  w.api.scroll();
  await flush(); await flush();

  check("it asked for the page above, from the cursor",
        w.calls[1].includes("before=60"), w.calls[1]);
  check("the older records went on top",
        texts(w.pane)[0] === "20" && texts(w.pane).length === 80, texts(w.pane).slice(0, 3));
  const grew = w.pane.scrollHeight - heightBefore;
  check("and the reader did not move: scrollTop grew by exactly the height"
        + " of what was inserted above them",
        w.pane.scrollTop === 100 + grew, { top: w.pane.scrollTop, grew });
  check("the cursor walked back with them", w.api.cursor === 20, w.api.cursor);
})();

/* --- the top is the end of the asking ------------------------------------ */
(async () => {
  const w = build();
  w.api.walkTo("s8");
  w.script([{ status: 200, body: { records: recs(0, 40), has_more: false, cursor: 0, total: 40 } }]);
  w.api.toggle();
  await flush(); await flush();

  check("a first page that reached the top says so", w.api.more === false);
  w.pane.scrollTop = 0;
  w.api.scroll();
  await flush();
  check("so scrolling further up asks for nothing", w.calls.length === 1, w.calls);
})();

/* --- one fetch at a time -------------------------------------------------- */
(async () => {
  const w = build();
  w.api.walkTo("s8");
  w.script([{ status: 200, body: { records: recs(60, 100), has_more: true, cursor: 60, total: 100 } },
            { status: 200, body: { records: recs(20, 60), has_more: true, cursor: 20, total: 100 } }]);
  w.api.toggle();
  await flush(); await flush();
  w.pane.scrollTop = 0;
  // A flick fires scroll on every frame; only one page may go out.
  w.api.scroll(); w.api.scroll(); w.api.scroll(); w.api.scroll();
  await flush(); await flush();
  check("a flick does not send a page request per frame",
        w.calls.length === 2, w.calls);
})();

/* --- the follow-forward moves only a reader at the bottom ---------------- */
(async () => {
  const w = build();
  w.api.walkTo("s8");
  w.script([{ status: 200, body: { records: recs(60, 100), has_more: true, cursor: 60, total: 100 } }]);
  w.api.toggle();
  await flush(); await flush();

  // Sitting at the bottom: a new turn arrives under them.
  w.script([{ status: 200, body: { records: recs(60, 102), has_more: true, cursor: 60, total: 102 } }]);
  w.api.poll();
  await flush(); await flush();
  check("only the records they had not seen are appended",
        texts(w.pane).length === 42 && texts(w.pane)[41] === "101",
        texts(w.pane).slice(-3));
  check("the top cursor did not move — scrolling up still resumes at 60",
        w.api.cursor === 60, w.api.cursor);
  check("and they were carried to the newest", w.pane.scrollTop === w.pane.scrollHeight);

  // Scrolled up to read something: nothing may drag them back.
  w.pane.scrollTop = 500;
  w.script([{ status: 200, body: { records: recs(60, 104), has_more: true, cursor: 60, total: 104 } }]);
  w.api.poll();
  await flush(); await flush();
  check("a reader who scrolled up is not asked for", w.calls.length === 2, w.calls.length);
  check("and is left exactly where they were", w.pane.scrollTop === 500, w.pane.scrollTop);
})();

/* --- walking to another session closes it -------------------------------- */
(async () => {
  const w = build();
  w.api.walkTo("s8");
  w.script([{ status: 200, body: { records: recs(0, 10), has_more: false, cursor: 0, total: 10 } }]);
  w.api.toggle();
  await flush(); await flush();
  check("open on s8", w.api.open === true);

  w.api.walkTo("s9");
  w.api.apply();
  check("the pane does not follow the reader to another session's terminal",
        w.api.open === false);
  check("the pane is hidden", w.pane.className.includes("hidden"), w.pane.className);
  check("the terminal takes its space back",
        !w.nodes.terminal.className.includes("under-log"));
  check("and the follow-forward stopped", w.api.polling === false);
})();

/* --- a session with no conversation says so, once ------------------------ */
(async () => {
  const w = build();
  w.api.walkTo("s8");
  w.script([{ status: 200, body: { records: [], has_more: false, source: null, total: 0 } }]);
  w.api.toggle();
  await flush(); await flush();
  const note = w.pane.querySelector(".log-note");
  check("an empty conversation is explained, not left blank",
        note && note.textContent.includes("no conversation"),
        note && note.textContent);
})();

/* --- a refusal is reported rather than swallowed ------------------------- */
(async () => {
  const w = build();
  w.api.walkTo("s8");
  w.script([{ status: 500, body: null }]);
  w.api.toggle();
  await flush(); await flush();
  const note = w.pane.querySelector(".log-note");
  check("a failed page says why", note && note.textContent.includes("could not read"),
        note && note.textContent);
})();

/* --- blocks: prose whole, tool traffic clipped and labelled -------------- */
(async () => {
  const w = build();
  w.api.walkTo("s8");
  w.script([{ status: 200, body: { total: 1, has_more: false, cursor: 0, records: [{
    seq: 0, role: "assistant", ts: "2026-08-25T01:02:03Z", blocks: [
      { type: "text", text: "the prose" },
      { type: "thinking", text: "weighing it" },
      { type: "tool_use", name: "Bash", id: "t1", text: "{cmd}", clipped: true, full: 4000 },
      { type: "tool_result", id: "t1", error: true, text: "boom", clipped: false },
    ],
  }] } }]);
  w.api.toggle();
  await flush(); await flush();

  const rec = w.pane.querySelector(".log-rec");
  const kinds = rec.children.map((c) => c.className);
  check("the record carries a head and one node per block",
        kinds.length === 5 && kinds[0] === "log-head", kinds);
  check("prose is prose", rec.querySelector(".log-text").textContent === "the prose");
  check("thinking is set apart", !!rec.querySelector(".log-think"));
  const pre = rec.querySelector(".log-pre");
  check("a clipped block says how much it is holding back",
        pre.textContent.includes("more characters"), pre.textContent);
  check("an errored result is marked as one",
        !!kinds.find((c) => c.includes("log-err")), kinds);
})();

/* --- the markup and wiring the code reaches for -------------------------- */
{
  const html = fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
              "index.html"), "utf8");
  const css = fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
              "style.css"), "utf8");
  check("index.html declares the button and the pane",
        html.includes('id="term-log"') && html.includes('id="term-log-pane"'));
  check("the pane sits in the terminal's own column, after the grid",
        html.indexOf('id="term-log-pane"') > html.indexOf('id="terminal"'));
  check("it starts hidden", /id="term-log-pane"[^>]*class="[^"]*hidden/.test(html));
  check("the button is wired to the toggle",
        src.includes('$("term-log").addEventListener("click", toggleTranscript)'));
  check("the pane is a real scroller — this is the whole feature, so pin it",
        /#term-log-pane\s*\{[^}]*overflow-y:\s*auto/.test(css));
  check("and a flick off its end stays in it",
        /#term-log-pane\s*\{[^}]*overscroll-behavior:\s*contain/.test(css));
  check("prose wraps rather than growing a sideways scrollbar",
        /\.log-text\s*\{[^}]*white-space:\s*pre-wrap/.test(css));
}

process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log(`all ${ran} transcript checks passed`);
});
