/* Run the real renderTopology against a stub DOM and inspect what it built.
   Complements layout_check.js: that one checks the maths, this one checks
   that the drawing code actually assembles the SVG it is supposed to. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

const a = src.indexOf("const RING = {");
const b = src.indexOf("/* The send/add forms must survive the 2s poll");
if (a < 0 || b <= a) throw new Error("cannot locate the topology section");
const code = src.slice(a, b);

/* ---- stub DOM --------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", classes: new Set(), handlers: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    closest() { return null; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    contains: (c) => n.classes.has(c),
  };
  return n;
}
const document = {
  createElementNS: (_ns, tag) => node(tag),
  createElement: (tag) => node(tag),
};
const window = { addEventListener() {} };

function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const refreshMeshView = () => {};
const refreshMeshList = () => {};
/* The requests the panel sends are half of what it does, so they are
   recorded rather than swallowed: a switch that draws correctly and PATCHes
   the wrong pair is the failure worth catching. */
const sent = [];
const api = async (path, opts) => {
  sent.push({ path, ...(opts || {}) });
  return { ok: true, json: async () => ({}) };
};
const confirm = () => true;
const alert = () => {};
function meshDotClass(r) {
  if (r === "idle") return "idle";
  if (r === "busy" || r === "starting") return "busy";
  if (r === "remote-connected") return "starting";
  return "exited";
}

const ctx = {};
new Function(
  "exports", "document", "window", "el", "refreshMeshView", "refreshMeshList",
  "api", "confirm", "alert", "meshDotClass",
  code +
  "\nObject.assign(exports, {renderTopology, renderWiring," +
  " setFocus: (v) => { meshFocus = v; }, getFocus: () => meshFocus});"
)(ctx, document, window, el, refreshMeshView, refreshMeshList, api, confirm,
  alert, meshDotClass);

/* ---- walk helpers ------------------------------------------------------ */
function all(root, pred, out = []) {
  if (pred(root)) out.push(root);
  for (const k of root.kids) all(k, pred, out);
  return out;
}
/* Classes arrive two ways: svg() writes a `class` attribute, while the
   interaction code calls classList.add. Both count. */
const hasClass = (n, c) =>
  n.classes.has(c) || String(n.attrs.class || "").split(/\s+/).includes(c);
const withClass = (root, c) => all(root, (n) => hasClass(n, c));
const byTag = (root, t) => all(root, (n) => n.tag === t);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* ---- a two-machine mesh with a spawn tree on each ---------------------- */
const info = {
  name: "team", self: "work-pc", primary: null, authority: "work-pc",
  peers: [
    { machine: "work-pc", rank: 0, self: true, ok: null, queued: 0,
      members: ["lead", "w-api", "w-web"] },
    { machine: "laptop", rank: 1, self: false, ok: true, queued: 0,
      members: ["scout", "probe"] },
  ],
  members: [
    { handle: "lead", parent: null, machine: "", role: "lead",
      session: "s0", reachability: "idle", owed: 0 },
    { handle: "w-api", parent: "lead", machine: "", role: "worker",
      session: "s1", reachability: "busy", owed: 2 },
    { handle: "w-web", parent: "lead", machine: "", role: "worker",
      session: "s2", reachability: "idle", owed: 0 },
    { handle: "scout", parent: null, machine: "laptop", role: "scout",
      session: "s3", reachability: "remote-connected", owed: 0 },
    { handle: "probe", parent: "scout", machine: "laptop", role: "worker",
      session: "s4", reachability: "remote-connected", owed: 0 },
  ],
  links: [
    { a: "work-pc", b: "laptop", enabled: true, cuttable: true, editable: true },
  ],
  // A wired mesh: the two roots reach each other (the packaged rule), each
  // child hangs off its parent, and `lead` has wired one of its workers to
  // the other cluster's root by hand. Everything else is closed — which is
  // most of the ten pairs, and is exactly what does NOT get drawn.
  member_links: [
    { a: "lead", b: "w-api", enabled: true },     // spawn: drawn as an elbow
    { a: "lead", b: "w-web", enabled: true },     // spawn: drawn as an elbow
    { a: "lead", b: "scout", enabled: true },     // root <-> root
    { a: "lead", b: "probe", enabled: false },
    { a: "w-api", b: "w-web", enabled: false },   // siblings are strangers
    { a: "w-api", b: "scout", enabled: true },    // wired by hand
    { a: "w-api", b: "probe", enabled: false },
    { a: "w-web", b: "scout", enabled: false },
    { a: "w-web", b: "probe", enabled: false },
    { a: "scout", b: "probe", enabled: true },    // spawn: drawn as an elbow
  ],
};

