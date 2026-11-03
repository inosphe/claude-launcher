/* The VM page while the feature is off (daemon.vm_enabled; vm.html #vm-off, vm.js).

   Serves the real VM page against a fixture daemon whose switch starts off,
   and drives a real browser:
     1. the page shows the off card in place of the VM list, with what the host
        lacks, and asks for no VM route but the switch;
     2. "VM 기능 켜기" posts {enabled: true}; the page reloads into the VM list,
        with "VM 기능 끄기" in the header;
     3. "VM 기능 끄기" asks first, then posts {enabled: false}, and the page is
        the off card again;
     4. the session mode (vm?session=s1) shows the same off card. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");

const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const HOST = { macos: true, apple_silicon: false, tart: false, ok: false };
let enabled = false;
const posts = [];
const vmGets = [];

const json = (res, body, status = 200) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
};
const server = http.createServer((req, res) => {
  const p = new URL(req.url, "http://fixture").pathname;
  if (p === "/api/vm-enabled") {
    if (req.method === "POST") {
      let data = "";
      req.on("data", (c) => { data += c; });
      req.on("end", () => {
        const body = JSON.parse(data || "{}");
        posts.push(body);
        enabled = body.enabled;
        json(res, { enabled, host: HOST });
      });
      return;
    }
    return json(res, { enabled, host: HOST });
  }
  if (p.startsWith("/api/vm")) {
    vmGets.push(p);
    if (!enabled) return json(res, { ok: false, code: "vm_disabled", error: "vm_disabled: off" }, 409);
    if (p === "/api/vm") return json(res, { vms: [], slot_limit: 2, host_locked: false, share_roots: [], share_root_rows: [], share_catalog: [] });
    return json(res, {});
  }
  if (p.startsWith("/api/")) return json(res, {}, 404);
  const rel = p === "/vm" ? "vm.html" : p.replace(/^\/static\//, "");
  try {
    const body = fs.readFileSync(path.join(root, rel));
    res.setHeader("Content-Type", TYPES[path.extname(rel)] || "application/octet-stream");
    res.end(body);
  } catch { res.statusCode = 404; res.end(); }
});

(async () => {
  await safeListen(server);
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await (await browser.newContext({ viewport: { width: 1200, height: 800 } })).newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));

    // 1. off: the card, the host note, no VM route asked for
    await page.goto(base + "vm");
    await page.locator("#vm-off").waitFor({ state: "visible", timeout: 5000 });
    assert.equal(await page.locator("#vm-main").isHidden(), true);
    assert.equal(await page.locator("#vm-disable").isHidden(), true);
    const note = await page.locator("#vm-off-host").textContent();
    assert.match(note, /Apple Silicon Mac이 아닙니다/);
    assert.match(note, /tart가 설치되어 있지 않습니다/);
    await page.waitForTimeout(300);
    assert.deepEqual(vmGets, [], "an off page polls no VM route");

    // 2. on: posts the switch, reloads into the list
    await page.locator("#vm-enable").click();
    await page.locator("#vm-main").waitFor({ state: "visible", timeout: 5000 });
    assert.deepEqual(posts, [{ enabled: true }]);
    assert.equal(await page.locator("#vm-off").isHidden(), true);
    await page.locator("#vm-disable").waitFor({ state: "visible", timeout: 5000 });
    assert.ok(vmGets.includes("/api/vm"), "the list is read once the feature is on");

    // 3. off again, after a confirmation
    page.once("dialog", (d) => d.accept());
    await page.locator("#vm-disable").click();
    await page.locator("#vm-off").waitFor({ state: "visible", timeout: 5000 });
    assert.deepEqual(posts, [{ enabled: true }, { enabled: false }]);

    // 4. the session mode shows the same card
    await page.goto(base + "vm?embed=1&session=s1");
    await page.locator("#vm-off").waitFor({ state: "visible", timeout: 5000 });
    assert.equal(await page.locator("#vm-main").isHidden(), true);

    assert.deepEqual(errors, [], "no page errors");
    console.log("vmoff ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
