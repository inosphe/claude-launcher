/* The footer's prompter row (daemon/prompter.py via /api/prompter/prompts):
   hidden unless a server answered with prompts and a live session is attached,
   one line of chips with a More/Less toggle that only appears when the chips
   overflow, and a chip inserts its body into the field without sending. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

function slice(name) {
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

class Classes {
  constructor(owner) { this.owner = owner; this.set = new Set(); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, on) {
    const want = on === undefined ? !this.set.has(c) : !!on;
    if (want) this.set.add(c); else this.set.delete(c);
    return want;
  }
}

class Node {
  constructor(tag, cls, text) {
    this.tag = tag;
    this.classList = new Classes(this);
    for (const c of String(cls || "").split(/\s+/).filter(Boolean)) this.classList.add(c);
    this.textContent = text || "";
    this.children = [];
    this.listeners = {};
    this.attrs = {};
    this.scrollHeight = 26;
    this.clientHeight = 26;
  }
  set innerHTML(v) { this.children = []; }
  appendChild(n) { this.children.push(n); return n; }
  addEventListener(ev, fn) { this.listeners[ev] = fn; }
  click() { this.listeners.click && this.listeners.click(); }
  setAttribute(k, v) { this.attrs[k] = v; }
  querySelector(sel) {
    const cls = sel.replace(/^\./, "");
    for (const c of this.children) {
      if (c.classList.contains(cls)) return c;
      const deeper = c.querySelector(sel);
      if (deeper) return deeper;
    }
    return null;
  }
}

const row = new Node("div", "hidden");
const inserted = [];
let overflowHeight = 26;
const storage = {};

const ctx = {};
const harness = new Function(
  "exports", "$", "el", "insertPromptPreset", "localStorage",
  `let currentName = null, sessionEnded = false;
   let prompterState = { connected: false, prompts: [] };
   let prompterExpanded = false;
   ${slice("renderTermPrompterRow")}
   ${slice("syncPrompterToggle")}
   exports.render = renderTermPrompterRow;
   exports.set = (k, v) => { eval(k + " = v"); };
   exports.get = (k) => eval(k);`
);
harness(
  ctx,
  (id) => (id === "term-prompter-row" ? row : null),
  (tag, cls, text) => {
    const n = new Node(tag, cls, text);
    if (String(cls).includes("term-prompter-chips")) {
      Object.defineProperty(n, "scrollHeight", { get: () => overflowHeight });
    }
    return n;
  },
  (text) => { inserted.push(text); return true; },
  { getItem: (k) => storage[k] ?? null, setItem: (k, v) => { storage[k] = v; } },
);

let failures = 0;
const check = (name, value) => {
  if (value) return;
  failures++;
  console.log("FAIL " + name);
};
const chips = () => row.querySelector(".term-prompter-chips");
const toggle = () => row.querySelector(".term-prompter-toggle");

const prompts = [
  { id: 1, name: "align", title: "", body: "align body" },
  { id: 2, name: "commit", title: "Commit it", body: "commit body" },
];

ctx.set("currentName", "s1");
ctx.set("prompterState", { enabled: true, connected: false, prompts, error: "down" });
ctx.render();
check("hidden when the server is not connected", row.classList.contains("hidden"));

ctx.set("prompterState", { enabled: false, connected: false, prompts: [] });
ctx.render();
check("hidden when no server is configured", row.classList.contains("hidden"));

ctx.set("prompterState", { enabled: true, connected: true, prompts, url: "https://p" });
ctx.set("currentName", null);
ctx.render();
check("hidden without an attached session", row.classList.contains("hidden"));

ctx.set("currentName", "s1");
ctx.render();
check("shown when connected with prompts", !row.classList.contains("hidden"));
check("one chip per prompt", chips() && chips().children.length === 2);
check("chip label falls back to the name", chips().children[0].textContent === "align");
check("chip label prefers the title", chips().children[1].textContent === "Commit it");
check("collapsed by default", !row.classList.contains("expanded"));
check("toggle hidden when chips fit one line", toggle().classList.contains("hidden"));

chips().children[1].click();
check("a chip inserts its body", inserted.length === 1 && inserted[0] === "commit body");

overflowHeight = 80;
ctx.render();
check("toggle shown when chips overflow", !toggle().classList.contains("hidden"));
check("toggle offers More while collapsed", toggle().textContent.includes("More"));

toggle().click();
check("toggle expands the row", row.classList.contains("expanded"));
check("expanded toggle offers Less", toggle().textContent.includes("Less"));
check("expanded state is remembered", storage["claunch.prompterExpanded"] === "1");

toggle().click();
check("toggle collapses again", !row.classList.contains("expanded"));
check("collapsed state is remembered", storage["claunch.prompterExpanded"] === "0");

ctx.set("sessionEnded", true);
ctx.render();
check("hidden for an ended session", row.classList.contains("hidden"));

console.log(failures ? `\n${failures} failure(s)` : "all prompter checks passed");
process.exit(failures ? 1 : 0);
