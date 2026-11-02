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
  share_rows: [
    { name: "src", host_path: "/work/proj", mode: "ro", guest_path: "/Volumes/My Shared Files/src",
      origin: { kind: "vm", by_session: "", at: 1 }, state: "applied" },
    { name: "cat1", host_path: "/work/cat", mode: "ro", guest_path: "/Volumes/My Shared Files/cat1",
      origin: { kind: "catalog-all", by_session: "", at: 1 }, state: "pending" },
    { name: "clash", host_path: "/work/clash", mode: "rw", guest_path: "/Volumes/My Shared Files/clash",
      origin: { kind: "catalog-vms", by_session: "", at: 1 }, state: "left_out", why: "name_conflict: VM 자체 항목이 우선합니다" },
  ],
  shares_pending: true, shares_skipped: { old: "vm_share_forbidden: nope" },
  requests: [{ id: "r1", session: "s2", scopes: ["files", "exec"], reason: "build it",
    shares: [{ name: "out", host_path: "/work/out", mode: "rw", guest_path: "/Volumes/My Shared Files/out" }] }],
};
VM.cloned_from = {};
const BETA = { ...VM, id: "beta", cloned_from: { kind: "preset", preset: "gold", preset_created_at: 1700000000, at: 1700000500 }, tart_name: "beta", state: "stopped", ready: false, channel: "idle", locked: false,
  requests: [], shares_pending: false, shares_skipped: {},
  share_rows: [{ name: "cat1", host_path: "/work/cat", mode: "ro", guest_path: "/Volumes/My Shared Files/cat1",
    origin: { kind: "catalog-all", by_session: "", at: 1 }, state: "pending" }] };
const GAMMA = { ...BETA, id: "gamma", tart_name: "gamma", state: "stopped",
  cloned_from: { kind: "preset", preset: "old", preset_created_at: 1600000000, at: 1700000600 }, requests: [], share_rows: [], shared_dirs: [] };
const TMPL = { ...BETA, id: "tmpl", tart_name: "preset-gold", template: "gold", cloned_from: {}, state: "stopped", requests: [], share_rows: [], shared_dirs: [] };
const DELTA = { ...BETA, id: "delta", tart_name: "delta", state: "stopped", requests: [], share_rows: [], shared_dirs: [], cloned_from: {}, template: "" };
const EPSILON = { ...DELTA, id: "epsilon", tart_name: "epsilon", locked: true };
let REMOVED = [];
const PLANS = {
  delta: { id: "delta", tart_name: "delta", state: "stopped", locked: false, size_gb: 93, disk_gb: 100, tart_present: true, template: "",
    presets: ["gold"], descendants: ["kid"], blockers: [],
    warnings: ["이 VM에서 만든 프리셋: gold. 프리셋의 복사본(preset-<이름>)은 남지만 원본 VM이 사라집니다", "이 VM에서 만든 프리셋으로 만든 VM(후손): kid. 후손의 디스크는 독립이라 영향이 없습니다"] },
  epsilon: { id: "epsilon", tart_name: "epsilon", state: "stopped", locked: true, size_gb: 12, disk_gb: 50, tart_present: true, template: "",
    presets: [], descendants: [], blockers: [{ code: "vm_locked", reason: "epsilon는 잠겨 있습니다. 먼저 잠금을 해제하십시오" }], warnings: [] },
};
let gammaRelease = null;     // lets the held lifecycle request answer
let gammaFail = false;
const GAMMA_STATE = { start: "running", stop: "stopped", suspend: "suspended", force_stop: "stopped" };
let ONLY = [
  { session: "s1", on: false, can_enable: true, why: "", since: 0, blocks: [], limits: "x" },
  { session: "s9", on: false, can_enable: false, why: "이 세션은 VM 전용 hook 없이 시작되었습니다", since: 0, blocks: [] },
  { session: "s7", on: true, can_enable: true, why: "", since: 1700000000,
    blocks: [{ at: 1700000050, kind: "build", rule: "make", head: "make deploy" }] },
];
const CATALOG = [
  { name: "cat1", host_path: "/work/cat", mode: "ro", scope: "all", vms: [], origin: { kind: "catalog-all", by_session: "", at: 1 } },
  { name: "clash", host_path: "/work/clash", mode: "rw", scope: "vms", vms: ["alpha"], origin: { kind: "catalog-vms", by_session: "", at: 1 } },
];
let ROOTS = [{ path: "/work", real: "/work", ok: true, reason: "" },
  { path: "/old", real: "/old", ok: false, reason: "폴더가 없거나 폴더가 아닙니다" }];
