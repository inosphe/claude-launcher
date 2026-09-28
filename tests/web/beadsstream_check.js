/* The Beads board reads ONE page and shows it.

   Three things this pins, all of which were different under the infinite
   scroll it replaced: a page request carries the status filter (so the page
   is a page of what is drawn), a page REPLACES what was on screen rather
   than being merged onto it, and the answer to a request the reader has
   already moved past is dropped. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let n = src.indexOf(") {", start) + 2; n < src.length; n++) {
    if (src[n] === "{") depth++;
    if (src[n] === "}" && !--depth) return src.slice(head, n + 1);
  }
  throw new Error(name);
}
const ctx = vm.createContext({ URLSearchParams, Math, requests: [], shown: [] });
vm.runInContext(`
let beadsOpen = true, beadsSection = "board", beadsLoading = false, beadsMore = false;
let beadsPage = 0, beadsTotal = null, beadsStreamVersion = 0, beadsPri = null;
let beadsCache = null, beadsError = "", beadsSort = "updated_at", beadsDirection = "desc";
let beadsFilter = "active", beadsLayout = "tree", beadsSession = "", beadsWorkspace = "/repo";
let beadsLanePages = {};
const BEADS_PAGE_SIZE = 48;
const BEADS_ACTIVE = new Set(["open", "in_ready", "in_progress", "in_review", "blocked"]);
function renderBeads() { shown.push(beadsCache && beadsCache.marker); }
async function refreshBeadsDetail() {}
function refreshBeadsRelated() {}
function api(url) { return new Promise(resolve => requests.push({ url, resolve })); }
` + slice("loadBeadsPage") + slice("restartBeadsStream")
  + slice("beadsReadPage") + slice("beadsGoToPage") + slice("beadsPageCount") + slice("beadsStatusQuery"), ctx);

function reply(n, marker, { more = true, total = 200 } = {}) {
  ctx.requests[n].resolve({
    ok: true, status: 200,
    json: async () => ({ marker, boards: [{ root: "/repo", has_more: more, total }], has_more: more, total,
                         next_offset: more ? undefined : null }),
  });
}
const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  // The default filter is `active`, so the request names those statuses and
  // the daemon cuts the page to them.
  vm.runInContext("restartBeadsStream()", ctx);
  const first = ctx.requests[0].url;
  assert(first.includes("offset=0") && first.includes("limit=48"), first);
  for (const s of ["open", "in_ready", "in_progress", "in_review", "blocked"]) {
    assert(first.includes(`status=${s}`), `${s} missing from ${first}`);
  }

  // A filter change renumbers the pages, so it restarts at the first one.
  vm.runInContext('beadsPage = 3; beadsFilter = "closed"; restartBeadsStream()', ctx);
  assert.strictEqual(vm.runInContext("beadsPage", ctx), 0);
  assert(ctx.requests[1].url.includes("status=closed"), ctx.requests[1].url);
  assert(!ctx.requests[1].url.includes("status=open"), ctx.requests[1].url);

  // The answer to the request that was superseded is dropped.
  reply(0, "stale");
  await flush();
  assert.deepStrictEqual(ctx.shown, [null, null]);   // two renders, no data
  assert.strictEqual(vm.runInContext("beadsLoading", ctx), true);
  reply(1, "page-1");
  await flush();
  assert.strictEqual(vm.runInContext("beadsCache.marker", ctx), "page-1");
  assert.strictEqual(vm.runInContext("beadsTotal", ctx), 200);
  // 200 issues at 48 a page is five pages, the last one short.
  assert.strictEqual(vm.runInContext("beadsPageCount()", ctx), 5);

  // Next page: a fresh offset, and the page REPLACES the one before it.
  vm.runInContext("beadsGoToPage(1)", ctx);
  assert(ctx.requests[2].url.includes("offset=48"), ctx.requests[2].url);
  reply(2, "page-2");
  await flush();
  assert.strictEqual(vm.runInContext("beadsCache.marker", ctx), "page-2");
  assert.strictEqual(vm.runInContext("beadsPage", ctx), 1);

  // A page past the last one is clamped to the last one rather than asking
  // the daemon for an offset the board does not have.
  const before = ctx.requests.length;
  vm.runInContext("beadsGoToPage(99)", ctx);
  assert(ctx.requests[before].url.includes(`offset=${4 * 48}`), ctx.requests[before].url);
  assert.strictEqual(vm.runInContext("beadsPage", ctx), 1); // old rows until the response arrives
  reply(before, "page-5", { more: false, total: 200 });
  await flush();

  // A daemon that reports no total leaves next/previous working and the page
  // count unknown, rather than the control refusing to draw.
  vm.runInContext("beadsGoToPage(0)", ctx);
  ctx.requests[ctx.requests.length - 1].resolve({
    ok: true, status: 200, json: async () => ({ marker: "no-total", boards: [{ root: "/repo", has_more: true }], has_more: true }),
  });
  await flush();
  assert.strictEqual(vm.runInContext("beadsTotal", ctx), null);
  assert.strictEqual(vm.runInContext("beadsPageCount()", ctx), 0);
  assert.strictEqual(vm.runInContext("beadsMore", ctx), true);

  console.log("beadsstream_check ok");
})().catch(err => { console.error(err); process.exitCode = 1; });
