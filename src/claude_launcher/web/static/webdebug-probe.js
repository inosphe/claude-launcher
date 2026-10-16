/* claunch web debug probe (claunch-dsb28).

   Injected first into every HTML page the web debug proxy serves
   (daemon/webdebug.py wraps this file as `(function(CONFIG){ ... })(cfg)`,
   cfg = {parents: [allowed parent origins], target: origin}). It answers
   the web debug page that frames it, over postMessage, and nobody else:
   a message counts only when it comes from window.parent and from an
   origin the proxy's enter link registered.

   Messages in:  {claunch: "wd", type: "cmd", id, op, args}
   Messages out: {claunch: "wd", type: "res", id, ok, value | error}
                 {claunch: "wd", type: "ready" | "nav", url, title}
                 {claunch: "wd", type: "picked", element} / "pick-cancel"
                 {claunch: "wd", type: "console", entry}

   Nothing here acts on its own: picking starts only when the parent asks
   (the user pressed the button), and every agent action is flashed on the
   element so the user sees what was done. */
"use strict";
if (window.__claunchProbe) return;
window.__claunchProbe = true;
if (window.parent === window) return;

const PARENTS = new Set((CONFIG && CONFIG.parents) || []);
let parentOrigin = null;
const consoleLog = [];
const CONSOLE_MAX = 300;
let consoleSeq = 0;
const refs = new Map();      // "e12" -> WeakRef(element)
const refOf = new WeakMap(); // element -> "e12"
let refSeq = 0;

function post(msg) {
  msg.claunch = "wd";
  const targets = parentOrigin ? [parentOrigin] : [...PARENTS];
  for (const origin of targets) {
    try { window.parent.postMessage(msg, origin); } catch (_) { /* origin mismatch */ }
  }
}

/* ---- console capture ---- */
function fmt(value) {
  if (value instanceof Error) return value.stack || String(value);
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, safeReplacer(), 0); } catch (_) { return String(value); }
}
function safeReplacer() {
  const seen = new WeakSet();
  return (_k, v) => {
    if (v instanceof Element) return describeShort(v);
    if (typeof v === "object" && v !== null) {
      if (seen.has(v)) return "[circular]";
      seen.add(v);
    }
    if (typeof v === "function") return `[function ${v.name || "anonymous"}]`;
    return v;
  };
}
function record(level, args) {
  const text = Array.from(args, fmt).join(" ").slice(0, 4000);
  const entry = { seq: ++consoleSeq, level, text, at: new Date().toISOString() };
  consoleLog.push(entry);
  if (consoleLog.length > CONSOLE_MAX) consoleLog.shift();
  if (level === "error") post({ type: "console", entry });
}
for (const level of ["log", "info", "warn", "error", "debug"]) {
  const orig = console[level];
  console[level] = function (...args) {
    try { record(level, args); } catch (_) { /* never break the page */ }
    return orig.apply(this, args);
  };
}
window.addEventListener("error", (e) => {
  record("error", [e.error || `${e.message} (${e.filename}:${e.lineno}:${e.colno})`]);
});
window.addEventListener("unhandledrejection", (e) => {
  record("error", ["Unhandled promise rejection:", e.reason]);
});

/* ---- navigation ---- */
function here() { return { url: location.href, title: document.title }; }
function announce(type) { post({ type, ...here() }); }
for (const name of ["pushState", "replaceState"]) {
  const orig = history[name];
  history[name] = function (...args) {
    const r = orig.apply(this, args);
    setTimeout(() => announce("nav"), 0);
    return r;
  };
}
window.addEventListener("popstate", () => announce("nav"));
window.addEventListener("hashchange", () => announce("nav"));

