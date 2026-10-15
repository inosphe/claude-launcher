/* The light theme in the shipped page (theme.js, claunch-xpe81).

   Serves the real index.html and its static files against a fixture whose
   API answers nothing, and drives the theme switch in a real browser:
     1. a first visit is dark, and the switch in the rail header offers light;
     2. pressing it turns the page light (background light, text dark), keeps
        the choice in localStorage, and flips the switch's glyph;
     3. the terminals are inverted as a picture (.xterm carries the filter),
        and the vendored xterm.css is not rewritten; the picture keeps its
        tones: no channel of a pale colour is clipped to 0, text is a soft
        black, dim text stays grey (claunch-bj6ee);
     4. switching back restores every rewritten declaration to the text it
        had: the sheets after light-then-dark equal the sheets before;
     5. a reload comes back light before any app script has run;
     6. a <style> added while light is themed when it lands, and a sheet
        marked data-theme-copy is skipped and dropped on the next switch;
     7. another tab's switch (a storage event) is followed. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.statusCode = 404;
    res.setHeader("Content-Type", "application/json");
    return res.end("{}");
  }
  const rel = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, "");
  const file = path.join(root, rel);
  try {
    const body = fs.readFileSync(file);
    res.setHeader("Content-Type", TYPES[path.extname(file)] || "application/octet-stream");
    res.end(body);
  } catch { res.statusCode = 404; res.end(); }
});

function sheetsText() {
  return Array.from(document.styleSheets)
    .filter((s) => !(s.ownerNode && s.ownerNode.dataset && "themeCopy" in s.ownerNode.dataset))
    .map((s) => { try { return Array.from(s.cssRules).map((r) => r.cssText).join("\n"); } catch { return ""; } })
    .join("\n----\n");
}

const lum = (css) => {
  const [r, g, b] = css.match(/[\d.]+/g).slice(0, 3).map(Number);
  return (Math.max(r, g, b) + Math.min(r, g, b)) / 2 / 255;
};

/* The colours a light-theme terminal shows for a few dark-palette colours.
   A filter's output is only on screen, so the swatches are photographed and
   the photo read back through a canvas. */
async function terminalSwatches(page) {
  const names = { bg: "#14161a", text: "#ffffff", dim: "#999999", lavender: "#b1b9f9" };
  await page.evaluate((names) => {
    const x = document.createElement("div");
    x.className = "xterm";
    x.id = "swatch-probe";
    x.style.cssText = "position:fixed;left:0;top:0;z-index:99999;display:flex;background:#14161a";
    for (const [name, colour] of Object.entries(names)) {
      const s = document.createElement("div");
      s.style.cssText = `width:40px;height:40px;background:${colour}`;
      s.dataset.name = name;
      x.append(s);
    }
    document.body.append(x);
  }, names);
  const shot = await page.screenshot({ clip: { x: 0, y: 0, width: 40 * Object.keys(names).length, height: 40 } });
  await page.evaluate(() => document.getElementById("swatch-probe").remove());
  return page.evaluate(async ({ png, keys }) => {
    const img = new Image();
    img.src = "data:image/png;base64," + png;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width; c.height = img.height;
    const g = c.getContext("2d");
    g.drawImage(img, 0, 0);
    const scale = img.width / (40 * keys.length);
    const out = {};
    keys.forEach((k, i) => {
      const [r, gg, b] = g.getImageData(Math.floor((i * 40 + 20) * scale), Math.floor(20 * scale), 1, 1).data;
      out[k] = `rgb(${r}, ${gg}, ${b})`;
    });
    return out;
  }, { png: shot.toString("base64"), keys: Object.keys(names) });
}

