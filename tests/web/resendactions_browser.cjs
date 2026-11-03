/* The hidden resend/revert actions of a thread message must not occupy
   layout (claunch-tpbqz.2). They used to hide with visibility:hidden inside
   the meta flex row, which kept their width and left an empty gap beside
   every user message. They now float absolutely on the pane background
   above the bubble's top-right corner.

   Serves the real style.css through a route stub and checks in headless
   chromium:
     1. off-hover the actions are absolutely positioned and hidden, and the
        bubble is exactly as wide as the same bubble without the actions;
     2. hovering shows them as a chip straddling the bubble's top edge
        without changing the bubble's width, and the button is clickable;
     3. the focus-within rule still reveals them (keyboard path). */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const REPLICA = `<!doctype html><html><head>
<link rel="stylesheet" href="http://resend.test/static/style.css">
<style>body { margin: 0; background: #14161a; }</style></head><body>
<div class="th-messages" style="height: 400px; width: 600px;">
  <div class="th-msg th-user" id="u1">
    <div class="th-msg-meta">
      <span class="th-msg-who">나</span><span class="th-msg-at">오후 1:55</span>
      <span class="th-msg-actions" id="acts">
        <button class="th-act" type="button">재전송</button>
        <button class="th-act th-act-revert" id="revert" type="button">revert</button>
      </span>
    </div>
    <div class="th-msg-body">짧은 메시지</div>
  </div>
</div></body></html>`;

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
    await page.route("http://resend.test/**", (route) => {
      const u = new URL(route.request().url());
      if (u.pathname === "/static/style.css")
        return route.fulfill({ body: fs.readFileSync(path.join(root, "style.css")), contentType: "text/css" });
      return route.fulfill({ body: REPLICA, contentType: "text/html" });
    });
    await page.goto("http://resend.test/");

    // the stylesheet actually applied (user bubble's dark blue)
    assert.equal(await page.evaluate(() =>
      getComputedStyle(document.querySelector(".th-msg.th-user")).backgroundColor), "rgb(18, 39, 74)");

    const off = await page.evaluate(() => {
      const user = document.getElementById("u1");
      const acts = document.getElementById("acts");
      const clone = user.cloneNode(true);
      clone.querySelector(".th-msg-actions").remove();
      clone.style.visibility = "hidden";
      user.parentNode.appendChild(clone);
      const out = {
        position: getComputedStyle(acts).position,
        visibility: getComputedStyle(acts).visibility,
        userWidth: user.getBoundingClientRect().width,
        cloneWidth: clone.getBoundingClientRect().width,
      };
      clone.remove();
      return out;
    });
    assert.equal(off.position, "absolute", "hidden actions must be out of flow");
    assert.equal(off.visibility, "hidden");
    assert(Math.abs(off.userWidth - off.cloneWidth) <= 0.5,
      `hidden actions still occupy layout: ${off.userWidth}px vs ${off.cloneWidth}px`);

    await page.hover("#u1");
    const on = await page.evaluate(() => {
      const user = document.getElementById("u1");
      const box = document.getElementById("acts").getBoundingClientRect();
      const userBox = user.getBoundingClientRect();
      return {
        visibility: getComputedStyle(document.getElementById("acts")).visibility,
        userWidth: userBox.width,
        straddles: box.top < userBox.top && box.bottom > userBox.top,
      };
    });
    assert.equal(on.visibility, "visible", "hover must reveal the actions");
    assert(Math.abs(on.userWidth - off.userWidth) <= 0.5, "hover must not move the bubble");
    assert(on.straddles, "the chip must straddle the bubble's top edge");
    await page.click("#revert"); // a real hit target

    // keyboard path: focus-within still reveals the actions
    await page.evaluate(() => { document.getElementById("u1").blur(); document.body.click(); });
    const focus = await page.evaluate(() => {
      const user = document.getElementById("u1");
      user.tabIndex = -1;
      user.focus();
      document.getElementById("revert").focus();
      return getComputedStyle(document.getElementById("acts")).visibility;
    });
    assert.equal(focus, "visible", "focus-within must reveal the actions");

    console.log("resend/revert hidden actions stay out of layout: passed",
      `${off.userWidth}px == no-actions ${off.cloneWidth}px, hover=${on.visibility}`);
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
