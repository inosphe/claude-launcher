/* Exercise overlapping stream requests when the operator changes sort. */
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
const ctx = vm.createContext({ URLSearchParams, requests: [], merged: [] });
vm.runInContext(`
let beadsOpen = true, beadsSection = "board", beadsLoading = false, beadsMore = true;
let beadsNextOffset = 0, beadsStreamVersion = 0, beadsPri = null, beadsCache = null;
let beadsError = "", beadsSort = "updated_at", beadsDirection = "desc";
const BEADS_STREAM_PAGE = 48;
function renderBeads() {}
function mergeBeadsPage(data) { merged.push(data.marker); beadsCache = data; }
async function refreshBeadsDetail() {}
function refreshBeadsRelated() {}
function api(url) { return new Promise(resolve => requests.push({ url, resolve })); }
` + slice("loadBeadsPage") + slice("restartBeadsStream"), ctx);
function reply(n, marker, more = true) {
  ctx.requests[n].resolve({ ok: true, status: 200,
    json: async () => ({ marker, has_more: more, next_offset: more ? 48 : null }) });
}
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  vm.runInContext("restartBeadsStream()", ctx);
  assert(ctx.requests[0].url.includes("sort=updated_at&direction=desc"));
  vm.runInContext('beadsSort = "priority"; beadsDirection = "asc"; restartBeadsStream()', ctx);
  assert(ctx.requests[1].url.includes("sort=priority&direction=asc"));
  reply(0, "stale");
  await flush();
  assert.deepStrictEqual(ctx.merged, []);
  assert.strictEqual(vm.runInContext("beadsLoading", ctx), true);
  reply(1, "current");
  await flush();
  assert.deepStrictEqual(ctx.merged, ["current"]);
  vm.runInContext("loadBeadsPage()", ctx);
  assert(ctx.requests[2].url.includes("offset=48"));
  assert(ctx.requests[2].url.includes("sort=priority&direction=asc"));
  reply(2, "next", false);
  await flush();
  assert.deepStrictEqual(ctx.merged, ["current", "next"]);
  assert.strictEqual(vm.runInContext("beadsLoading || beadsMore", ctx), false);
  console.log("beadsstream_check ok");
})().catch(err => { console.error(err); process.exitCode = 1; });
