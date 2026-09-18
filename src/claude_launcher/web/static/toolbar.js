/* Keep the session controls on one line at the actual column width. Original
 * controls retain their text and handlers; CSS supplies the compact glyphs. */
(() => {
  const header = document.getElementById("term-header");
  if (!header) return;
  const icons = {
    "term-goto": "⇱", "term-pin": "📌", "term-status": "●",
    "term-handle": "@", "term-tps": "↯", "term-timer": "◷",
    "term-timer-skip": "⏭", "term-hold": "⇥", "term-link": "↻",
    "term-scroll": "↥", "term-resume": "▶", "term-brief": "▤",
    "term-observer": "◉", "term-log": "≡", "term-rebrief": "↻",
    "term-fork": "⑂", "term-merge": "↩", "term-pause": "Ⅱ",
    "term-kill": "×", "term-archive": "▣", "term-splitbtn": "⬒",
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
      const sources = item.matches("button") ? [item] : item.querySelectorAll("button");
      for (const source of sources.length ? sources : [item]) {
        const proxy = document.createElement(source.matches("button") ? "button" : "span");
        proxy.className = "term-btn";
        proxy.textContent = source.textContent.trim() || source.getAttribute("aria-label");
        proxy.setAttribute("aria-label", source.getAttribute("aria-label") || source.textContent);
        proxy.title = source.title;
        if (source.matches("button")) {
          proxy.type = "button";
          proxy.disabled = source.disabled;
          if (source.hasAttribute("aria-pressed")) proxy.setAttribute("aria-pressed", source.getAttribute("aria-pressed"));
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
  menu.addEventListener("toggle", () => more.setAttribute("aria-expanded", String(menu.matches(":popover-open"))));
  function fit() {
    frame = 0;
    observer.disconnect();
    header.classList.remove("term-compact");
    header.querySelectorAll(".toolbar-overflow").forEach(el => el.classList.remove("toolbar-overflow"));
    more.classList.add("hidden");
    overflow = [];
    const title = document.getElementById("term-title");
    if (title) title.title = title.textContent.trim();
    for (const [id, icon] of Object.entries(icons)) {
      const el = document.getElementById(id);
      if (!el) continue;
      el.dataset.toolbarIcon = icon;
      const label = el.textContent.trim() || id.replace("term-", "");
      const previous = descriptions.get(el);
      const description = previous && el.title === previous.rendered ? previous.description : el.title;
      const rendered = description && description !== label ? `${label} — ${description}` : label;
      descriptions.set(el, { description, rendered });
      el.title = rendered;
      el.setAttribute("aria-label", rendered);
    }
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
    observer.observe(header, { subtree: true, childList: true, characterData: true, attributes: true,
      attributeFilter: ["class", "title", "disabled", "aria-pressed"] });
  }
  function schedule() { if (!frame) frame = requestAnimationFrame(fit); }
  const observer = new MutationObserver(schedule);
  new ResizeObserver(schedule).observe(header);
  document.fonts.ready.then(schedule);
  fit();
})();