(async () => {
  await safeListen(server);
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(base);

    // 1. dark by default
    const before = await page.evaluate(() => ({
      theme: document.documentElement.dataset.theme,
      bg: getComputedStyle(document.body).backgroundColor,
      glyph: document.getElementById("theme-toggle").textContent,
    }));
    assert.equal(before.theme, "dark");
    assert.equal(before.bg, "rgb(20, 22, 26)");
    assert.equal(before.glyph, "☀");
    const darkSheets = await page.evaluate(sheetsText);
    const vendorBefore = await page.evaluate(() => Array.from(document.styleSheets)
      .find((s) => /vendor\/xterm\.css/.test(s.href)).cssRules.length &&
      Array.from(Array.from(document.styleSheets).find((s) => /vendor\/xterm\.css/.test(s.href)).cssRules)
        .map((r) => r.cssText).join("\n"));

    // 2. the switch turns the page light
    await page.click("#theme-toggle");
    const light = await page.evaluate(() => {
      const x = document.createElement("div");
      x.className = "xterm";
      document.body.append(x);
      const out = {
        theme: document.documentElement.dataset.theme,
        scheme: document.documentElement.style.colorScheme,
        bg: getComputedStyle(document.body).backgroundColor,
        fg: getComputedStyle(document.body).color,
        rail: getComputedStyle(document.getElementById("sidebar")).backgroundColor,
        stored: localStorage.getItem("claunch.theme"),
        glyph: document.getElementById("theme-toggle").textContent,
        pressed: document.getElementById("theme-toggle").getAttribute("aria-pressed"),
        filter: getComputedStyle(x).filter,
      };
      x.remove();
      return out;
    });
    assert.equal(light.theme, "light");
    assert.equal(light.scheme, "light");
    assert.ok(lum(light.bg) > 0.85, `page background ${light.bg}`);
    assert.ok(lum(light.fg) < 0.2, `body text ${light.fg}`);
    assert.ok(lum(light.rail) > 0.8, `rail background ${light.rail}`);
    assert.equal(light.stored, "light");
    assert.equal(light.glyph, "☾");
    assert.equal(light.pressed, "true");
    // 3. terminals are inverted as a picture; xterm.css is untouched
    assert.match(light.filter, /url\("?#claunch-light-term"?\)/);
    const swatch = await terminalSwatches(page);
    assert.ok(lum(swatch.bg) > 0.85, `terminal background ${swatch.bg}`);
    // the dark palette's lavender (inline code) stays a blue, not a clipped navy
    const lav = swatch.lavender.match(/\d+/g).map(Number);
    assert.ok(Math.min(...lav) >= 30 && lav[2] === Math.max(...lav), `lavender became ${swatch.lavender}`);
    const text = swatch.text.match(/\d+/g).map(Number);
    assert.ok(Math.min(...text) >= 10 && Math.max(...text) <= 60, `white text became ${swatch.text}`);
    assert.ok(lum(swatch.dim) - lum(swatch.text) > 0.15 && lum(swatch.bg) - lum(swatch.dim) > 0.3,
      `dim text ${swatch.dim} between text ${swatch.text} and background ${swatch.bg}`);
    const vendorLight = await page.evaluate(() =>
      Array.from(Array.from(document.styleSheets).find((s) => /vendor\/xterm\.css/.test(s.href)).cssRules)
        .map((r) => r.cssText).join("\n"));
    assert.equal(vendorLight, vendorBefore, "vendored xterm.css was rewritten");

    // 4. back to dark restores every declaration
    await page.click("#theme-toggle");
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    assert.equal(await page.evaluate(sheetsText), darkSheets, "dark sheets differ after a round trip");
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), "rgb(20, 22, 26)");

    // 5. a reload remembers light, before any app script runs
    await page.click("#theme-toggle");
    await page.addInitScript(() => {
      document.addEventListener("readystatechange", () => {
        if (document.readyState === "interactive") window.__themeAtInteractive = document.documentElement.dataset.theme;
      });
    });
    await page.reload();
    const reloaded = await page.evaluate(() => ({
      early: window.__themeAtInteractive,
      theme: document.documentElement.dataset.theme,
      bg: getComputedStyle(document.body).backgroundColor,
    }));
    assert.equal(reloaded.early, "light");
    assert.equal(reloaded.theme, "light");
    assert.ok(lum(reloaded.bg) > 0.85, `reloaded background ${reloaded.bg}`);

    // 6. late sheets and copies
    const late = await page.evaluate(async () => {
      const div = document.createElement("div");
      div.id = "late-probe";
      document.body.append(div);
      const style = document.createElement("style");
      style.textContent = "#late-probe { background-color: #14161a; }";
      document.head.append(style);
      await new Promise((r) => setTimeout(r, 0));
      const copy = document.createElement("style");
      copy.dataset.themeCopy = "";
      copy.textContent = "#late-probe { color: #d8dbe0; }";
      document.head.append(copy);
      await new Promise((r) => setTimeout(r, 0));
      const out = {
        lateBg: getComputedStyle(div).backgroundColor,
        copyColor: getComputedStyle(div).color,
      };
      window.claunchTheme.set("dark");
      out.copyGone = !copy.isConnected;
      out.lateBgDark = getComputedStyle(div).backgroundColor;
      return out;
    });
    assert.ok(lum(late.lateBg) > 0.85, `late sheet not themed: ${late.lateBg}`);
    assert.equal(late.copyColor, "rgb(216, 219, 224)", "a data-theme-copy sheet was rewritten");
    assert.equal(late.copyGone, true, "the copy survived a switch");
    assert.equal(late.lateBgDark, "rgb(20, 22, 26)");

    // 7. another tab's switch is followed
    const other = await context.newPage();
    await other.goto(base);
    assert.equal(await other.evaluate(() => document.documentElement.dataset.theme), "dark");
    await page.evaluate(() => window.claunchTheme.set("light"));
    await other.waitForFunction(() => document.documentElement.dataset.theme === "light", null, { timeout: 5000 });

    const themeErrors = errors.filter((e) => /theme/i.test(e));
    assert.deepEqual(themeErrors, [], `theme errors: ${themeErrors.join("; ")}`);
    console.log("theme_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
