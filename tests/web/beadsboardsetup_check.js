/* The Settings page's Beads boards card, for a board br 0.7 needs more
   than a database for, run against the real functions from app.js.

   br 0.7 refuses a status filter on in_ready/in_review unless the board's
   .beads/policy.yaml declares them, and writes engine files beside the
   database that a .gitignore from before 0.7 does not cover. The card's one
   init route (claunch beads init --workspace <name>) makes all three. What
   has to hold: a board with no database offers Create; a board that has one
   but lacks either file says so and offers Set up, which names what it adds;
   a complete board offers neither; a policy.yaml claunch will not rewrite is
   flagged but gets no button for it; and the notice after the press says
   what was made. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("node:assert/strict");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
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

function el(tag, cls, text) {
  return {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], handlers: {}, title: "", attrs: {},
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
    setAttribute(k, v) { this.attrs[k] = v; },
  };
}

const inits = [];
const ctx = {
  el,
  beadsBoardsDraft: {},
  beadsBoardsBusy: "",
  beadsBoardInit: (b) => inits.push(b.board),
  beadsBoardSet() {}, beadsBoardReset() {},
};
vm.createContext(ctx);
vm.runInContext(
  ["plural", "beadsBoardPathProblem", "beadsBoardSetupMissing",
   "beadsBoardInitNotice", "beadsBoardSettingsRow"].map(slice).join("\n"),
  ctx);

function walk(node, out = []) {
  out.push(node);
  for (const c of node.children || []) walk(c, out);
  return out;
}
const buttons = (row) => walk(row).filter((n) => n.tag === "button");
const badges = (row) => walk(row).filter((n) => /\bbadge\b/.test(n.cls)).map((n) => n.text);

const base = {
  board: "gds6", kind: "workspace", path: "/w/gds6",
  db: "/w/gds6/.beads/beads.db", default_db: "/w/gds6/.beads/beads.db",
  configured: false, path_exists: true, shared_with: [], suggestions: [],
};
const complete = { database: true, policy: "declared", gitignore: true, complete: true };

/* ---- no database: Create, as before, now naming the whole setup -------- */
let row = ctx.beadsBoardSettingsRow({ ...base, exists: false,
  setup: { database: false, policy: "missing", gitignore: false, complete: false } });
let make = buttons(row).find((b) => b.text === "Create");
assert.ok(make, "a board with no database offers Create");
assert.match(make.title, /br init --prefix gds6/);
assert.match(make.title, /claunch beads init --workspace gds6/);
assert.equal(badges(row).includes("not set up for br 0.7"), false,
             "a missing database is its own badge, not 'not set up'");
make.handlers.click[0]();
assert.deepEqual(inits, ["gds6"]);

/* ---- database there, gitignore missing: Set up, and what it adds ------- */
row = ctx.beadsBoardSettingsRow({ ...base, exists: true, issues: 3,
  setup: { database: true, policy: "declared", gitignore: false, complete: false } });
assert.ok(badges(row).includes("not set up for br 0.7"));
make = buttons(row).find((b) => b.text === "Set up");
assert.ok(make, "a board made before br 0.7 offers Set up");
assert.match(make.title, /\.gitignore lines/);
assert.doesNotMatch(make.title, /policy\.yaml/);
assert.match(make.title, /database is not touched/);
assert.equal(buttons(row).some((b) => b.text === "Create"), false);

row = ctx.beadsBoardSettingsRow({ ...base, exists: true, issues: 3,
  setup: { database: true, policy: "missing", gitignore: true, complete: false } });
assert.match(buttons(row).find((b) => b.text === "Set up").title,
             /policy\.yaml declaring in_ready, in_review/);

/* ---- complete: neither button ------------------------------------------ */
row = ctx.beadsBoardSettingsRow({ ...base, exists: true, issues: 3, setup: complete });
assert.deepEqual(buttons(row).map((b) => b.text), ["Save"]);
assert.equal(badges(row).includes("not set up for br 0.7"), false);
assert.ok(badges(row).includes("3 issues"));

/* ---- an older daemon sends no setup: the card stays as it was ---------- */
row = ctx.beadsBoardSettingsRow({ ...base, exists: true, issues: 1 });
assert.deepEqual(buttons(row).map((b) => b.text), ["Save"]);

/* ---- a policy.yaml claunch will not rewrite: flagged, no button -------- */
row = ctx.beadsBoardSettingsRow({ ...base, exists: true, issues: 0,
  setup: { database: true, policy: "unreadable", gitignore: true, complete: true } });
assert.ok(badges(row).includes("policy.yaml unreadable"));
assert.deepEqual(buttons(row).map((b) => b.text), ["Save"]);

/* ---- the notice after a press says what was made ----------------------- */
const b = { ...base };
assert.equal(
  ctx.beadsBoardInitNotice(b, { created: true, policy: true, gitignore: true,
                                board: { db: "/w/gds6/.beads/beads.db" } }),
  "gds6: database created at /w/gds6/.beads/beads.db, policy.yaml written, .gitignore lines added.");
assert.equal(
  ctx.beadsBoardInitNotice(b, { created: true, imported: true, policy: true, gitignore: false }),
  "gds6: database rebuilt from issues.jsonl at /w/gds6/.beads/beads.db, policy.yaml written.");
assert.equal(
  ctx.beadsBoardInitNotice(b, { created: false, policy: false, gitignore: true }),
  "gds6: .gitignore lines added.");
assert.equal(
  ctx.beadsBoardInitNotice(b, { created: false, policy: false, gitignore: false }),
  "gds6 was already set up — nothing changed.");

console.log("beadsboardsetup_check ok");
