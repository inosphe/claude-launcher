/* The sidebar PTY host badge (claunch-xnnct.3), run against stub nodes.

   GET /api/daemon carries pty_host: state off / connected / stale /
   unreachable, the host's pid, proto and code id, its start time and the
   daemon's clock at the read. boot() hands it to renderPtyHostBadge. What is
   checked: each state's text and class; the title lists pid, proto, code,
   state and sessions; the uptime is computed when the pointer enters the
   badge, with the browser-daemon clock offset taken out; and a daemon older
   than the field leaves the badge hidden. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

function build() {
  const badge = {
    className: "badge hidden", textContent: "", title: "", dataset: {}, on: {},
    classList: {
      add(c) { if (!badge.className.split(" ").includes(c)) badge.className += ` ${c}`; },
      remove(c) { badge.className = badge.className.split(" ").filter((x) => x !== c).join(" "); },
      contains(c) { return badge.className.split(" ").includes(c); },
    },
    addEventListener(ev, fn) { (badge.on[ev] = badge.on[ev] || []).push(fn); },
    enter() { (badge.on.mouseenter || []).forEach((fn) => fn()); },
  };
  const clock = { ms: 1700000000000 };
  const FakeDate = { now: () => clock.ms };
  const code = slice("/* The PTY host badge (claunch-xnnct.3)", "async function refreshMeshList(");
  const api = new Function("$", "Date", code + "\nreturn { renderPtyHostBadge };")(
    (id) => (id === "pty-host-badge" ? badge : null), FakeDate);
  return { api, badge, clock };
}

const at = 1700000000;   // the daemon's clock at the read, epoch seconds

{
  const w = build();
  w.api.renderPtyHostBadge({ configured: false, state: "off", pid: null, proto: null,
                             code: null, daemon_code: "p2-aaaa", stale: false,
                             started_at: null, sessions: 0, now: at });
  check("off is shown, not hidden", !w.badge.classList.contains("hidden"), w.badge.className);
  check("off reads as off", w.badge.textContent === "pty host: off", w.badge.textContent);
  check("off is the grey class", w.badge.classList.contains("pty-host-off"), w.badge.className);
  check("off's title says the daemon owns the PTYs",
        /daemon owns the session PTYs/.test(w.badge.title) && /state: off/.test(w.badge.title),
        w.badge.title);
  check("off has no uptime", !/uptime/.test(w.badge.title), w.badge.title);
}

{
  const w = build();
  w.api.renderPtyHostBadge({ configured: true, state: "connected", pid: 4242, proto: 2,
                             code: "p2-1234abcd", daemon_code: "p2-1234abcd", stale: false,
                             started_at: at - 3700, sessions: 3, now: at });
  check("connected names the pid", w.badge.textContent === "pty host: 4242", w.badge.textContent);
  check("connected is the green class", w.badge.classList.contains("pty-host-connected"),
        w.badge.className);
  for (const want of ["pid: 4242", "proto: 2", "code: p2-1234abcd", "state: connected",
                      "sessions: 3", "uptime: 1h 1m"]) {
    check(`connected title has ${want}`, w.badge.title.includes(want), w.badge.title);
  }
  // The uptime is computed at hover: two hours later it reads two hours more.
  w.clock.ms += 2 * 3600 * 1000;
  w.badge.enter();
  check("the hover recomputes the uptime", w.badge.title.includes("uptime: 3h 1m"), w.badge.title);
  w.api.renderPtyHostBadge({ configured: true, state: "connected", pid: 4242, proto: 2,
                             code: "p2-1234abcd", daemon_code: "p2-1234abcd", stale: false,
                             started_at: at - 3700, sessions: 3, now: at });
  w.badge.enter();
  check("one hover listener, however often it is drawn",
        (w.badge.on.mouseenter || []).length === 1, (w.badge.on.mouseenter || []).length);
}

{
  // A browser whose clock is ten minutes ahead of the daemon's.
  const w = build();
  w.clock.ms = (at + 600) * 1000;
  w.api.renderPtyHostBadge({ configured: true, state: "connected", pid: 1, proto: 2,
                             code: "p2-x", daemon_code: "p2-x", stale: false,
                             started_at: at - 120, sessions: 0, now: at });
  check("the clock offset is taken out of the uptime", w.badge.title.includes("uptime: 2m"),
        w.badge.title);
}

{
  const w = build();
  w.api.renderPtyHostBadge({ configured: true, state: "stale", pid: 99, proto: 2,
                             code: null, daemon_code: "p2-new", stale: true,
                             started_at: at - 5, sessions: 1, now: at });
  check("stale reads as stale", w.badge.textContent === "pty host: stale", w.badge.textContent);
  check("stale is the yellow class", w.badge.classList.contains("pty-host-stale"), w.badge.className);
  check("stale's title names the fix and both codes",
        /--with-pty-host/.test(w.badge.title) && /code: \(none reported\)/.test(w.badge.title)
          && /daemon code: p2-new/.test(w.badge.title), w.badge.title);
}

{
  const w = build();
  w.api.renderPtyHostBadge({ configured: true, state: "unreachable", pid: 55, proto: null,
                             code: null, daemon_code: "p2-x", stale: false,
                             started_at: null, sessions: 0, now: at });
  check("unreachable reads as unreachable", w.badge.textContent === "pty host: unreachable",
        w.badge.textContent);
  check("unreachable is the red class", w.badge.classList.contains("pty-host-unreachable"),
        w.badge.className);
  check("unreachable says sessions spawn in the daemon",
        /spawn in the daemon/.test(w.badge.title) && /pid: 55/.test(w.badge.title), w.badge.title);
}

{
  const w = build();
  w.api.renderPtyHostBadge(undefined);
  check("a daemon older than the field leaves the badge hidden",
        w.badge.classList.contains("hidden"), w.badge.className);
}

check("the badge sits in the link row", /<div id="link-row">[\s\S]*id="pty-host-badge"[\s\S]*<\/div>/.test(html));
check("boot draws it", /renderPtyHostBadge\(info\.pty_host\)/.test(src));
for (const k of ["off", "connected", "stale", "unreachable"]) {
  check(`a ${k} colour exists`, css.includes(`.badge.pty-host-${k}`));
}

process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log("all pty-host-badge checks passed");
});
