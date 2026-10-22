/* Keep the session controls on one line at the actual column width. Original
 * controls retain their text and handlers; CSS supplies the compact glyphs. */
(() => {
  const header = document.getElementById("term-header");
  if (!header) return;
  const icons = {
    "term-goto": "⇱", "term-pin": "📌", "term-status": "●",
    "term-handle": "@", "term-tps": "↯", "term-timer": "◷",
    "term-note": "note",
    "term-timer-skip": "⏭", "term-hold": "⇥", "term-link": "↻", "term-conn": "⇄", "term-force": "↻",
    "term-scroll": "↥", "term-resume": "▶", "term-brief": "▤",
    "term-observer": "◉", "term-webdebug": "⧉", "term-blockbar": "☰", "term-rebrief": "↻",
    "term-fork": "⑂", "term-merge": "↩", "term-idle-hold": "☾", "term-pause": "Ⅱ",
    "term-kill": "×", "term-archive": "▣", "term-splitbtn": "⬒", "term-simsbtn": "◬",
    "term-chartbtn": "◇", "term-categorybtn": "⌘", "term-qabtn": "?", "term-threadsbtn": "🗪",
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
  // Each item's width as the last fit left it. A text change inside an item
  // that leaves its width where it was cannot change what fits, and the
  // reminder countdown changes its text every second -- the measuring pass
  // for that was a forced style and layout of the header once a second
  // (claunch-i3es6).
  let widths = new Map();
  // The text each item held at that fit, for the same reason and read the
  // same way: a settled row's item width is its share of the space, so a
  // text change can only be shown not to matter by comparing texts.
  let texts = new Map();
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
  // The lifecycle group (resume | hold | pause | kill | archive) keeps one Tab stop
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
    tabular = new WeakMap();
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
    widths = new Map(items.map(el => [el, el.getBoundingClientRect().width]));
    texts = new Map(items.map(el => [el, el.textContent]));
    watch();
  }
  // A text change that only swapped digits inside a `tabular-nums` run cannot
  // move a width: every digit in such a run is the same width, so the same
  // number of characters at the same positions lays out the same. That is the
  // one text change this row gets once a second — the reminder chip's
  // countdown — so without this the measuring pass below ran at 1 Hz for as
  // long as a session was on screen (claunch-hwbuc). The app writes the
  // countdown with `textContent` (paintTermTimer), which a MutationObserver
  // reports as a childList swap rather than a characterData edit, so the
  // comparison is made against the text each item had at the last fit.
  //
  // It is a claim about the tabular run only, so it is checked twice: every
  // character that moved must be a digit on both sides, and the element the
  // text sits in must actually resolve to `font-variant-numeric: tabular-nums`
  // (computed, not assumed — the chip gets it from the stylesheet, and a
  // harness that lifts this function out alone still has to answer the same
  // way). The style answer is cached per element: it cannot change without a
  // class or an attribute, and both are records this observer sees.
  // Replaced at the start of every fit: a fit writes classes, and a class is
  // exactly what a computed style can move under (review of claunch-hwbuc,
  // s1305-rev). Cached between fits only, for the mutations between them.
  let tabular = new WeakMap();
  function tabularUnder(node) {
    // A characterData record's target is the text node; the style belongs to
    // the element it sits in.
    const start = node && node.nodeType === Node.ELEMENT_NODE ? node : node && node.parentElement;
    if (!start) return false;
    const known = tabular.get(start);
    if (known !== undefined) return known;
    let answer = false;
    for (let el = start; el && el !== header; el = el.parentElement) {
      if (getComputedStyle(el).fontVariantNumeric.includes("tabular-nums")) { answer = true; break; }
    }
    tabular.set(start, answer);
    return answer;
  }
  function digitsOnly(before, after) {
    if (typeof before !== "string" || before.length !== after.length) return false;
    for (let i = 0; i < after.length; i++) {
      if (before[i] === after[i]) continue;
      if (!(before[i] >= "0" && before[i] <= "9") || !(after[i] >= "0" && after[i] <= "9")) return false;
    }
    return true;
  }
  // True when every text change in `records` sits inside an item the last
  // fit measured, on the row, at the width it was measured at. A change to
  // the row itself (an item added or removed) or to an item in the menu is
  // not, and neither is anything the last fit did not see. Only a row that
  // is neither compact nor overflowing is the state fit measures in, so only
  // there does an unchanged width mean an unchanged answer: a compact button
  // is a fixed-width glyph whatever its label says, and a settled row's item
  // width is its share of the space rather than its content's. In those
  // states a text change therefore has to be provably width-neutral — digits
  // swapped inside a tabular run, at the same length — before the same answer
  // can be assumed.
  function sameWidths(records) {
    const settled = header.classList.contains("term-compact") || overflow.length > 0;
    const touched = new Set();
    for (const r of records) {
      let node = r.target.nodeType === Node.ELEMENT_NODE ? r.target : r.target.parentElement;
      while (node && node !== header && !widths.has(node)) node = node.parentElement;
      if (!node || node === header) return false;
      touched.add(node);
      if (settled && !(digitsOnly(texts.get(node), node.textContent) && tabularUnder(r.target))) return false;
    }
    for (const el of touched) {
      if (el.classList.contains("toolbar-overflow")) return false;
      if (Math.abs(el.getBoundingClientRect().width - widths.get(el)) > 0.5) return false;
    }
    return true;
  }
  function watch() {
    observer.observe(header, { subtree: true, childList: true, characterData: true, attributes: true,
      attributeFilter: ["class", "title", "disabled", "aria-pressed", "aria-checked"] });
  }
  function schedule() { if (!frame) frame = requestAnimationFrame(fit); }
  // A tooltip or a pressed state does not change what fits on the line, and
  // neither does a text change that leaves its item as wide as it was
  // (sameWidths), so those alone re-describe the controls without the
  // measuring pass, which takes the compact and overflow classes off and
  // puts them back.
  const quiet = r => r.type === "attributes" && (r.attributeName === "title" || r.attributeName === "aria-pressed" || r.attributeName === "aria-checked");
  const observer = new MutationObserver((records) => {
    const texts = records.filter(r => !quiet(r));
    if (texts.every(r => r.type !== "attributes") && (!texts.length || sameWidths(texts))) {
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
