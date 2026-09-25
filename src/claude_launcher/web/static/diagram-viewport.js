/* Shared, local-only navigation for SVG diagrams. The renderer still owns
   SVG content and node actions; this module owns the surrounding viewport.
   Polls replace the SVG, so retain navigation by route, host and diagram slot. */
(() => {
  "use strict";
  const selector = "svg.wfd, svg.mesh-ring, svg.flow-ring";
  const states = new Map();
  const live = new Map();
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

  function mount(svg, key) {
    const bounds = svg.viewBox.baseVal;
    if (!(bounds.width > 0 && bounds.height > 0)) return;
    let width = bounds.width, height = bounds.height;
    const state = states.get(key) || { zoom: 1, left: 0, top: 0, fit: true };
    states.delete(key);
    states.set(key, state);
    if (states.size > 100) states.delete(states.keys().next().value);
    const box = document.createElement("div");
    box.className = "diagram-viewer";
    const bar = document.createElement("div");
    bar.className = "diagram-tools";
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Diagram navigation");
    const viewport = document.createElement("div");
    viewport.className = "diagram-viewport";
    viewport.tabIndex = 0;
    viewport.setAttribute("role", "region");
    viewport.setAttribute("aria-label", "Diagram. Arrow keys to pan, plus or minus to zoom, Home to fit.");
    const stage = document.createElement("div");
    stage.className = "diagram-stage";
    svg.before(box);
    box.append(bar, viewport);
    viewport.append(stage);
    stage.append(svg);
    const output = document.createElement("output");
    output.className = "diagram-scale";
    output.setAttribute("aria-label", "Diagram zoom");
    const abort = new AbortController();
    const on = (target, type, fn, options = {}) =>
      target.addEventListener(type, fn, { ...options, signal: abort.signal });
    let space = false, dragged = false, suppressUntil = 0;
    let focused = null;
    on(document, "focusin", (e) => { focused = box.contains(e.target) ? e.target : null; });
    const pointers = new Map();
    let gesture = null;
    const fitScale = () => Math.min(1, (viewport.clientWidth - 24) / width,
      (viewport.clientHeight - 24) / height);
    const minScale = () => Math.min(0.1, Math.max(0.001, fitScale()));
    const save = () => {
      if (!viewport.isConnected) return;
      state.left = viewport.scrollLeft;
      state.top = viewport.scrollTop;
    };
    function draw() {
      const w = width * state.zoom, h = height * state.zoom;
      svg.style.width = `${w}px`;
      svg.style.height = `${h}px`;
      stage.style.width = `${Math.max(viewport.clientWidth, w + 24)}px`;
      stage.style.height = `${Math.max(viewport.clientHeight, h + 24)}px`;
      output.value = `${Math.round(state.zoom * 100)}%`;
    }
    function zoom(value, x = viewport.clientWidth / 2, y = viewport.clientHeight / 2) {
      const old = state.zoom;
      const next = clamp(value, minScale(), 4);
      const px = (viewport.scrollLeft + x - 12) / old;
      const py = (viewport.scrollTop + y - 12) / old;
      state.zoom = next;
      state.fit = false;
      draw();
      viewport.scrollLeft = px * next + 12 - x;
      viewport.scrollTop = py * next + 12 - y;
      save();
    }
    function fit() {
      state.fit = true;
      state.zoom = Math.max(0.001, fitScale());
      draw();
      viewport.scrollLeft = viewport.scrollTop = 0;
      save();
    }
    function button(label, title, action) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "wf-btn";
      b.textContent = label;
      b.title = title;
      b.setAttribute("aria-label", title);
      on(b, "click", action);
      bar.append(b);
      return b;
    }
    button("−", "Zoom out", () => zoom(state.zoom / 1.25));
    bar.append(output);
    button("+", "Zoom in", () => zoom(state.zoom * 1.25));
    button("Fit", "Fit entire diagram (Home)", fit);
    button("Width", "Fit diagram width", () => {
      zoom((viewport.clientWidth - 24) / width);
      viewport.scrollLeft = viewport.scrollTop = 0;
      save();
    });
    button("100%", "Actual size", () => zoom(1));
    const pan = button("Pan", "Drag nodes to pan instead of selecting", () => {
      const enabled = pan.getAttribute("aria-pressed") !== "true";
      pan.setAttribute("aria-pressed", String(enabled));
      viewport.classList.toggle("pan-mode", enabled);
    });
    pan.setAttribute("aria-pressed", "false");
    const expand = button("Expand", "Expand diagram (Escape to close)", () => {
      const expanded = box.classList.toggle("diagram-expanded");
      expand.setAttribute("aria-pressed", String(expanded));
      expand.textContent = expanded ? "Collapse" : "Expand";
    });
    expand.setAttribute("aria-pressed", "false");
    on(window, "keydown", (e) => {
      if (e.key === "Escape" && box.classList.contains("diagram-expanded")) {
        expand.click(); expand.focus(); e.preventDefault();
      }
    });
    const hint = document.createElement("div");
    hint.className = "diagram-help";
    hint.textContent = "Scroll the page · Ctrl/⌘ + scroll to zoom · drag background or Space + drag · pinch to zoom";
    box.append(hint);
    on(viewport, "scroll", save, { passive: true });
    on(viewport, "wheel", (e) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        const r = viewport.getBoundingClientRect();
        const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? viewport.clientHeight : 1;
        zoom(state.zoom * Math.exp(-clamp(e.deltaY * unit, -500, 500) * 0.002),
          e.clientX - r.left, e.clientY - r.top);
      } else if (e.shiftKey && !e.deltaX) {
        e.preventDefault();
        viewport.scrollLeft += e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? viewport.clientWidth : 1);
        save();
      } else if (!box.classList.contains("diagram-expanded")) {
        // Ordinary wheel input reads the content below the diagram, even
        // when the diagram itself has room to scroll. Walk outward at an
        // ancestor's boundary (desktop column -> page, mobile -> run page).
        e.preventDefault();
        let dx = e.deltaX, dy = e.deltaY;
        if (e.deltaMode === 1) { dx *= 16; dy *= 16; }
        if (e.deltaMode === 2) { dx *= viewport.clientWidth; dy *= viewport.clientHeight; }
        for (let parent = box.parentElement; parent && (dx || dy); parent = parent.parentElement) {
          const css = getComputedStyle(parent);
          const left = parent.scrollLeft, top = parent.scrollTop;
          if (/^(auto|scroll|overlay)$/.test(css.overflowX)) parent.scrollLeft += dx;
          if (/^(auto|scroll|overlay)$/.test(css.overflowY)) parent.scrollTop += dy;
          dx -= parent.scrollLeft - left;
          dy -= parent.scrollTop - top;
        }
      }
    }, { passive: false });
    const point = (e) => ({ x: e.clientX, y: e.clientY });
    function baseline() {
      const ps = [...pointers.values()];
      if (!ps.length) { gesture = null; return; }
      const a = ps[0], b = ps[1] || a;
      gesture = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2,
        distance: Math.hypot(a.x - b.x, a.y - b.y),
        zoom: state.zoom, left: viewport.scrollLeft, top: viewport.scrollTop };
    }
    on(viewport, "pointerdown", (e) => {
      if (e.button !== 0 && e.button !== 1) return;
      const rect = viewport.getBoundingClientRect();
      // Leave native scrollbar thumbs and tracks to the browser.
      if (e.clientX >= rect.left + viewport.clientLeft + viewport.clientWidth ||
          e.clientY >= rect.top + viewport.clientTop + viewport.clientHeight) return;
      if (!pointers.size) { dragged = false; suppressUntil = 0; }
      const node = e.target.closest(".wfd-node, .mesh-agent, .mesh-cluster.draggable, .flow-card, .flow-pip, .mesh-link-hit");
      if (e.pointerType !== "touch" && e.button !== 1 && !space &&
          pan.getAttribute("aria-pressed") !== "true" && node) return;
      pointers.set(e.pointerId, point(e));
      baseline();
      if (pointers.size > 1) dragged = true;
      // Capture only after movement: a tap must still click its SVG node.
      if (e.button === 1 || space) e.preventDefault();
      e.stopPropagation();
      viewport.focus({ preventScroll: true });
    }, { capture: true });
    on(window, "pointermove", (e) => {
      if (!pointers.has(e.pointerId) || !gesture) return;
      pointers.set(e.pointerId, point(e));
      const ps = [...pointers.values()], a = ps[0], b = ps[1] || a;
      const x = (a.x + b.x) / 2, y = (a.y + b.y) / 2;
      if (!dragged && Math.hypot(x - gesture.x, y - gesture.y) < 5) return;
      dragged = true;
      viewport.classList.add("dragging");
      e.preventDefault();
      if (viewport.isConnected) viewport.setPointerCapture(e.pointerId);
      state.fit = false;
      if (ps.length > 1 && gesture.distance > 0) {
        state.zoom = clamp(gesture.zoom * Math.hypot(a.x - b.x, a.y - b.y) / gesture.distance, minScale(), 4);
        draw();
      }
      const r = viewport.getBoundingClientRect(), ratio = state.zoom / gesture.zoom;
      viewport.scrollLeft = (gesture.left + gesture.x - r.left - 12) * ratio + 12 - (x - r.left);
      viewport.scrollTop = (gesture.top + gesture.y - r.top - 12) * ratio + 12 - (y - r.top);
      save();
    }, { passive: false });
    function release(e) {
      if (!pointers.delete(e.pointerId)) return;
      if (dragged) suppressUntil = Date.now() + 400;
      if (viewport.hasPointerCapture(e.pointerId)) viewport.releasePointerCapture(e.pointerId);
      baseline();
      if (!pointers.size) viewport.classList.remove("dragging");
    }
    on(window, "pointerup", release);
    on(window, "pointercancel", release);
    // Touch navigation must not complete a mesh edit on the SVG underneath.
    on(viewport, "pointerup", (e) => {
      if (pointers.has(e.pointerId)) { release(e); e.stopPropagation(); }
    }, { capture: true });
    on(viewport, "click", (e) => {
      if (e.detail !== 0 && Date.now() < suppressUntil) { e.preventDefault(); e.stopImmediatePropagation(); }
    }, { capture: true });
    on(viewport, "keydown", (e) => {
      if (e.target !== viewport) return;
      const amount = e.shiftKey ? 200 : 50;
      if (e.key === " ") { space = true; viewport.classList.add("pan-mode"); }
      else if (e.key === "+" || e.key === "=") zoom(state.zoom * 1.25);
      else if (e.key === "-") zoom(state.zoom / 1.25);
      else if (e.key === "Home" || e.key === "0") fit();
      else if (e.key === "ArrowLeft") viewport.scrollLeft -= amount;
      else if (e.key === "ArrowRight") viewport.scrollLeft += amount;
      else if (e.key === "ArrowUp") viewport.scrollTop -= amount;
      else if (e.key === "ArrowDown") viewport.scrollTop += amount;
      else return;
      e.preventDefault(); save();
    });
    function clearSpace() {
      space = false;
      viewport.classList.toggle("pan-mode", pan.getAttribute("aria-pressed") === "true");
    }
    on(viewport, "keyup", (e) => { if (e.key === " ") clearSpace(); });
    on(window, "blur", () => { clearSpace(); pointers.clear(); baseline(); viewport.classList.remove("dragging"); });
    on(viewport, "blur", clearSpace);
    const resize = new ResizeObserver(() => {
      if (!viewport.clientWidth || !viewport.clientHeight) return;
      if (state.fit) fit();
      else { draw(); viewport.scrollLeft = state.left; viewport.scrollTop = state.top; }
    });
    resize.observe(viewport);
    live.set(box, {
      key,
      replace(next) {
        next.before(box);
        svg.replaceWith(next);
        svg = next;
        width = next.viewBox.baseVal.width;
        height = next.viewBox.baseVal.height;
        if (state.fit) fit();
        else { draw(); viewport.scrollLeft = state.left; viewport.scrollTop = state.top; }
        if (focused && document.activeElement === document.body) focused.focus({ preventScroll: true });
      },
      dispose() { resize.disconnect(); abort.abort(); },
    });
  }

  function scan() {
    const slots = new Map();
    document.querySelectorAll(selector).forEach((svg) => {
      const host = svg.closest("[id]");
      const kind = ["wfd", "mesh-ring", "flow-ring"].find((c) => svg.classList.contains(c));
      // The page's address without its `?detail=` rail: opening the rail
      // beside a diagram must not re-key (and so reset) that diagram.
      const prefix = `${location.hash.split("?")[0]}|${host ? host.id : "page"}|${kind}`;
      const slot = slots.get(prefix) || 0;
      slots.set(prefix, slot + 1);
      if (!svg.closest(".diagram-viewer")) {
        const key = `${prefix}|${slot}`;
        const previous = [...live].find(([box, controller]) => !box.isConnected && controller.key === key);
        if (previous) previous[1].replace(svg);
        else mount(svg, key);
      }
    });
    for (const [box, controller] of live) {
      if (!box.isConnected) { controller.dispose(); live.delete(box); }
    }
  }
  // Terminal output also mutates this document frequently. Only diagram
  // additions/removals require rescanning; text and toolbar updates do not.
  const relevant = (node) => node.nodeType === 1 &&
    (node.matches(`${selector}, .diagram-viewer`) || node.querySelector(`${selector}, .diagram-viewer`));
  new MutationObserver((records) => {
    if (records.some((r) => [...r.addedNodes, ...r.removedNodes].some(relevant))) scan();
  }).observe(document.body, { childList: true, subtree: true });
  scan();
})();