/* ---- elements ---- */
function refFor(el) {
  let id = refOf.get(el);
  if (!id) {
    id = "e" + (++refSeq);
    refOf.set(el, id);
    refs.set(id, new WeakRef(el));
  }
  return id;
}
function cssEscape(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/[^\w-]/g, "\\$&"); }
function uniqueSelector(el) {
  if (!(el instanceof Element)) return "";
  if (el.id && document.querySelectorAll("#" + cssEscape(el.id)).length === 1) return "#" + cssEscape(el.id);
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && node !== document.documentElement) {
    let part = node.localName;
    if (node.id && document.querySelectorAll("#" + cssEscape(node.id)).length === 1) {
      parts.unshift("#" + cssEscape(node.id));
      break;
    }
    const parent = node.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter((c) => c.localName === node.localName);
      if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
    }
    parts.unshift(part);
    node = parent;
  }
  return parts.join(" > ");
}
function describeShort(el) {
  let s = el.localName;
  if (el.id) s += "#" + el.id;
  const cls = (typeof el.className === "string" ? el.className : "").trim().split(/\s+/).filter(Boolean);
  if (cls.length) s += "." + cls.slice(0, 3).join(".");
  return s;
}
function textOf(el) {
  const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  return t.slice(0, 300);
}
function nameOf(el) {
  return (el.getAttribute("aria-label") || el.getAttribute("title") || el.getAttribute("alt")
    || el.getAttribute("placeholder") || (el.labels && el.labels[0] && textOf(el.labels[0]))
    || textOf(el) || el.getAttribute("name") || el.value || "").toString().slice(0, 120);
}
function rectOf(el) {
  const r = el.getBoundingClientRect();
  return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) };
}
const STYLE_KEYS = ["display", "position", "color", "background-color", "font-size", "font-weight",
  "width", "height", "margin", "padding", "border", "z-index", "opacity", "visibility", "overflow"];
function stylesOf(el) {
  const cs = getComputedStyle(el);
  return STYLE_KEYS.map((k) => `${k}: ${cs.getPropertyValue(k)}`).join("; ");
}
function info(el) {
  const html = el.outerHTML || "";
  const attrs = {};
  for (const a of ["name", "type", "href", "src", "role", "aria-label", "placeholder", "value", "for", "data-testid"]) {
    const v = el.getAttribute && el.getAttribute(a);
    if (v != null) attrs[a] = String(v).slice(0, 200);
  }
  return {
    ref: refFor(el),
    label: describeShort(el),
    selector: uniqueSelector(el),
    text: textOf(el),
    attrs,
    rect: rectOf(el),
    styles: stylesOf(el),
    html: html.length > 2000 ? html.slice(0, 2000) + "…" : html,
  };
}
function resolve(args) {
  if (args.ref) {
    const w = refs.get(String(args.ref));
    const el = w && w.deref();
    if (!el || !el.isConnected) throw new Error(`ref ${args.ref} is gone (take a new snapshot)`);
    return el;
  }
  if (args.selector) {
    const el = document.querySelector(String(args.selector));
    if (!el) throw new Error(`no element matches ${args.selector}`);
    return el;
  }
  throw new Error("give ref or selector");
}
function visible(el) {
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return false;
  const cs = getComputedStyle(el);
  return cs.visibility !== "hidden" && cs.display !== "none" && cs.opacity !== "0";
}

/* ---- highlight overlay ---- */
let overlay = null;
let overlayLabel = null;
function ensureOverlay() {
  if (overlay && overlay.isConnected) return;
  overlay = document.createElement("div");
  overlay.setAttribute("data-claunch-overlay", "");
  overlay.style.cssText = "position:fixed;pointer-events:none;z-index:2147483647;border:2px solid #e8590c;"
    + "background:rgba(232,89,12,.12);border-radius:2px;display:none;transition:all .05s;box-sizing:border-box";
  overlayLabel = document.createElement("div");
  overlayLabel.style.cssText = "position:absolute;left:-2px;top:-20px;font:11px/16px system-ui,sans-serif;"
    + "background:#e8590c;color:#fff;padding:1px 5px;border-radius:2px;white-space:nowrap";
  overlay.appendChild(overlayLabel);
  (document.body || document.documentElement).appendChild(overlay);
}
function showOverlay(el, label) {
  ensureOverlay();
  const r = el.getBoundingClientRect();
  overlay.style.left = r.left + "px";
  overlay.style.top = r.top + "px";
  overlay.style.width = r.width + "px";
  overlay.style.height = r.height + "px";
  overlayLabel.textContent = label || describeShort(el);
  overlayLabel.style.top = r.top < 22 ? (r.height + 2) + "px" : "-20px";
  overlay.style.display = "block";
}
function hideOverlay() { if (overlay) overlay.style.display = "none"; }
let flashTimer = null;
function flash(el, label, ms = 1200) {
  showOverlay(el, label);
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { if (!picking) hideOverlay(); }, ms);
}

