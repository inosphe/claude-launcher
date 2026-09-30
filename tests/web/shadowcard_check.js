/* Remote-shadow sessions in the page, run against the real functions from
   app.js (daemon/shadow.py is the host side).

   What has to hold: a shadow card is state only -- name, @machine, role,
   note, briefing, cflow position, the host's error -- with no control of any
   kind inside it; clicking it goes to the shadow's own address and never to
   #/s/<name>, which is the LOCAL session of that name; the mesh roster links
   a row as a shadow only on the server's word that it is remote, so this
   daemon's own member is never addressed as another's; the shadow list lives
   apart from sessionsCache, so no local control can ever find a remote row;
   the shadow terminal is built with stdin off and never given an onData
   handler; and the host's refusal stops the reconnect loop. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.search(new RegExp(`(^|\\n)(async )?function ${name}\\(`));
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* A DOM just big enough for the card: elements with classes, text,
   children, attributes and listeners. */
class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.className = "";
    this.textContent = "";
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.listeners = {};
    const self = this;
    this.classList = {
      add(c) { if (!self.has(c)) self.className = (self.className + " " + c).trim(); },
      remove(c) { self.className = self.className.split(" ").filter((x) => x !== c).join(" "); },
      toggle(c, on) { if (on) this.add(c); else this.remove(c); },
      contains(c) { return self.has(c); },
    };
  }
  has(c) { return this.className.split(" ").includes(c); }
  append(...ns) { this.children.push(...ns); }
  appendChild(n) { this.children.push(n); return n; }
  replaceChildren(...ns) { this.children = ns; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(k, fn) { (this.listeners[k] = this.listeners[k] || []).push(fn); }
  fire(k, ev) { for (const fn of this.listeners[k] || []) fn(ev || {}); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
  text() { return this.all().map((n) => n.textContent).join("|"); }
}
const document = { createElement: (t) => new Node(t) };
const byId = {};
const $ = (id) => byId[id] || null;

const location = { hash: "", protocol: "http:", host: "h" };
const sockets = [];
class FakeSocket {
  constructor(u) { this.url = u; sockets.push(this); }
  close() { this.closed = true; }
}
const terms = [];
class FakeTerminal {
  constructor(opts) { this.opts = opts; this.onDataCalls = 0; this.writes = []; terms.push(this); }
  open(host) { this.host = host; }
  onData() { this.onDataCalls++; }
  resize(c, r) { this.size = [c, r]; }
  reset() { this.wasReset = true; }
  write(d) { this.writes.push(d); }
  dispose() { this.disposed = true; }
}

const names = [
  "el", "shadowKey", "shadowHash", "shadowRow", "refreshShadows", "shadowStatus",
  "shadowCflowGated", "shadowCflowText", "renderShadowCard", "renderShadowRail",
  "renderShadowHead", "setShadowStatus", "openShadow", "connectShadow",
  "shadowFrame", "closeShadow", "parseHash", "meshRosterLink",
];
const pollLine = src.match(/^const SHADOW_POLL_MS = .+$/m);
if (!pollLine) throw new Error("cannot locate SHADOW_POLL_MS in app.js");

const ctx = {};
new Function(
  "exports", "document", "$", "location", "WebSocket", "Terminal", "api", "url",
  "sessionsCache", "showView", "setTimeout", "clearTimeout",
  [pollLine[0],
   "let shadowRows = [], shadowPolledAt = 0, shadowSig = '';",
   "let shadowOpen = null, shadowTerm = null, shadowSock = null, shadowTicket = 0, shadowRetry = null;",
   "let currentPage = 'home';",
   ...names.map(slice)].join("\n") + `
exports.render = renderShadowCard;
exports.renderRail = renderShadowRail;
exports.refresh = refreshShadows;
exports.parseHash = parseHash;
exports.rosterLink = meshRosterLink;
exports.open = (m, n) => { currentPage = 'shadow'; openShadow(m, n); };
exports.frame = shadowFrame;
exports.close = closeShadow;
exports.rows = () => shadowRows;
exports.state = () => ({ shadowOpen, shadowTerm, shadowSock });
`)(ctx, document, $, location, FakeSocket, FakeTerminal,
   async () => ({ ok: true, json: async () => ctx.nextDoc }),
   (p) => p, ctx.sessionsCache = new Map(), () => {},
   (fn) => { ctx.retry = fn; return 1; }, () => {});

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const row = {
  machine: "shared-daemon-kimyori", session: "s4",
  meshes: [{ mesh: "gds6", handle: "s4", role: "worker", roles: ["worker"], linked: true }],
  card: {
    session: "s4", handle: "s4", role: "worker", status: "busy", exited: false,
    note: "reviewing the parser",
    briefing: { one_line: "fixes the tokenizer", goal: "green sweep" },
    cflow: { workflow: "improv-worker", step_id: "review", status: "waiting_approval" },
  },
  error: null,
};

/* The card says what the host said, and nothing on it is a control. */
const card = ctx.render(row);
const text = card.text();
for (const want of ["s4", "@shared-daemon-kimyori", "worker", "reviewing the parser",
                    "fixes the tokenizer", "improv-worker", "review", "waiting_approval"]) {
  check(`the card shows ${want}`, text.includes(want), true);
}
check("no control anywhere inside the card",
      card.all().filter((n) => ["BUTTON", "INPUT", "TEXTAREA", "SELECT", "A", "FORM"]
        .includes(n.tagName)).map((n) => n.tagName), []);
check("a gated position is marked like the local rail's",
      card.all().some((n) => n.has("shadow-cflow") && n.has("gated")), true);
check("the card is addressed by machine and name",
      [card.dataset.machine, card.dataset.session], ["shared-daemon-kimyori", "s4"]);

/* Clicking goes to the shadow, never to the local #/s/ of the same name. */
card.fire("click");
check("click opens the shadow view", location.hash, "#/r/shared-daemon-kimyori/s4");
location.hash = "";
card.fire("keydown", { key: "Enter", preventDefault() {} });
check("Enter does the same", location.hash, "#/r/shared-daemon-kimyori/s4");

/* An unreachable host: the row says why, and the dot does not claim dead. */
const lost = ctx.render({ ...row, card: null, error: "daemon did not answer" });
check("an unanswered row shows the host's error",
      lost.text().includes("daemon did not answer"), true);
check("and its dot reads unknown",
      lost.all().some((n) => n.has("shadow-dot-unknown")), true);

/* The address. */
check("#/r/<machine>/<name> is the shadow page",
      ctx.parseHash("#/r/shared-daemon-kimyori/s4"),
      { page: "shadow", machine: "shared-daemon-kimyori", name: "s4" });
check("#/s/<name> is still the local terminal",
      ctx.parseHash("#/s/s4"), { page: "terminal", name: "s4" });

/* The mesh roster's link. The page's own idea of locality reads the relay
   name, which is blank on a daemon with no relay; the server's `local` is
   the deciding word for the shadow link. */
check("this daemon's member, even when the page misreads it, opens its terminal",
      ctx.rosterLink({ session: "s25", machine: "shared-daemon-main", local: true },
                     false, "shared-daemon-main"),
      { hash: "#/s/s25", title: "attach this session's terminal" });
check("this daemon's member, read right, opens its terminal",
      ctx.rosterLink({ session: "s25", machine: "", local: true }, true, ""),
      { hash: "#/s/s25", title: "attach this session's terminal" });
check("another daemon's member opens its shadow",
      ctx.rosterLink({ session: "s4", machine: "shared-daemon-kimyori", local: false },
                     false, "shared-daemon-kimyori"),
      { hash: "#/r/shared-daemon-kimyori/s4", title: "view this remote session (read-only)" });
check("a row without the field is not guessed remote",
      ctx.rosterLink({ session: "s4", machine: "shared-daemon-kimyori" },
                     false, "shared-daemon-kimyori"), null);
check("and keeps the page's guess for its terminal link",
      ctx.rosterLink({ session: "s4", machine: "" }, true, ""),
      { hash: "#/s/s4", title: "attach this session's terminal" });

/* The list lives apart from sessionsCache. */
byId["shadow-rail"] = new Node("section");
byId["shadow-list"] = new Node("ul");
byId["shadow-rail-count"] = new Node("span");
(async () => {
  ctx.nextDoc = { shadows: [row] };
  await ctx.refresh(true);
  check("the shadow list holds the remote row", ctx.rows().length, 1);
  check("sessionsCache is untouched", ctx.sessionsCache.size, 0);
  check("the rail section shows, counted",
        [byId["shadow-rail"].has("hidden"), byId["shadow-rail-count"].textContent],
        [false, "1"]);
  check("grouped under its daemon",
        byId["shadow-list"].children[0].children[0].textContent, "shared-daemon-kimyori");
  ctx.nextDoc = { shadows: [] };
  await ctx.refresh(true);
  check("an empty list hides the section", byId["shadow-rail"].has("hidden"), true);

  /* The shadow terminal: stdin off, no onData, sized by the host. */
  byId["shadow-term"] = new Node("div");
  byId["shadow-head"] = new Node("div");
  byId["shadow-status"] = new Node("p");
  ctx.open("shared-daemon-kimyori", "s4");
  const t = terms[terms.length - 1];
  check("the terminal is built with stdin off", t.opts.disableStdin, true);
  check("and nothing listens for its input", t.onDataCalls, 0);
  check("one socket, to the shadow route",
        sockets[sockets.length - 1].url, "ws://h/api/shadows/shared-daemon-kimyori/s4/ws");
  ctx.frame({ type: "init", cols: 120, rows: 40, exited: false });
  check("init sizes the terminal to the host's grid", t.size, [120, 40]);
  check("a refusal ends the reconnect loop",
        ctx.frame({ type: "shadow_error", error: "not a member" }), true);
  check("and says why", byId["shadow-status"].textContent, "not a member");
  check("an unknown frame is ignored", ctx.frame({ type: "notice", text: "x" }), false);
  ctx.close();
  check("closing disposes the terminal and the socket",
        [t.disposed, sockets[sockets.length - 1].closed, ctx.state().shadowOpen], [true, true, null]);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("shadowcard: ok");
})().catch((err) => { console.error(err); process.exit(1); });
