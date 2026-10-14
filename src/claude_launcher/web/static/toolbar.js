/* Keep the session controls on one line at the actual column width. Original
 * controls retain their text and handlers; CSS supplies the compact glyphs. */
(() => {
  const header = document.getElementById("term-header");
  if (!header) return;
  const icons = {
    "term-goto": "⇱", "term-pin": "📌", "term-status": "●",
    "term-handle": "@", "term-tps": "↯", "term-timer": "◷",
    "term-note": "note",
    "term-timer-skip": "⏭", "term-hold": "⇥", "term-link": "↻",
    "term-scroll": "↥", "term-resume": "▶", "term-brief": "▤",
    "term-observer": "◉", "term-blockbar": "☰", "term-rebrief": "↻",
    "term-fork": "⑂", "term-merge": "↩", "term-pause": "Ⅱ",
    "term-kill": "×", "term-archive": "▣", "term-splitbtn": "⬒", "term-simsbtn": "◬",
    "term-chartbtn": "◇",
    "term-details": "ⓘ", "term-zoom-level": "A",
    "term-zoom-out": "−", "term-zoom-in": "+",
  };
  const more = document.createElement("button");
  more.id = "term-more";
  more.className = "term-btn hidden";
  more.type = "button";
  more.textContent = "⋯";
  more.title = "More session controls";
  more.setAttribute("aria-label", more.title);
  more.setAttribute("aria-expanded", "false");
  more.setAttribute("aria-controls", "term-overflow-menu");
  header.append(more);
  const menu = document.createElement("div");
  menu.id = "term-overflow-menu";
  menu.setAttribute("popover", "auto");
  menu.setAttribute("aria-label", "More session controls");
  document.body.append(menu);
  const descriptions = new WeakMap();
  let overflow = [];
  let frame = 0;
  function populateMenu() {
    menu.replaceChildren();
    for (const item of overflow) {
      // A group (the reminder frame) goes into the menu as its visible
      // buttons, together; a hidden one has nothing to offer there. The view
      // switch (CLI | HTML) goes in as one button per radio, pressed on the
      // view on screen.
      const sources = item.matches("button") ? [item]
        : item.querySelectorAll('button:not(.hidden), input[type="radio"]');
      for (const source of sources.length ? sources : [item]) {
        if (source.matches('input[type="radio"]')) {
          const proxy = document.createElement("button");
          proxy.type = "button";
          proxy.className = "term-btn";
          proxy.textContent = source.closest("label")?.textContent.trim() || source.value;
          proxy.title = source.closest("label")?.title || "";
          proxy.setAttribute("aria-pressed", String(source.checked));
          proxy.addEventListener("click", () => { menu.hidePopover(); source.click(); });
          menu.append(proxy);
          continue;
        }
        const proxy = document.createElement(source.matches("button") ? "button" : "span");
        proxy.className = "term-btn";
        proxy.textContent = source.textContent.trim() || source.getAttribute("aria-label");
        proxy.setAttribute("aria-label", source.getAttribute("aria-label") || source.textContent);
        proxy.title = source.title;
        if (source.matches("button")) {
          proxy.type = "button";
          proxy.disabled = source.disabled;
          if (source.hasAttribute("aria-pressed")) proxy.setAttribute("aria-pressed", source.getAttribute("aria-pressed"));
          // A segment of the lifecycle group (resume | pause | kill |
          // archive) is a radio: in the menu it is pressed on the state the
          // session is in, like the view switch's buttons.
          if (source.getAttribute("role") === "radio") proxy.setAttribute("aria-pressed", source.getAttribute("aria-checked") || "false");
          proxy.addEventListener("click", () => { menu.hidePopover(); source.click(); });
        }
        menu.append(proxy);
      }
    }
  }
  function positionMenu() {
    const rect = more.getBoundingClientRect();
    menu.style.top = `${Math.min(rect.bottom + 4, innerHeight - menu.offsetHeight - 8)}px`;
    menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
  }
  more.addEventListener("click", () => {
    if (menu.matches(":popover-open")) menu.hidePopover();
    else { populateMenu(); menu.showPopover(); positionMenu(); }
  });
  // `beforetoggle` fires synchronously as the popover opens or closes (Escape,
  // light dismiss, hidePopover); `toggle` is queued as a task and can land
  // well after, leaving the button reading expanded on a closed menu
  // (claunch-gqp8c). `toggle` stays to settle on the state that took.
  menu.addEventListener("beforetoggle", (e) => more.setAttribute("aria-expanded", String(e.newState === "open")));
  menu.addEventListener("toggle", () => more.setAttribute("aria-expanded", String(menu.matches(":popover-open"))));
  // The lifecycle group (resume | pause | kill | archive) keeps one Tab stop
  // and moves between its segments with the arrow keys, as a radio group does
  // (claunch-468ec). The stop is the focused segment, else the checked one,
  // else the first that can be pressed — the checked segment is often the one
  // the state disables (resume while it runs). An arrow only moves focus: it
  // does not press, because kill and archive end the session and a key that
  // walks the group must not fire them. Space or Enter presses, as on any
  // button.
  const life = document.getElementById("term-life");
  const lifeSegments = () => (life ? [...life.querySelectorAll('[role="radio"]')] : []);
  const pressable = (b) => !b.disabled && !b.classList.contains("hidden") && !b.closest(".toolbar-overflow");
  function syncLifeTabstop() {
    const segments = lifeSegments();
    const open = segments.filter(pressable);
    const stop = open.find(b => b === document.activeElement)
      || open.find(b => b.getAttribute("aria-checked") === "true") || open[0];
    for (const b of segments) {
      const value = b === stop ? "0" : "-1";
      if (b.getAttribute("tabindex") !== value) b.setAttribute("tabindex", value);
    }
  }
  if (life) life.addEventListener("keydown", (e) => {
    const open = lifeSegments().filter(pressable);
    const at = open.indexOf(document.activeElement);
    if (at < 0) return;
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    const next = step ? open[(at + step + open.length) % open.length]
      : e.key === "Home" ? open[0] : e.key === "End" ? open[open.length - 1] : null;
    if (!next) return;
    e.preventDefault();
    next.focus();
    syncLifeTabstop();
  });
  // The label-plus-description each control's tooltip and aria-label carry.
  // Written only where it moved: the session poll rewrites these controls
  // every tick, and each write is a mutation a page-watching extension pays
  // for (claunch-v87).
  function describe() {
    const title = document.getElementById("term-title");
    if (title) {
      // The tooltip carries the full name a long title clips, and what a click does.
      const name = title.textContent.trim();
      const tip = name ? `${name} — click to copy` : "";
      if (title.title !== tip) title.title = tip;
    }
    for (const [id, icon] of Object.entries(icons)) {
      const el = document.getElementById(id);
      if (!el) continue;
      if (el.dataset.toolbarIcon !== icon) el.dataset.toolbarIcon = icon;
      const label = el.textContent.trim() || id.replace("term-", "");
      const previous = descriptions.get(el);
      const description = previous && el.title === previous.rendered ? previous.description : el.title;
      const rendered = description && description !== label ? `${label} — ${description}` : label;
      descriptions.set(el, { description, rendered });
      el.title = rendered;
      el.setAttribute("aria-label", rendered);
    }
    syncLifeTabstop();
  }
  function fit() {
    frame = 0;
    observer.disconnect();
    header.classList.remove("term-compact");
    header.querySelectorAll(".toolbar-overflow").forEach(el => el.classList.remove("toolbar-overflow"));
    more.classList.add("hidden");
    overflow = [];
    describe();
    const items = [...header.children].flatMap(el => el.classList.contains("term-actions") ? [...el.children] : [el])
      .filter(el => el !== more && el.id !== "term-details" && getComputedStyle(el).display !== "none");
    const fits = () => {
      const style = getComputedStyle(header);
      const available = header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const visible = items.filter(el => !el.classList.contains("toolbar-overflow"));
      if (!more.classList.contains("hidden")) visible.push(more);
      const needed = visible.reduce((sum, el) => sum + el.getBoundingClientRect().width, 0)
        + Math.max(0, visible.length - 1) * parseFloat(style.columnGap);
      return needed <= available + 0.5;
    };
    if (!fits()) header.classList.add("term-compact");
    if (!fits()) {
      more.classList.remove("hidden");
      for (const item of [...items].reverse()) {
        if (fits()) break;
        if (item.id === "term-title") continue;
        item.classList.add("toolbar-overflow");
        overflow.unshift(item);
      }
    }
    if (menu.matches(":popover-open")) {
      if (!overflow.length) menu.hidePopover();
      else if (!menu.contains(document.activeElement)) { populateMenu(); positionMenu(); }
    }
    // Again now the overflow is settled: a group moved into the menu has no
    // stop on the row.
    syncLifeTabstop();
    watch();
  }
  function watch() {
    observer.observe(header, { subtree: true, childList: true, characterData: true, attributes: true,
      attributeFilter: ["class", "title", "disabled", "aria-pressed", "aria-checked"] });
  }
  function schedule() { if (!frame) frame = requestAnimationFrame(fit); }
  // A tooltip or a pressed state does not change what fits on the line, so
  // those alone re-describe the controls without the measuring pass, which
  // takes the compact and overflow classes off and puts them back.
  const observer = new MutationObserver((records) => {
    if (records.every(r => r.type === "attributes" && (r.attributeName === "title" || r.attributeName === "aria-pressed" || r.attributeName === "aria-checked"))) {
      if (frame) return;
      observer.disconnect();
      describe();
      watch();
      return;
    }
    schedule();
  });
  new ResizeObserver(schedule).observe(header);
  document.fonts.ready.then(schedule);
  fit();
})();
