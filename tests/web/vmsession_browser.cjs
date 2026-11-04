/* The VM page in session mode (vm?embed=1&session=<name>), the page behind the
   dashboard's ▣ vm pane (vm-session-pane.js).

   Against a fixture server with a mutable VM list this checks:
     1. only the session's connected VMs are listed, with state, scope chips and
        the lock/template marks; the sidebar and the global sections are hidden;
     2. 연결 해제 asks (naming that the VM is not stopped or deleted), a dismissed
        dialog sends nothing, an accepted one revokes that session only;
     3. 새 VM 만들어 연결 with a default preset: one press, no dialog, a busy button
        that cannot be pressed twice, create then grant (view+control), an
        automatic name that skips a taken one; exec/files only when ticked;
     4. no default preset: the button waits for a choice from the dropdown;
     5. 기존 VM 연결: scopes from the checkboxes, the template-copy warning;
     6. the connected VM's permissions row edits only this session, and the
        shared-folder section is the page's own, with the restart note;
     7. the document has no scrollbar. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };

const mk = (id, extra = {}) => ({
  id, tart_name: id, state: "stopped", ready: false, channel: "idle", controller: "", grants: {}, human: "idle",
  ip: "", fault_reason: "", expose: [], allow: [], locked: false, template: "", cloned_from: {},
  shared_dirs: [], share_rows: [], shares_pending: false, shares_skipped: {}, requests: [], ...extra });
let VMS = [
  mk("mine", { state: "running", grants: { s1: ["view", "control"], other: ["files"] },
    share_rows: [{ name: "src", host_path: "/work/proj", mode: "ro", guest_path: "/Volumes/My Shared Files/src",
      origin: { kind: "vm", by_session: "", at: 1 }, state: "applied" }] }),
  mk("locked1", { grants: { s1: ["view"] }, locked: true }),
  mk("tmplcopy", { tart_name: "preset-gold", template: "gold" }),
  mk("free", {}),
  mk("held", { grants: { other: ["view", "control"] } }),
  mk("s1-vm", {}),
];
let PRESETS = [{ name: "gold", present: true }, { name: "silver", present: true }];
let DEFAULT = "gold";
const posts = [];
let release = null;
const json = (res, body, status = 200) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
};
const server = http.createServer((req, res) => {
  const p = new URL(req.url, "http://fixture").pathname;
  if (p === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
  if (req.method === "GET") {
    if (p === "/api/vm") return json(res, { vms: VMS, host_locked: false, share_roots: ["/work"], share_root_rows: [], share_catalog: [], presets: [], preset_default: {} });
    if (p === "/api/vm-only") return json(res, { sessions: [] });
    if (p === "/api/vm-requests") return json(res, { requests: [] });
    if (p === "/api/vm-presets") return json(res, { presets: PRESETS, default: DEFAULT, default_lost: DEFAULT ? "" : "기본 프리셋 'old'이(가) 삭제되어 해제되었습니다" });
    if (p.startsWith("/api/")) return json(res, {}, 404);
    const rel = p === "/vm" ? "vm.html" : p.replace(/^\/static\//, "");
    try {
      const body = fs.readFileSync(path.join(root, rel));
      res.setHeader("Content-Type", TYPES[path.extname(rel)] || "application/octet-stream");
      return res.end(body);
    } catch (_) { res.statusCode = 404; return res.end("no"); }
  }
  let data = "";
  req.on("data", (c) => { data += c; });
  req.on("end", () => {
    const body = data ? JSON.parse(data) : {};
    posts.push([p, body]);
    let m;
    if ((m = /^\/api\/vm-presets\/([^/]+)\/create$/.exec(p))) {
      const done = () => {
        if (VMS.some((v) => v.id === body.id)) return json(res, { ok: false, code: "vm_exists", error: `${body.id} is already registered` }, 409);
        VMS.push(mk(body.id, { state: "running" }));
        json(res, { vm: { id: body.id, state: "running" }, started: true }, 201);
      };
      if (body.hold) return done();
      release = done;
      return;
    }
    if ((m = /^\/api\/vm\/([^/]+)\/grant$/.exec(p))) {
      const vm = VMS.find((v) => v.id === m[1]);
      if ((body.scopes || []).includes("control")) {
        const holders = Object.entries(vm.grants).filter(([n, sc]) => n !== body.session && sc.includes("control")).map(([n]) => n);
        if (holders.length && !body.steal) {
          return json(res, { ok: false, code: "vm_control_held", holders, error: `vm_control_held: ${vm.id}의 control은 ${holders.join(", ")}이(가) 가지고 있습니다` }, 409);
        }
        for (const h of holders) vm.grants[h] = vm.grants[h].filter((x) => x !== "control");
      }
      vm.grants[body.session] = [...new Set([...(vm.grants[body.session] || []), ...body.scopes])];
      return json(res, vm);
    }
    if ((m = /^\/api\/vm\/([^/]+)\/revoke$/.exec(p))) {
      const vm = VMS.find((v) => v.id === m[1]);
      if (body.scopes) vm.grants[body.session] = (vm.grants[body.session] || []).filter((s) => !body.scopes.includes(s));
      else delete vm.grants[body.session];
      if (vm.grants[body.session] && !vm.grants[body.session].length) delete vm.grants[body.session];
      return json(res, vm);
    }
    json(res, { ok: true });
  });
});

(async () => {
  await safeListen(server);
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await (await browser.newContext({ viewport: { width: 1000, height: 700 } })).newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    let asked = [];
    let answer = false;
    page.on("dialog", async (d) => { asked.push(d.message()); answer ? await d.accept() : await d.dismiss(); });
    await page.goto(base + "vm?embed=1&session=s1");
    const rows = page.locator("#vm-sess-list li");
    await rows.first().waitFor({ timeout: 8000 });

    // 1. only this session's VMs, with their marks; the rest of the page is out of the way
    assert.deepEqual(await rows.locator(".vm-sess-pick").allInnerTexts(), ["mine", "locked1"]);
    const mine = rows.filter({ hasText: "mine" });
    assert.match(await mine.innerText(), /running/);
    assert.deepEqual(await mine.locator(".vm-chip").allInnerTexts(), ["running", "view", "control"]);
    assert.match(await rows.filter({ hasText: "locked1" }).innerText(), /잠김/);
    for (const hidden of ["#vm-side", "#vm-lineage", "#vm-onlymode", "#vm-sharing", "#vm-requests-box"]) {
      assert.equal(await page.locator(hidden).isVisible(), false, `${hidden} is hidden in session mode`);
    }
    // 6. the permissions row is this session's only, with the page's own editor; the share section is there
    await page.locator("#vm-sess-list .vm-sess-pick", { hasText: "mine" }).click();
    assert.equal(await page.locator("#vm-grants tbody tr").count(), 1);
    assert.match(await page.locator("#vm-grants tbody tr").innerText(), /s1/);
    await page.getByLabel("s1 files").check();
    await page.waitForFunction(() => document.querySelector('#vm-grants input[aria-label="s1 files"]').checked);
    assert.deepEqual(posts.find(([u]) => u === "/api/vm/mine/grant")[1], { session: "s1", scopes: ["files"] });
    await page.getByLabel("s1 files").uncheck();
    await page.waitForFunction(() => !document.querySelector('#vm-grants input[aria-label="s1 files"]').checked);
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm/mine/revoke").pop()[1], { session: "s1", scopes: ["files"] });
    assert.equal(VMS[0].grants.other.join(), "files", "the other session's grant is untouched");
    assert.equal(await page.locator("#vm-shares-block").isVisible(), true);
    assert.match(await page.locator("#vm-sess-share-note").innerText(), /다시 시작해야 적용/);
    assert.equal(await page.locator("#vm-share-go").isVisible(), true, "the share editor is the page's own");

    // 2. 연결 해제
    const n0 = posts.length;
    await mine.getByRole("button", { name: "연결 해제" }).click();
    await page.waitForTimeout(300);
    assert.match(asked.pop(), /정지되거나 삭제되지 않습니다/);
    assert.equal(posts.length, n0, "a dismissed dialog sends nothing");
    answer = true;
    await mine.getByRole("button", { name: "연결 해제" }).click();
    await page.waitForFunction(() => ![...document.querySelectorAll("#vm-sess-list .vm-sess-pick")].some((b) => b.textContent === "mine"));
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm/mine/revoke").pop()[1], { session: "s1" });
    assert.equal(VMS[0].state, "running", "the VM itself is untouched");
    assert.equal(VMS[0].grants.other.join(), "files");
    answer = false;

    // 3. one-click new VM with a default preset: the name skips the taken s1-vm
    const go = page.locator("#vm-sess-new");
    await page.waitForFunction(() => /gold/.test(document.getElementById("vm-sess-new").textContent));
    assert.match(await page.locator("#vm-sess-new-note").innerText(), /s1-vm-2/);
    asked = [];
    await go.click();
    await page.locator("#vm-sess-new.vm-working").waitFor({ timeout: 3000 });
    assert.equal(await go.isDisabled(), true, "the busy button cannot be pressed again");
    await go.click({ force: true }).catch(() => {});
    assert.equal(posts.filter(([u]) => u.endsWith("/create")).length, 1, "one create request");
    assert.deepEqual(asked, [], "no dialog for the one-click flow");
    release();
    await page.waitForFunction(() => [...document.querySelectorAll("#vm-sess-list .vm-sess-pick")].some((b) => b.textContent === "s1-vm-2"));
    assert.deepEqual(posts.find(([u]) => u === "/api/vm-presets/gold/create")[1], { id: "s1-vm-2" });
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm/s1-vm-2/grant").pop()[1], { session: "s1", scopes: ["view", "control"] });
    await page.locator("#vm-sess-new:not(.vm-working)").waitFor();
    // exec is asked about, and then granted along with files
    await page.locator("#vm-sess-new-exec").check();
    await page.locator("#vm-sess-new-files").check();
    answer = true;
    await go.click();
    await page.waitForTimeout(200);
    assert.match(asked.pop(), /exec/);
    release();
    await page.waitForFunction(() => [...document.querySelectorAll("#vm-sess-list .vm-sess-pick")].some((b) => b.textContent === "s1-vm-3"));
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm/s1-vm-3/grant").pop()[1], { session: "s1", scopes: ["view", "control", "exec", "files"] });
    answer = false;

    // 4. no default preset: guidance and a dropdown, the button waits for a choice
    DEFAULT = "";
    await page.waitForFunction(() => !document.getElementById("vm-sess-new-pick").hidden, null, { timeout: 15000 });
    assert.match(await page.locator("#vm-sess-new-note").innerText(), /기본 프리셋이 없습니다/);
    assert.match(await page.locator("#vm-sess-new-note").innerText(), /삭제되어 해제/);
    assert.equal(await go.isDisabled(), true);
    await page.locator("#vm-sess-new-pick").selectOption("silver");
    assert.equal(await go.isDisabled(), false);

    // 5. connect an existing VM: scopes from the boxes, the template copy warns
    const pick = page.locator("#vm-sess-pick");
    assert.equal(await page.locator("#vm-sess-connect").isDisabled(), true);
    await pick.selectOption("free");
    await page.locator("#vm-sess-ex-control").uncheck();
    await page.locator("#vm-sess-connect").click();
    await page.waitForFunction(() => [...document.querySelectorAll("#vm-sess-list .vm-sess-pick")].some((b) => b.textContent === "free"));
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm/free/grant").pop()[1], { session: "s1", scopes: ["view"] });
    await pick.selectOption("tmplcopy");
    asked = [];
    await page.locator("#vm-sess-connect").click();
    await page.waitForTimeout(300);
    assert.match(asked.pop(), /프리셋 gold의 복사본/);
    assert.equal(posts.some(([u]) => u === "/api/vm/tmplcopy/grant"), false, "dismissed: nothing granted");

    // 5b. control is held by one session: the steal box names the holder and is sent only when ticked
    await pick.selectOption("s1-vm");
    assert.equal(await page.locator("#vm-sess-steal-row").isHidden(), true, "no holder: no steal box");
    await pick.selectOption("held");
    await page.locator("#vm-sess-ex-control").check();
    await page.locator("#vm-sess-steal-row").waitFor({ state: "visible", timeout: 5000 });
    assert.match(await page.locator("#vm-sess-steal-label").innerText(), /other에서 control을 빼앗아 부여/);
    await page.locator("#vm-sess-connect").click();
    await page.locator("#vm-activity", { hasText: /vm_control_held.*other/ }).waitFor({ timeout: 5000 });
    assert.equal(posts.filter(([u]) => u === "/api/vm/held/grant").pop()[1].steal, undefined, "unticked: refused, nothing stolen");
    assert.deepEqual(VMS.find((v) => v.id === "held").grants.other, ["view", "control"]);
    await page.locator("#vm-sess-steal").check();
    await page.locator("#vm-sess-connect").click();
    await page.waitForFunction(() => [...document.querySelectorAll("#vm-sess-list .vm-sess-pick")].some((b) => b.textContent === "held"));
    assert.deepEqual(posts.filter(([u]) => u === "/api/vm/held/grant").pop()[1], { session: "s1", scopes: ["view", "control"], steal: true });
    assert.deepEqual(VMS.find((v) => v.id === "held").grants.other, ["view"], "the holder keeps its other scopes");

    // 7. no document scrollbar
    const m = await page.evaluate(() => ({ win: innerHeight, doc: document.documentElement.scrollHeight }));
    assert.ok(m.doc <= m.win, `no document scrollbar (${JSON.stringify(m)})`);
    assert.deepEqual(errors, []);
    console.log("vmsession ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
