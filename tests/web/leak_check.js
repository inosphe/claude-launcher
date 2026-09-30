/* What a dashboard left open all day accumulates.

   Every other harness here slices one function out of app.js and drives it.
   This one does the opposite: it boots the WHOLE shipped file against a stub
   browser built from the shipped index.html, then runs the page the way an
   unattended tab runs it — the 2s poll, over and over — and measures what is
   bigger afterwards.

   That is the only way to see this class of bug. A leak is not a wrong
   answer in any single tick; it is the same tick being right five thousand
   times while something behind it grows. So the check is a shape, not a
   value: warm the page up, take a census, run a long stretch more, take
   another, and hold the two to the same number. Live DOM nodes, live event
   listeners, live sockets, live timers, and the module-level caches the page
   keys by session name — each one is a place where "per tick" would show up
   as "per day".

   The stub world is deliberately dumb: nodes that remember their children
   and their listeners, a selector engine that understands only the handful
   of patterns app.js actually writes, and sockets/terminals that do nothing
   but count themselves. Nothing here simulates a browser well enough to
   catch a rendering bug — that is what the other harnesses are for. It is
   only good enough to answer "does this grow?", which is the question. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = path.join(
  __dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* ---- census counters --------------------------------------------------- */
/* Live, not lifetime: a listener on a node that has since been thrown away
   is not a leak, and counting registrations would flag every rebuild. What
   matters is what is still hanging off something the page can reach. */
const live = { sockets: 0, terminals: 0, timers: 0 };
let createdNodes = 0;

/* ---- stub DOM ---------------------------------------------------------- */
/* A style object that answers to both spellings the page uses: assignment
   (`el.style.display = "none"`) and the CSS-variable API, which is how the
   layout writes --app-h. */
function makeStyle() {
  const props = Object.create(null);
  const s = {
    setProperty: (k, v) => { props[k] = String(v); },
    getPropertyValue: (k) => props[k] || "",
    removeProperty: (k) => { delete props[k]; },
    _props: props,
  };
  return new Proxy(s, {
    get: (t, k) => (k in t ? t[k] : (props[k] === undefined ? "" : props[k])),
    set: (t, k, v) => { props[k] = v; return true; },
  });
}

function makeEl(tag, ns) {
  createdNodes += 1;
  const n = {
    tagName: String(tag).toUpperCase(),
    localName: String(tag).toLowerCase(),
    ns: ns || null,
    childNodes: [],
    parentNode: null,
    _attrs: Object.create(null),
    _listeners: [],
    _classes: new Set(),
    dataset: Object.create(null),
    style: makeStyle(),
    _text: "",
    disabled: false,
    value: "",
    checked: false,
    type: "",
    title: "",
    href: "",
    hidden: false,
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    offsetWidth: 0,
    offsetHeight: 0,
    options: [],
    selectedIndex: -1,
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => c && n._classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n._classes.delete(c)),
    toggle: (c, on) => (on === undefined
      ? (n._classes.has(c) ? n._classes.delete(c) : n._classes.add(c))
      : (on ? n._classes.add(c) : n._classes.delete(c))),
    contains: (c) => n._classes.has(c),
  };
  Object.defineProperty(n, "className", {
    get: () => [...n._classes].join(" "),
    set: (v) => {
      n._classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
  });
  Object.defineProperty(n, "textContent", {
    get: () => n._text,
    set: (v) => { detachAll(n); n._text = v === null || v === undefined ? "" : String(v); },
  });
  Object.defineProperty(n, "innerHTML", {
    get: () => n._text,
    set: (v) => { detachAll(n); n._text = String(v); },
  });
  Object.defineProperty(n, "firstChild", { get: () => n.childNodes[0] || null });
  Object.defineProperty(n, "children", { get: () => n.childNodes.slice() });
  n.appendChild = (c) => {
    if (!c) return c;
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = n;
    n.childNodes.push(c);
    return c;
  };
  n.append = (...cs) => cs.forEach((c) => n.appendChild(c));
  n.prepend = (...cs) => cs.reverse().forEach((c) => {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = n;
    n.childNodes.unshift(c);
  });
  n.insertBefore = (c, ref) => {
    const i = ref ? n.childNodes.indexOf(ref) : -1;
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = n;
    if (i < 0) n.childNodes.push(c);
    else n.childNodes.splice(i, 0, c);
    return c;
  };
  n.removeChild = (c) => {
    const i = n.childNodes.indexOf(c);
    if (i >= 0) n.childNodes.splice(i, 1);
    if (c) c.parentNode = null;
    return c;
  };
  n.remove = () => { if (n.parentNode) n.parentNode.removeChild(n); };
  n.replaceChildren = (...cs) => { detachAll(n); cs.forEach((c) => n.appendChild(c)); };
  n.setAttribute = (k, v) => {
    n._attrs[k] = String(v);
    if (k === "class") n.className = v;
    if (k === "id") n.id = String(v);
  };
  n.getAttribute = (k) => (k in n._attrs ? n._attrs[k] : null);
  n.removeAttribute = (k) => { delete n._attrs[k]; };
  n.hasAttribute = (k) => k in n._attrs;
  n.addEventListener = (type, fn) => { n._listeners.push({ type, fn }); };
  n.removeEventListener = (type, fn) => {
    const i = n._listeners.findIndex((l) => l.type === type && l.fn === fn);
    if (i >= 0) n._listeners.splice(i, 1);
  };
  n.dispatchEvent = () => true;
  n.getBoundingClientRect = () => ({
    x: 0, y: 0, top: 0, left: 0, right: 100, bottom: 100, width: 100, height: 100,
  });
  n.focus = () => {};
  n.blur = () => {};
  n.click = () => {};
  n.scrollIntoView = () => {};
  n.contains = (other) => {
    for (let p = other; p; p = p.parentNode) if (p === n) return true;
    return false;
  };
  n.closest = (sel) => {
    for (let p = n; p; p = p.parentNode) if (matches(p, sel)) return p;
    return null;
  };
  n.querySelector = (sel) => query(n, sel)[0] || null;
  n.querySelectorAll = (sel) => query(n, sel);
  return n;
}

