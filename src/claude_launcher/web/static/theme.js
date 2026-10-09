/* The light theme (claunch-xpe81).

   Every colour in this app is written for a dark page: style.css alone holds
   some two thousand colour literals, chosen one rule at a time. A second,
   hand-kept palette beside them would drift from the first the day someone
   adds a rule and forgets the other half, so the light theme is DERIVED from
   the dark one instead: on the way to light, every colour in the app's own
   sheets is rewritten in place to its lightness-inverted counterpart (same
   hue and saturation, lightness 1 - L), and on the way back the declarations
   the rewrite touched get their original values again. A rule added later is
   themed without anyone remembering to.

   What the plain inversion gets wrong, and what is done about it:
     - black with alpha is a shadow or a scrim, and a shadow inverted to white
       is a glow. Translucent black, and any black inside a *-shadow, is left
       as it is.
     - accent-coloured TEXT (green "ok", blue links) sits at mid lightness, so
       1 - L leaves it at mid lightness too: readable on near-black, faint on
       near-white. Saturated text colours are capped at ACCENT_TEXT_L.
     - `color-scheme: dark` asks the platform for dark scrollbars and popups;
       it becomes `light`.

   The terminals are not themed through their colours. A session draws with
   whatever its program asks for -- 16 ANSI colours, 256-colour indices and
   24-bit RGB alike -- and only the first are xterm options. The light theme
   inverts the terminal's PICTURE instead (style.css, `.xterm` under
   [data-theme="light"]: invert + hue-rotate), which turns every colour the
   program chose, whatever its kind, the way the rest of the page turned.
   For the same reason the vendored xterm.css and any rule aimed inside
   `.xterm` are never rewritten: the filter would invert them a second time.

   A sheet copied at run time from the themed sheets (the session grid's
   hover card copies the list's card rules, sessionGridTipStyles) is marked
   `data-theme-copy`. It is skipped here, because it copies values that are
   already themed, and dropped on every switch, so the next use copies again.

   The choice is the viewer's, kept in this browser (localStorage), and every
   tab of the dashboard follows a switch made in another. */
