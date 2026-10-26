/* Painted compact-card identities at the default 260px and a wider rail.
   Both harness suffixes must be fully visible even on a depth-four child.
   STATIC_ROOT lets a scratch stylesheet mutation exercise this assertion. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");

const root = process.env.STATIC_ROOT || path.join(__dirname, "../../src/claude_launcher/web/static");

/* Checks include asynchronous refresh states, hidden on compact cards. */
const fixtureChecks = [
  { id: "tests", name: "Tests", question: "Did the targeted tests pass?",
    answer: "yes", reported_at: "2026-10-26T03:00:00Z", source: "agent" },
  { id: "tmpl", name: "Template", question: "Was the template followed?",
    answer: "no", reported_at: "2026-10-26T03:00:00Z", source: "agent" },
];

const sessions = [
  {name:"p0", status:"idle", profile:"nc", harness:"claude"},
  {name:"p1", status:"idle", profile:"nc", harness:"claude",parent:"p0"},
  {name:"p2", status:"idle", profile:"nc", harness:"claude",parent:"p1"},
  {name:"p3", status:"idle", profile:"nc", harness:"claude",parent:"p2"},
  ...["claude", "codex"].map(harness=>({name:harness, status:"idle", role:"worker",
    profile:"opencode", harness, parent:"p3", cwd:"/repo", status_checks:fixtureChecks,
    briefing:{one_line:"Keep the full harness identity visible",state:"idle"}})),
];

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname === "/boot.html") {
    res.setHeader("Content-Type", "text/html");
    return res.end(`<!doctype html><script>
      localStorage.setItem("claunch_token:/", "fixture-token");
      location.replace("/");
    </script>`);
  }
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "card" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: true };
    else if (url.pathname.endsWith("/capture")) data = { lines: [] };
    else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html"
    : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript"
      : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});


(async () => {
 await safeListen(server);
 const browser=await chromium.launch({headless:true});
 try {
  const errors = [];
  for (const theme of ["dark", "light"]) for (const width of [260, 440]) {
   const page=await browser.newPage({viewport:{width:1400,height:1200}});
   page.on("pageerror", (e) => errors.push(String(e)));
   await page.addInitScript(({width, theme}) => {
     localStorage.setItem("claunch_railw:/", String(width));
     localStorage.setItem("claunch.theme", theme);
   }, {width, theme});
   await page.goto(`http://127.0.0.1:${server.address().port}/boot.html`);
   await page.locator('#session-list li[data-name="codex"]').waitFor();
   const measured=await page.evaluate(()=>["claude","codex"].map(name=>{
    const li=document.querySelector(`#session-list li[data-name="${name}"]`);
    const meta=li.querySelector(".meta");
    const b=meta.getBoundingClientRect();
    const rb=li.getBoundingClientRect();
    const refreshes = [...li.querySelectorAll(".sess-brief-rowref,.sess-status-check-rowref")];
    const position = () => { const r=meta.getBoundingClientRect(); return [r.x,r.y,r.width,r.height]; };
    const positions = [position()];
    for (const text of ["checking…", "checks · waiting", "checks ✓", "checks ⟳"]) {
      refreshes.forEach(e => { e.textContent=text; e.classList.add("requesting"); });
      positions.push(position());
    }
    const hidden = refreshes.every(e => e.getBoundingClientRect().width===0);
    return {name,text:meta.textContent,width:b.width,needed:meta.scrollWidth,available:meta.clientWidth,
      contained:b.width > 0 && b.height > 0 && b.left>=rb.left && b.right<=rb.right,
      hidden,positions,mode:li.dataset.railMode,depth:li.dataset.railDepth};
   }));
   console.log(JSON.stringify({width, theme, measured}));
   for (const m of measured) {
     assert.equal(m.mode, "compact");
     assert.equal(m.depth, "4");
     assert.equal(m.text, `opencode/${m.name}`);
     assert.ok(m.contained, "the identity is painted inside its card");
     assert.ok(m.hidden, "compact refresh controls have no painted box");
     for (const pos of m.positions) assert.deepEqual(pos,m.positions[0],
       "the profile box stays fixed throughout refresh-label changes");
     assert.ok(m.available >= m.needed,
       `${theme} ${width}px ${m.text}: ${m.available}px clips ${m.needed}px`);
   }
   assert.notEqual(measured[0].text,measured[1].text);
   await page.close();
  }
  assert.deepEqual(errors, []);
  console.log("railmeta_browser: ok");
 }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