/* Cutting a subtree loose. The point of doing it explicitly rather than just
   dropping the array is the census: a node whose parent forgot it is no
   longer live, and neither are the listeners on it. */
function detachAll(n) {
  for (const c of n.childNodes) c.parentNode = null;
  n.childNodes = [];
}

/* ---- a very small selector engine -------------------------------------- */
/* Only what app.js writes: comma lists, descendant chains, and per-step
   `tag`, `#id`, `.class` and `[attr]` / `[attr=value]`. Anything richer
   would be a second implementation of a browser, and the harness would then
   be testing itself. */
function parseStep(step) {
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const re = /(^[a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=["']?([^\]"']*)["']?)?\]/g;
  let m;
  while ((m = re.exec(step))) {
    if (m[1]) out.tag = m[1].toLowerCase();
    else if (m[2]) out.id = m[2];
    else if (m[3]) out.classes.push(m[3]);
    else if (m[4]) out.attrs.push([m[4], m[5]]);
  }
  return out;
}

function matchStep(node, st) {
  if (st.tag && node.localName !== st.tag) return false;
  if (st.id && node.id !== st.id) return false;
  for (const c of st.classes) if (!node._classes.has(c)) return false;
  for (const [k, v] of st.attrs) {
    let have;
    if (k.startsWith("data-")) {
      const key = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      have = node.dataset[key];
    } else if (k === "name") {
      have = node._attrs.name !== undefined ? node._attrs.name : node.name;
    } else {
      have = node._attrs[k] !== undefined ? node._attrs[k] : node[k];
    }
    if (have === undefined || have === null) return false;
    if (v !== undefined && String(have) !== String(v)) return false;
  }
  return true;
}

function matches(node, sel) {
  return sel.split(",").some((one) => {
    const steps = one.trim().split(/\s+/).map(parseStep);
    const last = steps[steps.length - 1];
    if (!matchStep(node, last)) return false;
    let i = steps.length - 2;
    let p = node.parentNode;
    while (i >= 0) {
      if (!p) return false;
      if (matchStep(p, steps[i])) i -= 1;
      p = p.parentNode;
    }
    return true;
  });
}

function walk(root, fn) {
  for (const c of root.childNodes) { fn(c); walk(c, fn); }
}