(function (root) {
  "use strict";

  const STORAGE_KEY = "claunch.theme";
  const THEMES = ["dark", "light"];
  const ACCENT_TEXT_L = 0.40;
  const ACCENT_S = 0.30;
  const TEXT_PROPS = new Set(["color", "caret-color", "-webkit-text-fill-color"]);
  const COLOR_PROP = /color|background|border|outline|shadow|^fill$|^stroke$|column-rule|text-decoration|^--/;
  const COLOR_TOKEN = /#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)|\b(?:white|black)\b/g;

  /* ---- colour arithmetic (pure; tests/web/theme_check.js drives it) ---- */

  function parseColor(token) {
    const t = token.trim().toLowerCase();
    if (t === "white") return [255, 255, 255, 1];
    if (t === "black") return [0, 0, 0, 1];
    if (t[0] === "#") {
      let h = t.slice(1);
      if (h.length === 3 || h.length === 4) h = h.split("").map((c) => c + c).join("");
      if (h.length !== 6 && h.length !== 8) return null;
      const n = (i) => parseInt(h.slice(i, i + 2), 16);
      return [n(0), n(2), n(4), h.length === 8 ? n(6) / 255 : 1];
    }
    const m = /^(rgba?|hsla?)\(([^)]*)\)$/.exec(t);
    if (!m) return null;
    const parts = m[2].split(/[\s,/]+/).filter(Boolean);
    if (parts.length < 3) return null;
    const alpha = parts.length > 3
      ? (parts[3].endsWith("%") ? parseFloat(parts[3]) / 100 : parseFloat(parts[3])) : 1;
    if (m[1].startsWith("rgb")) {
      const ch = (p) => (p.endsWith("%") ? parseFloat(p) * 2.55 : parseFloat(p));
      const rgb = parts.slice(0, 3).map(ch);
      if (rgb.some(Number.isNaN)) return null;
      return [...rgb, alpha];
    }
    const hue = parseFloat(parts[0]);
    const s = parseFloat(parts[1]) / 100;
    const l = parseFloat(parts[2]) / 100;
    if ([hue, s, l].some(Number.isNaN)) return null;
    return [...hslToRgb(hue / 360, s, l), alpha];
  }

  function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const l = (max + min) / 2;
    if (max === min) return [0, 0, l];
    const d = max - min;
    const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    let h;
    if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    return [h / 6, s, l];
  }

  function hslToRgb(h, s, l) {
    if (s === 0) return [l * 255, l * 255, l * 255];
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    const f = (t) => {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };
    return [f(h + 1 / 3) * 255, f(h) * 255, f(h - 1 / 3) * 255];
  }

  function formatColor([r, g, b, a]) {
    const c = (v) => Math.round(Math.min(255, Math.max(0, v)));
    if (a >= 1) return `rgb(${c(r)}, ${c(g)}, ${c(b)})`;
    return `rgba(${c(r)}, ${c(g)}, ${c(b)}, ${+a.toFixed(3)})`;
  }

  /* One colour, dark to light, as the property `prop` uses it. Returns the
     token unchanged when it is not a colour this code can read. */
  function lightColor(token, prop) {
    const rgba = parseColor(token);
    if (!rgba) return token;
    const [r, g, b, a] = rgba;
    const black = r === 0 && g === 0 && b === 0;
    if (black && (a < 1 || /shadow/.test(prop || ""))) return token;
    const [h, s, l] = rgbToHsl(r, g, b);
    let next = 1 - l;
    if (TEXT_PROPS.has(prop) && s > ACCENT_S) next = Math.min(next, ACCENT_TEXT_L);
    return formatColor([...hslToRgb(h, s, next), a]);
  }

  /* A whole declaration value: every colour in it, gradients and shadow
     lists included. */
  function lightValue(value, prop) {
    if (prop === "color-scheme") return value.replace(/\bdark\b/g, "light");
    return value.replace(COLOR_TOKEN, (token) => lightColor(token, prop));
  }

  /* ---- the sheets ---- */

  // declaration -> Map(prop -> [dark value, priority]); what a switch back
  // to dark writes again.
  let originals = new WeakMap();
  let current = "dark";

  function themable(sheet) {
    const node = sheet.ownerNode;
    if (node && node.dataset && "themeCopy" in node.dataset) return false;
    return !/\/vendor\//.test(sheet.href || "");
  }

  function eachDeclaration(rules, fn) {
    for (const rule of rules) {
      if (rule.selectorText && rule.selectorText.includes(".xterm")) continue;
      if (rule.style) fn(rule.style);
      if (rule.cssRules) eachDeclaration(rule.cssRules, fn);
    }
  }

  function themeDeclaration(style) {
    let saved = originals.get(style);
    if (!saved) {
      saved = new Map();
      for (let i = 0; i < style.length; i++) {
        const prop = style[i];
        if (prop !== "color-scheme" && !COLOR_PROP.test(prop)) continue;
        const value = style.getPropertyValue(prop);
        if (!value) continue;
        const next = lightValue(value, prop);
        if (next !== value) saved.set(prop, [value, style.getPropertyPriority(prop), next]);
      }
      originals.set(style, saved);
    }
    for (const [prop, [, priority, next]] of saved) style.setProperty(prop, next, priority);
  }

  function restoreDeclaration(style) {
    const saved = originals.get(style);
    if (!saved) return;
    for (const [prop, [value, priority]] of saved) style.setProperty(prop, value, priority);
  }

  function rulesOf(sheet) {
    try { return sheet.cssRules; } catch { return null; }   // another origin's sheet
  }

  function applySheets(fn) {
    for (const sheet of Array.from(root.document.styleSheets)) {
      if (!themable(sheet)) continue;
      const rules = rulesOf(sheet);
      if (rules) eachDeclaration(rules, fn);
    }
  }

  function apply(theme) {
    const doc = root.document;
    if (!THEMES.includes(theme)) theme = "dark";
    current = theme;
    doc.documentElement.dataset.theme = theme;
    doc.documentElement.style.colorScheme = theme === "light" ? "light" : "";
    applySheets(theme === "light" ? themeDeclaration : restoreDeclaration);
    for (const copy of Array.from(doc.querySelectorAll("style[data-theme-copy]"))) copy.remove();
    syncButtons();
    try {
      root.dispatchEvent(new CustomEvent("claunch-theme", { detail: { theme } }));
    } catch { /* no CustomEvent: nobody to tell */ }
  }

  /* A sheet that arrives after the first pass (a <style> a page adds, a
     <link> that loads late) is themed when it lands, if the page is light. */
  function watchSheets() {
    if (typeof MutationObserver !== "function") return;
    const onSheet = () => { if (current === "light") applySheets(themeDeclaration); };
    new MutationObserver((records) => {
      for (const rec of records) {
        for (const node of rec.addedNodes) {
          if (node.nodeName === "LINK") node.addEventListener("load", onSheet, { once: true });
          else if (node.nodeName === "STYLE") onSheet();
        }
      }
    }).observe(root.document.head, { childList: true });
  }

  function stored() {
    try { return root.localStorage.getItem(STORAGE_KEY); } catch { return null; }
  }

  function set(theme) {
    if (!THEMES.includes(theme)) return;
    try { root.localStorage.setItem(STORAGE_KEY, theme); } catch { /* kept for this page only */ }
    if (theme !== current) apply(theme);
  }

  function toggle() { set(current === "light" ? "dark" : "light"); }

  /* The switch: any element with [data-theme-toggle]. Its label says where a
     press goes, not where the page is. */
  function syncButtons() {
    const next = current === "light" ? "dark" : "light";
    for (const btn of root.document.querySelectorAll("[data-theme-toggle]")) {
      btn.textContent = next === "light" ? "☀" : "☾";
      btn.title = `switch to the ${next} theme`;
      btn.setAttribute("aria-label", btn.title);
      btn.setAttribute("aria-pressed", current === "light" ? "true" : "false");
    }
  }

  function bindButtons() {
    root.document.addEventListener("click", (ev) => {
      const btn = ev.target && ev.target.closest && ev.target.closest("[data-theme-toggle]");
      if (btn) toggle();
    });
    syncButtons();
  }

  /* A colour a script puts on an element itself (style.color = ..., an SVG
     stroke): the same rewrite the sheets get, for the theme in force. */
  function color(token, prop) {
    return current === "light" ? lightColor(token, prop || "color") : token;
  }

  const api = {
    STORAGE_KEY, get: () => current, set, toggle, color, lightColor, lightValue, parseColor,
  };

  if (typeof module === "object" && module.exports) module.exports = api;
  if (!root.document) return;
  root.claunchTheme = api;

  // Loaded in <head> after the stylesheets, so the sheets are parsed and the
  // page is themed before its first paint.
  const first = stored();
  watchSheets();
  if (first === "light") apply("light");
  else root.document.documentElement.dataset.theme = "dark";
  if (root.document.readyState === "loading") {
    root.document.addEventListener("DOMContentLoaded", bindButtons, { once: true });
  } else {
    bindButtons();
  }
  root.addEventListener("storage", (ev) => {
    if (ev.key === STORAGE_KEY && THEMES.includes(ev.newValue) && ev.newValue !== current) {
      apply(ev.newValue);
    }
  });
})(typeof window !== "undefined" ? window : globalThis);
