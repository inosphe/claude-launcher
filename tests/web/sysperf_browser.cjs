/* The rail header's CPU / MEM / IO badge and the System performance page
   (sysperf-page.js, claunch-zuk0b).

   Serves the real index.html against a fixture whose only API answer is
   /api/sysperf, and checks in a real browser:
     1. the badge sits in the sidebar header, shows the latest sample's
        numbers, is graded by its busiest number and links to #/sysperf;
     2. the badge polls the latest sample only (no ?history);
     3. clicking it opens the page, which reads the history once and draws
        three line charts (CPU, Memory, Disk I/O with a read/write legend),
        the detail table, the per-drive capacity table and the log location;
     4. hovering a chart shows one tooltip listing every series;
     5. leaving the page drops it: the view is hidden again.
   Set SYSPERF_SHOT=<path> to save a screenshot of the page. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const now = Math.floor(Date.now() / 1000);
// Two drives, as claunch-frgt1 reports them: the daemon root's filesystem
// and a workspace on another mount, the latter nearly full.
// The first drive is the WSL shape claunch-frgt1.1 exists for: bytes are
// fine, inodes are nearly gone. The second reports no inode table at all,
// as WSL's drvfs and Windows do.
const usage = [
  { mount: "/", label: "/", total: 1000 * 2 ** 30, used: 450 * 2 ** 30, free: 550 * 2 ** 30, pct: 45,
    inodes: { total: 67108864, used: 66871820, free: 237044, pct: 100 } },
  { mount: "/mnt/d", label: "/mnt/d", total: 2000 * 2 ** 30, used: 1900 * 2 ** 30, free: 100 * 2 ** 30, pct: 95,
    inodes: null },
];
const history = Array.from({ length: 60 }, (_, i) => ({
  ts: now - (59 - i) * 5, interval_s: 5, source: "procfs", cpus: 8,
  cpu_pct: 20 + 15 * Math.sin(i / 6), mem_total: 16 * 2 ** 30,
  mem_used: 9 * 2 ** 30, mem_pct: 56.3, load1: 1.5,
  disk_read_bps: 1024 * 1024 * (1 + Math.cos(i / 5)), disk_write_bps: 300 * 1024,
  disk_busy_pct: 12, disks: { sda: { read_bps: 1048576, write_bps: 307200, busy_pct: 12 } },
  usage,
}));
const latest = { ...history[history.length - 1], cpu_pct: 93.4 };
const requests = [];

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/sysperf") {
      requests.push(url.search);
      const body = { enabled: true, interval_s: 5, error: null, latest };
      if (url.searchParams.get("history")) {
        Object.assign(body, {
          history, history_max: 720,
          log: { path: "/home/u/.claude-launcher/daemon/logs/sysperf.log", max_bytes: 2097152, backups: 5 },
        });
      }
      return res.end(JSON.stringify(body));
    }
    res.statusCode = 404;
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

(async () => {
  await safeListen(server);
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(base);

    // 1. the badge in the header
    const badge = page.locator("#sidebar > header #sysperf-badge");
    await badge.waitFor({ state: "visible" });
    await page.waitForFunction(() => /CPU 93%/.test(document.getElementById("sysperf-badge").textContent));
    const text = await badge.textContent();
    // The fullest drive is /mnt/d by bytes (95 %); "/" is fuller by inodes
    // (100 %) and is what the badge names. Both readings are shown.
    assert.match(text, /^CPU 93% · MEM 56% · IO [\d.]+M\/s · DISK \/ 45% · INO 100% \+1$/);
    assert.match(await badge.getAttribute("title"), /disk \/ \(\/\): /);
    assert.match(await badge.getAttribute("title"), /inodes \/ \(\/\): 66,871,820 used of 67,108,864, 237,044 free \(100%\)/);
    assert.match(await badge.getAttribute("title"), /disk \/mnt\/d \(\/mnt\/d\): .* \(95%\)/);
    assert.equal(await badge.getAttribute("href"), "#/sysperf");
    assert.match(await badge.getAttribute("class"), /latency-bad/, "graded by its busiest number");
    assert.match(await badge.getAttribute("title"), /of 8 CPUs/);
    // 2. the header poll asks for the latest sample only
    assert.equal(requests[0], "", "the badge's first read carries no ?history");

    // 3. the page
    await badge.click();
    await page.waitForFunction(() => location.hash === "#/sysperf");
    await page.waitForSelector("#sysperf-view svg.sysperf-chart");
    assert.ok(requests.includes("?history=1"), "the page reads the history");
    const view = await page.evaluate(() => {
      const v = document.getElementById("sysperf-view");
      return {
        hidden: v.classList.contains("hidden"),
        titles: [...v.querySelectorAll(".sysperf-card h3")].map((h) => h.textContent),
        charts: v.querySelectorAll("svg.sysperf-chart").length,
        lines: [...v.querySelectorAll("path.sysperf-line")].map((p) => p.getAttribute("class")),
        legend: [...v.querySelectorAll(".sysperf-legend-item")].map((e) => e.textContent),
        table: v.querySelector(".sysperf-table").textContent,
        capacity: v.querySelector(".sysperf-usage")?.textContent ?? "",
        full: [...v.querySelectorAll(".sysperf-usage td")].filter((td) => td.className === "s-full").length,
        log: v.textContent.includes("sysperf.log"),
        wide: document.documentElement.scrollWidth <= window.innerWidth,
      };
    });
    assert.equal(view.hidden, false);
    assert.deepEqual(view.titles, ["CPU", "Memory", "Disk I/O", "Now", "Disk capacity", "Log"]);
    assert.equal(view.charts, 3);
    assert.equal(view.lines.length, 4, "CPU, memory, disk read and disk write");
    assert.deepEqual(view.legend, ["Read", "Write"]);
    assert.match(view.table, /of 8 logical CPUs/);
    assert.match(view.capacity, /\/mnt\/d/);
    assert.match(view.capacity, /95%/);
    assert.match(view.capacity, /45%/);
    assert.match(view.capacity, /66,871,820 \/ 67,108,864/, "the inode count itself is shown");
    assert.equal(view.full, 2, "the 95% drive by bytes and the 100% one by inodes");
    assert.ok(view.log, "the page says where the log is");
    assert.ok(view.wide, "no horizontal page scroll");

    // 4. hover
    const chart = page.locator("#sysperf-view .sysperf-card").nth(2).locator("rect.sysperf-hit");
    const box = await chart.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    const tip = page.locator("#sysperf-view .sysperf-card").nth(2).locator(".sysperf-tip");
    await tip.waitFor({ state: "visible" });
    const rows = await tip.locator(".sysperf-tip-row span").allTextContents();
    assert.deepEqual(rows, ["Read", "Write"]);
    if (process.env.SYSPERF_SHOT) await page.screenshot({ path: process.env.SYSPERF_SHOT, fullPage: true });

    // 5. leave
    await page.evaluate(() => { location.hash = "#/"; });
    await page.waitForFunction(() => document.getElementById("sysperf-view").classList.contains("hidden"));
    assert.deepEqual(errors.filter((e) => /sysperf|SysPerf/.test(e)), []);
  } finally {
    await browser.close();
    server.close();
  }
})().catch((err) => { console.error(err); process.exit(1); });
