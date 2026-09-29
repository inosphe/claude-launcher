/* The session grid stays inside #terminal at every box size and text size.

   The page sets `* { box-sizing: border-box }`, and #terminal carries a 6px
   padding. FitAddon.proposeDimensions reads the parent's computed `height`
   and `width` as the room the grid has; under border-box those are the
   border-box sizes, padding included, so the addon counted 12px of padding as
   room. Whenever the box's remainder after whole rows plus those 12px reached
   one cell, fit() claimed one row (or column) more than fits, and #terminal's
   overflow:hidden clipped it -- the last line of the session, usually the
   status line under the prompt, drawn past the bottom of the page. Which
   window heights and text sizes hit it depends on the cell height, so it
   showed up only at some resolutions and font sizes (claunch-pgz2j).

   This drives shipped style.css and the vendored xterm + fit addon in a real
   browser, sweeps the box height and width against every text size the zoom
   control offers, and asserts the drawn screen ends inside the content box. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const PAGE = `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/vendor/xterm.css">
<link rel="stylesheet" href="/style.css">
<script src="/vendor/xterm.js"></script><script src="/vendor/addon-fit.js"></script>
</head><body style="margin:0">
<div id="col" style="display:flex;flex-direction:column;width:800px;height:600px">
  <div id="terminal"></div>
</div></body></html>`;

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname === "/") { res.setHeader("Content-Type", "text/html"); return res.end(PAGE); }
  const file = path.join(root, url.pathname);
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : "text/css");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const bad = await page.evaluate(() => {
      const host = document.getElementById("terminal");
      const col = document.getElementById("col");
      const term = new Terminal({ fontSize: 13, scrollback: 1000 });
      const fit = new FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open(host);
      const out = [];
      for (let font = 8; font <= 28; font++) {
        term.options.fontSize = font;
        for (let h = 200; h <= 520; h += 1) {
          const w = 400 + h;   // walks the width through the cell pitch too
          col.style.height = `${h}px`;
          col.style.width = `${w}px`;
          fit.fit();
          const box = host.getBoundingClientRect();
          const cs = getComputedStyle(host);
          const bottom = box.bottom - parseFloat(cs.paddingBottom);
          const right = box.right - parseFloat(cs.paddingRight);
          const screen = host.querySelector(".xterm-screen").getBoundingClientRect();
          if (screen.bottom > bottom + 0.5 || screen.right > right + 0.5) {
            out.push({ font, h, w, rows: term.rows, cols: term.cols,
                       over_y: +(screen.bottom - bottom).toFixed(1),
                       over_x: +(screen.right - right).toFixed(1) });
          }
        }
      }
      return out;
    });
    assert.equal(bad.length, 0,
      `grid drawn past #terminal's content box in ${bad.length} cases, e.g. ${JSON.stringify(bad.slice(0, 5))}`);
    console.log("termfit: grid stays inside #terminal across font 8-28 x box 200-520px");
  } finally {
    await browser.close();
    server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