function query(root, sel) {
  const out = [];
  walk(root, (n) => { if (matches(n, sel)) out.push(n); });
  return out;
}

/* ---- the document, built from the shipped index.html -------------------- */
/* The real markup, crudely parsed, rather than a hand-written stub. app.js
   reaches the page by id for the most part, but not only: the spawn form is
   found by `#new-session select[name=parent]` and friends, so the tags and
   the name attributes have to be there too. Parsing the shipped file is also
   what stops this harness from drifting — markup renamed in index.html and
   not here would leave $() holding null, which is exactly what it would do
   in the browser. */
const VOID_TAGS = new Set([
  "area", "base", "br", "col", "embed", "hr", "img", "input",
  "link", "meta", "param", "source", "track", "wbr",
]);

function parseHTML(text) {
  const root = makeEl("body");
  const ids = new Map();
  const stack = [root];
  const re = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][\w-]*)((?:\s+[^>]*?)?)(\/?)>/g;
  let m;
  while ((m = re.exec(text))) {
    if (m[0].startsWith("<!--")) continue;
    const [, closing, tag, rawAttrs, selfClose] = m;
    const name = tag.toLowerCase();
    if (closing) {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].localName === name) { stack.length = i; break; }
      }
      continue;
    }
    const node = makeEl(name);
    for (const a of rawAttrs.matchAll(/([\w:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g)) {
      const key = a[1];
      const val = a[2] !== undefined ? a[2]
        : a[3] !== undefined ? a[3]
          : a[4] !== undefined ? a[4] : "";
      node._attrs[key] = val;
      if (key === "id") { node.id = val; ids.set(val, node); }
      else if (key === "class") node.className = val;
      else if (key === "name") node.name = val;
      else if (key === "type") node.type = val;
      else if (key === "value") node.value = val;
      else if (key.startsWith("data-")) {
        node.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = val;
      }
    }
    stack[stack.length - 1].appendChild(node);
    if (!selfClose && !VOID_TAGS.has(name) && name !== "script") stack.push(node);
  }
  return { root, ids };
}

const parsed = parseHTML(html);
const docRoot = parsed.root;
const byId = parsed.ids;

/* A <form>'s named controls reachable as `form.<name>`. app.js uses that
   shorthand throughout the spawn wizard, and it is the one piece of the DOM
   API that is not a method call and so cannot be stubbed on the prototype.
   A name shared by several controls — a radio group like worktree_mode —
   comes back as a RadioNodeList in a browser: iterable, and its `value` is
   the checked member's. Binding only the first node would hand app.js a
   single element where it writes `for (const radio of f.worktree_mode)`. */
for (const form of query(docRoot, "form")) {
  const named = new Map();
  walk(form, (node) => {
    const name = node._attrs.name;
    if (!name) return;
    if (!named.has(name)) named.set(name, []);
    named.get(name).push(node);
  });
  for (const [name, members] of named) {
    if (name in form) continue;
    if (members.length === 1) {
      const node = members[0];
      Object.defineProperty(form, name, { get: () => node, configurable: true });
    } else {
      const group = members.slice();
      Object.defineProperty(group, "value", {
        get: () => {
          const on = group.find((n) => n.checked);
          return on ? on.value : "";
        },
        configurable: true,
      });
      Object.defineProperty(form, name, { get: () => group, configurable: true });
    }
  }
}

const document = {
  body: docRoot,
  documentElement: makeEl("html"),
  hidden: false,
  hasFocus: () => true,
  title: "",
  cookie: "",
  createElement: (t) => makeEl(t),
  createElementNS: (ns, t) => makeEl(t, ns),
  createTextNode: (t) => {
    const n = makeEl("#text");
    n._text = String(t);
    return n;
  },
  createDocumentFragment: () => makeEl("#fragment"),
  getElementById: (id) => byId.get(id) || null,
  querySelector: (sel) => query(docRoot, sel)[0] || null,
  querySelectorAll: (sel) => query(docRoot, sel),
  addEventListener: (type, fn) => docListeners.push({ type, fn }),
  removeEventListener: (type, fn) => {
    const i = docListeners.findIndex((l) => l.type === type && l.fn === fn);
    if (i >= 0) docListeners.splice(i, 1);
  },
  activeElement: null,
};
const docListeners = [];
const winListeners = [];

