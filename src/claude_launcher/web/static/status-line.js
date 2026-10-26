/* The rail header's one status line (claunch-m5cux).

   The header carried five badges of its own — relay, the two round trips,
   the P2P road, the PTY host and the machine — each with its detail in a
   native `title`. On a rail narrower than its widest moment they wrapped
   onto two rows, and the detail behind a native tooltip is a pointer-only,
   one-second-late reader for the numbers the badges exist to show.

   The badges keep their rendering, their colours and their titles; what
   changed is where they sit. The header shows one line of their numbers,
   and the badges live in an overlay behind it: hovering the line (or
   focusing it, or clicking it, where there is no pointer) opens the
   overlay, which prints every visible badge's text and title as detail.

   This file owns no badge: the renderers still draw them, then call
   `globalThis.StatusLine.sync()`. Everything here is read off the DOM, so
   a badge that hides itself (a daemon too old to report pty_host, say)
   simply stops appearing on both the line and the overlay. */
(() => {
  "use strict";

  const BADGES = [
    "relay-badge", "latency-badge", "p2p-badge", "pty-host-badge", "gil-badge",
    "sysperf-badge",
  ];
  // What each group of detail is about, in the overlay's own words.
  const NAME = {
    "relay-badge": "relay",
    "latency-badge": "latency",
    "p2p-badge": "p2p",
    "pty-host-badge": "pty host",
    "gil-badge": "python",
    "sysperf-badge": "machine",
  };
  // The class each badge grades itself with, and what that grade is worth
  // when the line takes the worst of them for its dot.
  const GRADES = [
    ["latency-bad", 3], ["relay-down", 3], ["pty-host-unreachable", 3],
    ["latency-slow", 2], ["relay-partial", 2], ["pty-host-stale", 2],
    ["p2p-connecting", 2], ["gil-reenabled", 2],
    ["latency-ok", 1], ["relay-on", 1], ["pty-host-connected", 1], ["p2p-on", 1],
  ];
  const DOTS = ["status-calm", "status-ok", "status-slow", "status-bad"];

  const $ = (id) => document.getElementById(id);
  const shown = (badge) => !!(badge && !badge.classList.contains("hidden"));

  let hovering = false;        // the pointer is on the line or the overlay
  let focused = false;         // the line or the overlay has the keyboard
  let pinned = false;          // clicked open: a pointer leaving keeps it
  let closeTimer = null;       // the seam between the line and the overlay
  let detailSignature = "";    // what the overlay's detail was built from
  let detailStale = true;      // a badge moved while the overlay was shut
  let bound = false;

  function severity(badge) {
    for (const [cls, weight] of GRADES) {
      if (badge.classList.contains(cls)) return weight;
    }
    return 0;
  }

  /* The pill's own words, minus what one line makes redundant: the relay
     badge's "relay:" (the line has no room for a label it repeats), the "
     p2p relay" case (the normal road, which is the one thing on this row
     that says nothing), and the machine badge's upper case, which was sized
     for a pill of its own. */
  function shortText(id, badge) {
    const text = (badge.textContent || "").trim();
    if (id === "relay-badge") {
      const rest = text.replace(/^relay:\s*/, "");
      return /^(off|down)\b/.test(rest) ? `relay ${rest}` : rest;
    }
    if (id === "p2p-badge") return text === "relay" ? "" : `p2p ${text}`;
    if (id === "pty-host-badge") return text.replace(/^pty host:?\s*/, "pty ");
    if (id === "sysperf-badge") {
      return text.replace(/\b(CPU|MEM|IO|DISK|INO)\b/g, (word) => word.toLowerCase());
    }
    return text;
  }

  /* One badge's detail: its title, which is the text the pill used to keep
     behind the pointer, printed where it can be read. */
  function group(id, badge) {
    const box = document.createElement("div");
    box.className = "status-group";
    const name = document.createElement("div");
    name.className = "status-group-name";
    name.textContent = NAME[id] || id;
    box.appendChild(name);
    const lines = (badge.getAttribute("title") || badge.textContent || "")
      .split("\n").filter((line) => line.trim());
    for (const line of lines) {
      const row = document.createElement("div");
      row.className = "status-group-line";
      row.textContent = line;
      box.appendChild(row);
    }
    return box;
  }

  /* Print every visible badge's detail, in the rail's order. */
  function rebuildDetail(visible) {
    const detail = $("status-detail");
    if (!detail) return;
    detail.replaceChildren(...visible.map(([id, badge]) => group(id, badge)));
    detailStale = false;
  }

  function menuOpen() {
    const menu = $("p2p-menu");
    return !!(menu && !menu.classList.contains("hidden"));
  }

  function draw() {
    const overlay = $("status-overlay");
    const line = $("status-line");
    if (!overlay || !line) return;
    const show = hovering || focused || pinned || menuOpen();
    overlay.classList.toggle("hidden", !show);
    line.setAttribute("aria-expanded", show ? "true" : "false");
    if (show && detailStale) {
      const visible = [];
      for (const id of BADGES) {
        const badge = $(id);
        if (shown(badge)) visible.push([id, badge]);
      }
      rebuildDetail(visible);
    }
    if (!show) {
      // The P2P menu is the overlay's, so it goes down with it.
      const menu = $("p2p-menu");
      if (menu) menu.classList.add("hidden");
    }
  }

  function close() {
    hovering = false;
    focused = false;
    pinned = false;
    // The menu is the overlay's, so it goes first: draw() reads it to decide
    // whether anything still wants the overlay open.
    const menu = $("p2p-menu");
    if (menu) menu.classList.add("hidden");
    draw();
  }

  function bind() {
    if (bound) return;
    const row = $("link-row");
    const line = $("status-line");
    const overlay = $("status-overlay");
    if (!row || !line || !overlay) return;
    bound = true;
    const press = (which) => {
      clearTimeout(closeTimer);
      if (which === "focus") focused = true; else hovering = true;
      draw();
    };
    /* A pointer crossing the seam between the line and the overlay leaves
       the row for a few pixels; closing on the spot would make the detail
       unreachable, so the close waits a moment a re-entry cancels. */
    const release = (which) => {
      clearTimeout(closeTimer);
      if (pinned || menuOpen()) return;
      closeTimer = setTimeout(() => {
        if (which === "focus") focused = false; else hovering = false;
        draw();
      }, 120);
    };
    row.addEventListener("mouseenter", () => press("pointer"));
    row.addEventListener("mouseleave", () => release("pointer"));
    row.addEventListener("focusin", () => press("focus"));
    row.addEventListener("focusout", (e) => {
      if (!row.contains(e.relatedTarget)) release("focus");
    });
    // No pointer (touch, keyboard): the click is how the overlay opens, and
    // a second one puts it away. Unpinning clears the other two holds as
    // well — under the pointer `hovering` is still true, and a badge clicked
    // a moment ago may still hold the keyboard — so the second click means
    // closed rather than being overruled by the state that opened it. The
    // next hover (after the pointer leaves) opens it again.
    line.addEventListener("click", (e) => {
      e.stopPropagation();
      pinned = !pinned;
      if (!pinned) {
        hovering = false;
        focused = false;
      }
      draw();
    });
    // The machine badge is a link, and what it opens is the reader's next
    // move: the overlay gets out of the way instead of covering the page the
    // click just called for. Its focus alone would otherwise hold the row
    // open until something else took it (claunch-m5cux review).
    const machine = $("sysperf-badge");
    if (machine) machine.addEventListener("click", () => close());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && (hovering || focused || pinned)) close();
    });
    document.addEventListener("click", (e) => {
      if ((hovering || focused || pinned) && !row.contains(e.target)) close();
    });
  }

  /* Redraw the line and the detail from the badges as they stand now. The
     renderers call this after every change; a badge that is not drawn yet
     (the first paint, before the daemon's answers) contributes nothing. */
  function sync() {
    const row = $("link-row");
    const line = $("status-line");
    const overlay = $("status-overlay");
    if (!row || !line || !overlay) return;
    bind();

    const parts = [];
    const visible = [];
    let worst = 0;
    for (const id of BADGES) {
      const badge = $(id);
      if (!shown(badge)) continue;
      visible.push([id, badge]);
      const text = shortText(id, badge);
      if (text) parts.push(text);
      worst = Math.max(worst, severity(badge));
    }
    for (const cls of DOTS) row.classList.toggle(cls, cls === DOTS[worst]);
    line.classList.toggle("hidden", visible.length === 0);
    const text = line.querySelector(".status-text");
    if (text) text.textContent = parts.join(" · ");

    if (!visible.length) {
      close();
      return;
    }
    // The detail is only ever read with the overlay open, so a badge that
    // moves while it is shut (the machine badge re-reads every few seconds)
    // only marks the detail stale; opening it rebuilds.
    const signature = visible
      .map(([id, b]) => `${id}\u0000${b.textContent}\u0000${b.getAttribute("title") || ""}`)
      .join("\u0001");
    if (signature === detailSignature) return;
    detailSignature = signature;
    detailStale = true;
    if (overlay.classList.contains("hidden")) return;
    rebuildDetail(visible);
  }

  globalThis.StatusLine = {
    sync, close,
    open: () => { hovering = true; focused = true; draw(); },
  };
})();
