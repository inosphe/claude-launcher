/* The v2 run view's collections table and trigger lines (docs/cflow-v2.md
   2.7, 2.8), run against the real functions sliced from app.js. */
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8");

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

const ctx = {};
new Function("ctx",
  `${slice("cflow2CollectionRows")} ${slice("cflow2StatusValues")} ${slice("cflow2TriggerLines")}
   Object.assign(ctx, { cflow2CollectionRows, cflow2StatusValues, cflow2TriggerLines });`)(ctx);

// A queue: keyed by its key field, status apart, the rest as name=value.
const queue = {
  kind: "queue", key: "issue",
  status: { values: ["requested", "waiting", "landed"] },
  items: [
    { issue: "c-1", status: "requested", branch: "w1", tip: "abc", requested_by: "w1", note: null },
    { issue: "c-2", status: "landed", branch: "w2", tip: "def", requested_by: "w2" },
  ],
};
assert.deepStrictEqual(ctx.cflow2CollectionRows(queue), [
  { key: "c-1", status: "requested", fields: "branch=w1  tip=abc  requested_by=w1", links: [] },
  { key: "c-2", status: "landed", fields: "branch=w2  tip=def  requested_by=w2", links: [] },
]);
assert.deepStrictEqual(ctx.cflow2StatusValues(queue), ["requested", "waiting", "landed"]);
assert.deepStrictEqual(ctx.cflow2StatusValues({ status_values: ["a"] }), ["a"]);
assert.deepStrictEqual(ctx.cflow2StatusValues({}), []);

// A log: keyed by sequence number, the writer shown first.
const log = { kind: "log", items: [{ seq: 3, at: "t", by: "lead", name: "cut", extra: { a: 1 } }] };
assert.deepStrictEqual(ctx.cflow2CollectionRows(log), [
  { key: "#3", status: "", fields: 'by=lead  name=cut  extra={"a":1}', links: [] },
]);
assert.deepStrictEqual(ctx.cflow2CollectionRows(undefined), []);

// URL fields leave the text and become links: a PR reads "PR #N" from its
// number field or, failing that, from the URL; any other URL keeps its name.
const prs = {
  kind: "queue", key: "number",
  items: [
    { number: 12, url: "https://github.com/o/r/pull/12", branch: "s1", status: "open" },
    { issue: "c-3", pr_url: "https://github.com/o/r/pull/7", medium: "pr", status: "requested" },
    { number: 13, url: "https://example.com/x", status: "open" },
    { number: 14, url: "not a url", status: "open" },
  ],
};
const prRows = ctx.cflow2CollectionRows(prs);
assert.deepStrictEqual(prRows[0], { key: "12", status: "open", fields: "branch=s1",
  links: [{ field: "url", href: "https://github.com/o/r/pull/12", label: "PR #12" }] });
assert.deepStrictEqual(prRows[1].links,
  [{ field: "pr_url", href: "https://github.com/o/r/pull/7", label: "PR #7" }]);
assert.ok(!prRows[1].fields.includes("pr_url="));
assert.deepStrictEqual(prRows[2].links, [{ field: "url", href: "https://example.com/x", label: "url" }]);
assert.deepStrictEqual(prRows[3].links, []);
assert.strictEqual(prRows[3].fields, "url=not a url");

// Trigger lines: only trigger_* events, with what/where/why.
const lines = ctx.cflow2TriggerLines([
  { ts: "2026-10-06T10:00:00", event: "trigger_queued", do: "enqueue-landing", node: "req", at: "leave", trigger: "t1" },
  { ts: "2026-10-06T10:00:05", event: "entered", node: "x" },
  { ts: "2026-10-06T10:00:09", event: "trigger_skipped", do: "push", node: "req", trigger: "t2",
    detail: "parent 'lead' has no live run that declares 'landing'" },
  { ts: "2026-10-06T10:00:10", event: "trigger_done", do: "checks", trigger: "t3", detail: "asked for 2 status check(s)" },
]);
assert.strictEqual(lines.length, 3);
assert.strictEqual(lines[0].cls, "queued");
assert.strictEqual(lines[0].text, "2026-10-06 10:00:00  queued  enqueue-landing req (leave) [t1]");
assert.strictEqual(lines[1].cls, "skipped");
assert.ok(lines[1].text.endsWith("— parent 'lead' has no live run that declares 'landing'"));
assert.strictEqual(lines[2].cls, "done");
assert.ok(lines[2].text.includes("done  checks [t3] — asked for 2 status check(s)"));

// done/skipped events carry only the id: what and where come from the queued event.
const joined = ctx.cflow2TriggerLines([
  { ts: "2026-10-06T10:00:00", event: "trigger_queued", do: "briefing", node: "n1", at: "enter", trigger: "t9" },
  { ts: "2026-10-06T10:00:02", event: "trigger_skipped", trigger: "t9", reason: "the run has no driver session" },
]);
assert.strictEqual(joined[1].text,
  "2026-10-06 10:00:02  skipped  briefing n1 (enter) [t9] — the run has no driver session");

console.log("cflow2collections ok");
