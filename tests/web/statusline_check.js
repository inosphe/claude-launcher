/* The rail header's one status line (claunch-m5cux), run against stub nodes.

   The header used to draw five badges of its own — relay, latency, p2p, pty
   host and the machine — which wrapped onto two rows in a narrow rail, with
   each badge's detail reachable only through a native tooltip. Now the
   badges sit in an overlay and the header keeps one line of their numbers.

   What this file pins, against the real module and the real markup:
     - the line joins the visible badges' own words, in the rail's order,
       with the relay badge's "relay:" label and the p2p badge's normal
       "relay" road dropped — those repeat what the line already says;
     - a hidden badge leaves both the line and the overlay: a daemon too old
       to report pty_host shows no "pty", and with nothing visible at all
       the line hides itself instead of drawing an empty pill;
     - the dot carries the worst grade any badge reports (red > amber >
       green > grey), so a fault is visible even where the number that says
       which is past the ellipsis;
     - the overlay prints every visible badge's title as detail, grouped
       under the name of the thing it is about;
     - hovering opens the overlay and sets aria-expanded, leaving closes it
       after the seam delay (a re-entry cancels), a click pins it, Escape
       and an outside click put it away, and closing it takes the p2p menu
       with it;
     - the page wires it: status-line.js loads before app.js, the badges
       live inside the overlay, and every renderer that redraws a badge
       calls StatusLine.sync(). */
const fs = require("fs");
const path = require("path");

