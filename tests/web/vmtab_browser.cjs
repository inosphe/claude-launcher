/* The dashboard's VM tab (#/vm; vm-page.js, vm.js).

   Serves the real index.html and the VM panel against a fixture daemon, and
   drives a real browser:
     1. the nav has a VM link; opening it shows #vm-view with the panel in a
        frame (vm?embed=1) and hides the other views;
     2. the panel lists the registered VM; the register form lists tart's VMs,
        marks the registered one and an unusable name as not selectable, and
        registering posts the VM and then a reconcile;
     3. granting takes its session from the session list and sends the
        scopes ticked (exec asks for a confirmation first);
     3b. the file exchange section: the folders, the "not executed" text, the guest
        folder setting (user route), the recent-transfer log and the files tick;
     3c. shared folders (roots shown, add with an rw confirmation, remove, pending
        note) and access requests (approve with a confirmation for exec/rw, deny);
     4. leaving the tab removes the frame. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const posts = [];
const hits = [];
let sessionsDown = false;
const VM = {
  id: "alpha", tart_name: "alpha", state: "running", ready: true, channel: "up", controller: "",
  grants: { s1: ["files"] }, human: "idle", ip: "", fault_reason: "", expose: ["2222:22"], allow: [],
  net_pending: true, net_applied_known: true, locked: false,
  shared_dirs: [{ name: "src", host_path: "/work/proj", mode: "ro", guest_path: "/Volumes/My Shared Files/src" }],
  shares_pending: true, shares_skipped: { old: "vm_share_forbidden: nope" },
  requests: [{ session: "s2", scopes: ["files", "exec"], reason: "build it",
    shares: [{ name: "out", host_path: "/work/out", mode: "rw", guest_path: "/Volumes/My Shared Files/out" }] }],
};
const BETA = { ...VM, id: "beta", tart_name: "beta", state: "stopped", ready: false, channel: "idle", locked: false };
const json = (res, body, status = 200) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
};
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  const p = url.pathname;
  if (req.method === "GET" && p.startsWith("/api/vm")) hits.push(p);
  if (req.method === "POST" && /^\/api\/vm\/beta\/(un)?lock$/.test(p)) {
    BETA.locked = p.endsWith("/lock") && !p.endsWith("/unlock");
    posts.push([p, {}]);
    return json(res, BETA);
  }
  if (req.method === "POST") {
    let data = "";
    req.on("data", (c) => { data += c; });
    req.on("end", () => {
      posts.push([p, data ? JSON.parse(data) : {}]);
      json(res, { ok: true, changed: [] });
    });
    return;
  }
  if (p === "/api/vm") return json(res, { vms: [VM, BETA], slot_limit: 2, host_locked: false, share_roots: ["/work"] });
  if (p === "/api/vm/alpha/files") {
    return json(res, { host_dir: "/home/u/.claude-launcher/daemon/vm/alpha/files", guest_dir: "/Users/admin/work",
      guest_dir_default: "/Users/admin/work", limits: { max_bytes: 536870912, max_items: 10000, timeout: 600 },
      recent: [
        { at: 1700000000, session: "s1", op: "put", path: "n.txt", ok: true, bytes: 5, items: 1 },
        { at: 1700000100, session: "s1", op: "get", path: "../x", ok: false, code: "vm_files_unsafe" },
      ] });
  }
  if (p === "/api/vm-tart") {
    return json(res, { tart: [
      { name: "alpha", state: "running", running: true, registered: true, id: "alpha", usable: true },
      { name: "beta", state: "stopped", running: false, registered: false, id: "", usable: true },
      { name: "bad name", state: "stopped", running: false, registered: false, id: "", usable: false },
    ] });
  }
  if (p === "/api/vm-presets") {
    return json(res, { presets: [{ name: "gold", tart_name: "preset-gold", description: "깨끗한 설치",
      source: "alpha", created_at: 1700000000, present: true }] });
  }
  if (p === "/api/webdebug/sessions" && sessionsDown) return json(res, {}, 500);
  if (p === "/api/webdebug/sessions") return json(res, { sessions: [{ name: "s1", status: "running" }] });
  if (p.startsWith("/api/")) return json(res, {}, 404);
  const rel = p === "/" ? "index.html" : p === "/vm" ? "vm.html" : p.replace(/^\/static\//, "");
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
    const page = await (await browser.newContext({ viewport: { width: 1300, height: 900 } })).newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(base + "#/vm");

    // 1. the tab and its frame
    assert.equal(await page.locator("#rail-nav a[data-page=vm]").count(), 1, "the nav has a VM link");
    await page.waitForSelector("#vm-view iframe", { timeout: 5000 });
    assert.equal(await page.evaluate(() => document.getElementById("vm-view").classList.contains("hidden")), false);
    assert.equal(await page.evaluate(() => document.getElementById("hive-view").classList.contains("hidden")), true);
    assert.match(await page.getAttribute("#vm-view iframe", "src"), /vm\?embed=1$/);
    const frame = page.frameLocator("#vm-view iframe");

    // 2. the panel and the register form
    await frame.locator(".vm-card", { hasText: "alpha" }).waitFor({ timeout: 5000 });
    assert.equal(await frame.locator("body.embed").count(), 1, "the panel knows it is embedded");
    await frame.locator("#vm-register summary").click();
    await frame.locator("#vm-tart-pick option", { hasText: "beta" }).waitFor({ state: "attached", timeout: 5000 });
    const options = await frame.locator("#vm-tart-pick option").evaluateAll(
      (os) => os.map((o) => ({ text: o.textContent, disabled: o.disabled })));
    assert.ok(options.find((o) => o.text.includes("alpha") && o.text.includes("등록됨") && o.disabled),
      `the registered VM is shown and not selectable: ${JSON.stringify(options)}`);
    assert.ok(options.find((o) => o.text.includes("bad name") && o.disabled), "an unusable name is not selectable");
    assert.equal(await frame.locator("#vm-register-go").isDisabled(), true, "nothing picked yet");
    await frame.locator("#vm-tart-pick").selectOption("beta");
    assert.equal(await frame.locator("#vm-register-id").inputValue(), "beta", "the id defaults to the tart name");
    await frame.locator("#vm-register-go").click();
    await page.waitForFunction(() => true);
    await frame.locator("#vm-activity", { hasText: "완료" }).waitFor({ timeout: 5000 });
    const reg = posts.findIndex(([u, b]) => u === "/api/vm" && b.id === "beta" && b.tart_name === "beta");
    assert.ok(reg >= 0, `registering posts the VM: ${JSON.stringify(posts)}`);
    assert.equal(posts[reg + 1][0], "/api/vm-reconcile", "and then a reconcile");

    // 2b. presets: listed, saved from the selected (stopped) VM, created, deleted
    await frame.locator("#vm-presets summary").click();
    await frame.locator("#vm-preset-list li", { hasText: "gold" }).waitFor({ timeout: 5000 });
    assert.equal(await frame.locator("#vm-preset-save-go").isDisabled(), true, "alpha runs: nothing to save");
    await frame.locator("#vm-preset-newid").fill("fresh");
    await frame.locator("#vm-preset-list li", { hasText: "gold" }).getByRole("button", { name: "새 VM" }).click();
    await frame.locator("#vm-activity", { hasText: "gold에서 fresh 만들기 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u]) => u === "/api/vm-presets/gold/create")[1], { id: "fresh" });
    let sure = "";
    page.once("dialog", async (d) => { sure = d.message(); await d.accept(); });
    await frame.locator("#vm-preset-list li", { hasText: "gold" }).getByRole("button", { name: "삭제" }).click();
    await frame.locator("#vm-activity", { hasText: "프리셋 gold 삭제 완료" }).waitFor({ timeout: 5000 });
    assert.match(sure, /preset-gold/, "the confirmation names the copy");
    assert.deepEqual(posts.find(([u]) => u === "/api/vm-presets/gold/delete")[1], { confirm: "gold" });

    // 2c. network exposure: shown, warned about, added (after a confirmation), removed
    assert.match(await frame.locator("#vm-net-warning").innerText(), /같은 LAN의 모든 기기/);
    await frame.locator("#vm-expose tbody tr", { hasText: "2222" }).waitFor({ timeout: 5000 });
    assert.match(await frame.locator("#vm-net-pending").innerText(), /다시 시작하면 적용/);
    await frame.locator("#vm-allow-cidr").fill("192.168.1.213");
    await frame.locator("#vm-allow-go").click();
    await frame.locator("#vm-activity", { hasText: "허용 192.168.1.213 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm/alpha/allow")[1], { action: "add", cidr: "192.168.1.213" });
    page.once("dialog", async (d) => { assert.match(d.message(), /LAN/); await d.accept(); });
    await frame.locator("#vm-expose-ext").fill("8080");
    await frame.locator("#vm-expose-int").fill("80");
    await frame.locator("#vm-expose-go").click();
    await frame.locator("#vm-activity", { hasText: "포트 8080:80 노출 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm/alpha/expose" && b.action === "add")[1],
      { action: "add", external: "8080", internal: "80" });
    await frame.locator("#vm-expose tbody tr", { hasText: "2222" }).getByRole("button", { name: "삭제" }).click();
    await frame.locator("#vm-activity", { hasText: "포트 2222 노출 해제 완료" }).waitFor({ timeout: 5000 });
    // 2d. the VM lock: a stopped VM is locked, start is disabled with the reason, then unlocked
    await frame.locator(".vm-card", { hasText: "beta" }).click();
    await frame.locator("#vm-name", { hasText: "beta" }).waitFor({ timeout: 5000 });
    assert.equal(await frame.locator("#vm-guard-toggle").isDisabled(), false);
    assert.equal(await frame.locator("#vm-start").isDisabled(), false);
    await frame.locator("#vm-guard-toggle").click();
    await frame.locator("#vm-guard-chip").waitFor({ state: "visible", timeout: 5000 });
    assert.match(await frame.locator("#vm-guard-note").innerText(), /잠겨 있어 시작할 수 없고/);
    assert.equal(await frame.locator("#vm-start").isDisabled(), true, "a locked VM cannot be started from the panel");
    assert.match(await frame.locator("#vm-guard-toggle").innerText(), /잠금 해제/);
    page.once("dialog", async (d) => { assert.match(d.message(), /다시 잠그십시오/); await d.accept(); });
    await frame.locator("#vm-guard-toggle").click();
    await frame.locator("#vm-guard-chip").waitFor({ state: "hidden", timeout: 5000 });
    assert.equal(await frame.locator("#vm-start").isDisabled(), false);
    assert.ok(posts.some(([u]) => u === "/api/vm/beta/lock") && posts.some(([u]) => u === "/api/vm/beta/unlock"));
    await frame.locator(".vm-card", { hasText: "alpha" }).click();
    await frame.locator("#vm-name", { hasText: "alpha" }).waitFor({ timeout: 5000 });
    assert.equal(await frame.locator("#vm-guard-toggle").isDisabled(), true, "a running VM is not locked (shut it down first)");

    // 3. granting from the session list
    await frame.locator("#vm-grant-session option", { hasText: "s1" }).waitFor({ state: "attached", timeout: 15000 });
    await frame.locator("#vm-grant-session").selectOption("s1");
    await frame.locator("#vm-grant-control").check();
    await frame.locator("#vm-grant").click();
    await frame.locator("#vm-activity", { hasText: "s1 view+control 부여" }).waitFor({ timeout: 5000 });
    const grant = posts.find(([u]) => u === "/api/vm/alpha/grant");
    assert.deepEqual(grant[1], { session: "s1", scopes: ["view", "control"] });
    // 3c. shared folders and requests
    assert.match(await frame.locator("#vm-share-roots").innerText(), /\/work/, "the allowed root is shown");
    await frame.locator("#vm-shares tbody tr", { hasText: "/Volumes/My Shared Files/src" }).waitFor({ timeout: 5000 });
    assert.match(await frame.locator("#vm-share-pending").innerText(), /다시 시작하면 적용.*제외된 공유 old/s);
    await frame.locator("#vm-share-name").fill("out");
    await frame.locator("#vm-share-path").fill("/work/out");
    await frame.locator("#vm-share-rw").check();
    let rwAsked = "";
    page.once("dialog", async (d) => { rwAsked = d.message(); await d.accept(); });
    await frame.locator("#vm-share-go").click();
    await frame.locator("#vm-activity", { hasText: "공유 out 추가 완료" }).waitFor({ timeout: 5000 });
    assert.match(rwAsked, /rw/, "rw asks first");
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm/alpha/share" && b.action === "add")[1],
      { action: "add", name: "out", host_path: "/work/out", mode: "rw" });
    await frame.locator("#vm-shares tbody tr", { hasText: "src" }).getByRole("button", { name: "삭제" }).click();
    await frame.locator("#vm-activity", { hasText: "공유 src 삭제 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm/alpha/share" && b.action === "rm")[1], { action: "rm", name: "src" });
    await frame.locator("#vm-requests tbody tr", { hasText: "s2" }).waitFor({ timeout: 5000 });
    assert.match(await frame.locator("#vm-requests tbody tr").innerText(), /files \+ exec.*out \(rw\) \/work\/out.*build it/s);
    let apAsked = "";
    page.once("dialog", async (d) => { apAsked = d.message(); await d.dismiss(); });
    await frame.locator("#vm-requests tbody tr").getByRole("button", { name: "승인" }).click();
    await page.waitForTimeout(200);
    assert.match(apAsked, /exec.*out 쓰기 가능\(rw\)/s, "approving exec and an rw share asks first");
    assert.equal(posts.filter(([u]) => u === "/api/vm/alpha/approve").length, 0, "a dismissed confirmation approves nothing");
    page.once("dialog", async (d) => { await d.accept(); });
    await frame.locator("#vm-requests tbody tr").getByRole("button", { name: "승인" }).click();
    await frame.locator("#vm-activity", { hasText: "s2 요청 승인 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u]) => u === "/api/vm/alpha/approve")[1], { session: "s2" });
    await frame.locator("#vm-requests tbody tr").getByRole("button", { name: "거절" }).click();
    await frame.locator("#vm-activity", { hasText: "s2 요청 거절 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u]) => u === "/api/vm/alpha/deny")[1], { session: "s2" });

    // 3b. the file exchange
    await frame.locator("#vm-files-host", { hasText: "/vm/alpha/files" }).waitFor({ timeout: 5000 });
    assert.match(await frame.locator("#vm-files-note").innerText(), /받은 파일은 실행하지 않습니다/);
    const rows = await frame.locator("#vm-files-log tbody tr").allInnerTexts();
    assert.equal(rows.length, 2, "two transfers are logged");
    assert.match(rows[0], /s1\s+get\s+\.\.\/x\s+거절: vm_files_unsafe/, "the newest comes first, a refusal shows its code");
    assert.match(rows[1], /s1\s+put\s+n\.txt\s+5 B\s+완료/);
    assert.equal(await frame.locator("#vm-files-dir").inputValue(), "/Users/admin/work");
    assert.equal(await frame.locator("#vm-files-default").isDisabled(), true, "already the default");
    await frame.locator("#vm-files-dir").fill("/Users/admin/exchange");
    await frame.locator("#vm-files-save").click();
    await frame.locator("#vm-activity", { hasText: "게스트 작업 폴더 저장 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u]) => u === "/api/vm/alpha/files_dir")[1], { dir: "/Users/admin/exchange" });
    assert.equal(await frame.getByLabel("s1 files").isChecked(), true, "the grants table has a files tick");
    assert.equal(await frame.getByLabel("s1 files").count(), 1);
    await frame.getByLabel("s1 files").uncheck();
    await frame.locator("#vm-activity", { hasText: "s1 files 회수 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm/alpha/revoke").pop()[1], { session: "s1", scopes: ["files"] });
    await frame.locator("#vm-grant-control").uncheck();
    await frame.locator("#vm-grant-files").check();
    await frame.locator("#vm-grant").click();
    await frame.locator("#vm-activity", { hasText: "s1 view+files 부여" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm/alpha/grant").pop()[1], { session: "s1", scopes: ["view", "files"] },
      "files is ticked on its own: no confirmation, no exec or control");
    assert.equal(await frame.locator("#vm-files-default").isDisabled(), true, "the default button is off while the default is set");
    assert.equal(posts.filter(([u]) => u === "/api/vm/alpha/files_dir").length, 1);

    let asked = "";
    page.on("dialog", async (d) => { asked = d.message(); await d.dismiss(); });
    await frame.locator("#vm-grant-exec").check();
    const before = posts.length;
    await frame.locator("#vm-grant").click();
    await page.waitForTimeout(300);
    assert.match(asked, /exec/, "exec asks first");
    assert.equal(posts.length, before, "a dismissed confirmation grants nothing");

    // 4. leaving the tab
    await page.evaluate(() => { location.hash = "#/settings"; });
    await page.waitForFunction(() => !document.querySelector("#vm-view iframe"), null, { timeout: 5000 });
    // nothing in the panel polls once the tab is left (its refresh runs every 2 s)
    await page.waitForTimeout(500);
    const seen = hits.length;
    await page.waitForTimeout(4500);
    assert.equal(hits.length, seen, `no VM request after leaving the tab: ${hits.slice(seen)}`);

    // the session list unreadable: the name is typed
    sessionsDown = true;
    await page.evaluate(() => { location.hash = "#/vm"; });
    await page.waitForSelector("#vm-view iframe");
    const again = page.frameLocator("#vm-view iframe");
    await again.locator("#vm-grant-session-text").waitFor({ state: "visible", timeout: 5000 });
    assert.equal(await again.locator("#vm-grant-session").isHidden(), true);
    await again.locator("#vm-grant-session-text").fill("typed-one");
    await again.locator("#vm-grant").click();
    await again.locator("#vm-activity", { hasText: "typed-one view 부여" }).waitFor({ timeout: 5000 });
    assert.deepEqual(errors, [], "no page errors");
    console.log("vmtab ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
