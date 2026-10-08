/* Mesh chat between two browsers on one daemon (chat.js, claunch-woxcm).

   Drives the shipped page against a fixture daemon whose chat routes keep one
   in-memory room, with two browser contexts — two separate localStorages, so
   two people:
     1. each browser mints its own key on first load and keeps it on reload;
     2. the nav entry sits after Mesh and the floating button is on every
        page except the Chat page itself;
     3. A renames itself and posts from the Chat page; the post carries A's
        key and new name;
     4. B, on another page, gets the badge on the nav and the floating
        button, and a toast it can answer from without opening the room;
     5. A sees B's reply in the open room under B's name, and its own
        message on the right;
     6. opening the room in B's modal clears B's badge. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const messages = [{ seq: 1, id: "h1", at: "2026-10-07T10:00:00+00:00", key: "historyKey01", name: "old", machine: "", body: "history" }];
const posts = [];
function summary() {
  return { mesh: "m", address: "m@pcA", project: "", authority: "pcA", local: true,
           last_seq: messages.length, last: messages[messages.length - 1] || null };
}
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    if (url.pathname === "/api/chat/rooms") return res.end(JSON.stringify({ machine: "pcA", rooms: [summary()] }));
    if (url.pathname === "/api/chat/rooms/m/messages") {
      if (req.method === "POST") {
        let raw = "";
        req.on("data", (c) => { raw += c; });
        req.on("end", () => {
          const body = JSON.parse(raw);
          posts.push(body);
          const entry = { seq: messages.length + 1, id: `p${posts.length}`, at: new Date().toISOString(),
                          key: body.key, name: body.name, machine: "pcA", body: body.body };
          messages.push(entry);
          res.end(JSON.stringify({ ...summary(), message: entry }));
        });
        return;
      }
      const after = url.searchParams.get("after");
      const rows = after == null ? messages.slice(-200) : messages.slice(Number(after));
      return res.end(JSON.stringify({ ...summary(), messages: rows }));
    }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions: [], llm_configured: true };
    else if (url.pathname === "/api/operator/pending") data = { total: 0 };
    else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ headless: true });
  try {
    const ctxA = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const ctxB = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const a = await ctxA.newPage(), b = await ctxB.newPage();
    for (const p of [a, b]) p.on("pageerror", (err) => { throw err; });
    await a.goto(base + "#/chat/m");
    await b.goto(base + "#/");
    await a.waitForSelector("body.chat-page");
    const identity = (p) => p.evaluate(() => JSON.parse(localStorage.getItem("claunch-chat-identity")));
    const idA = await identity(a), idB = await identity(b);
    assert.match(idA.key, /^[0-9a-f]{32}$/);
    assert.notEqual(idA.key, idB.key, "two browsers are two people");
    // B's first poll after the reload is its starting point: what came before
    // it is history, so A posts only after it.
    const firstPoll = b.waitForResponse((r) => r.url().endsWith("/api/chat/rooms"));
    await b.reload();
    await firstPoll;
    await b.waitForSelector('body[data-page="home"]');
    assert.equal((await identity(b)).key, idB.key, "a reload keeps the key");

    // 2. nav entry after Mesh; the floating button everywhere but the page
    const nav = await a.$$eval("#rail-nav a", (els) => els.map((e) => e.dataset.page));
    assert.equal(nav[nav.indexOf("meshes") + 1], "chat");
    assert.equal(await a.isVisible(".chat-fab"), false, "no button over the Chat page");
    assert.equal(await b.isVisible(".chat-fab"), true);
    // ...and it is one control with the Operator's: both buttons are segments
    // of the single #fab-dock, and only one dock exists.
    const dockOf = (p) => p.evaluate(() => ({
      docks: document.querySelectorAll(".fab-dock").length,
      parts: [...document.querySelectorAll("#fab-dock > button")].map((e) => e.className.split(" ")[0]),
      visible: [...document.querySelectorAll("#fab-dock > button")].filter((e) => e.offsetParent).map((e) => e.className.split(" ")[0]),
    }));
    assert.deepEqual(await dockOf(b), { docks: 1, parts: ["operator-fab", "chat-fab"], visible: ["operator-fab", "chat-fab"] });
    assert.deepEqual((await dockOf(a)).visible, ["operator-fab"], "on the Chat page the dock keeps only the Operator's segment");
    // The dock's remaining segment does not cover the composer's send button.
    const clear = (p, sel) => p.evaluate((sel) => {
      const r1 = document.getElementById("fab-dock").getBoundingClientRect();
      const r2 = document.querySelector(sel).getBoundingClientRect();
      return r1.right <= r2.left || r2.right <= r1.left || r1.bottom <= r2.top || r2.bottom <= r1.top;
    }, sel);
    assert.ok(await clear(a, "#chat-view .chat-composer button[type=submit]"), "the dock covers the send button");

    // 3. A renames itself and posts
    await a.waitForSelector("#chat-view .chat-feed .chat-msg");
    await a.fill("#chat-view .chat-who input", "Alice");
    await a.click("#chat-view .chat-who button");
    assert.equal((await identity(a)).name, "Alice");
    await a.fill("#chat-view .chat-composer textarea", "hello from A");
    await a.press("#chat-view .chat-composer textarea", "Enter");
    await a.waitForFunction(() => [...document.querySelectorAll("#chat-view .chat-mine .chat-text")].some((e) => e.textContent === "hello from A"));
    assert.deepEqual([posts[0].key, posts[0].name, posts[0].body], [idA.key, "Alice", "hello from A"]);

    // 4. B is told, and answers from the toast
    await b.waitForSelector(".chat-toast", { timeout: 15000 });
    assert.equal(await b.textContent("#chat-nav-badge"), "1");
    assert.equal(await b.textContent("#chat-fab-badge"), "1");
    assert.match(await b.textContent(".chat-toast"), /Alice[\s\S]*hello from A/);
    if (process.env.CHAT_SHOT_T) await b.screenshot({ path: process.env.CHAT_SHOT_T });
    // The toasts stay clear of the dock wherever the dock stands: its own
    // corner, and above a session page's input bar (#term-input shown), on a
    // desktop and on a phone.
    const toastClear = () => b.evaluate(() => {
      const d = document.getElementById("fab-dock").getBoundingClientRect();
      const t = document.querySelector(".chat-toasts").getBoundingClientRect();
      return t.bottom <= d.top;
    });
    for (const size of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
      await b.setViewportSize(size);
      for (const bar of [false, true]) {
        await b.evaluate((bar) => document.getElementById("term-input").classList.toggle("hidden", !bar), bar);
        assert.ok(await toastClear(), `toasts over the dock at ${size.width}px, input bar ${bar ? "shown" : "hidden"}`);
      }
    }
    await b.evaluate(() => document.getElementById("term-input").classList.add("hidden"));
    await b.setViewportSize({ width: 1440, height: 900 });
    await b.fill(".chat-toast-reply input", "hi Alice");
    await b.click(".chat-toast-reply button[type=submit]");
    await b.waitForSelector(".chat-toast", { state: "detached" });
    assert.deepEqual([posts[1].key, posts[1].body], [idB.key, "hi Alice"]);

    // 5. A sees the reply in the open room, without a toast for it
    await a.waitForFunction(() => [...document.querySelectorAll("#chat-view .chat-msg:not(.chat-mine) .chat-text")].some((e) => e.textContent === "hi Alice"), null, { timeout: 15000 });
    const replyName = await a.$$eval("#chat-view .chat-msg", (els) => els.at(-1).querySelector("strong").textContent);
    assert.equal(replyName, idB.name);
    assert.equal(await a.$(".chat-toast"), null, "the room on screen raises no toast");
    if (process.env.CHAT_SHOT_A) await a.screenshot({ path: process.env.CHAT_SHOT_A });

    // 6. B's badge: its own reply was read; opening the modal reads the rest
    await b.click(".chat-fab");
    await b.waitForSelector(".chat-modal[open] .chat-msg");
    await b.waitForFunction(() => document.getElementById("chat-fab-badge").hidden);
    if (process.env.CHAT_SHOT_B) await b.screenshot({ path: process.env.CHAT_SHOT_B });
  } finally {
    await browser.close();
    server.close();
  }
  console.log("chat_browser: ok");
})().catch((err) => { console.error(err); process.exit(1); });