/* ---- picking (only when the parent asks) ---- */
let picking = false;
function onPickMove(e) {
  const el = e.target;
  if (el && el !== overlay && el.nodeType === 1) showOverlay(el, describeShort(el));
}
function stopEvent(e) { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation(); }
function onPickClick(e) {
  stopEvent(e);
  const el = e.target;
  endPick();
  if (el && el.nodeType === 1) {
    flash(el, describeShort(el), 1500);
    post({ type: "picked", element: info(el), ...here() });
  }
}
function onPickKey(e) {
  if (e.key === "Escape") { stopEvent(e); endPick(); post({ type: "pick-cancel" }); }
}
const PICK_SWALLOW = ["mousedown", "mouseup", "pointerdown", "pointerup", "auxclick", "dblclick", "contextmenu"];
function startPick() {
  if (picking) return;
  picking = true;
  document.addEventListener("mousemove", onPickMove, true);
  document.addEventListener("click", onPickClick, true);
  for (const t of PICK_SWALLOW) document.addEventListener(t, stopEvent, true);
  document.addEventListener("keydown", onPickKey, true);
  document.documentElement.style.cursor = "crosshair";
}
function endPick() {
  if (!picking) return;
  picking = false;
  document.removeEventListener("mousemove", onPickMove, true);
  document.removeEventListener("click", onPickClick, true);
  for (const t of PICK_SWALLOW) document.removeEventListener(t, stopEvent, true);
  document.removeEventListener("keydown", onPickKey, true);
  document.documentElement.style.cursor = "";
  hideOverlay();
}

/* ---- agent operations ---- */
const INTERACTIVE = "a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],"
  + "[role=tab],[role=menuitem],[role=option],[role=switch],[contenteditable=''],[contenteditable=true],[onclick],[tabindex]";
