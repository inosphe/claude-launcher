/* The rail's tab strips scroll by dragging, not only by their scrollbar
   (claunch-h3xa3).

   Both strips -- the project tabs (#project-tabs) and the group tabs
   (#session-group-tabs) -- are wider than the rail as soon as there are a few
   projects or groups, and their scrollbar is thin. app.js
   enableTabDragScroll lets the mouse drag the strip itself. This runs the
   real function from app.js and the real style.css in Chromium, on each
   strip's own markup from index.html filled with more tabs than fit, and
   drives it with real mouse input:

   - a drag moves scrollLeft by the distance dragged, and the strip carries
     `.dragging` (the grabbing cursor) only while it lasts;
   - the click the browser sends when that drag is released does not pick the
     tab it ended on;
   - a short press -- under TAB_DRAG_THRESHOLD pixels of travel -- is still a
     click on its tab, and so is the first click after a drag;
   - installing twice adds no second set of listeners (the renderers call it
     on every rebuild);
   - both renderers do install it on their strip. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const STATIC = path.join(__dirname, "../../src/claude_launcher/web/static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
function threshold() {
  const m = src.match(/const TAB_DRAG_THRESHOLD = (\d+);/);
  if (!m) throw new Error("cannot locate TAB_DRAG_THRESHOLD in app.js");
  return Number(m[1]);
}
function strip(id) {
  const m = html.match(new RegExp(`<div id="${id}"[^>]*>`));
  if (!m) throw new Error(`cannot locate #${id} in index.html`);
  return m[0].replace(/\bhidden\b/, "") + "</div>";
}

// Both renderers install the drag on the strip they fill.
for (const [renderer, id] of [["renderProjectTabs", "project-tabs"],
                              ["renderSessionGroupTabs", "session-group-tabs"]]) {
  const body = slice(renderer);
  assert.ok(body.includes(`$("${id}")`), `${renderer} fills #${id}`);
  assert.ok(body.includes("enableTabDragScroll(bar)"), `${renderer} installs the drag on #${id}`);
}

const STRIPS = [
  { id: "project-tabs", tab: "project-tab" },
  { id: "session-group-tabs", tab: "session-group-tab" },
];
const page_html = `<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head>
<body style="margin:0"><aside id="sidebar" style="width:260px;display:flex;flex-direction:column">
${STRIPS.map(s => strip(s.id)).join("\n")}
</aside></body></html>`;

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 800, height: 400 } });
    const errors = [];
    page.on("pageerror", e => errors.push(String(e)));
    await page.setContent(page_html);
    await page.addScriptTag({ content: `const TAB_DRAG_THRESHOLD = ${threshold()};\n${slice("enableTabDragScroll")}\nwindow.enableTabDragScroll = enableTabDragScroll;` });
    await page.evaluate((strips) => {
      window.picked = [];
      for (const s of strips) {
        const bar = document.getElementById(s.id);
        for (let i = 0; i < 12; i++) {
          const el = document.createElement("div");
          el.className = s.tab;
          el.textContent = `${s.id}-tab-${i}`;
          el.addEventListener("click", () => window.picked.push(el.textContent));
          bar.appendChild(el);
        }
        window.enableTabDragScroll(bar);
        window.enableTabDragScroll(bar);
      }
    }, STRIPS);

    for (const s of STRIPS) {
      const bar = page.locator(`#${s.id}`);
      const geo = await bar.evaluate(n => ({ sw: n.scrollWidth, cw: n.clientWidth }));
      assert.ok(geo.sw > geo.cw + 200, `#${s.id} overflows the rail (${geo.sw} > ${geo.cw})`);
      const box = await bar.boundingBox();
      const y = box.y + box.height / 2;
      const left = () => bar.evaluate(n => n.scrollLeft);
      const picked = () => page.evaluate(() => window.picked.splice(0));
      assert.equal(await left(), 0);

      // A drag to the left scrolls the strip right by the distance dragged.
      await page.mouse.move(box.x + 200, y);
      await page.mouse.down();
      await page.mouse.move(box.x + 160, y, { steps: 4 });
      await page.mouse.move(box.x + 80, y, { steps: 4 });
      assert.equal(await bar.evaluate(n => n.classList.contains("dragging")), true, `#${s.id} is .dragging mid-drag`);
      assert.equal(await bar.evaluate(n => getComputedStyle(n).cursor), "grabbing");
      await page.mouse.up();
      assert.equal(await left(), 120, `#${s.id} scrolled by the 120px dragged`);
      assert.equal(await bar.evaluate(n => n.classList.contains("dragging")), false);
      assert.deepEqual(await picked(), [], `the release of a drag on #${s.id} picks no tab`);

      // Dragging back the other way scrolls back.
      await page.mouse.move(box.x + 60, y);
      await page.mouse.down();
      await page.mouse.move(box.x + 110, y, { steps: 5 });
      await page.mouse.up();
      assert.equal(await left(), 70);
      assert.deepEqual(await picked(), []);

      // A short press -- under the threshold -- stays a click on its tab, and
      // it is the first click after a drag: the flag the drag left is down.
      // The tab is one the scrolled strip shows whole.
      const shown = await bar.evaluate((n) => {
        const r = n.getBoundingClientRect();
        return [...n.children].findIndex((t) => {
          const b = t.getBoundingClientRect();
          return b.left >= r.left && b.right <= r.right;
        });
      });
      assert.ok(shown >= 0, `#${s.id} shows a whole tab`);
      const name = `${s.id}-tab-${shown}`;
      const tab = page.locator(`#${s.id} .${s.tab}`).nth(shown);
      const tb = await tab.boundingBox();
      await page.mouse.move(tb.x + 10, tb.y + tb.height / 2);
      await page.mouse.down();
      await page.mouse.move(tb.x + 10 + threshold() - 2, tb.y + tb.height / 2);
      await page.mouse.up();
      assert.equal(await left(), 70, "a press under the threshold does not scroll");
      assert.deepEqual(await picked(), [name], `a short press on #${s.id} picks its tab`);
      await tab.click();
      assert.deepEqual(await picked(), [name]);

      // A drag released off the strip still ends: the next click is a click.
      await page.mouse.move(box.x + 100, y);
      await page.mouse.down();
      await page.mouse.move(box.x + 140, y + 200, { steps: 4 });
      await page.mouse.up();
      assert.equal(await left(), 30);
      await tab.click();
      assert.deepEqual(await picked(), [name]);
    }

    // A strip that fits does not start a drag: its press is a click.
    await page.evaluate(() => {
      const bar = document.getElementById("project-tabs");
      while (bar.children.length > 1) bar.lastChild.remove();
      bar.scrollLeft = 0;
    });
    const only = page.locator("#project-tabs .project-tab").first();
    const ob = await only.boundingBox();
    await page.mouse.move(ob.x + 4, ob.y + ob.height / 2);
    await page.mouse.down();
    await page.mouse.move(ob.x + 30, ob.y + ob.height / 2, { steps: 3 });
    await page.mouse.up();
    assert.equal(await page.locator("#project-tabs").evaluate(n => n.classList.contains("dragging")), false);
    assert.deepEqual(await page.evaluate(() => window.picked.splice(0)), ["project-tabs-tab-0"]);

    assert.deepEqual(errors, []);
    console.log("tabdragscroll_browser: ok");
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
