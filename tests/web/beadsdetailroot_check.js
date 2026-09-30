/* The detail pane follows an issue to the board it is on.

   A link to an issue carries no board -- a rail pill, a report's "issue ↗",
   an address typed by hand -- so an id that is not on the loaded page is
   asked for on the selected board, and the daemon answers from whichever
   board has it (claunch-ob8sz). What must hold:

   1. the request names the selected board when the id is not on the page,
      and the board the page listed it under when it is;
   2. an answer from another board the listing has moves the tabs and lanes
      there: the selection changes and the stream restarts once;
   3. an answer from the board already selected restarts nothing;
   4. an answer naming a board the listing lacks changes nothing -- the next
      page read would reset it to the first tab and come back here again;
   5. a refusal is shown as the pane's error and moves nothing. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let n = src.indexOf(") {", start) + 2; n < src.length; n++) {
    if (src[n] === "{") depth++;
    if (src[n] === "}" && !--depth) return src.slice(head, n + 1);
  }
  throw new Error(name);
}
const ctx = vm.createContext({ requests: [], restarts: [], answer: null });
vm.runInContext(`
let beadsOpen = true, beadsSection = "board";
let beadsFocus = "", beadsDetail = null, beadsWorkspace = "/gds6";
let beadsCache = { boards: [
  { root: "/gds6", issues: [{ id: "gds6-wt1.2" }] },
  { root: "/claunch", issues: [] },
] };
function restartBeadsStream() { restarts.push(beadsWorkspace); }
async function api(url) { requests.push(url); return answer; }
` + slice("beadsRootOf") + slice("refreshBeadsDetail"), ctx);

function reply(status, body) {
  ctx.answer = { ok: status === 200, status, json: async () => body };
}
const run = (code) => vm.runInContext(code, ctx);

(async () => {
  // 2. The user's case: the gds6 board is selected and a claunch issue is
  // opened. It is not on the page, so the selected board is asked; the
  // daemon answers from the claunch board and the page moves there.
  run('beadsFocus = "claunch-kg3oe.1.1"');
  reply(200, { root: "/claunch", issue: { id: "claunch-kg3oe.1.1" } });
  await run("refreshBeadsDetail()");
  assert.strictEqual(ctx.requests[0],
    "/api/beads/claunch-kg3oe.1.1?cwd=" + encodeURIComponent("/gds6"));
  assert.strictEqual(run("beadsWorkspace"), "/claunch");
  assert.deepStrictEqual(ctx.restarts, ["/claunch"]);
  assert.strictEqual(run("beadsDetail.issue.id"), "claunch-kg3oe.1.1");

  // 3. The poll after the move asks again and finds it where it now is.
  await run("refreshBeadsDetail()");
  assert.strictEqual(ctx.requests[1],
    "/api/beads/claunch-kg3oe.1.1?cwd=" + encodeURIComponent("/claunch"));
  assert.deepStrictEqual(ctx.restarts, ["/claunch"]);

  // 1. An id the page listed is asked for on the board it was listed under,
  // even while another board is selected.
  run('beadsFocus = "gds6-wt1.2"');
  reply(200, { root: "/gds6", issue: { id: "gds6-wt1.2" } });
  await run("refreshBeadsDetail()");
  assert.strictEqual(ctx.requests[2],
    "/api/beads/gds6-wt1.2?cwd=" + encodeURIComponent("/gds6"));
  assert.strictEqual(run("beadsWorkspace"), "/gds6");
  assert.deepStrictEqual(ctx.restarts, ["/claunch", "/gds6"]);

  // 4. A board the listing does not have is not selected.
  run('beadsFocus = "elsewhere-1"');
  reply(200, { root: "/elsewhere", issue: { id: "elsewhere-1" } });
  await run("refreshBeadsDetail()");
  assert.strictEqual(run("beadsWorkspace"), "/gds6");
  assert.strictEqual(ctx.restarts.length, 2);
  assert.strictEqual(run("beadsDetail.issue.id"), "elsewhere-1");

  // 5. No board has it: the pane says so and nothing moves.
  run('beadsFocus = "nowhere-1"');
  reply(404, { error: "br show nowhere-1 --json failed (3): ISSUE_NOT_FOUND" });
  await run("refreshBeadsDetail()");
  assert(run("beadsDetail.error").includes("ISSUE_NOT_FOUND"));
  assert.strictEqual(run("beadsWorkspace"), "/gds6");
  assert.strictEqual(ctx.restarts.length, 2);

  console.log("beadsdetailroot: ok");
})().catch((err) => { console.error(err); process.exit(1); });
