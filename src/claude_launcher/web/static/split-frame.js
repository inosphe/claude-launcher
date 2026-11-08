/* The one frame every pane in the terminal's split slot (#term-wf) sits in
   (claunch-039we): the run, the simulations, the category tree, a
   pi-durable chart, and the conversation pane (threads / QA). Each of them
   used to draw its own top edge, so only the conversation pane had ⤢ and ×;
   the frame gives every kind the same head row:

     [lead ............................ ☐ float ⤢ ×]
     [body                                          ]
     [                                         grip ]

   lead   the pane's name (the header button's label), or whatever the pane
          puts there instead -- the conversation pane's tabs
   ☐ float  takes the pane out of the terminal's column and floats it over
          the terminal instead, so sizing or closing it no longer resizes
          what is underneath (claunch-xkw8o); it asks through onFloat, and
          ⤢ has no meaning while it is on, so it is hidden
   ⤢/⤡   enlarges the pane to the whole column and gives the terminal back
   ×      closes the pane
   grip   the floating pane's resize corner

   The page owns the layout (app.js remembers the float flag, ⤢ and × per
   session), so the controls only ask through onMax / onFloat / onClose;
   setMax draws which way ⤢ points and setFloat draws the checkbox and hides
   ⤢. A pane mounts into `body` and never touches the head, so a new kind of
   pane gets the same controls without drawing any of its own. The frame is
   kept on its host, so whoever holds the host (syncSplitPane) can reach it
   with of(host). */
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

  function drawFloat(box, on) {
    box.checked = !!on;
  }

  /* Replace host's content with a fresh frame. opts: kind (the pane's
     key, kept as data-kind), title (the lead's text), max, float, onMax(on),
     onFloat(on), onClose(). */
  function mount(host, opts = {}) {
    const root = node("div", "split-frame");
    if (opts.kind) root.dataset.kind = opts.kind;
    const head = node("div", "split-frame-head");
    const lead = node("div", "split-frame-lead");
    if (opts.title) lead.append(node("span", "split-frame-title", opts.title));
    const ctl = node("div", "split-frame-ctl");
    const floatWrap = node("label", "split-frame-float");
    floatWrap.title = "float this pane over the terminal — sizing it or closing it "
      + "no longer resizes the terminal";
    const floatBox = node("input", "split-frame-floatbox");
    floatBox.type = "checkbox";
    floatWrap.append(floatBox, node("span", "split-frame-floattext", "float"));
    const big = node("button", "split-frame-btn split-frame-max");
    big.type = "button";
    const shut = node("button", "split-frame-btn split-frame-close", "×");
    shut.type = "button";
    shut.title = "close this pane — the terminal takes the column back";
    shut.setAttribute("aria-label", "close");
    ctl.append(floatWrap, big, shut);
    head.append(lead, ctl);
    const body = node("div", "split-frame-body");
    const grip = node("div", "split-frame-grip");
    grip.title = "drag to resize the floating pane";
    root.append(head, body, grip);
    host.replaceChildren(root);

    let max = !!opts.max;
    let float = !!opts.float;
    const frame = {
      root, head, lead, body, grip,
      max: () => max,
      float: () => float,
      setMax(on) { max = !!on; drawMax(big, max); },
      setFloat(on) {
        float = !!on;
        drawFloat(floatBox, float);
        root.classList.toggle("floating", float);
        // ⤢ enlarges the pane within the column; a floating pane is not in
        // the column, so the button says nothing there.
        big.hidden = float;
      },
    };
    floatBox.addEventListener("change", () => {
      if (opts.onFloat) opts.onFloat(!!floatBox.checked);
      else frame.setFloat(!!floatBox.checked);
    });
    big.addEventListener("click", () => {
      if (opts.onMax) opts.onMax(!max);
      else frame.setMax(!max);
    });
    shut.addEventListener("click", () => {
      if (opts.onClose) opts.onClose();
      else unmount(host);
    });
    frame.setMax(max);
    frame.setFloat(float);
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

  function setFloat(host, on) {
    const f = of(host);
    if (f) f.setFloat(on);
  }

  function unmount(host) {
    if (!host) return;
    frames.delete(host);
    host.replaceChildren();
  }

  return { mount, of, setMax, setFloat, unmount };
})();
