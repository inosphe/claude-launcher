/* The stall-ping settings box, run against a stub DOM.

   The box is small but sits in a hostile spot: the flows list rebuilds on
   every 2s poll, and this box holds a TEXTAREA somebody is part-way through
   typing into. So the things worth pinning are the ones that would look fine
   in a screenshot and be wrong in use — that a second render is a no-op (a
   half-typed message survives the poll), that a failed fetch releases the
   guard instead of leaving an empty box for ever, that Save actually sends
   all three settings, and that a blank message is shown back as whatever the
   daemon kept rather than as the empty box that was sent. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`async function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* ---- stub DOM --------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", value: "", checked: false, dataset: {},
    handlers: {}, title: "", type: "", className: "", placeholder: "",
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get innerHTML() { return this.html || ""; },
    set innerHTML(v) { this.html = String(v); this.kids = []; },
  };
  return n;
}
const document = { createElement: (tag) => node(tag), activeElement: null };

function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = String(cls);
  if (text !== undefined) n.text = String(text);
  return n;
}

/* every node in the tree, so assertions can hunt by tag/class */
function all(n, out = []) {
  out.push(n);
  n.kids.forEach((k) => all(k, out));
  return out;
}
const find = (root, tag) => all(root).filter((n) => n.tag === tag);

function assert(cond, msg) {
  if (!cond) { console.error(`FAIL: ${msg}`); process.exit(1); }
}

/* ---- harness ---------------------------------------------------------- */
const PACKAGED = "This run has not moved in a long time and nothing is holding it.";

function harness({ getDefaults, putReply, failGet }) {
  const box = node("div");
  box.id = "cflow-ping-defaults";
  const calls = [];
  const api = async (url, opts) => {
    calls.push({ url, opts });
    if (!opts) {
      if (failGet) throw new Error("auth overlay is up");
      return { ok: true, status: 200, json: async () => ({ defaults: getDefaults }) };
    }
    return { ok: putReply.ok !== false, status: putReply.status || 200,
             json: async () => putReply.body };
  };
  const alerts = [];
  const fn = new Function(
    "$", "api", "el", "document", "alert",
    `${slice("renderStallPingDefaults")}; return renderStallPingDefaults;`
  )(() => box, api, el, document, (m) => alerts.push(m));
  return { box, calls, alerts, render: fn };
}

const DEFAULTS = {
  enabled: false, interval: 900, message: PACKAGED, min_interval: 60,
};

(async () => {
  /* ---- the settings reach the widgets ---------------------------------- */
  let h = harness({ getDefaults: { ...DEFAULTS, enabled: true, interval: 1200 },
                    putReply: { body: {} } });
  await h.render();
  const box = h.box;
  const checks = find(box, "input").filter((n) => n.type === "checkbox");
  assert(checks.length === 1, "one on/off checkbox");
  assert(checks[0].checked === true, "the checkbox reflects enabled");
  const nums = find(box, "input").filter((n) => n.type === "number");
  assert(nums.length === 1, "one interval field");
  assert(nums[0].value === "1200", `interval prefilled (got ${nums[0].value})`);
  assert(nums[0].min === "60", `the min comes from min_interval (got ${nums[0].min})`);
  const areas = find(box, "textarea");
  assert(areas.length === 1, "the message gets a textarea, not an input");
  assert(areas[0].value === PACKAGED, "the message is prefilled");
  const btns = find(box, "button");
  assert(btns.length === 1, "one save button");

  /* ---- once, so the 2s poll cannot wipe a half-typed message ----------- */
  areas[0].value = "half-typed…";
  await h.render();
  assert(find(box, "textarea")[0].value === "half-typed…",
         "a second render leaves a part-typed message alone");
  assert(h.calls.filter((c) => !c.opts).length === 1, "and does not re-fetch");

  /* ---- Save sends all three settings ----------------------------------- */
  checks[0].checked = false;
  nums[0].value = "300";
  find(box, "textarea")[0].value = "oi";
  await btns[0].handlers.click[0]();
  const put = h.calls.find((c) => c.opts && c.opts.method === "PUT");
  assert(put, "Save issues a PUT");
  assert(put.url === "/api/cflow/ping", `to the ping endpoint (got ${put.url})`);
  assert(JSON.parse(put.opts.body).enabled === false, "enabled is sent");
  assert(JSON.parse(put.opts.body).interval === 300, "interval is sent as a number");
  assert(JSON.parse(put.opts.body).message === "oi", "message is sent");

  /* ---- a blank message shows back what the daemon kept ------------------ */
  h = harness({
    getDefaults: DEFAULTS,
    putReply: { body: { defaults: { ...DEFAULTS, message: PACKAGED } } },
  });
  await h.render();
  const area = find(h.box, "textarea")[0];
  area.value = "   ";
  await find(h.box, "button")[0].handlers.click[0]();
  assert(area.value === PACKAGED,
         "blanking the message redisplays the default the daemon restored");

  /* ---- a rejected PUT surfaces, and does not rewrite the box ----------- */
  h = harness({
    getDefaults: DEFAULTS,
    putReply: { ok: false, status: 400, body: { error: "ping interval must be at least 60s" } },
  });
  await h.render();
  const a2 = find(h.box, "textarea")[0];
  a2.value = "mine";
  await find(h.box, "button")[0].handlers.click[0]();
  assert(h.alerts.length === 1 && /at least 60s/.test(h.alerts[0]),
         "the daemon's refusal is shown to the user");
  assert(a2.value === "mine", "a refused save does not overwrite what was typed");

  /* ---- a failed GET releases the guard so a later visit retries -------- */
  h = harness({ getDefaults: DEFAULTS, putReply: { body: {} }, failGet: true });
  await h.render();
  assert(!h.box.dataset.ready, "a failed fetch clears the once-guard");
  assert(find(h.box, "button").length === 0, "and builds nothing");

  console.log("pingbox_check ok");
})();
