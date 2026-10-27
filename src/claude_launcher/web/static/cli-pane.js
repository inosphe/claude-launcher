/* A session's own shell, in the terminal's split pane (❯ cli; claunch-jg8pt).

   Every session has one shell on the daemon machine, with the user's own
   privileges (daemon/session_shells.py, daemon/clipty.py's SessionShell).
   This pane is its viewer:

     [status ......... restart?]
     [proposals the session's agent made: run / dismiss]
     [xterm]

   The pane holds one socket per mount, addressed by the session name
   (/api/sessions/<name>/shell/ws) — the address IS the boundary, so this
   file never names another session. The frame dialect is the CLI tab's
   (app.js ensureCliSocket), plus `suggestions`: the agent may not type, it
   proposes (shell_suggest) and only the user's run/dismiss settles a row.
   `run` goes to the accept route with no body, which is the same route the
   MCP tools are refused — an agent-shaped accept is a 403 there.

   The dashboard's CLI tab (#cli) is the user's own shell and has no agent
   frames at all; it is not this pane and this pane does not share it.

   All user- and agent-supplied text goes through textContent. */
globalThis.CliPane = (() => {
  "use strict";

  let host = null;          // the split frame's body we mounted into
  let name = "";            // the session whose shell is shown
  let term = null, fit = null, ws = null;
  let strip = null, status = null, act = null;
  let rows = [];            // pending proposals, as the daemon last sent them
  let remoteResize = false, retry = 0, retryTimer = null, fitObserver = null;

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  const enc = encodeURIComponent;
  const base = () => "/api/sessions/" + enc(name) + "/shell";
  const socketUrl = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const path = base() + "/ws";
    return `${proto}://${location.host}${typeof url === "function" ? url(path) : path}`;
  };

  function setStatus(state) {
    if (!status) return;
    status.textContent =
      state === "live" ? "live"
      : state === "exited" ? "exited"
      : state === "down" ? "offline"
      : state === "connecting" ? "…"
      : "off";
    status.className = "badge " + (state === "live" ? "idle" : "exited");
    if (act) {
      act.classList.toggle("hidden", state !== "exited" && state !== "down");
      act.textContent = state === "exited" ? "restart" : state === "down" ? "retry" : "";
      act.title = state === "exited"
        ? "this session's shell exited — start a fresh one"
        : "the daemon stopped answering — try the socket again";
    }
  }

  /* ---- the proposal strip ----

     Fed by the socket's init frame (whatever was already pending) and by
     every `suggestions` frame after it, so a row the agent withdraws
     disappears here without a poll. `run` types the line into this
     session's shell through the daemon's accept route; `dismiss` only
     settles the row. */
  function drawStrip() {
    if (!strip) return;
    strip.replaceChildren();
    strip.classList.toggle("hidden", rows.length === 0);
    for (const item of rows) {
      const row = node("div", "cli-suggest-row");
      const who = node("span", "cli-suggest-who", item.session || "agent");
      who.title = "proposed by this session's agent";
      const text = node("code", "cli-suggest-text", item.text || "");
      const why = node("span", "cli-suggest-why", item.why || "");
      if (item.why) why.title = item.why;
      const run = node("button", "cli-suggest-btn cli-suggest-run", "run");
      run.type = "button";
      run.title = "type this into this session's shell and press Enter";
      run.addEventListener("click", () => decide(item.id, "accept", row));
      const dismiss = node("button", "cli-suggest-btn", "dismiss");
      dismiss.type = "button";
      dismiss.title = "decline it — nothing is typed";
      dismiss.addEventListener("click", () => decide(item.id, "reject", row));
      row.append(who, text, why, run, dismiss);
      strip.append(row);
    }
  }

  async function decide(id, verb, row) {
    if (row) row.classList.add("busy");
    try {
      const resp = await api(`${base()}/suggestions/${enc(id)}/${verb}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!resp.ok) {
        if (row) row.classList.remove("busy");
        return;
      }
      // The daemon broadcasts the new queue too; drop the row now so the
      // click feels immediate even before that frame lands.
      rows = rows.filter((s) => s.id !== id);
      drawStrip();
    } catch {
      if (row) row.classList.remove("busy");
    }
  }

  /* ---- the socket ---- */

  function actClick() {
    if (!ws) return;
    if (ws.readyState === WebSocket.OPEN) {
      if (act && act.textContent === "restart") ws.send(JSON.stringify({ type: "restart" }));
      return;
    }
    if (act && act.textContent === "retry") connect();
  }

  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    setStatus("connecting");
    const sock = new WebSocket(socketUrl());
    sock.binaryType = "arraybuffer";
    ws = sock;
    sock.onopen = () => { if (ws === sock) retry = 0; };
    sock.onmessage = (ev) => {
      if (ws !== sock) return;
      if (typeof ev.data === "string") {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "init") {
          retry = 0;
          if (fit && term) {
            remoteResize = true;
            try { term.resize(msg.cols, msg.rows); } finally { remoteResize = false; }
          }
          if (msg.exited) {
            if (term) term.write(
              "\r\n\x1b[90m[this session's shell exited — restart it from here]\x1b[0m\r\n");
            setStatus("exited");
          } else {
            setStatus("live");
            refit();
          }
          rows = Array.isArray(msg.suggestions) ? msg.suggestions : [];
          drawStrip();
        } else if (msg.type === "suggestions") {
          rows = Array.isArray(msg.items) ? msg.items : [];
          drawStrip();
        } else if (msg.type === "exit") {
          if (term) term.write(
            `\r\n\x1b[90m[this session's shell exited (code ${msg.code}) — restart it from here]\x1b[0m\r\n`);
          setStatus("exited");
        } else if (msg.type === "resize") {
          if (term && (term.cols !== msg.cols || term.rows !== msg.rows)) {
            remoteResize = true;
            try { term.resize(msg.cols, msg.rows); } finally { remoteResize = false; }
          }
        }
      } else if (term) {
        term.write(new Uint8Array(ev.data));
      }
    };
    sock.onclose = () => {
      if (ws !== sock) return;
      ws = null;
      // A close only ever means "try again": the daemon names the shell,
      // this pane just looks at it. Back off so a stopped daemon is not
      // hammered by a pane that is open in a tab nobody is watching.
      if (!host) return;   // closed by unmount()
      setStatus("down");
      const wait = Math.min(15000, 1000 * Math.pow(2, Math.min(retry, 4)));
      retry += 1;
      retryTimer = setTimeout(() => { retryTimer = null; connect(); }, wait);
    };
  }

  function refit() {
    if (!fit || !term) return;
    try { fit.fit(); } catch { /* the pane is hidden or 0x0 */ }
  }

  /* ---- mount / unmount ---- */

  function mount(target, session) {
    if (term && host === target && name === session) return;
    unmount();
    host = target;
    name = session;

    const root = node("div", "cli-pane");
    const head = node("div", "cli-pane-head");
    status = node("span", "badge cli-pane-status");
    act = node("button", "term-btn cli-pane-act hidden");
    act.type = "button";
    act.addEventListener("click", actClick);
    head.append(node("span", "cli-pane-title", "shell · " + session), status, act);
    strip = node("div", "cli-suggest cli-pane-suggest hidden");
    strip.setAttribute("aria-live", "polite");
    const termHost = node("div", "cli-pane-term");
    root.append(head, strip, termHost);
    target.replaceChildren(root);
    // The frame's body is a scroller for the other panes; a terminal needs a
    // definite box instead, so the pane claims the body (style.css).
    target.classList.add("cli-pane-host");
    setStatus("off");

    term = new Terminal({
      fontFamily: "Cascadia Mono, Consolas, Menlo, monospace",
      fontSize: typeof fontSize === "number" ? fontSize : 13,
      theme: { background: "#14161a" },
      // A raw shell has no daemon-side screen; xterm's own scrollback is
      // what the wheel browses here (the CLI tab's rule, verbatim).
      scrollback: 5000,
      macOptionClickForcesSelection: true,
    });
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(termHost);
    term.onData((data) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        // Binary, on purpose: TEXT frames are this protocol's controls.
        ws.send(new TextEncoder().encode(data));
      }
    });
    term.onResize(({ cols, rows: r }) => {
      if (remoteResize) return;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "resize", cols, rows: r }));
      }
    });
    fitObserver = new ResizeObserver(() => refit());
    fitObserver.observe(root);
    refit();
    connect();
  }

  function unmount() {
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    if (fitObserver) { try { fitObserver.disconnect(); } catch {} fitObserver = null; }
    if (host) { try { host.classList.remove("cli-pane-host"); } catch {} }
    const sock = ws;
    ws = null;
    host = null;
    name = "";
    rows = [];
    if (sock) { try { sock.close(); } catch {} }
    if (term) { try { term.dispose(); } catch {} }
    term = null;
    fit = null;
    strip = status = act = null;
  }

  return { mount, unmount };
})();
