/* The commit page (commit-page.js, claunch-tb145) and its route.

   What every commit on the dashboard relies on:
     - the address names the sha and the repository (session or directory),
       and keeps the rail's project narrowing;
     - a linked element opens that address and stops the click there, because
       the rail card around a chip attaches the session on its own click;
     - parseHash reads #/commit/<sha>?session=|cwd= back into a page;
     - the side-by-side view pairs a run of removals with the additions after
       it and keeps context lines on both sides;
     - the page draws the metadata, the file list and the numbered lines. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const pageSrc = fs.readFileSync(path.join(STATIC, "commit-page.js"), "utf8");
const appSrc = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

function slice(src, name) {
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

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tagName: String(tag).toUpperCase(), kids: [], text: "", attrs: {}, dataset: {},
    listeners: {}, title: "", id: "", href: "", style: {},
    classList: null,
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { for (const c of cs) this.kids.push(c); },
    replaceChildren(...cs) { this.kids = cs; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    get textContent() { return this.text + this.kids.map((k) => k.textContent).join(""); },
    set textContent(v) { this.text = String(v); this.kids = []; },
    all() { const out = [this]; for (const k of this.kids) out.push(...(k.all ? k.all() : [])); return out; },
    find(cls) { return this.all().filter((x) => x.classList && x.classList.contains(cls)); },
  };
  const set = new Set();
  n.classList = {
    add: (...c) => c.forEach((x) => set.add(x)),
    contains: (c) => set.has(c),
    toggle: (c, on) => (on ? set.add(c) : set.delete(c)),
  };
  Object.defineProperty(n, "className", {
    get: () => [...set].join(" "),
    set: (v) => { set.clear(); String(v).split(/\s+/).filter(Boolean).forEach((x) => set.add(x)); },
  });
  return n;
}
const root = node("div");
const ctx = {
  URLSearchParams, Date, Number, String, Set, console,
  document: {
    createElement: node,
    createTextNode: (t) => { const n = node("#text"); n.text = String(t); return n; },
    getElementById: (id) => (id === "commit-view" ? root : null),
  },
  location: { hash: "#/" },
  history: { length: 2, back() {} },
  localStorage: { getItem: () => null, setItem() {} },
  navigator: {},
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(pageSrc, ctx);
const P = ctx.CommitPage;

/* ---- the address ------------------------------------------------------- */
assert.strictEqual(P.href("abc1234", { session: "s833" }), "#/commit/abc1234?session=s833");
assert.strictEqual(P.href("abc1234", { cwd: "C:\\repo\\wt" }),
  "#/commit/abc1234?cwd=C%3A%5Crepo%5Cwt");
// session wins over cwd: the session's directory is the one the list read.
assert.strictEqual(P.href("abc1234", { session: "s1", cwd: "/x" }), "#/commit/abc1234?session=s1");
vm.runInContext('var currentProject = "claunch";', ctx);
assert.strictEqual(P.href("abc1234", { session: "s1" }), "#/commit/abc1234?session=s1&project=claunch");
vm.runInContext('currentProject = "";', ctx);

/* ---- a linked element -------------------------------------------------- */
const chip = node("span");
P.link(chip, "abc1234", { session: "s1" });
assert.ok(chip.classList.contains("commit-link"));
assert.strictEqual(chip.attrs.role, "link");
let stopped = false, prevented = false;
chip.listeners.click[0]({ stopPropagation() { stopped = true; }, preventDefault() { prevented = true; } });
assert.ok(stopped && prevented, "the card's own click must not run");
assert.strictEqual(ctx.location.hash, "#/commit/abc1234?session=s1");
const anchor = node("a");
P.link(anchor, "def5678", { cwd: "/r" });
assert.strictEqual(anchor.href, "#/commit/def5678?cwd=%2Fr");
prevented = false; stopped = false;
anchor.listeners.click[0]({ stopPropagation() { stopped = true; }, preventDefault() { prevented = true; } });
assert.ok(stopped && !prevented, "an anchor navigates by its href (new tab works)");