const LANDMARK = "h1,h2,h3,h4,label,img[alt],[role=dialog],[role=alert],nav,main,form,table";
function snapshot(args) {
  const max = Math.min(Number(args.max) || 200, 600);
  const lines = [];
  const seen = new Set();
  const nodes = document.querySelectorAll(INTERACTIVE + "," + LANDMARK);
  for (const el of nodes) {
    if (lines.length >= max) break;
    if (seen.has(el) || el.closest("[data-claunch-overlay]") || !visible(el)) continue;
    seen.add(el);
    const r = rectOf(el);
    const role = el.getAttribute("role") || el.localName + (el.type && el.localName === "input" ? `[${el.type}]` : "");
    let line = `${refFor(el)} ${role} ${JSON.stringify(nameOf(el))}`;
    if (el.disabled) line += " disabled";
    if (el.localName === "a" && el.getAttribute("href")) line += ` href=${el.getAttribute("href").slice(0, 120)}`;
    if ((el.localName === "input" || el.localName === "textarea" || el.localName === "select") && el.value)
      line += ` value=${JSON.stringify(String(el.value).slice(0, 80))}`;
    if (el.checked) line += " checked";
    line += ` @${r.x},${r.y} ${r.width}x${r.height}`;
    lines.push(line);
  }
  const text = (document.body ? document.body.innerText : "").replace(/\n{3,}/g, "\n\n");
  const textMax = Math.min(Number(args.text) || 3000, 20000);
  return {
    ...here(),
    viewport: { width: innerWidth, height: innerHeight, scrollX: Math.round(scrollX), scrollY: Math.round(scrollY),
      pageHeight: document.documentElement.scrollHeight },
    elements: lines.join("\n"),
    truncated: nodes.length > lines.length && lines.length >= max,
    text: text.length > textMax ? text.slice(0, textMax) + "…" : text,
    console_errors: consoleLog.filter((e) => e.level === "error").length,
  };
}
function center(el) {
  const r = el.getBoundingClientRect();
  return { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
}
function click(args) {
  const el = resolve(args);
  el.scrollIntoView({ block: "center", inline: "center" });
  const at = center(el);
  const opts = { bubbles: true, cancelable: true, composed: true, view: window, button: 0, ...at };
  el.dispatchEvent(new PointerEvent("pointerdown", opts));
  el.dispatchEvent(new MouseEvent("mousedown", opts));
  if (typeof el.focus === "function") el.focus({ preventScroll: true });
  el.dispatchEvent(new PointerEvent("pointerup", opts));
  el.dispatchEvent(new MouseEvent("mouseup", opts));
  el.click();
  flash(el, "agent: click " + describeShort(el));
  return { clicked: info(el), ...here() };
}
function setValue(el, value) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}
function key(el, k) {
  const opts = { key: k, code: k === "Enter" ? "Enter" : k, bubbles: true, cancelable: true };
  const down = el.dispatchEvent(new KeyboardEvent("keydown", opts));
  el.dispatchEvent(new KeyboardEvent("keypress", opts));
  el.dispatchEvent(new KeyboardEvent("keyup", opts));
  return down;
}
function type(args) {
  const el = resolve(args);
  el.scrollIntoView({ block: "center" });
  el.focus();
  const text = String(args.text ?? "");
  if (el.isContentEditable) {
    if (args.clear !== false) document.execCommand("selectAll", false, null);
    document.execCommand("insertText", false, text);
  } else if ("value" in el) {
    setValue(el, (args.clear === false ? el.value : "") + text);
  } else {
    throw new Error(`${describeShort(el)} does not take text`);
  }
  if (args.submit) {
    const proceed = key(el, "Enter");
    if (proceed && el.form) {
      if (typeof el.form.requestSubmit === "function") el.form.requestSubmit(); else el.form.submit();
    }
  }
  flash(el, "agent: type into " + describeShort(el));
  return { typed: info(el), ...here() };
}
function press(args) {
  const el = args.ref || args.selector ? resolve(args) : (document.activeElement || document.body);
  key(el, String(args.key || "Enter"));
  return { pressed: String(args.key || "Enter"), target: describeShort(el) };
}
function scroll(args) {
  if (args.ref || args.selector) {
    const el = resolve(args);
    el.scrollIntoView({ block: "center" });
    flash(el, "agent: scroll to " + describeShort(el));
  } else {
    window.scrollBy(Number(args.x) || 0, Number(args.y) || 0);
  }
  return { scrollX: Math.round(scrollX), scrollY: Math.round(scrollY) };
}
function query(args) {
  const all = Array.from(document.querySelectorAll(String(args.selector || "")));
  const max = Math.min(Number(args.max) || 20, 100);
  return { count: all.length, matches: all.slice(0, max).map(info) };
}
async function evaluate(args) {
  const code = String(args.code || "");
  // An expression first, then a function body (so `return x` works too).
  let fn;
  try { fn = new Function(`return (async () => (${code}))()`); }
  catch (_) { fn = new Function(`return (async () => { ${code} })()`); }
  const value = await fn();
  let out;
  try { out = JSON.parse(JSON.stringify(value === undefined ? null : value, safeReplacer())); }
  catch (_) { out = String(value); }
  return { result: out };
}
function readConsole(args) {
  const since = Number(args.since) || 0;
  const levels = args.level ? new Set(String(args.level).split(",")) : null;
  const rows = consoleLog.filter((e) => e.seq > since && (!levels || levels.has(e.level)));
  const out = { entries: rows.slice(-(Number(args.max) || 100)), last: consoleSeq };
  if (args.clear) consoleLog.length = 0;
  return out;
}
function rect(args) {
  if (!(args.ref || args.selector)) {
    return { rect: { x: 0, y: 0, width: innerWidth, height: innerHeight } };
  }
  const el = resolve(args);
  if (args.scroll !== false) el.scrollIntoView({ block: "center" });
  return { rect: rectOf(el), element: describeShort(el) };
}
function highlight(args) {
  const el = resolve(args);
  el.scrollIntoView({ block: "center" });
  flash(el, args.label || describeShort(el), Math.min(Number(args.ms) || 2000, 10000));
  return { highlighted: describeShort(el) };
}
const OPS = {
  snapshot, query, click, type, press, scroll, eval: evaluate, console: readConsole, rect, highlight,
  pick: () => { startPick(); return { picking: true }; },
  "pick-cancel": () => { endPick(); return { picking: false }; },
  hide: () => { hideOverlay(); return {}; },
  where: () => here(),
  back: () => { history.back(); return {}; },
  forward: () => { history.forward(); return {}; },
  reload: () => { setTimeout(() => location.reload(), 0); return {}; },
};

window.addEventListener("message", async (e) => {
  if (e.source !== window.parent || !PARENTS.has(e.origin)) return;
  const msg = e.data;
  if (!msg || msg.claunch !== "wd" || msg.type !== "cmd") return;
  parentOrigin = e.origin;
  const op = OPS[msg.op];
  try {
    if (!op) throw new Error(`unknown operation ${msg.op}`);
    const value = await op(msg.args || {});
    post({ type: "res", id: msg.id, ok: true, value });
  } catch (err) {
    post({ type: "res", id: msg.id, ok: false, error: String((err && err.message) || err) });
  }
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => announce("ready"));
} else {
  announce("ready");
}
window.addEventListener("load", () => announce("nav"));