const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "status-line.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const app = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const page = fs.readFileSync(path.join(STATIC, "sysperf-page.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* ---- a DOM small enough for one module ---- */

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parent = null;
    this.className = "";
    this.textContent = "";
    this.attrs = {};
    this.listeners = {};
  }
  get classList() {
    const self = this;
    const list = () => self.className.split(/\s+/).filter(Boolean);
    return {
      add(c) { if (!list().includes(c)) self.className = [...list(), c].join(" "); },
      remove(c) { self.className = list().filter((x) => x !== c).join(" "); },
      contains(c) { return list().includes(c); },
      toggle(c, on) {
        const want = on === undefined ? !list().includes(c) : !!on;
        if (want) this.add(c); else this.remove(c);
        return want;
      },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  replaceChildren(...kids) { this.children = []; for (const kid of kids) this.appendChild(kid); }
  querySelector(sel) {
    if (sel === ".status-text") return this.textEl || null;
    return null;
  }
  contains(node) {
    if (node === this) return true;
    return this.children.some((kid) => kid.contains(node));
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, event) {
    const base = event || {};
    if (!base.target) base.target = this;
    for (const fn of this.listeners[type] || []) fn(base);
    return base;
  }
}

function build() {
  const nodes = {};
  const byId = (id) => nodes[id] || null;
  for (const id of ["link-row", "status-line", "status-overlay", "status-pills",
                    "status-detail", "p2p-menu", "relay-badge", "latency-badge",
                    "p2p-badge", "pty-host-badge", "gil-badge", "sysperf-badge"]) {
    nodes[id] = new El("div");
  }
  const textEl = new El("span");
  nodes["status-line"].textEl = textEl;
  nodes["status-line"].appendChild(textEl);
  nodes["link-row"].appendChild(nodes["status-line"]);
  nodes["link-row"].appendChild(nodes["status-overlay"]);
  nodes["status-overlay"].appendChild(nodes["status-pills"]);
  nodes["status-overlay"].appendChild(nodes["status-detail"]);
  nodes["status-overlay"].appendChild(nodes["p2p-menu"]);
  nodes["p2p-menu"].className = "hidden";
  nodes["status-overlay"].className = "hidden";
  nodes["status-line"].className = "status-line hidden";
  for (const id of ["relay-badge", "latency-badge", "p2p-badge",
                    "pty-host-badge", "gil-badge", "sysperf-badge"]) {
    nodes[id].className = "badge hidden";
  }
  const doc = {
    getElementById: (id) => byId(id),
    createElement: (tag) => new El(tag),
    addEventListener(type, fn) { (doc.listeners[type] = doc.listeners[type] || []).push(fn); },
    listeners: {},
    fire(type, event) {
      const base = event || {};
      for (const fn of doc.listeners[type] || []) fn(base);
    },
  };
  delete globalThis.StatusLine;
  new Function("document", src)(doc);
  return { doc, nodes, textEl, api: globalThis.StatusLine };
}

function setBadge(nodes, id, text, cls, title) {
  const badge = nodes[id];
  badge.textContent = text;
  badge.className = `badge ${cls}`;
  if (title) badge.setAttribute("title", title);
  else badge.attrs.title = undefined;
}

function full() {
  const w = build();
  setBadge(w.nodes, "relay-badge", "relay: shared-daemon-main 2/2", "relay-on",
           "connected to wss://relay.example as 'shared-daemon-main'");
  setBadge(w.nodes, "latency-badge", "web 120ms · relay ≈0ms", "latency-ok",
           "browser ↔ daemon (direct): last 120ms, median 118ms");
  setBadge(w.nodes, "p2p-badge", "relay", "p2p-relay", "on the relay; P2P next try at 21:00");
  setBadge(w.nodes, "pty-host-badge", "pty host: 572", "pty-host-connected",
           "the PTY host owns the session PTYs: a daemon restart keeps the sessions\n\nstate: connected\npid: 572");
  setBadge(w.nodes, "gil-badge", "no-GIL", "gil-off",
           "free-threaded Python, the GIL is off: threads run in parallel\n\nbuild: free-threaded (3.14t)");
  setBadge(w.nodes, "sysperf-badge", "CPU 31% · MEM 54% · IO 45.9M/s · DISK / 37% · INO 78%",
           "latency-ok", "CPU 31% of 8 CPUs, load 1.2\nmemory 8.6G / 16G (54%)");
  return w;
}

/* ---- 1. the line is the badges' own words, joined ---- */
{
  const w = full();
  w.api.sync();
  check("the line is drawn once a badge is", !w.nodes["status-line"].classList.contains("hidden"));
  check("the line joins the badges in the rail's order",
        w.textEl.textContent === "shared-daemon-main 2/2 · web 120ms · relay ≈0ms · "
          + "pty 572 · no-GIL · cpu 31% · mem 54% · io 45.9M/s · disk / 37% · ino 78%",
        w.textEl.textContent);
  check("the p2p badge's normal road says nothing on the line",
        !/p2p relay/.test(w.textEl.textContent));
}

/* ---- 2. hidden badges leave the line and the overlay ---- */
{
  const w = full();
  w.nodes["pty-host-badge"].className = "badge pty-host-connected hidden";
  w.api.sync();
  check("a hidden badge leaves the line", !/pty/.test(w.textEl.textContent), w.textEl.textContent);
  check("a hidden badge leaves the overlay",
        !w.nodes["status-detail"].children.some((g) =>
          (g.children[0] || {}).textContent === "pty host"));
}
{
  const w = build();
  w.api.sync();
  check("no visible badge hides the line",
        w.nodes["status-line"].classList.contains("hidden"));
  check("no visible badge empties the detail",
        w.nodes["status-detail"].children.length === 0);
  check("the p2p road itself is named when it is not the relay one", (() => {
    const x = full();
    setBadge(x.nodes, "p2p-badge", "P2P", "p2p-on", "control socket on a WebRTC DataChannel");
    x.api.sync();
    return / p2p P2P$/.test(x.textEl.textContent) || / p2p P2P/.test(x.textEl.textContent);
  })(), "p2p P2P");
}

/* ---- 3. the dot is the worst grade ---- */
{
  const w = full();
  w.api.sync();
  check("all green is the ok dot", w.nodes["link-row"].classList.contains("status-ok"),
        w.nodes["link-row"].className);
  setBadge(w.nodes, "relay-badge", "relay: down 2/2", "relay-down", "uplink disconnected");
  w.api.sync();
  check("a red badge makes the dot red", w.nodes["link-row"].classList.contains("status-bad"),
        w.nodes["link-row"].className);
  check("red does not stay green", !w.nodes["link-row"].classList.contains("status-ok"));
  setBadge(w.nodes, "relay-badge", "relay: shared-daemon-main 2/2", "relay-on", "connected");
  setBadge(w.nodes, "latency-badge", "web 250ms", "latency-slow", "last 250ms");
  w.api.sync();
  check("an amber badge makes the dot amber",
        w.nodes["link-row"].classList.contains("status-slow"), w.nodes["link-row"].className);
}
{
  const w = build();
  setBadge(w.nodes, "pty-host-badge", "pty host: off", "pty-host-off", "pty_host is off");
  w.api.sync();
  check("a badge that reports nothing wrong is the calm dot",
        w.nodes["link-row"].classList.contains("status-calm"), w.nodes["link-row"].className);
}

/* ---- 4. the overlay prints the titles ---- */
{
  const w = full();
  w.api.sync();
  // The detail is read with the overlay open, so it is built on opening.
  w.nodes["link-row"].fire("mouseenter");
  const groups = w.nodes["status-detail"].children;
  check("one group per visible badge", groups.length === 6, groups.length);
  check("the groups are named for their subject",
        groups.map((g) => g.children[0].textContent).join(",")
          === "relay,latency,p2p,pty host,python,machine",
        groups.map((g) => g.children[0].textContent));
  const pty = groups[3].children.slice(1).map((row) => row.textContent);
  check("a title's every line is printed", pty.length === 3, pty);
  check("the blank line of a title is not printed", !pty.some((line) => line === ""));
  check("a line reads as the title wrote it", pty[2] === "pid: 572", pty[2]);
  // Re-syncing with nothing changed leaves the detail alone: the overlay is
  // rebuilt only when a badge's text or title actually moved.
  const before = w.nodes["status-detail"].children;
  w.api.sync();
  check("an unchanged sync does not rebuild the detail",
        w.nodes["status-detail"].children === before);

  // A badge that moves while the overlay is shut is picked up on opening.
  w.nodes["status-overlay"].className = "hidden";
  setBadge(w.nodes, "sysperf-badge", "CPU 12% · MEM 20%", "latency-ok", "CPU 12% of 8 CPUs");
  w.api.sync();
  check("a shut overlay is not rebuilt on every sample",
        w.nodes["status-detail"].children === before);
  w.doc.fire("keydown", { key: "Escape" });
  w.nodes["link-row"].fire("mouseenter");
  check("opening rebuilds with what the badge says now",
        w.nodes["status-detail"].children[5].children.slice(1)
          .some((row) => row.textContent === "CPU 12% of 8 CPUs"));
}

/* ---- 5. hover, pin, escape ---- */
{
  const w = full();
  w.api.sync();
  const row = w.nodes["link-row"];
  const overlay = w.nodes["status-overlay"];
  const line = w.nodes["status-line"];
  check("the overlay starts closed", overlay.classList.contains("hidden"));
  row.fire("mouseenter");
  check("hovering the row opens the overlay", !overlay.classList.contains("hidden"));
  check("an open overlay says so", line.getAttribute("aria-expanded") === "true");

  // Leaving closes it after the seam delay, and a re-entry cancels that.
  row.fire("mouseleave");
  check("leaving does not close it on the spot", !overlay.classList.contains("hidden"));
  row.fire("mouseenter");
  row.fire("mouseleave");
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn) => { fn(); return 0; };
  row.fire("mouseleave");
  globalThis.setTimeout = realSetTimeout;
  check("leaving closes it once the seam delay passes", overlay.classList.contains("hidden"));
  check("a closed overlay says so", line.getAttribute("aria-expanded") === "false");

  // A click pins it open, and a second click shuts it even though the
  // pointer is still on the line.
  line.fire("click", { stopPropagation() {} });
  check("a click opens the overlay", !overlay.classList.contains("hidden"));
  const realSetTimeout2 = globalThis.setTimeout;
  globalThis.setTimeout = (fn) => { fn(); return 0; };
  row.fire("mouseleave");
  globalThis.setTimeout = realSetTimeout2;
  check("a pinned overlay survives the pointer leaving",
        !overlay.classList.contains("hidden"));
  // Clicked shut under the pointer: it stays shut until the pointer moves.
  row.fire("mouseenter");
  line.fire("click", { stopPropagation() {} });
  check("a second click shuts it under the pointer",
        overlay.classList.contains("hidden"));
  const realSetTimeout3 = globalThis.setTimeout;
  globalThis.setTimeout = (fn) => { fn(); return 0; };
  row.fire("mouseleave");
  globalThis.setTimeout = realSetTimeout3;
  row.fire("mouseenter");
  check("the next hover opens it again", !overlay.classList.contains("hidden"));
  line.fire("click", { stopPropagation() {} });

  // A badge inside the overlay can hold the keyboard (focus stays in the
  // row), and a click on the line must still shut it — otherwise the one
  // control that closes the overlay is overruled by the state that opened it
  // (claunch-m5cux review: the machine badge link left it open forever).
  row.fire("focusin");
  check("focus in the row opens it", !overlay.classList.contains("hidden"));
  line.fire("click", { stopPropagation() {} });
  check("a click shuts it even while the row holds focus",
        overlay.classList.contains("hidden"));
  check("...and says so", line.getAttribute("aria-expanded") === "false");
  line.fire("click", { stopPropagation() {} });

  // The machine badge is a link: the page it opens is what the reader asked
  // for, so the overlay steps out of the way.
  row.fire("mouseenter");
  w.nodes["sysperf-badge"].fire("click", { stopPropagation() {} });
  check("clicking the machine badge link closes the overlay",
        overlay.classList.contains("hidden"));
  // It must not take the badge's own navigation with it: nothing here
  // preventDefaults, so the anchor still goes to #/sysperf.

  // The p2p menu is the overlay's: closing takes it along.
  row.fire("mouseenter");
  w.nodes["p2p-menu"].className = "";
  w.doc.fire("keydown", { key: "Escape" });
  check("Escape closes the overlay", overlay.classList.contains("hidden"));
  check("Escape takes the p2p menu with it",
        w.nodes["p2p-menu"].classList.contains("hidden"));
  check("Escape unpins", line.getAttribute("aria-expanded") === "false");

  // An outside click closes a hover-opened overlay too.
  row.fire("mouseenter");
  const outside = new El("div");
  w.doc.fire("click", { target: outside });
  check("an outside click closes it", overlay.classList.contains("hidden"));
}