/* ---- stub browser ------------------------------------------------------ */
const localStore = new Map();
const localStorage = {
  getItem: (k) => (localStore.has(k) ? localStore.get(k) : null),
  setItem: (k, v) => localStore.set(k, String(v)),
  removeItem: (k) => localStore.delete(k),
  clear: () => localStore.clear(),
};
localStore.set("claunch_token:/", "tok");

let timerSeq = 0;
let clockNow = Date.now();
class ClockDate extends Date {
  static now() { return clockNow; }
}
const timers = new Map();
function setIntervalStub(fn, ms) {
  timerSeq += 1;
  timers.set(timerSeq, { fn, ms, kind: "interval" });
  live.timers = timers.size;
  return timerSeq;
}
function setTimeoutStub(fn, ms) {
  timerSeq += 1;
  timers.set(timerSeq, { fn, ms, kind: "timeout" });
  live.timers = timers.size;
  return timerSeq;
}
function clearTimerStub(id) {
  timers.delete(id);
  live.timers = timers.size;
}

/* Sockets and terminals count themselves in and out: those are the two
   objects on this page big enough that one left behind per session switch
   would matter, and both have an explicit close/dispose the page is
   supposed to call. */
class FakeSocket {
  constructor() {
    this.readyState = 1;
    this.binaryType = "";
    this.onopen = null;
    this.onclose = null;
    this.onmessage = null;
    this.closed = false;
    live.sockets += 1;
  }
  send() {}
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    live.sockets -= 1;
  }
}
FakeSocket.OPEN = 1;
FakeSocket.CLOSED = 3;

class FakeTerminal {
  constructor(opts) {
    this.opts = opts || {};
    this.options = this.opts;
    this.cols = 80;
    this.rows = 24;
    this.element = makeEl("div");
    this.disposed = false;
    live.terminals += 1;
  }
  loadAddon() {}
  open(parent) { parent.appendChild(this.element); }
  onData() {}
  onResize() {}
  attachCustomWheelEventHandler() {}
  attachCustomKeyEventHandler() {}
  write() {}
  resize(c, r) { this.cols = c; this.rows = r; }
  focus() {}
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    live.terminals -= 1;
  }
}

/* ---- the daemon this page thinks it is talking to ----------------------- */
/* Enough of an answer for each polled route that the render paths run for
   real. The session list is the interesting one: it is what the rail is
   rebuilt from every two seconds. */
let SESSIONS = [];
let LLM_ON = false;
// Which incarnation the summariser is describing. A name can be reused, so
// this is how the check tells a fresh briefing from the dead session's one.
let BRIEF_TAG = "first";
function sessionPayload(n) {
  return {
    name: n, status: "idle", pid: 1000 + n.length, cwd: "/tmp",
    harness: "claude", profile: "nc", cols: 80, rows: 24,
    parent: null, role: "worker",
  };
}

let fetchCount = 0;
function answer(pathname) {
  if (pathname.endsWith("/api/health")) {
    return { status: "ok", version: "0.0.0-test", boot_id: "boot-1" };
  }
  if (pathname.includes("/briefing")) {
    const who = /sessions\/([^/]+)\/briefing/.exec(pathname);
    return {
      "one-line-job-description": `what ${who ? who[1] : "?"} is for`,
      goal: BRIEF_TAG, now: "n", progress: "p", state: "working",
    };
  }
  if (pathname.includes("/api/sessions?view=rail")) {
    return { sessions: SESSIONS.map(sessionPayload), llm_configured: LLM_ON };
  }
  if (pathname.endsWith("/api/daemon")) {
    return {
      version: "0.0.0-test", boot_id: "boot-1", sessions: SESSIONS.length,
      relay: { configured: false, connected: false, name: null },
      profiles: [], harnesses: [], workspaces: [],
    };
  }
  if (pathname.includes("/api/meshes")) return { meshes: [] };
  if (pathname.includes("/api/mesh")) return { meshes: [], members: [], edges: [] };
  if (pathname.includes("/api/cflow")) return { runs: [], workflows: [] };
  if (pathname.includes("/api/workspaces")) return { workspaces: [] };
  if (pathname.includes("/queued")) return { queued: [], holder: null };
  if (pathname.includes("/api/profiles")) return { profiles: [] };
  if (pathname.includes("/api/harnesses")) return { harnesses: [] };
  if (pathname.includes("/api/roles")) return { roles: [] };
  return {};
}