const json = (res, body, status = 200) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
};
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  const p = url.pathname;
  if (p === "/api/batch") { res.statusCode = 404; return res.end("{}"); }   // the page falls back to single reads
  if (req.method === "GET" && p.startsWith("/api/vm")) hits.push(p);
  const life = req.method === "POST" && /^\/api\/vm\/gamma\/(start|stop|suspend|force_stop)$/.exec(p);
  if (life) {
    posts.push([p, {}]);
    if (gammaFail) return json(res, { ok: false, code: "vm_busy_state", error: "vm_busy_state: gamma is busy" }, 409);
    gammaRelease = () => { GAMMA.state = GAMMA_STATE[life[1]]; json(res, GAMMA); };   // answers when the test says
    return;
  }
  if (req.method === "POST" && /^\/api\/vm\/beta\/(un)?lock$/.test(p)) {
    BETA.locked = p.endsWith("/lock") && !p.endsWith("/unlock");
    posts.push([p, {}]);
    return json(res, BETA);
  }
  if (req.method === "GET" && p === "/api/vm-share-roots") return json(res, { roots: ROOTS, enabled: ROOTS.some((r) => r.ok) });
  const planOf = /^\/api\/vm\/(delta|epsilon)\/destroy$/.exec(p);
  if (planOf && req.method === "GET") return json(res, PLANS[planOf[1]]);
  if (req.method === "DELETE" && /^\/api\/vm\/(delta|epsilon)$/.test(p)) {
    const id = p.split("/").pop();
    posts.push(["DELETE " + p, {}]);
    REMOVED.push(id);
    return json(res, { ok: true, unregistered: id, tart_kept: id });
  }
  if (req.method === "GET" && p === "/api/vm-requests") return json(res, { requests: VM.requests.map((r) => ({ vm: "alpha", ...r })) });
  if (req.method === "GET" && p === "/api/vm-only") return json(res, { sessions: ONLY });
  if (req.method === "POST") {
    let data = "";
    req.on("data", (c) => { data += c; });
    req.on("end", () => {
      const body = data ? JSON.parse(data) : {};
      posts.push([p, body]);
      if (planOf) { REMOVED.push(planOf[1]); return json(res, { ok: true, destroyed: planOf[1], tart_name: planOf[1], size_gb: 93, warnings: [] }); }
      if (p === "/api/vm-share-roots") {
        if (body.action === "add") {
          if (body.path === "/home/u") return json(res, { ok: false, code: "vm_share_forbidden", error: "vm_share_forbidden: 공유할 수 없는 폴더입니다" }, 400);
          ROOTS.push({ path: body.path, real: body.path, ok: true, reason: "" });
        } else {
          ROOTS = ROOTS.filter((r) => r.path !== body.path);
        }
        return json(res, { roots: ROOTS, enabled: ROOTS.some((r) => r.ok) });
      }
      if (p === "/api/vm-only") {
        const row = ONLY.find((r) => r.session === body.session);
        if (row) { row.on = body.on; row.since = body.on ? 1700000200 : 0; }
        return json(res, { sessions: ONLY });
      }
      json(res, { ok: true, changed: [] });
    });
    return;
  }
  if (p === "/api/vm") return json(res, { vms: [VM, BETA, GAMMA, TMPL, DELTA, EPSILON].filter((v) => !REMOVED.includes(v.id)), slot_limit: 2, host_locked: false, share_roots: ROOTS.filter((r) => r.ok).map((r) => r.path), share_root_rows: ROOTS, share_catalog: CATALOG,
    presets: [{ name: "gold", source: "alpha", created_at: 1700000000, description: "깨끗한 설치" }] });
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

    // 1b. the nav item counts the pending requests and the dashboard raised one card for them
    await page.waitForFunction(() => { const b = document.getElementById("vm-nav-badge"); return b && !b.hidden && b.textContent === "1"; }, null, { timeout: 8000 });
    await page.waitForFunction(() => /s2 asks for a VM/.test(document.getElementById("notices").innerText), null, { timeout: 8000 });
    const cardText = await page.locator("#notices").innerText();
    assert.match(cardText, /VM alpha: scopes: files\+exec; share out \(rw, WRITE\) \/work\/out/);
    assert.equal(await page.locator("#notices .notice", { hasText: "s2 asks for a VM" }).count(), 1, "one card per request");

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
    // the pending request is a banner at the top of the page, above the VM detail, with its warnings
    const card = frame.locator("#vm-requests .vm-request", { hasText: "s2" });
    await card.waitFor({ timeout: 5000 });
    assert.match(await frame.locator("#vm-requests-title").innerText(), /대기 중인 요청 1건/);
    const bannerY = (await frame.locator("#vm-requests-box").boundingBox()).y;
    const detailY = (await frame.locator("#vm-detail").boundingBox()).y;
    assert.ok(bannerY < detailY, "the banner is above the VM detail");
    const text = await card.innerText();
    assert.match(text, /s2 → VM alpha/);
    assert.match(text, /권한\s+files \+ exec/);
    assert.match(text, /공유 폴더\s+out \(rw\) \/work\/out/);
    assert.match(text, /exec은 게스트에서 셸 실행/, "the exec warning is on the card");
    assert.match(text, /쓰기 가능\(rw\) 공유: out.*\.git\/hooks/s, "the rw warning is on the card");
    assert.match(text, /사유\s+build it/);
    let apAsked = "";
    page.once("dialog", async (d) => { apAsked = d.message(); await d.dismiss(); });
    await card.getByRole("button", { name: "승인" }).click();
    await page.waitForTimeout(200);
    assert.match(apAsked, /exec.*out 쓰기 가능\(rw\)/s, "approving exec and an rw share asks first");
    assert.equal(posts.filter(([u]) => u === "/api/vm/alpha/approve").length, 0, "a dismissed confirmation approves nothing");
    page.once("dialog", async (d) => { await d.accept(); });
    await card.getByRole("button", { name: "승인" }).click();
    await frame.locator("#vm-activity", { hasText: "s2 요청 승인 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u]) => u === "/api/vm/alpha/approve")[1], { session: "s2", id: "r1" });
    await card.getByRole("button", { name: "거절" }).click();
    await frame.locator("#vm-activity", { hasText: "s2 요청 거절 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u]) => u === "/api/vm/alpha/deny")[1], { session: "s2", id: "r1" });

    // 3d. the share catalog and the VM x share matrix: on the default screen, editable
    const sharing = frame.locator("#vm-sharing");
    assert.equal(await sharing.isVisible(), true, "the sharing section is on the page without opening anything");
    assert.match(await frame.locator("#vm-cat-roots").innerText(), /\/work/, "the allowed root is shown");
    assert.match(await frame.locator("#vm-cat-note").innerText(), /\.env.*\.git\/hooks/s, "the risks are on the page");
    assert.match(await frame.locator("#vm-cat-pending").innerText(), /alpha/, "VMs that need a restart are named");
    const cat = await frame.locator("#vm-catalog tbody tr").allInnerTexts();
    assert.equal(cat.length, 2);
    assert.match(cat[0], /cat1\s+\/work\/cat\s+ro\s+모든 VM\s+카탈로그\(전체\)/);
    assert.match(cat[1], /clash\s+\/work\/clash\s+rw\s+VM: alpha\s+카탈로그\(지정\)/);
    const head = (await frame.locator("#vm-matrix thead").innerText()).replace(/\s+/g, " ");
    assert.match(head, /VM src cat1 clash/, `matrix columns: ${head}`);
    const mrows = await frame.locator("#vm-matrix tbody tr").allInnerTexts();
    assert.match(mrows[0].replace(/\s+/g, " "), /alpha ro 적용됨 ro 적용 대기 rw 제외 — name_conflict/);
    assert.match(mrows[1].replace(/\s+/g, " "), /beta - ro 적용 대기 -/);
    assert.match(await frame.locator("#vm-matrix tbody tr:first-child td:nth-child(2)").getAttribute("title"), /\/work\/proj.*VM 설정/);
    // a row in the VM's own table says where it came from and its state
    const own = await frame.locator("#vm-shares tbody tr", { hasText: "clash" }).innerText();
    assert.match(own, /카탈로그\(지정\).*제외: name_conflict/s);
    // add for all VMs, read-write: asks first, and sends the confirmation
    await frame.locator("#vm-cat-name").fill("tools");
    await frame.locator("#vm-cat-path").fill("/work/tools");
    await frame.locator("#vm-cat-rw").check();
    let catAsked = "";
    page.once("dialog", async (d) => { catAsked = d.message(); await d.dismiss(); });
    await frame.locator("#vm-cat-go").click();
    await page.waitForTimeout(200);
    assert.match(catAsked, /모든 VM.*rw/s, "an rw entry for every VM asks first");
    assert.equal(posts.filter(([u]) => u === "/api/vm-share-catalog").length, 0, "a dismissed confirmation sends nothing");
    page.once("dialog", async (d) => { await d.accept(); });
    await frame.locator("#vm-cat-go").click();
    await frame.locator("#vm-activity", { hasText: "카탈로그 tools 추가 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm-share-catalog" && b.name === "tools")[1],
      { action: "put", name: "tools", host_path: "/work/tools", mode: "rw", scope: "all", vms: [], confirm_rw: true, replace: false });
    // for listed VMs, read-only: no dialog, the picked VMs are sent
    await frame.locator("#vm-cat-name").fill("docs");
    await frame.locator("#vm-cat-path").fill("/work/docs");
    await frame.locator("#vm-cat-scope-vms").check();
    await frame.locator("#vm-cat-vms input[value=beta]").check();
    await frame.locator("#vm-cat-go").click();
    await frame.locator("#vm-activity", { hasText: "카탈로그 docs 추가 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm-share-catalog" && b.name === "docs")[1],
      { action: "put", name: "docs", host_path: "/work/docs", mode: "ro", scope: "vms", vms: ["beta"], confirm_rw: false, replace: false });
    // edit: the name is fixed, the save replaces
    await frame.locator("#vm-catalog tbody tr", { hasText: "cat1" }).getByRole("button", { name: "수정" }).click();
    assert.equal(await frame.locator("#vm-cat-name").isDisabled(), true);
    assert.equal(await frame.locator("#vm-cat-path").inputValue(), "/work/cat");
    await frame.locator("#vm-cat-path").fill("/work/cat2");
    await frame.locator("#vm-cat-go").click();
    await frame.locator("#vm-activity", { hasText: "카탈로그 cat1 저장 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm-share-catalog" && b.name === "cat1")[1],
      { action: "put", name: "cat1", host_path: "/work/cat2", mode: "ro", scope: "all", vms: [], confirm_rw: false, replace: true });
    assert.equal(await frame.locator("#vm-cat-name").isDisabled(), false, "the form is back to adding");
    await frame.locator("#vm-catalog tbody tr", { hasText: "clash" }).getByRole("button", { name: "삭제" }).click();
    await frame.locator("#vm-activity", { hasText: "카탈로그 clash 삭제 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm-share-catalog" && b.action === "rm")[1], { action: "rm", name: "clash" });

    // 3f. the allowed roots: listed with their state, added, refused with the reason, removed
    const roots = (await frame.locator("#vm-roots tbody tr").allInnerTexts()).map((t) => t.replace(/\s+/g, " "));
    assert.equal(roots.length, 2);
    assert.match(roots[0], /^\/work 적용 중/);
    assert.match(roots[1], /^\/old 적용 안 됨: 폴더가 없거나/);
    await frame.locator("#vm-root-path").fill("/home/u");
    await frame.locator("#vm-root-go").click();
    await frame.locator("#vm-activity", { hasText: "공유 루트 /home/u 추가 실패" }).waitFor({ timeout: 5000 });
    assert.match(await frame.locator("#vm-activity").innerText(), /공유할 수 없는 폴더/, "the refusal reason is shown");
    await frame.locator("#vm-root-path").fill("/work/more");
    await frame.locator("#vm-root-go").click();
    await frame.locator("#vm-activity", { hasText: "공유 루트 /work/more 추가 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm-share-roots" && b.path === "/work/more")[1], { action: "add", path: "/work/more" });
    assert.equal(await frame.locator("#vm-roots tbody tr").count(), 3);
    for (const gone of ["/work/more", "/old", "/work"]) {
      await frame.locator("#vm-roots tbody tr", { hasText: gone }).first().getByRole("button", { name: "삭제" }).click();
      await frame.locator("#vm-activity", { hasText: `공유 루트 ${gone} 삭제 완료` }).waitFor({ timeout: 5000 });
    }
    assert.match(await frame.locator("#vm-cat-roots").innerText(), /공유가 꺼져 있습니다/, "an empty list says sharing is off");
    assert.equal(await frame.locator("#vm-cat-go").isDisabled(), true);
    assert.equal(await frame.locator("#vm-roots").isVisible(), false);

    // 3h. the clone lineage: always on the page, read only, no guessing
    assert.equal(await frame.locator("#vm-lineage").isVisible(), true, "the lineage section is on the page without opening anything");
    assert.match(await frame.locator("#vm-lineage-note").innerText(), /출처 기록 없음.*삭제된 프리셋/s);
    assert.match(await frame.locator("#vm-lineage h1").innerText(), /전체 복제 계보\(모든 VM·프리셋\)/, "the title says it is not the selected VM's");
    assert.equal(await frame.locator("#vm-lineage-all").evaluate((d) => d.open), false, "the whole tree is folded by default");
    assert.equal(await frame.locator("#vm-lineage-tree").isVisible(), false);
    // the selected VM's own line is on the detail and in the section, whatever the tree shows
    const lineOf = async (id) => {
      await frame.locator(".vm-card", { hasText: id }).first().click();
      await frame.locator("#vm-name", { hasText: id }).waitFor({ timeout: 5000 });
      return [(await frame.locator("#vm-lineage-line").innerText()).replace(/\s+/g, " "),
              (await frame.locator("#vm-lineage-current").innerText()).replace(/\s+/g, " ")];
    };
    let [top, cur] = await lineOf("beta");
    assert.equal(top, "계보: beta ← 프리셋 gold ← alpha(출처 기록 없음)");
    assert.equal(cur, "beta ← 프리셋 gold ← alpha(출처 기록 없음)", "the folded section shows the selected VM's line");
    assert.ok((await frame.locator("#vm-lineage-line").boundingBox()).y < (await frame.locator("#vm-actions").boundingBox()).y, "the line is near the title, above the buttons");
    [top] = await lineOf("gamma");
    assert.match(top, /^계보: gamma ← 삭제된 프리셋 old\(이 프리셋 이전의 계보는 알 수 없음\)$/);
    [top] = await lineOf("tmpl");
    assert.match(top, /^계보: tmpl\(프리셋 gold의 복사본: 프리셋 템플릿, 일반 VM이 아님\) ← alpha\(출처 기록 없음\)$/);
    [top] = await lineOf("alpha");
    assert.match(top, /^계보: alpha: 출처 기록 없음\(직접 등록했거나 이 기능 이전에 만든 VM, 추측하지 않음\) · 이 VM에서 만든 프리셋: gold → beta$/);
    await frame.locator("#vm-lineage-all summary").click();
    assert.equal(await frame.locator("#vm-lineage-tree").isVisible(), true, "unfolded");
    // the selected VM stands out in the whole tree; its ancestors are marked, others are not
    const here = frame.locator("#vm-lineage-tree li.vm-tree-selected");
    assert.equal(await here.count(), 1);
    assert.match(await here.first().innerText(), /VM alpha.*현재 선택/s);
    assert.ok(Number(await here.first().locator("> span").evaluate((e) => getComputedStyle(e).fontWeight)) >= 700, "bold");
    await lineOf("beta");
    assert.equal(await frame.locator("#vm-lineage-tree li.vm-tree-selected").count(), 1);
    assert.match(await frame.locator("#vm-lineage-tree li.vm-tree-selected").first().innerText(), /VM beta.*현재 선택/s, "the highlight follows the selection");
    assert.equal(await frame.locator("#vm-lineage-tree li.vm-tree-ancestor").count(), 2, "gold and alpha are marked as its ancestors");
    assert.equal(await frame.locator("#vm-lineage-tree li.vm-tree-selected", { hasText: "VM alpha" }).count(), 0, "only the selected VM is highlighted");
    const lin = (await frame.locator("#vm-lineage-tree").innerText()).replace(/[ \t]+/g, " ");
    assert.match(lin, /VM alpha · 실행 중 · 출처 기록 없음/, "a VM without a record says so");
    assert.match(lin, /프리셋 gold · 저장 .* · 원본 alpha · 깨끗한 설치/, "the preset shows its time, source and description");
    assert.match(lin, /VM beta · 정지/, "the VM made from the preset is its child");
    assert.match(lin, /삭제된 프리셋 old에서 복제/, "a VM whose preset is gone says so");
    const order = (t) => lin.indexOf(t);
    assert.ok(order("VM alpha") < order("프리셋 gold") && order("프리셋 gold") < order("VM beta"), "alpha > gold > beta");
    assert.ok(order("삭제된 프리셋 old") < order("VM gamma"), "gamma is under the deleted-preset node");
    assert.equal(await frame.locator("#vm-lineage-tree li li li").count() >= 1, true, "the tree is nested");
    assert.equal(await frame.locator("#vm-lineage button, #vm-lineage input").count(), 0, "read only");

    // 3i. a preset's copy registered as a VM is marked, and starting or granting it asks first
    const tcard = frame.locator(".vm-card", { hasText: "tmpl" });
    assert.match(await tcard.innerText(), /프리셋 템플릿 · stopped/, "the list row says what it is");
    assert.match(await frame.locator(".vm-card", { hasText: "alpha" }).innerText(), /VM · running/, "an ordinary VM says VM");
    assert.match(await tcard.getAttribute("class"), /vm-template-card/);
    await tcard.click();
    await frame.locator("#vm-name", { hasText: "tmpl" }).waitFor({ timeout: 5000 });
    assert.equal(await frame.locator("#vm-template-chip").isVisible(), true, "the badge is on the detail");
    assert.match(await frame.locator("#vm-template-note").innerText(), /프리셋 gold의 복사본.*일반 VM이 아닙니다.*복제본이 달라집니다/s);
    const startsOf = () => posts.filter(([u]) => u === "/api/vm/tmpl/start").length;
    let startAsk = "";
    page.once("dialog", async (d) => { startAsk = d.message(); await d.dismiss(); });
    await frame.locator("#vm-start").click();
    await page.waitForTimeout(200);
    assert.match(startAsk, /템플릿을 시작하면 이후.*복제본이 바뀝니다/s, "starting a template asks first");
    assert.equal(startsOf(), 0, "a dismissed confirmation starts nothing");
    page.once("dialog", async (d) => { await d.accept(); });
    await frame.locator("#vm-start").click();
    await frame.locator("#vm-activity", { hasText: "시작 요청이 처리되었습니다" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm/tmpl/start")[0][1], { confirm_template: true });
    let grantAsk = "";
    page.once("dialog", async (d) => { grantAsk = d.message(); await d.dismiss(); });
    await frame.locator("#vm-grant-session-text").fill("s1").catch(() => {});
    await frame.locator("#vm-grant").click();
    await page.waitForTimeout(200);
    assert.match(grantAsk, /프리셋 gold의 복사본.*복제본이 바뀝니다/s, "granting a template warns");
    assert.match(await frame.locator("#vm-lineage-tree").innerText(), /VM tmpl · .* · 프리셋 템플릿/, "the lineage marks it too");
    await frame.locator(".vm-card", { hasText: "alpha" }).click();
    assert.equal(await frame.locator("#vm-template-chip").isVisible(), false, "an ordinary VM has no badge");

    // 3j. unregister and physical delete: two separate things, the delete in its own red zone
    await frame.locator(".vm-card", { hasText: "delta" }).click();
    await frame.locator("#vm-name", { hasText: "delta" }).waitFor({ timeout: 5000 });
    assert.equal(await frame.locator("#vm-danger").isVisible(), true, "the danger zone is on the detail");
    assert.match(await frame.locator("#vm-danger").innerText(), /디스크 이미지까지 영구 삭제.*되돌릴 수 없습니다/s);
    assert.match(await frame.locator("#vm-unregister").innerText(), /등록 해제 \(tart VM은 남음\)/);
    assert.match(await frame.locator("#vm-danger h2").innerText(), /위험 영역: 물리적 삭제/);
    await frame.locator("#vm-destroy-size", { hasText: "93 GB" }).waitFor({ timeout: 5000 });
    assert.match(await frame.locator("#vm-destroy-warnings").innerText(), /프리셋: gold.*후손\): kid/s, "presets and descendants are listed");
    const destroy = frame.locator("#vm-destroy");
    assert.equal(await destroy.isDisabled(), true, "nothing typed: disabled");
    await frame.locator("#vm-destroy-confirm").fill("delt");
    assert.equal(await destroy.isDisabled(), true, "a wrong id: disabled");
    await frame.locator("#vm-destroy-confirm").fill("delta");
    assert.equal(await destroy.isDisabled(), false, "the id typed again: enabled");
    const destroysOf = () => posts.filter(([u]) => /\/destroy$/.test(u)).length;
    await destroy.click();
    await frame.locator("#vm-activity", { hasText: "delta 물리적 삭제 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u]) => u === "/api/vm/delta/destroy")[1], { confirm: "delta" });
    assert.equal(destroysOf(), 1);
    await frame.locator(".vm-card", { hasText: "delta" }).waitFor({ state: "detached", timeout: 8000 });
    // a VM the daemon says cannot be deleted: the reason is shown and the button stays off even with the id typed
    await frame.locator(".vm-card", { hasText: "epsilon" }).click();
    await frame.locator("#vm-name", { hasText: "epsilon" }).waitFor({ timeout: 5000 });
    await frame.locator("#vm-destroy-blockers", { hasText: "잠겨 있습니다" }).waitFor({ timeout: 5000 });
    await frame.locator("#vm-destroy-confirm").fill("epsilon");
    assert.equal(await destroy.isDisabled(), true, "a refused VM cannot be deleted from the page");
    // unregister: asks, says the tart VM stays, and only unregisters
    let unregAsk = "";
    page.once("dialog", async (d) => { unregAsk = d.message(); await d.dismiss(); });
    await frame.locator("#vm-unregister").click();
    await page.waitForTimeout(200);
    assert.match(unregAsk, /등록만 해제.*그대로 남습니다.*세션 권한 \d+건, 공유 폴더 \d+개, 대기 요청 \d+건/s, "the confirmation lists what is released");
    assert.equal(posts.filter(([u]) => u.startsWith("DELETE")).length, 0, "a dismissed confirmation unregisters nothing");
    page.once("dialog", async (d) => { await d.accept(); });
    await frame.locator("#vm-unregister").click();
    await frame.locator("#vm-activity", { hasText: "epsilon 등록 해제 완료" }).waitFor({ timeout: 5000 });
    assert.equal(posts.filter(([u]) => u === "DELETE /api/vm/epsilon").length, 1);
    assert.equal(destroysOf(), 1, "unregistering never calls the delete route");
    await frame.locator(".vm-card", { hasText: "alpha" }).click();

    // 3g. lifecycle buttons show at once that a press was taken, lock the conflicting ones, and
    // keep showing it until the state the press asked for is in the list
    const vmFrame = page.frames().find((f) => /\/vm\?embed=1/.test(f.url()));
    await frame.locator(".vm-card", { hasText: "gamma" }).click();
    await frame.locator("#vm-name", { hasText: "gamma" }).waitFor({ timeout: 5000 });
    const start = frame.locator("#vm-start"), stop = frame.locator("#vm-stop"), suspend = frame.locator("#vm-suspend"), force = frame.locator("#vm-force");
    assert.equal(await start.isDisabled(), false, "a stopped VM can be started");
    const lifePosts = () => posts.filter(([u]) => /^\/api\/vm\/gamma\/(start|stop|suspend|force_stop)$/.test(u)).length;
    // a failed press: the buttons come back and the reason is shown
    gammaFail = true;
    await start.click();
    await frame.locator("#vm-activity", { hasText: "시작 실패" }).waitFor({ timeout: 5000 });
    assert.match(await frame.locator("#vm-activity").innerText(), /vm_busy_state/);
    assert.equal(await start.isDisabled(), false, "the button is back after a failed request");
    assert.equal(await start.getAttribute("aria-busy"), "false");
    assert.equal(lifePosts(), 1);
    gammaFail = false;
    // a press that is taken: label, spinner, lock, and a second click sends nothing
    await start.click();
    await frame.locator("#vm-start.vm-working").waitFor({ timeout: 2000 });
    assert.match(await start.innerText(), /시작 중…/);
    assert.equal(await start.getAttribute("aria-busy"), "true");
    for (const b of [start, suspend, stop]) assert.equal(await b.isDisabled(), true, "conflicting buttons are locked");
    assert.equal(await frame.locator("#vm-guard-toggle").isDisabled(), true, "the lock toggle is locked too");
    GAMMA.state = "starting";                // the daemon now reports the transition
    await page.waitForFunction(() => document.querySelector("iframe").contentDocument.getElementById("vm-force").disabled === false, null, { timeout: 8000 });
    assert.equal(await force.isDisabled(), false, "force stop stays open as the way out");
    for (const b of [start, suspend, stop]) assert.equal(await b.isDisabled(), true, "still locked while starting");
    await start.click({ force: true }).catch(() => {});
    await page.waitForTimeout(150);
    assert.equal(lifePosts(), 2, "a second click on the busy button sends no second request");
    // the request answers; the list shows running; the progress goes and the stop button is open
    gammaRelease();
    await frame.locator("#vm-start.vm-working").waitFor({ state: "detached", timeout: 8000 });
    assert.equal(await stop.isDisabled(), false);
    assert.equal(await suspend.isDisabled(), false);
    assert.match(await start.innerText(), /▶ 시작/);
    // a VM the daemon reports mid-transition shows progress without a press here
    GAMMA.state = "stopping";
    await frame.locator("#vm-stop.vm-working").waitFor({ timeout: 8000 });
    assert.match(await stop.innerText(), /종료 중…/);
    for (const b of [start, suspend, stop]) assert.equal(await b.isDisabled(), true);
    GAMMA.state = "stopped";
    await frame.locator("#vm-stop.vm-working").waitFor({ state: "detached", timeout: 8000 });
    // an answer whose state never shows: after the timeout the buttons come back with a notice
    await vmFrame.evaluate(() => { window.VM_BUSY_MS = 1500; });
    const fixed = GAMMA_STATE.start;
    GAMMA_STATE.start = "stopped";           // the daemon says ok but the state does not change
    await start.click();
    await frame.locator("#vm-start.vm-working").waitFor({ timeout: 2000 });
    gammaRelease();
    await frame.locator("#vm-activity", { hasText: "아직 반영되지 않았습니다" }).waitFor({ timeout: 8000 });
    assert.equal(await start.isDisabled(), false, "the button is back after the timeout");
    GAMMA_STATE.start = fixed;
    await vmFrame.evaluate(() => { window.VM_BUSY_MS = 0; });
    await frame.locator(".vm-card", { hasText: "alpha" }).click();

    // 3e. VM-only mode: on the default screen, per session, with the reason and the blocks
    assert.equal(await frame.locator("#vm-onlymode").isVisible(), true, "the VM-only section is on the page without opening anything");
    assert.match(await frame.locator("#vm-only-note").innerText(), /보안 경계가 아닙니다/, "the limit is stated");
    const orows = (await frame.locator("#vm-only-rows tbody tr").allInnerTexts()).map((t) => t.replace(/\s+/g, " "));
    assert.match(orows[0], /s1 꺼짐/);
    assert.match(orows[1], /s9 켤 수 없음: 이 세션은 VM 전용 hook 없이/);
    assert.match(orows[2], /s7 켜짐 .*build\(make\) make deploy/);
    assert.equal(await frame.locator('#vm-only-rows input[aria-label="s9 VM 전용 켜기"]').isDisabled(), true, "a session that cannot be switched has a disabled switch");
    assert.equal(await frame.locator('#vm-only-rows input[aria-label="s7 VM 전용 켜기"]').isChecked(), true);
    // the same switch sits in the session row of the permissions table
    const gbox = frame.locator('#vm-grants input[aria-label="s1 VM 전용"]');
    assert.equal(await gbox.count(), 1, "the session row of the permissions table has the switch");
    assert.equal(await gbox.isChecked(), false);
    await frame.locator('#vm-only-rows input[aria-label="s1 VM 전용 켜기"]').check();
    await frame.locator("#vm-activity", { hasText: "s1 VM 전용 켜기 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.find(([u, b]) => u === "/api/vm-only" && b.session === "s1")[1], { session: "s1", on: true });
    await frame.locator("#vm-only-rows tbody tr", { hasText: /^s1.*켜짐/ }).waitFor({ timeout: 5000 });
    assert.equal(await frame.locator('#vm-grants input[aria-label="s1 VM 전용"]').isChecked(), true, "the permissions row follows");
    await frame.locator('#vm-grants input[aria-label="s1 VM 전용"]').uncheck();
    await frame.locator("#vm-activity", { hasText: "s1 VM 전용 끄기 완료" }).waitFor({ timeout: 5000 });
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm-only").pop()[1], { session: "s1", on: false });

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
