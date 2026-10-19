/* The sessions rail's row delta, page side (claunch-4thlk.3).

   The poll sends the digests of the rows it holds and the daemon answers
   with only the rows that changed, plus every name in order. What has to
   hold, run against the real sessionRowsTake/sessionRowDigests from app.js:

   * a whole answer (HTTP, an older daemon) is the rows as they came, and
     leaves no keep -- the next poll sends no digests and gets them all;
   * a delta answer rebuilds the full list in the daemon's order: changed and
     new rows from the answer, the rest from the keep, and a name the answer
     no longer lists is a row gone;
   * a row taken from the keep is the row as the daemon sent it, not as the
     page changed it since (the page writes optimistic overrides into the
     rows it caches);
   * an answer that names a row this page does not hold voids the poll and
     drops the keep, so the next poll is whole. */
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const src = fs.readFileSync(path.join(__dirname, "..", "..", "src", "claude_launcher",
                                      "web", "static", "app.js"), "utf8");

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

const ctx = new Function([
  "const sessionRowsKept = new Map();",
  slice("sessionRowDigests"),
  slice("sessionRowsTake"),
  "return { take: sessionRowsTake, digests: sessionRowDigests, kept: sessionRowsKept };",
].join("\n"))();

const row = (name, v) => ({ name, status: "running", v });

// A whole answer: as it came, and nothing kept.
let rows = ctx.take("current", { sessions: [row("a", 1), row("b", 1)] });
assert.deepStrictEqual(rows.map((r) => r.name), ["a", "b"]);
assert.deepStrictEqual(ctx.digests("current"), {}, "a whole answer leaves no digests");

// The first delta-capable answer carries every row and its digest.
rows = ctx.take("current", {
  delta: true, names: ["a", "b", "c"],
  sessions: [row("a", 1), row("b", 1), row("c", 1)],
  row_hashes: { a: "ha1", b: "hb1", c: "hc1" },
});
assert.deepStrictEqual(rows.map((r) => r.name), ["a", "b", "c"]);
assert.deepStrictEqual(ctx.digests("current"), { a: "ha1", b: "hb1", c: "hc1" });

// The page writes an override into a row it holds.
rows[0].v = "overridden";

// Nothing changed: no rows, every name. The keep answers, as sent.
rows = ctx.take("current", { delta: true, names: ["a", "b", "c"], sessions: [], row_hashes: {} });
assert.deepStrictEqual(rows, [row("a", 1), row("b", 1), row("c", 1)],
                       "a kept row is the daemon's, not the page's override");

// b changed, c is gone, d is new and lands where the daemon put it.
rows = ctx.take("current", {
  delta: true, names: ["d", "a", "b"],
  sessions: [row("d", 1), row("b", 2)],
  row_hashes: { d: "hd1", b: "hb2" },
});
assert.deepStrictEqual(rows, [row("d", 1), row("a", 1), row("b", 2)]);
assert.deepStrictEqual(ctx.digests("current"), { d: "hd1", a: "ha1", b: "hb2" },
                       "the gone row's digest is not sent again");

// Each poll state keeps its own rows.
assert.deepStrictEqual(ctx.digests("all"), {});

// A name this page does not hold: the poll is void and the keep dropped.
assert.strictEqual(ctx.take("current", {
  delta: true, names: ["a", "zz"], sessions: [], row_hashes: {},
}), null);
assert.deepStrictEqual(ctx.digests("current"), {}, "the next poll asks for every row");

console.log("railrowdelta_check ok");
