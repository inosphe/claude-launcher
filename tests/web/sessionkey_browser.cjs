/* The rail's key chip and its picker modal, on the shipped page (claunch-wllke.1).

   A profile that holds a key pool picks one key per launch; this is a
   *session* pinned to one key of that pool, switched from its rail row. The
   page is served whole (index.html, app.js, style.css) against a fixture
   daemon, so what is checked is what the operator would click:

     1. a pooled session's row carries the chip and a single-token profile's
        row carries none -- one key is not a choice;
     2. the chip names the key the row is on: the pinned key's label when the
        session is pinned, else the pool's current key; it is lit (`.on`) only
        while pinned;
     3. clicking the chip opens the floating modal, whose radio group lists
        every key plus, for a pinned session, "no pin"; no plaintext token is
        anywhere in the page;
     4. Cancel sends nothing at all;
     5. confirming restarts the session on the key that was selected -- one
        POST of that key id -- and the row redraws on the new pin;
     6. "no pin" sends `key_id: null` and the chip stops being lit.
*/
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const ago = (h) => new Date(Date.now() - h * 3600000).toISOString();
const SESSIONS = [
  { name: "s1", status: "running", profile: "work:claude", harness: "claude",
    cwd: "/repo", key_id: "k-bbbbbb", created_at: ago(1) },
  { name: "s2", status: "running", profile: "solo:claude", harness: "claude",
    cwd: "/repo", created_at: ago(1) },
];
const POOLS = {
  "work:claude": {
    profile: "work:claude", owner: "work", pooled: true, strategy: "rotation",
    keys: [
      { id: "k-aaaaaa", label: "", masked: "sk-A…BBBB", current: true, use_count: 2 },
      { id: "k-bbbbbb", label: "second", masked: "sk-C…DDDD", current: false, use_count: 1 },
    ],
  },
  "solo:claude": { profile: "solo:claude", pooled: false, keys: [] },
};
const posts = [];

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  // The daemon's route carries the profile percent-encoded (`work%3Aclaude`);
  // the fixture compares the decoded selector.
  const route = decodeURIComponent(url.pathname);
  if (route.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    let body = "";
    req.on("data", (c) => { body += c; });
    req.on("end", () => {
      // The page sends its reads as one batch by default; a 404 on this route
      // is the documented fallback to one request per read, which is what
      // lets this fixture answer each path on its own below.
      if (route === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
      let data = {};
      if (route === "/api/daemon") data = { version: "fixture", boot_id: "test" };
      else if (route === "/api/sessions") data = { sessions: SESSIONS, llm_configured: false };
      else if (route.endsWith("/capture")) data = { lines: ["Session snapshot"] };
      else if (/^\/api\/profiles\/[^/]+\/keys$/.test(route)) {
        const prof = route.slice("/api/profiles/".length, -"/keys".length);
        data = POOLS[prof] || { pooled: false, keys: [] };
      } else if (/^\/api\/sessions\/[^/]+\/key$/.test(route)) {
        const name = route.slice("/api/sessions/".length, -"/key".length);
        const sent = JSON.parse(body || "{}");
        posts.push({ method: req.method, route, body: sent });
        const row = SESSIONS.find((s) => s.name === name);
        if (sent.key_id === null) delete row.key_id;
        else row.key_id = sent.key_id;
        data = row;
      } else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(route)) {
        data = [];
      }
      res.end(JSON.stringify(data));
    });
    return;
  }
  const file = path.join(root, route === "/" ? "index.html"
    : route.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", TYPES[path.extname(file)] || "application/octet-stream");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

(async () => {
  const port = await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
    });
    await page.goto(`http://127.0.0.1:${port}/`);

    const until = async (fn, what) => {
      for (let i = 0; i < 150; i++) {
        if (await fn()) return;
        await page.waitForTimeout(100);
      }
      throw new Error(`timed out waiting for ${what}`);
    };
    const row = (name) => page.locator(`#session-list li.sess-card[data-name="${name}"]`);
    const chip = (name) => row(name).locator(".rail-actions .sess-key");
    const label = async (name) => (await chip(name).locator(".sess-key-label").textContent()).trim();

    // 1. Only the pooled profile gets a chip.
    await chip("s1").waitFor({ state: "attached" });
    assert.equal(await chip("s1").isVisible(), false,
                 "the compact card hides its pool picker");
    await row("s1").locator(".rail-name").click();
    await chip("s1").waitFor({ state: "visible" });
    assert.equal(await chip("s2").count(), 0,
                 "a single-token profile offered no choice, so it has no chip");

    // 2. The chip names the PINNED key, and is lit for it.
    assert.equal(await label("s1"), "second", "the pinned key's label is what the chip shows");
    assert(await chip("s1").evaluate((el) => el.classList.contains("on")),
           "a pinned session's chip is lit");
    assert.match(await chip("s1").getAttribute("title"), /pinned to key second/);

    // Nothing in the page carries a plaintext token, whatever the fixture's
    // pool holds: the modal is drawn from the masked list alone.
    assert(!(await page.content()).includes("sk-AAAA1111BBBB"), "no plaintext token on the page");

    // 3. The click opens the modal; Cancel sends nothing.
    await chip("s1").click();
    await page.locator("#modal-overlay").waitFor({ state: "visible" });
    assert.equal(await page.locator("#modal-title").textContent(), "Key for s1");
    const options = page.locator(".modal-choice");
    assert.equal(await options.count(), 3,
                 "two keys and, because the session is pinned, the no-pin answer");
    assert.match(await options.nth(0).innerText(), /^sk-A…BBBB/);
    assert.match(await options.nth(1).locator("small").innerText(), /pinned to this session/);
    assert.match(await options.nth(2).innerText(), /No pin/);
    assert.match(await page.locator("#modal-body").innerText(), /Switching restarts this session/);
    if (process.env.CLAUNCH_KEY_SCREENSHOT) {
      fs.mkdirSync(path.dirname(process.env.CLAUNCH_KEY_SCREENSHOT), { recursive: true });
      await page.screenshot({ path: process.env.CLAUNCH_KEY_SCREENSHOT });
    }
    await page.locator("#modal-actions button", { hasText: "Cancel" }).click();
    await page.locator("#modal-overlay").waitFor({ state: "hidden" });
    assert.equal(posts.length, 0, "Cancel sent no request");

    // 5. The safe default -- the first option -- is the key that gets asked for.
    await chip("s1").click();
    await page.locator("#modal-overlay").waitFor({ state: "visible" });
    await page.locator("#modal-actions button", { hasText: "Restart on this key" }).click();
    await page.waitForFunction(() => !document.querySelector("#modal-overlay:not(.hidden)"));
    await until(() => posts.length === 1, "the switch POST");
    assert.deepEqual(posts[0], { method: "POST", route: "/api/sessions/s1/key",
                                 body: { key_id: "k-aaaaaa" } });
    // The row redraws on the new pin: the chip now names the key it was moved to.
    await page.waitForFunction(() => {
      const el = document.querySelector('#session-list li.sess-card[data-name="s1"] .sess-key-label');
      return el && el.textContent.trim() === "sk-A…BBBB";
    });

    // 6. "No pin" sends null and the chip stops being lit.
    await chip("s1").click();
    await page.locator("#modal-overlay").waitFor({ state: "visible" });
    await page.locator(".modal-choice").nth(2).click();
    await page.locator("#modal-actions button", { hasText: "Restart on this key" }).click();
    await until(() => posts.length === 2, "the unpin POST");
    assert.deepEqual(posts[1], { method: "POST", route: "/api/sessions/s1/key",
                                 body: { key_id: null } });
    await page.waitForFunction(() =>
      !document.querySelector('#session-list li.sess-card[data-name="s1"] .sess-key.on'));

    assert.deepEqual(errors, [], "the page threw nothing");
    console.log("sessionkey browser checks passed");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