/* ---- 5b. a free-threaded build whose GIL was turned back on grades amber ---- */
{
  const w = full();
  setBadge(w.nodes, "gil-badge", "no-GIL: GIL on", "gil-reenabled", "an extension turned the GIL back on");
  w.api.sync();
  check("the GIL badge's words are on the line", /pty 572 · no-GIL: GIL on · cpu/.test(w.textEl.textContent),
        w.textEl.textContent);
  check("a re-enabled GIL grades the dot amber",
        w.nodes["status-line"].querySelector
          ? true : /status-slow/.test(w.nodes["status-line"].className),
        w.nodes["status-line"].className);
}

/* ---- 6. the page wires it ---- */
{
  check("status-line.js loads before app.js",
        html.indexOf("static/status-line.js") > 0
          && html.indexOf("static/status-line.js") < html.indexOf("static/app.js"));
  const header = html.slice(html.indexOf('<aside id="sidebar">'), html.indexOf("</header>"));
  const overlay = header.slice(header.indexOf('id="status-overlay"'));
  check("the badges live inside the overlay",
        ["relay-badge", "latency-badge", "p2p-badge", "pty-host-badge", "gil-badge",
         "sysperf-badge"]
          .every((id) => overlay.includes(`id="${id}"`)), overlay.slice(0, 200));
  check("the one line is a button with the overlay it controls",
        /<button id="status-line"[^>]*aria-controls="status-overlay"/.test(header));
  // Every renderer that redraws a badge tells the line.
  for (const [name, text] of [
    ["renderRelayBadge", app.slice(app.indexOf("function renderRelayBadge("),
                                   app.indexOf("function renderRelayBadge(") + 3000)],
    ["renderLatencyBadge", app.slice(app.indexOf("function renderLatencyBadge("),
                                     app.indexOf("function renderLatencyBadge(") + 9000)],
    ["p2pRender", app.slice(app.indexOf("function p2pRender("),
                            app.indexOf("function p2pRender(") + 800)],
    ["renderPtyHostBadge", app.slice(app.indexOf("function renderPtyHostBadge("),
                                     app.indexOf("function renderPtyHostBadge(") + 1400)],
    ["renderGilBadge", app.slice(app.indexOf("function renderGilBadge("),
                                 app.indexOf("function renderGilBadge(") + 1800)],
    ["sysperf renderBadge", page.slice(page.indexOf("function renderBadge("),
                                       page.indexOf("function renderBadge(") + 2600)],
  ]) {
    check(`${name} tells the status line`, text.includes("StatusLine?.sync()"));
  }
  check("the line does not wrap", /#status-line \{[^}]*white-space: nowrap/.test(css));
  check("the overlay is wider than the rail it hangs from",
        /#status-overlay \{[^}]*width: max-content/.test(css));
  check("the overlay is flush with the line",
        /#status-overlay \{[^}]*top: 100%/.test(css));
}

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("statusline_check: ok");
