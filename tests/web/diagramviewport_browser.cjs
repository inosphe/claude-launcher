/* Real browser geometry/input regression. Requires Playwright (NODE_PATH may
   point to an external install). Run: node tests/web/diagramviewport_browser.cjs */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
const start = source.indexOf("function escXml(");
const end = source.indexOf("/* ------------------------------------------------------------------ */", start);
const wfDiagramSvg = new Function(source.slice(start, end) + "; return wfDiagramSvg;")();
const workflow = { name: "Viewport regression", start: "step0", steps: Array.from({ length: 18 }, (_, i) => ({
  id: `step${i}`, title: `Workflow step ${i + 1}`, next: i === 17 ? null : `step${i + 1}`,
})) };
const diagram = (kind = "wfd") => `<svg class="${kind}" viewBox="0 0 1600 2400" width="1600" height="2400">
  <g class="wfd-node" data-step="first"><rect x="20" y="20" width="200" height="60"/></g>
  <rect x="1400" y="2300" width="180" height="80"/>
</svg>`;
(async () => {
  const browser = await chromium.launch({ headless: true });
  let checks = 0;
  try {
    for (const mobile of [false, true]) {
      const context = await browser.newContext({ viewport: mobile ? { width: 360, height: 740 } : { width: 1280, height: 900 }, hasTouch: true, isMobile: mobile });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(String(e)));
      await page.setContent(`<meta name="viewport" content="width=device-width, initial-scale=1">
        <div id="wf-view" style="display:block;width:100%;padding:12px;overflow:auto">
        <div class="wf-cols"><div class="wf-diagram">${diagram()}</div></div></div>`);
      await page.addStyleTag({ path: path.join(root, "style.css") });
      await page.addScriptTag({ path: path.join(root, "diagram-viewport.js") });
      const vp = page.locator(".diagram-viewport");
      await vp.waitFor();
      await page.waitForTimeout(80);
      const geometry = () => vp.evaluate((e) => ({ left: e.scrollLeft, top: e.scrollTop, w: e.clientWidth, h: e.clientHeight,
        sw: e.scrollWidth, sh: e.scrollHeight, svgW: e.querySelector("svg").getBoundingClientRect().width }));
      let g = await geometry();
      assert(g.sw <= g.w + 1 && g.sh <= g.h + 1, "fit contains whole diagram"); checks++;
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "no mobile page overflow"); checks++;
      async function checkPageWheel() {
        await page.evaluate(() => {
          const host = document.getElementById("wf-view");
          host.style.height = "600px";
          const below = document.createElement("div");
          below.id = "below-diagram";
          below.style.height = "2000px";
          host.append(below);
        });
        const initial = await geometry();
        const bounds = await vp.boundingBox();
        await page.mouse.move(bounds.x + 100, bounds.y + 100);
        await page.mouse.wheel(0, 120);
        await page.waitForTimeout(80);
        assert(await page.locator("#wf-view").evaluate((e) => e.scrollTop >= 119), "wheel over diagram scrolls run page"); checks++;
        assert.equal((await geometry()).top, initial.top, "ordinary wheel leaves canvas position unchanged"); checks++;
        await page.mouse.move(bounds.x + 100, bounds.y + 20);
        await page.mouse.wheel(0, -120);
        await page.waitForTimeout(80);
        assert.equal(await page.locator("#wf-view").evaluate((e) => e.scrollTop), 0, "reverse wheel scrolls page up"); checks++;
        await page.evaluate(() => {
          document.getElementById("below-diagram").remove();
          document.getElementById("wf-view").style.removeProperty("height");
        });
      }
      await checkPageWheel();
      await page.getByRole("button", { name: "Actual size", exact: true }).click();
      g = await geometry();
      assert.equal(g.svgW, 1600); checks++;
      assert(g.sw > g.w && g.sh > g.h, "both axes reachable at 100%"); checks++;
      await vp.evaluate((e) => { e.scrollLeft = 400; e.scrollTop = 500; });
      await page.waitForTimeout(40);
      const box = await vp.boundingBox();
      await page.mouse.move(box.x + 120, box.y + 120);
      const before = await geometry();
      await page.keyboard.down("Control");
      await page.mouse.wheel(0, -100);
      await page.keyboard.up("Control");
      await page.waitForTimeout(50);
      g = await geometry();
      assert(g.svgW > before.svgW, "ctrl-wheel zoom"); checks++;
      const oldPoint = (before.left + 120 - 12) / before.svgW;
      const newPoint = (g.left + 120 - 12) / g.svgW;
      assert(Math.abs(oldPoint - newPoint) < 0.002, "zoom anchors cursor"); checks++;
      await checkPageWheel();
      const scrolled = await geometry();
      await page.mouse.move(box.x + 120, box.y + 120);
      await page.keyboard.down("Shift");
      await page.mouse.wheel(0, 100);
      await page.keyboard.up("Shift");
      await page.waitForTimeout(50);
      assert((await geometry()).left > scrolled.left, "shift-wheel pans horizontally"); checks++;
      await vp.focus();
      await page.keyboard.press("ArrowRight");
      assert((await geometry()).left > scrolled.left, "keyboard pan"); checks++;
      const retained = await geometry();
      await page.evaluate((markup) => { document.querySelector(".wf-diagram").innerHTML = markup; }, diagram());
      await page.waitForTimeout(80);
      g = await geometry();
      assert.equal(g.svgW, retained.svgW, "poll retains zoom");
      assert(Math.abs(g.left - retained.left) < 2 && Math.abs(g.top - retained.top) < 2, "poll retains pan"); checks += 2;
      assert(await vp.evaluate((e) => e === document.activeElement), "poll retains keyboard focus"); checks++;
      // Drag across a renderer replacement: the same viewport keeps its gesture.
      await page.mouse.move(box.x + 150, box.y + 160);
      await page.mouse.down();
      await page.mouse.move(box.x + 120, box.y + 130);
      await page.evaluate((markup) => { document.querySelector(".wf-diagram").innerHTML = markup; }, diagram());
      await page.waitForTimeout(40);
      await page.mouse.move(box.x + 90, box.y + 100);
      await page.mouse.up();
      assert((await geometry()).top > retained.top + 50, "drag survives poll"); checks++;
      await page.getByRole("button", { name: "Actual size", exact: true }).click();
      await vp.evaluate((e) => { e.scrollLeft = 0; e.scrollTop = 0; });
      await page.waitForTimeout(50);
      await page.evaluate(() => {
        window.nodeClicks = 0;
        document.querySelector(".wfd-node").addEventListener("click", () => window.nodeClicks++);
      });
      await page.locator(".wfd-node").click();
      assert.equal(await page.evaluate(() => window.nodeClicks), 1, "node click preserved"); checks++;
      // Native touch events exercise PointerEvent generation and pinch anchoring.
      const cdp = await context.newCDPSession(page);
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: box.x + 80, y: box.y + 100 }, { x: box.x + 180, y: box.y + 100 }] });
      await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: box.x + 50, y: box.y + 100 }, { x: box.x + 210, y: box.y + 100 }] });
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      assert((await geometry()).svgW > 2000, "two-finger pinch zoom"); checks++;
      assert.equal(await page.evaluate(() => window.nodeClicks), 1, "pinch does not select node"); checks++;
      const preTouch = await geometry();
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: box.x + 180, y: box.y + 180 }] });
      await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: box.x + 100, y: box.y + 100 }] });
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      assert((await geometry()).top > preTouch.top + 60, "one-finger touch pans"); checks++;
      await vp.focus();
      await page.keyboard.press("Home");
      g = await geometry();
      assert(g.sw <= g.w + 1 && g.sh <= g.h + 1, "Home resets to fit"); checks++;
      await page.setViewportSize({ width: mobile ? 740 : 960, height: mobile ? 360 : 700 });
      await page.waitForTimeout(100);
      g = await geometry();
      assert(g.sw <= g.w + 1 && g.sh <= g.h + 1, "fit follows resize/orientation"); checks++;
      await page.getByRole("button", { name: "Fit diagram width", exact: true }).click();
      g = await geometry();
      assert(g.sw <= g.w + 1 && g.sh > g.h, "width fit permits vertical reading"); checks++;
      await page.getByRole("button", { name: "Expand diagram (Escape to close)", exact: true }).click();
      await page.waitForTimeout(50);
      assert.equal(await page.locator(".diagram-expanded").count(), 1); checks++;
      await page.keyboard.press("Escape");
      assert.equal(await page.locator(".diagram-expanded").count(), 0); checks++;
      for (const kind of ["mesh-ring", "flow-ring"]) {
        await page.evaluate((markup) => { document.querySelector(".wf-diagram").innerHTML = markup; }, diagram(kind));
        await page.waitForTimeout(50);
        assert.equal(await page.locator(".diagram-viewer").count(), 1, `${kind} mounts once`); checks++;
      }
      await page.evaluate((markup) => { document.querySelector(".wf-diagram").innerHTML = markup; },
        wfDiagramSvg(workflow, { status: "running", step_id: "step4", history: [] }, null));
      await page.waitForTimeout(80);
      await page.getByRole("button", { name: "Fit entire diagram (Home)", exact: true }).click();
      assert(await vp.evaluate((e) => {
        const r = e.getBoundingClientRect();
        return [...e.querySelectorAll(".wfd-node")].every((n) => {
          const b = n.getBoundingClientRect();
          return b.left >= r.left && b.right <= r.right && b.top >= r.top && b.bottom <= r.bottom;
        });
      }), "actual workflow renderer fits every node"); checks++;
      assert.deepEqual(errors, []); checks++;
      await context.close();
    }
    console.log(`${checks} browser checks passed (desktop and mobile)`);
  } finally { await browser.close(); }
})().catch((e) => { console.error(e); process.exitCode = 1; });
