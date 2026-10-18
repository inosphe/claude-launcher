/* The one frame every pane in the terminal's split slot (#term-wf) sits in
   (claunch-039we): the run, the simulations, the category tree, a
   pi-durable chart, and the conversation pane (threads / QA). Each of them
   used to draw its own top edge, so only the conversation pane had ⤢ and ×;
   the frame gives every kind the same head row:

     [lead ............................................ ⤢ ×]
     [body                                                  ]

   lead   the pane's name (the header button's label), or whatever the pane
          puts there instead -- the conversation pane's tabs
   ⤢/⤡   enlarges the pane to the whole column and gives the terminal back
   ×      closes the pane

   The page owns the layout (app.js remembers ⤢ and × per session), so the
   two buttons only ask through onMax / onClose; setMax draws which way ⤢
   points. A pane mounts into `body` and never touches the head, so a new
   kind of pane gets the same controls without drawing any of its own. The
   frame is kept on its host, so whoever holds the host (syncSplitPane) can
   reach it with of(host). */
globalThis.SplitFrame = (() => {
  "use strict";
  const frames = new WeakMap();

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  function drawMax(btn, on) {
    btn.textContent = on ? "⤡" : "⤢";
    btn.title = on
      ? "shrink this pane — the terminal comes back above it"
      : "enlarge this pane to the whole column";
    btn.setAttribute("aria-label", on ? "shrink" : "enlarge");
    btn.setAttribute("aria-pressed", String(on));
  }

  /* Replace host's content with a fresh frame. opts: kind (the pane's
     key, kept as data-kind), title (the lead's text), max, onMax(on),
     onClose(). */
  function mount(host, opts = {}) {
    const root = node("div", "split-frame");
    if (opts.kind) root.dataset.kind = opts.kind;
    const head = node("div", "split-frame-head");
    const lead = node("div", "split-frame-lead");
    if (opts.title) lead.append(node("span", "split-frame-title", opts.title));
    const ctl = node("div", "split-frame-ctl");
    const big = node("button", "split-frame-btn split-frame-max");
    big.type = "button";
    const shut = node("button", "split-frame-btn split-frame-close", "×");
    shut.type = "button";
    shut.title = "close this pane — the terminal takes the column back";
    shut.setAttribute("aria-label", "close");
    ctl.append(big, shut);
    head.append(lead, ctl);
    const body = node("div", "split-frame-body");
    root.append(head, body);
    host.replaceChildren(root);

    let max = !!opts.max;
    const frame = {
      root, head, lead, body,
      max: () => max,
      setMax(on) { max = !!on; drawMax(big, max); },
    };
    big.addEventListener("click", () => {
      if (opts.onMax) opts.onMax(!max);
      else frame.setMax(!max);
    });
    shut.addEventListener("click", () => {
      if (opts.onClose) opts.onClose();
      else unmount(host);
    });
    frame.setMax(max);
    frames.set(host, frame);
    return frame;
  }

  function of(host) {
    return (host && frames.get(host)) || null;
  }

  function setMax(host, on) {
    const f = of(host);
    if (f) f.setMax(on);
  }

  function unmount(host) {
    if (!host) return;
    frames.delete(host);
    host.replaceChildren();
  }

  return { mount, of, setMax, unmount };
})();