/* ---- parseHash --------------------------------------------------------- */
const route = {};
new Function("exports",
  slice(appSrc, "hashQuery") + slice(appSrc, "parseHash") +
  "exports.parseHash = parseHash;")(route);
const r = route.parseHash("#/commit/abc1234?session=s833&project=claunch");
assert.strictEqual(r.page, "commit");
assert.strictEqual(r.sha, "abc1234");
assert.strictEqual(r.session, "s833");
const rc = route.parseHash("#/commit/abc1234?cwd=" + encodeURIComponent("/home/u/repo"));
assert.strictEqual(rc.cwd, "/home/u/repo");
assert.strictEqual(route.parseHash("#/commit").page, "home");

/* ---- the side-by-side pairing ----------------------------------------- */
const rows = P.splitRows([
  [" ", 1, 1, "a"],
  ["-", 2, null, "b"],
  ["-", 3, null, "c"],
  ["+", null, 2, "B"],
  [" ", 4, 3, "d"],
  ["+", null, 4, "e"],
  ["\\", null, null, "No newline at end of file"],
]);
const show = (x) => (x ? x[3] : null);
assert.deepStrictEqual(Array.from(rows, ([l, rr]) => [show(l), show(rr)]), [
  ["a", "a"], ["b", "B"], ["c", null], ["d", "d"], [null, "e"],
  ["No newline at end of file", null],
]);
assert.strictEqual(rows[5][2], true, "the git note is a row of its own");

/* ---- the drawn page ---------------------------------------------------- */
const detail = {
  sha: "a".repeat(40), short: "aaaaaaa", parents: ["b".repeat(40)], merge: false,
  author: { name: "t", email: "t@x", date: "2026-10-18T01:00:00+09:00" },
  committer: { name: "t", email: "t@x", date: "2026-10-18T01:00:00+09:00" },
  subject: "change things", body: "Why.\n\nClaunch-Session: s833",
  trailers: [{ key: "Claunch-Session", value: "s833" }],
  cwd: "/r",
  stats: { files: 2, additions: 2, deletions: 1 },
  truncated: false, limits: { file_lines: 4000 },
  files: [
    { path: "a.txt", old_path: null, status: "modified", additions: 1, deletions: 1,
      binary: false, truncated: false, omitted: false,
      hunks: [{ header: "@@ -1,2 +1,2 @@", lines: [[" ", 1, 1, "x"], ["-", 2, null, "y"], ["+", null, 2, "Y"]] }] },
    { path: "b.bin", old_path: null, status: "added", additions: null, deletions: null,
      binary: true, truncated: false, omitted: false, hunks: [] },
  ],
};
ctx.api = async () => ({ ok: true, json: async () => detail });
P.open("aaaaaaa", { session: "s833" });
setImmediate(() => {
  const text = root.textContent;
  assert.ok(text.includes("change things"), "subject");
  assert.ok(text.includes("a".repeat(40)), "full sha");
  assert.ok(text.includes("Why."), "body prose");
  assert.ok(!/Why\.[\s\S]*Claunch-Session: s833[\s\S]*Claunch-Session/.test(text),
    "a trailer is a row, not repeated in the body");
  assert.ok(text.includes("파일 2개 변경"), "file count");
  assert.ok(text.includes("바이너리 파일"), "binary note");
  const parent = root.find("cd-parent")[0];
  assert.strictEqual(parent.href, "#/commit/" + "b".repeat(40) + "?session=s833");
  const session = root.find("cd-session")[0];
  assert.strictEqual(session.href, "#/s/s833");
  const added = root.find("cd-add");
  assert.strictEqual(added.length, 1);
  assert.strictEqual(added[0].textContent, "+Y");
  const nums = root.find("cd-new").map((n) => n.text);
  assert.deepStrictEqual(nums, ["1", "", "2"]);
  console.log("commitpage_check ok");
});