async function fetchStub(input) {
  fetchCount += 1;
  const pathname = String(input);
  const body = answer(pathname);
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

/* ---- boot app.js in a context we can read back -------------------------- */
const sandbox = {
  console,
  document,
  localStorage,
  fetch: fetchStub,
  WebSocket: FakeSocket,
  Terminal: FakeTerminal,
  FitAddon: { FitAddon: class { fit() {} proposeDimensions() { return { cols: 80, rows: 24 }; } } },
  Option: function Option(text, value) {
    const n = makeEl("option");
    n.textContent = text === undefined ? "" : String(text);
    n.value = value === undefined ? "" : String(value);
    n._attrs.value = n.value;
    return n;
  },
  TextEncoder,
  TextDecoder,
  URL,
  Promise,
  JSON,
  Math,
  Date: ClockDate,
  Number,
  String,
  Object,
  Array,
  Set,
  Map,
  Error,
  isNaN,
  parseInt,
  parseFloat,
  encodeURIComponent,
  decodeURIComponent,
  setInterval: setIntervalStub,
  setTimeout: setTimeoutStub,
  clearInterval: clearTimerStub,
  clearTimeout: clearTimerStub,
  queueMicrotask,
  location: {
    protocol: "http:", host: "127.0.0.1:8787", hostname: "127.0.0.1",
    pathname: "/", hash: "", search: "", href: "http://127.0.0.1:8787/",
    reload: () => {},
  },
  // app.js reads `?embed=1` once at load through URLSearchParams; the other
  // check scripts define no `location` at all, so only this sandbox — which
  // does, to exercise attach/detach routing — has to carry the query API.
  URLSearchParams,
  history: { replaceState: () => {}, pushState: () => {} },
  navigator: { userAgent: "node", clipboard: { writeText: async () => {} } },
  matchMedia: () => ({
    matches: false,
    addEventListener: () => {},
    removeEventListener: () => {},
  }),
  visualViewport: null,
  innerWidth: 1400,
  innerHeight: 900,
  devicePixelRatio: 1,
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  requestAnimationFrame: (fn) => setTimeoutStub(fn, 0),
  alert: () => {},
  confirm: () => true,
  prompt: () => null,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.addEventListener = (type, fn) => winListeners.push({ type, fn });
sandbox.window.removeEventListener = (type, fn) => {
  const i = winListeners.findIndex((l) => l.type === type && l.fn === fn);
  if (i >= 0) winListeners.splice(i, 1);
};
sandbox.addEventListener = sandbox.window.addEventListener;
sandbox.removeEventListener = sandbox.window.removeEventListener;

const ctx = vm.createContext(sandbox);
try {
  vm.runInContext(src, ctx, { filename: "app.js" });
} catch (e) {
  console.log(`FAIL boot — app.js did not evaluate: ${e && e.stack}`);
  process.exit(1);
}

const read = (expr) => vm.runInContext(expr, ctx);
const flush = () => new Promise((r) => setImmediate(r));

/* ---- census ------------------------------------------------------------ */
function nodes() {
  let n = 0;
  walk(docRoot, () => { n += 1; });
  return n;
}

function listeners() {
  let n = docListeners.length + winListeners.length;
  walk(docRoot, (el) => { n += el._listeners.length; });
  return n;
}

function census() {
  return {
    rows: query(docRoot, "#session-list li").length,
    nodes: nodes(),
    listeners: listeners(),
    createdNodes,
    sockets: live.sockets,
    terminals: live.terminals,
    timers: live.timers,
    keptTerms: read("keptTerms.size"),
    briefingCache: read("briefingCache.size"),
    briefingOpen: read("briefingOpen.size"),
    sessLayouts: Object.keys(
      JSON.parse(localStore.get("claunch_sesslayout:/") || "{}")).length,
  };
}

/* Fire every scheduled one-shot, oldest first. The page schedules its
   reconnect through setTimeout, so an outage cannot be played out without
   this; intervals are left alone (the poll is driven explicitly). */
async function runTimeouts() {
  for (const [id, t] of [...timers]) {
    if (t.kind !== "timeout") continue;
    clearTimerStub(id);
    await t.fn();
    await flush();
  }
}

async function ticks(n) {
  for (let i = 0; i < n; i++) {
    await vm.runInContext("pollTick()", ctx);
    await flush();
  }
}

/* ------------------------------------------------------------------------ */
/* the check                                                                */
/* ------------------------------------------------------------------------ */
async function main() {
  SESSIONS = ["s1", "s2", "s3", "s4", "s5"];
  await flush();
  await flush();

  await ticks(20);          // warm: first render, caches filled
  const warm = census();
  // A census of a page that never rendered would pass every growth check by
  // measuring nothing. The rail is the poll's own output, so it is the proof
  // that the stub world is real enough for the rest of these numbers to mean
  // something.
  check("the poll actually renders the rail", warm.rows === SESSIONS.length,
        { rows: warm.rows, sessions: SESSIONS.length });
  await ticks(400);         // and then a stretch of the same nothing
  const later = census();

  for (const key of Object.keys(warm)) {
    check(
      `idle poll does not grow ${key}`,
      later[key] <= warm[key],
      { warm: warm[key], later: later[key] },
    );
  }
  console.log(`idle warm =${JSON.stringify(warm)}`);
  console.log(`idle later=${JSON.stringify(later)}`);

  /* ---- 2. walking between terminals -------------------------------------
     The expensive objects on this page are the xterm instances and their
     sockets, and the keep-alive cache exists to hold a few of them open on
     purpose. "On purpose" is the whole claim being checked: the cache has a
     ceiling, so hopping between forty sessions must cost the same as hopping
     between four. */
  SESSIONS = Array.from({ length: 40 }, (_, i) => `t${i}`);
  await ticks(1);
  for (const name of SESSIONS) {
    await vm.runInContext(`attach(${JSON.stringify(name)})`, ctx);
    await flush();
  }
  const hopped = census();
  const cap = read("TERM_CACHE_MAX");
  check("walking 40 terminals keeps only the cache's worth of xterms",
        hopped.terminals <= cap, { terminals: hopped.terminals, cap });
  check("walking 40 terminals keeps only the cache's worth of sockets",
        hopped.sockets <= cap, { sockets: hopped.sockets, cap });
  check("the keep-alive cache stays inside its ceiling",
        hopped.keptTerms <= cap - 1, { kept: hopped.keptTerms, cap });
  console.log(`hop       =${JSON.stringify(hopped)}`);

  /* ---- 3. a day of sessions coming and going ----------------------------
     The rail is not a fixed list. Sessions are spawned and killed all day,
     and a dashboard that has been up since morning has watched hundreds of
     names appear and go away for good. Anything the page keys by session
     NAME therefore has to be swept when the name stops existing, or it is a
     per-session-ever cache pretending to be a per-session one — which is
     both a slow leak and, when a respawn reuses a name, a card showing the
     previous occupant's work.

     refreshSessions already sweeps the terminal keep-alive for exactly this
     reason; the point of this scenario is that it is the only sweep, and
     every other name-keyed store has to be held to the same rule. */
  LLM_ON = true;
  await vm.runInContext("detach()", ctx);
  const GENERATIONS = 60;
  for (let g = 0; g < GENERATIONS; g++) {
    const name = `gen${g}`;
    SESSIONS = [name];
    await ticks(1);
    // The reader folds the new session's card open to see what it is for...
    await vm.runInContext(`toggleBriefing(${JSON.stringify(name)})`, ctx);
    await flush();
    // ...and it is killed while still folded open, as most of them are.
  }
  SESSIONS = ["s1"];
  await ticks(2);
  const churned = census();
  console.log(`churn     =${JSON.stringify(churned)}`);

  check(
    "briefings of sessions that no longer exist are dropped",
    churned.briefingCache <= SESSIONS.length,
    { cached: churned.briefingCache, alive: SESSIONS.length, generations: GENERATIONS },
  );
  check(
    "folded-open marks for sessions that no longer exist are dropped",
    churned.briefingOpen <= SESSIONS.length,
    { open: churned.briefingOpen, alive: SESSIONS.length, generations: GENERATIONS },
  );
  check(
    "the rail is not carrying rows for sessions that are gone",
    churned.rows === SESSIONS.length, { rows: churned.rows },
  );

  /* ---- 3b. the same name, a different session ---------------------------
     The other half of what an unswept name-keyed cache costs. `claunch
     respawn` puts a NEW session behind an OLD name, which is why the link
     already re-checks pid and boot id rather than trusting the name. A
     briefing that outlived its session has no such test: left in the cache
     it is served under the new session's name and reads as a summary of
     work this session never did. Memory is the cheap half of this bug. */
  BRIEF_TAG = "first";
  SESSIONS = ["reused"];
  await ticks(1);
  await vm.runInContext('toggleBriefing("reused")', ctx);
  await flush();
  check("the first incarnation's briefing is what is cached",
        read('(briefingCache.get("reused")||{}).data.goal') === "first",
        { got: read('JSON.stringify(briefingCache.get("reused"))') });

  SESSIONS = [];                 // killed
  await ticks(1);
  BRIEF_TAG = "second";
  SESSIONS = ["reused"];         // respawned under the same name
  await ticks(1);
  check("a respawn under the same name does not inherit the dead session's briefing",
        !read('briefingCache.has("reused")'),
        { cached: read('JSON.stringify(briefingCache.get("reused"))') });
  await vm.runInContext('toggleBriefing("reused")', ctx);
  await flush();
  check("and folding it open now summarises the session that is actually there",
        read('(briefingCache.get("reused")||{}).data.goal') === "second",
        { got: read('JSON.stringify(briefingCache.get("reused"))') });

  /* ---- 4. an outage every few minutes, all day --------------------------
     A laptop that sleeps, a phone that loses its tunnel, a daemon that is
     restarted: the terminal's link is the one thing on this page that
     rebuilds itself, and it rebuilds a WebSocket each time. A hundred of
     those in a day is ordinary; a hundred sockets still open is not. */
  LLM_ON = false;
  SESSIONS = ["s1"];
  await ticks(1);
  await vm.runInContext('attach("s1")', ctx);
  await flush();
  const beforeFlaps = census();
  let flaps = 0;
  for (let i = 0; i < 100; i++) {
    const sock = read("ws");
    if (!sock) break;
    // This census models separate outages minutes apart, with a healthy
    // initialized connection in between. Short-lived opens are bounded by
    // reconnect_check.js instead of receiving a fresh budget every time.
    if (sock.onmessage) sock.onmessage({ data: JSON.stringify({
      type: "init", cols: 80, rows: 24, status: "idle", pid: 4242,
    }) });
    clockNow += 60000;
    sock.close();          // the browser's side of a dropped connection
    if (sock.onclose) sock.onclose();
    await flush();
    await runTimeouts();   // the backoff's scheduled retry
    const back = read("ws");
    // The next connection opens now and receives init on the next pass.
    if (back && back.onopen) back.onopen();
    await flush();
    flaps += 1;
  }
  const flapped = census();
  // Same trap as the rail census: a loop that stopped after one outage would
  // pass the socket count by never having opened a second one.
  check("the outage loop actually reconnected a hundred times", flaps === 100,
        { flaps, linkState: read("linkState") });
  console.log(`flap      =${JSON.stringify(flapped)}`);
  check("a hundred reconnects leave one socket, not a hundred",
        flapped.sockets <= beforeFlaps.sockets,
        { before: beforeFlaps.sockets, after: flapped.sockets });
  check("a hundred reconnects leave one xterm, not a hundred",
        flapped.terminals <= beforeFlaps.terminals,
        { before: beforeFlaps.terminals, after: flapped.terminals });
  check("a hundred reconnects do not stack up retry timers",
        flapped.timers <= beforeFlaps.timers + 1,
        { before: beforeFlaps.timers, after: flapped.timers });

  console.log(`fetches=${fetchCount}`);

  if (failures) {
    console.log(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("leak_check: ok");
}

main().catch((e) => {
  console.log(`FAIL harness — ${e && e.stack}`);
  process.exit(1);
});
