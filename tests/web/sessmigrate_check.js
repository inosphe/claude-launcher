/* The detail panel's migrate picker offers the repository's original
   checkout beside its launcher worktrees. Slice the shipped function, feed it
   a daemon-shaped /api/git answer, and pin the contracts: the original is
   listed first, the checkout the session already stands in is shown but
   cannot be picked, and picking the original posts it as 'cwd' (a
   'worktree' would cut a new checkout under .claude/worktrees instead).

   The same move is where a session re-files its project (claunch-51gyl.1):
   the second picker keeps the current filing unless one is chosen, and the
   answer line names the project and the cflow run slots that travelled. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let i = body; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (!depth) return src.slice(head, i + 1);
    }
  }
  throw new Error("unbalanced " + name);
}

function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    value: undefined, disabled: false, placeholder: "", type: "", checked: false,
    appendChild(child) {
      this.kids.push(child);
      if (this.tag === "select" && child.tag === "option" && this.value === undefined) {
        this.value = child.value;
      }
      return child;
    },
    append(...children) { children.forEach((c) => this.appendChild(c)); },
    addEventListener(kind, fn) { (this.handlers[kind] ||= []).push(fn); },
    fire(kind) { return Promise.all((this.handlers[kind] || []).map((fn) => fn())); },
    set innerHTML(value) { this.kids = []; this.value = undefined; },
    get innerHTML() { return ""; },
    get options() { return this.kids.filter((child) => child.tag === "option"); },
    get textContent() { return this.text; },
    set textContent(value) { this.text = String(value); },
    get className() { return [...this.classes].join(" "); },
    set className(value) {
      this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
    },
  };
  n.classList = {
    toggle(c, on) { if (on) n.classes.add(c); else n.classes.delete(c); },
    contains(c) { return n.classes.has(c); },
  };
  return n;
}

function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const document = { createElement: (tag) => node(tag) };
function walk(n, out = []) {
  for (const child of n.kids) { out.push(child); walk(child, out); }
  return out;
}
const tags = (n, tag) => walk(n).filter((x) => x.tag === tag);
const settle = () => new Promise((resolve) => setImmediate(resolve));

const ROOT = "F:\\works\\repo";
const GIT = {
  cwd: "", repo: true, root: ROOT, branch: "master", branches: ["master"],
  worktrees: ["s1-a", "s2-b"],
  paths: {
    "s1-a": "F:\\works\\repo\\.claude\\worktrees\\s1-a",
    "s2-b": "F:\\works\\repo\\.claude\\worktrees\\s2-b",
  },
};
const posts = [];
const api = async (url, opts) => {
  if (opts && opts.method === "POST") {
    posts.push({ url, body: JSON.parse(opts.body) });
    return {
      ok: true,
      status: 200,
      json: async () => ({
        cwd: "moved",
        children: [],
        slots_moved: ["w@main"],
        project: "hq",
        mirrors_refiled: [{ mesh: "m@pcA", was: "claunch", now: "hq" }],
      }),
    };
  }
  return { ok: true, status: 200, json: async () => GIT };
};
const stubs = `
let sessMigrateBox = null;
let currentName = null;
let projectsCache = [];
function detach() {}
async function refreshSessions() {}
function attach() {}
`;
const ctx = {};
new Function(
  "exports", "document", "el", "api",
  stubs + slice("worktreePathKey") + slice("sessMigrate") + `
Object.assign(exports, {
  render: sessMigrate,
  drop: () => { sessMigrateBox = null; },
  setProjects: (list) => { projectsCache = list; },
});`
)(ctx, document, el, api);

let failures = 0;
function check(label, condition, extra) {
  if (condition) return;
  failures++;
  console.error(`FAIL ${label}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
}
const opts = (sel) => tags(sel, "option").map((o) => ({
  value: o.value, text: o.text, disabled: o.disabled,
}));
const button = (box) => tags(box, "button").find((b) => b.text === "Migrate");

async function main() {
  // A session in a worktree: the original is offered, first and picked.
  const inWt = ctx.render({
    session: { name: "w", cwd: "f:/works/repo/.claude/worktrees/s1-a/" },
  });
  await settle();
  const sel = tags(inWt, "select")[0];
  const o = opts(sel);
  check("original checkout is listed first",
    o[0] && o[0].value === "root" && o[0].text === `original: ${ROOT}`, o);
  check("worktrees and the new-worktree entry follow",
    o.map((x) => x.value).join(",") === "root,wt:s1-a,wt:s2-b,new", o);
  check("the worktree the session stands in is marked and disabled",
    o[1].text === "worktree: s1-a (here)" && o[1].disabled, o[1]);
  check("the original is pickable from a worktree", !o[0].disabled, o[0]);
  check("the original is the default destination", sel.value === "root", sel.value);
  check("no project picker is drawn when the daemon knows no projects",
    tags(inWt, "select").length === 1, tags(inWt, "select").length);

  await button(inWt).fire("click");
  check("picking the original posts it as cwd, not as a worktree",
    posts.length === 1 && posts[0].body.cwd === ROOT
      && !("worktree" in posts[0].body) && posts[0].body.children === false,
    posts);

  // A session already in the original checkout: it is shown but disabled,
  // and the first worktree becomes the default.
  ctx.drop();
  const inRoot = ctx.render({ session: { name: "m", cwd: "F:/works/repo" } });
  await settle();
  const sel2 = tags(inRoot, "select")[0];
  const o2 = opts(sel2);
  check("the original is marked (here) and disabled when the session is in it",
    o2[0].text === `original: ${ROOT} (here)` && o2[0].disabled, o2[0]);
  check("the default skips the disabled original", sel2.value === "wt:s1-a", sel2.value);

  sel2.value = "wt:s2-b";
  await button(inRoot).fire("click");
  check("a picked worktree still posts as worktree",
    posts.length === 2 && posts[1].body.worktree === "s2-b" && !("cwd" in posts[1].body),
    posts[1]);

  sel2.value = "new";
  const nameIn = tags(inRoot, "input").find((x) => x.placeholder.startsWith("new worktree"));
  nameIn.value = "  fresh ";
  await button(inRoot).fire("click");
  check("new worktree posts the trimmed name",
    posts.length === 3 && posts[2].body.worktree === "fresh", posts[2]);

  // ---- re-filing the session under another project (claunch-51gyl.1) ----
  ctx.drop();
  ctx.setProjects([{ name: "claunch" }, { name: "hq" }]);
  const refile = ctx.render({
    session: { name: "p", cwd: "F:/works/repo", project: "claunch" },
  });
  await settle();
  const proj = tags(refile, "select")[1];
  check("a project picker is drawn beside the checkout picker", !!proj,
    tags(refile, "select").length);
  const po = opts(proj);
  check("keeping the current filing is the default",
    po[0].value === "" && po[0].text === "project: keep claunch as it is"
      && proj.value === "", po);
  check("the current project is not offered twice, the others are",
    po.map((x) => x.value).join(",") === ",default,hq", po);

  await button(refile).fire("click");
  check("a move with no filing picked does not send one",
    posts.length === 4 && !("project" in posts[3].body), posts[3]);

  proj.value = "hq";
  await button(refile).fire("click");
  check("a picked project travels with the move",
    posts.length === 5 && posts[4].body.project === "hq", posts[4]);
  const said = tags(refile, "p").pop().text;
  check("the answer names the new project and the run slots that moved",
    said.includes("project: hq") && said.includes("1 cflow run slot: w@main"), said);
  check("the answer names a mesh mirror the re-filing took with it",
    said.includes("1 mesh row re-filed: m@pcA (claunch → hq)"), said);

  // A session under no project: "keep default" first, every project after it.
  ctx.drop();
  const noProject = ctx.render({ session: { name: "q", cwd: "F:/works/repo" } });
  await settle();
  const po2 = opts(tags(noProject, "select")[1]);
  check("a session under no project keeps 'default' and offers the rest",
    po2.map((x) => x.value).join(",") === ",claunch,hq", po2);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("sessmigrate_check: ok");
}

main().catch((e) => { console.error(e); process.exit(1); });