{
  ctx.setFocus(null);
  const box = ctx.renderTopology(info);
  const svgs = byTag(box, "svg");
  check("one canvas", svgs.length === 1, svgs.length);
  const canvas = svgs[0];

  check("two clusters", withClass(canvas, "mesh-cluster").length === 2);
  check("the authority cluster is marked",
        withClass(canvas, "authority").length === 1);
  check("five agents", withClass(canvas, "mesh-agent").length === 5,
        withClass(canvas, "mesh-agent").length);
  check("three spawn edges", withClass(canvas, "mesh-spawn").length === 3,
        withClass(canvas, "mesh-spawn").length);
  // Open pairs get a line — but not the three that are already spawn elbows,
  // so what is left is lead<->scout and the hand-wired w-api<->scout.
  check("open member pairs, minus the spawn edges",
        withClass(canvas, "mesh-mlink").length === 2,
        withClass(canvas, "mesh-mlink").length);
  check("one peer edge", withClass(canvas, "mesh-edge-group").length === 1);
  // Peer edges are drawn, hovered and read — not cut. The graph is a full
  // interconnect by design, so the diagram offers no gesture that could take
  // a link out of it by accident.
  check("the peer edge is not a click target",
        !(withClass(canvas, "mesh-edge-group")[0].handlers.click || []).length);
  check("no wiring switches without a selection",
        withClass(canvas, "mesh-wire").length === 0);
  check("nothing dimmed without a selection",
        withClass(canvas, "dim").length === 0);
  check("no reach lines without a selection",
        withClass(canvas, "mesh-reach").length === 0);

  // the viewBox must contain every drawn cluster
  const vb = canvas.attrs.viewBox.split(" ").map(Number);
  check("viewBox is finite", vb.every(Number.isFinite), canvas.attrs.viewBox);
  check("viewBox has area", vb[2] > 0 && vb[3] > 0, vb);
  for (const rect of withClass(canvas, "mesh-cluster-box")) {
    const x = Number(rect.attrs.x), y = Number(rect.attrs.y);
    const w = Number(rect.attrs.width), h = Number(rect.attrs.height);
    check("cluster sits inside the viewBox",
          x >= vb[0] && y >= vb[1] && x + w <= vb[0] + vb[2] + 0.01
            && y + h <= vb[1] + vb[3] + 0.01, { x, y, w, h, vb });
  }
  // a spawn edge must be a path with a real d
  for (const p of withClass(canvas, "mesh-spawn")) {
    check("spawn edge has a path", /^M [-\d.]+ [-\d.]+ V/.test(p.attrs.d), p.attrs.d);
  }
}

/* ---- focus mode -------------------------------------------------------- */
{
  ctx.setFocus("w-api");
  const canvas = byTag(ctx.renderTopology(info), "svg")[0];
  const lit = withClass(canvas, "mesh-agent").filter((n) => !hasClass(n, "dim"));
  // w-api reaches its parent and the peer it was wired to, plus itself —
  // the sibling and the other cluster's child are strangers to it.
  check("focus lights the reachable set", lit.length === 3, lit.length);
  check("the unreachable peers are dimmed",
        withClass(canvas, "dim").length === 2,
        withClass(canvas, "dim").length);
  check("reach lines are drawn",
        withClass(canvas, "mesh-reach").length === 2,
        withClass(canvas, "mesh-reach").length);
  check("the focused agent is marked", withClass(canvas, "focus").length === 1);
  // Every other agent wears the switch for its pair with the selected one,
  // and which way the switch points is the pair's current state: '×' on the
  // two w-api can already message, '+' on the two it cannot.
  const wires = withClass(canvas, "mesh-wire");
  check("a switch on every other agent", wires.length === 4, wires.length);
  check("the connected pairs offer a disconnect",
        wires.filter((n) => hasClass(n, "on")).length === 2);
  check("the closed pairs offer a connect",
        wires.filter((n) => hasClass(n, "off")).length === 2);
  check("the selected agent carries no switch of its own",
        withClass(canvas, "focus").every((n) => !withClass(n, "mesh-wire").length));
  check("a switch is reachable by keyboard",
        wires.every((n) => n.attrs.tabindex === "0" && n.handlers.keydown));

  // ...and the switch must PATCH the pair it is drawn on, in the direction
  // it is drawn in. A '+' beside w-web connects w-api to w-web.
  sent.length = 0;
  const stray = { stopPropagation() {}, preventDefault() {} };
  wires.find((n) => hasClass(n, "off")).handlers.click[0](stray);
  check("the switch PATCHes one member edge", sent.length === 1, sent);
  check("...at the member-link route",
        /\/api\/mesh\/team\/members\/w-api\/links\/(w-web|probe)$/.test(sent[0].path),
        sent[0] && sent[0].path);
  check("...asking for the state the glyph offered",
        sent[0].method === "PATCH" && sent[0].body === '{"enabled":true}', sent[0]);
  sent.length = 0;
  wires.find((n) => hasClass(n, "on")).handlers.click[0](stray);
  check("an '×' asks for the other direction",
        sent.length === 1 && sent[0].body === '{"enabled":false}', sent);
}

