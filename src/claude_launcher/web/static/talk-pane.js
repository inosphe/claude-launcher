/* The session's conversation pane in the terminal's split slot: threads,
   QA, or both side by side (claunch-78dpm). docs/session-threads.md.

   Threads (threads-pane.js) and QA (qa-pane.js) stay separate panes with
   their own polls; this module only places them. A tab bar picks the mode:

     threads   the threads pane alone
     qa        the QA pane alone
     both      two columns -- threads on the left, QA on the right

   The mode is the session's split choice (sessLayout.pane in app.js), so
   the 🗪 threads and ? qa header buttons and a reload land on the same
   view; a tab press goes through opts.onMode, and app.js calls setMode back.

   Links between the two (daemon/talk_links.py) are followed here: a thread
   chip on a QA card selects that thread, a question chip in a thread
   scrolls to that card, and "쓰레드로 논의" opens the new-thread form about
   the question. From a single-mode view the pane moves to "both" first, so
   the place the link was followed from stays on screen. */
globalThis.TalkPane = (() => {
  "use strict";
  const MODES = [
    ["threads", "쓰레드"],
    ["qa", "QA"],
    ["both", "둘 다"],
  ];
  let host = null, name = "", mode = "threads", opts = {}, max = false;

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  /* ---- pure helpers (tests/web/talkpane_check.js) ---- */

  function isMode(m) {
    return MODES.some(([k]) => k === m);
  }

  /* Which panes a mode shows. */
  function shows(m) {
    return {threads: m === "threads" || m === "both", qa: m === "qa" || m === "both"};
  }

  /* The mode to be in to follow a link to `target` ("threads" or "qa")
     from mode `m`: unchanged when the target is already on screen. */
  function modeFor(m, target) {
    return shows(m)[target] ? m : "both";
  }

  /* ---- drawing ---- */

  function root() {
    let r = host && host.querySelector(".talk-pane");
    if (r) return r;
    r = node("div", "talk-pane");
    const tabs = node("div", "talk-tabs");
    tabs.setAttribute("role", "tablist");
    for (const [key, label] of MODES) {
      const b = node("button", "talk-tab", label);
      b.type = "button";
      b.dataset.mode = key;
      b.setAttribute("role", "tab");
      b.addEventListener("click", () => want(key));
      tabs.append(b);
    }
    // Right of the tabs (claunch-qkvb1): ⤢ enlarges the pane to the whole
    // column and ⤡ gives the terminal back; × closes the pane. Both ask the
    // page, which remembers them per session and calls setMax back.
    const ctl = node("div", "talk-ctl");
    const big = node("button", "talk-ctl-btn talk-max");
    big.type = "button";
    big.addEventListener("click", () => {
      if (opts.onMax) opts.onMax(!max);
      else setMax(!max);
    });
    const shut = node("button", "talk-ctl-btn talk-close", "×");
    shut.type = "button";
    shut.title = "close this pane — the terminal takes the column back";
    shut.setAttribute("aria-label", "close");
    shut.addEventListener("click", () => {
      if (opts.onClose) opts.onClose();
      else unmount();
    });
    ctl.append(big, shut);
    tabs.append(ctl);
    const body = node("div", "talk-body");
    body.append(node("div", "talk-col talk-threads"), node("div", "talk-col talk-qa"));
    r.append(tabs, body);
    host.replaceChildren(r);
    return r;
  }

  function drawMax(r) {
    const b = r.querySelector(".talk-max");
    if (!b) return;
    b.textContent = max ? "⤡" : "⤢";
    b.title = max
      ? "shrink this pane — the terminal comes back above it"
      : "enlarge this pane to the whole column";
    b.setAttribute("aria-label", max ? "shrink" : "enlarge");
    b.setAttribute("aria-pressed", String(max));
  }

  function draw() {
    const r = root();
    drawMax(r);
    for (const b of r.querySelectorAll(".talk-tab")) {
      const on = b.dataset.mode === mode;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", String(on));
    }
    const body = r.querySelector(".talk-body");
    body.dataset.mode = mode;
    const see = shows(mode);
    const tcol = body.querySelector(".talk-threads");
    const qcol = body.querySelector(".talk-qa");
    tcol.classList.toggle("hidden", !see.threads);
    qcol.classList.toggle("hidden", !see.qa);
    // A pane mounted on a column it already holds keeps its state (both
    // mount() calls return early), so a tab press does not reload it.
    if (see.threads) {
      globalThis.ThreadsPane?.mount(tcol, name, {
        api: opts.api, storageKey: opts.storageKey, onChange: opts.onThreadsChange,
        onQuestion: (id) => follow("qa", () => globalThis.QaPane?.focus(id)),
      });
    } else if (tcol.firstChild) {
      globalThis.ThreadsPane?.unmount();
    }
    if (see.qa) {
      globalThis.QaPane?.mount(qcol, name, {
        api: opts.api, onChange: opts.onQaChange,
        onThread: (id) => follow("threads", () => globalThis.ThreadsPane?.select(id)),
        onDiscuss: (q) => follow("threads", () => globalThis.ThreadsPane?.compose(q)),
      });
    } else if (qcol.firstChild) {
      globalThis.QaPane?.unmount();
    }
  }

  /* A tab press: the page owns the choice (it is remembered per session),
     so ask it; without an owner, switch here. */
  function want(m) {
    if (!isMode(m) || m === mode) return;
    if (opts.onMode) opts.onMode(m);
    else setMode(m);
  }

  function follow(target, then) {
    want(modeFor(mode, target));
    then();
  }

  function setMode(m) {
    if (!host || !isMode(m)) return;
    mode = m;
    draw();
  }

  /* Whether the pane fills the column: the page owns the layout, this only
     shows which way the button goes. */
  function setMax(on) {
    max = !!on;
    const r = host && host.querySelector(".talk-pane");
    if (r) drawMax(r);
  }

  function mount(target, session, o = {}) {
    if ("max" in o) max = !!o.max;
    if (host === target && name === session && target.querySelector(".talk-pane")) {
      opts = o;
      setMode(o.mode || mode);
      return;
    }
    unmount();
    host = target;
    name = session;
    opts = o;
    mode = isMode(o.mode) ? o.mode : "threads";
    draw();
  }

  function unmount() {
    globalThis.ThreadsPane?.unmount();
    globalThis.QaPane?.unmount();
    if (host) host.replaceChildren();
    host = null;
    name = "";
  }

  return {
    mount, unmount, setMode, setMax, isMode, shows, modeFor,
    mode: () => mode,
    max: () => max,
    refreshQa: () => { if (host && shows(mode).qa) globalThis.QaPane?.refresh(); },
  };
})();