/* ---- the Connections panel: the same edit as a list -------------------- */
{
  ctx.setFocus(null);
  const box = ctx.renderWiring(info);
  const opts = byTag(box, "option");
  check("the picker offers every member", opts.length === 6, opts.length);
  // With nobody selected the panel is the wiring as it stands: the open
  // pairs, which is the short list and the one somebody chose.
  const rows = withClass(box, "mesh-member");
  check("a row per connected pair", rows.length === 5, rows.length);
  check("each pair can be disconnected",
        byTag(box, "button").every((b) => b.text === "Disconnect"));
}
{
  ctx.setFocus("w-api");
  const box = ctx.renderWiring(info);
  const rows = withClass(box, "mesh-member");
  check("a row per other member", rows.length === 4, rows.length);
  const labels = byTag(box, "button").map((b) => b.text);
  check("connected rows disconnect, closed rows connect",
        labels.filter((t) => t === "Disconnect").length === 2
          && labels.filter((t) => t === "Connect").length === 2, labels);
  check("the bulk edits are offered",
        labels.includes("connect to all") && labels.includes("isolate"), labels);
  // Lineage is called out on the row, because cutting along a spawn edge is
  // the one disconnect that also stops a child reporting.
  const kin = withClass(box, "mesh-kin");
  check("the parent row says so", kin.length === 1 && kin[0].text === "parent",
        kin.map((k) => k.text));
  check("the picker keeps the selection",
        byTag(box, "option").some((o) => o.value === "w-api" && o.selected));
}
{
  // One member is nobody to talk to, and the panel says that rather than
  // drawing an empty list under a picker with one name in it.
  ctx.setFocus(null);
  const box = ctx.renderWiring({
    name: "solo", members: [info.members[0]], member_links: [],
  });
  check("a one-member mesh has nothing to wire",
        withClass(box, "mesh-member").length === 0
          && byTag(box, "select").length === 0);
}
{
  // A selection whose member has left must not survive the redraw.
  ctx.setFocus("ghost");
  ctx.renderTopology(info);
  check("a stale selection is dropped", ctx.getFocus() === null, ctx.getFocus());
}

/* ---- a mesh that never federated --------------------------------------- */
{
  ctx.setFocus(null);
  const local = {
    name: "solo", self: "", primary: null, authority: null,
    peers: [], links: [], member_links: [],
    members: [
      { handle: "lead", parent: null, machine: "", role: "lead",
        session: "s0", reachability: "idle", owed: 0 },
      { handle: "w1", parent: "lead", machine: "", role: "worker",
        session: "s1", reachability: "idle", owed: 0 },
    ],
  };
  const canvas = byTag(ctx.renderTopology(local), "svg")[0];
  check("a local-only mesh still draws", canvas !== undefined);
  check("one cluster", withClass(canvas, "mesh-cluster").length === 1);
  check("its tree is drawn", withClass(canvas, "mesh-spawn").length === 1);
  check("no peer edges", withClass(canvas, "mesh-edge-group").length === 0);
}

/* ---- an empty mesh ------------------------------------------------------ */
{
  const empty = { name: "e", self: "", primary: null, peers: [], links: [],
                  member_links: [], members: [] };
  const canvas = byTag(ctx.renderTopology(empty), "svg")[0];
  check("an empty mesh does not throw", canvas !== undefined);
  check("it says so", withClass(canvas, "mesh-cluster-empty").length === 1);
}

console.log(failures ? `\n${failures} failure(s)` : "all render checks passed");
process.exit(failures ? 1 : 0);
