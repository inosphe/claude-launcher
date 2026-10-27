/* The Settings card for daemon.webdebug_max_windows / webdebug_no_sandbox /
   webdebug_insecure_tls (claunch-o8pb7.2.1), run against stub nodes.

   GET /api/webdebug-setting answers the stored values, the effective values,
   the built-in defaults and which environment variable would override each;
   PUT {field} saves one (null restores the default) and answers the same
   shape. What is checked: the card reads "reading…" before the first answer;
   the number input shows the stored window count and refuses to go below one;
   each switch shows its stored value and is disabled while the value is the
   default; a change PUTs that field only; Reset sends null; a refused PUT
   shows the error and keeps the saved value; and an environment variable that
   overrides a value is named in the card, because an operator who set one
   would otherwise see the page's value apparently ignored. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

function node(tag, cls, text) {
  return {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], on: {}, disabled: false, id: "", value: "", type: "",
    checked: false, min: "", max: "", title: "", attrs: {},
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { for (const c of cs) this.children.push(c); },
    addEventListener(ev, fn) { (this.on[ev] = this.on[ev] || []).push(fn); },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
  };
}

function find(n, pred, out = []) {
  if (pred(n)) out.push(n);
  for (const c of n.children || []) find(c, pred, out);
  return out;
}
const inputById = (card, id) => find(card, (n) => n.id === id)[0];
const resets = (card) => find(card, (n) => n.tag === "button" && n.text === "Reset");
const texts = (card) => find(card, () => true).map((n) => n.text || "").join("\n");

const BASE = {
  values: { max_windows: 1, no_sandbox: false, insecure_tls: false },
  effective: { max_windows: 1, no_sandbox: false, insecure_tls: false },
  defaults: { max_windows: 1, no_sandbox: false, insecure_tls: false },
  env: {
    max_windows: { name: "CLAUNCH_WEBDEBUG_MAX_WINDOWS", set: false },
    no_sandbox: { name: "CLAUNCH_WEBDEBUG_NO_SANDBOX", set: false },
    insecure_tls: { name: "CLAUNCH_WEBDEBUG_INSECURE_TLS", set: false },
  },
};

function build(answers, wsOpen = true) {
  const calls = [];
  const redraws = [];
  const api = async (url, opts) => {
    calls.push({ url, method: (opts && opts.method) || "GET",
                 body: opts && opts.body ? JSON.parse(opts.body) : null });
    const next = answers.shift();
    return { ok: next.status === 200, status: next.status, json: async () => next.body };
  };
  const code = slice("/* The web debug browser", "/* Model tiers (claunch-cvife)");
  const w = new Function(
    "el", "document", "api", "renderWorkspaces", "wsOpen",
    code + "\nreturn { webdebugSetting, takeWebdebugSetting, refreshWebdebugSetting,"
         + " saveWebdebugSetting, webdebugSettingCard };")(
    node, { createElement: (tag) => node(tag) }, api, () => redraws.push(1), wsOpen);
  return { w, calls, redraws };
}

(async () => {
  {
    const { w } = build([]);
    const card = w.webdebugSettingCard();
    check("before the first answer the card is reading",
          card.children.some((c) => c.text === "reading…"));
    check("no control before the first answer",
          !inputById(card, "webdebug-max-windows") && !inputById(card, "webdebug-no_sandbox"));
  }

  {
    // The card is on the Settings page, so a redraw while that page is closed
    // rebuilds a view nobody is looking at. Every sibling refresher guards the
    // call with wsOpen; this one must too.
    const closed = build([{ status: 200, body: BASE }], false);
    await closed.w.refreshWebdebugSetting();
    check("a refresh does not redraw a closed Settings page",
          closed.redraws.length === 0, closed.redraws);
    const open = build([{ status: 200, body: BASE }], true);
    await open.w.refreshWebdebugSetting();
    check("a refresh redraws the open Settings page",
          open.redraws.length === 1, open.redraws);
  }

  {
    const { w, calls } = build([{ status: 200, body: BASE }]);
    await w.refreshWebdebugSetting();
    check("refresh is a GET of the setting",
          calls[0].url === "/api/webdebug-setting" && calls[0].method === "GET", calls);
    const card = w.webdebugSettingCard();
    check("the window count shows the stored value",
          inputById(card, "webdebug-max-windows").value === "1");
    check("the window count cannot go below one",
          inputById(card, "webdebug-max-windows").min === "1");
    check("the switches show their stored values",
          inputById(card, "webdebug-no_sandbox").checked === false
          && inputById(card, "webdebug-insecure_tls").checked === false);
    check("the switches are checkboxes",
          inputById(card, "webdebug-no_sandbox").type === "checkbox");
    check("the default is named beside the count",
          texts(card).includes("default 1"));
    check("every reset is disabled while the values are the defaults",
          resets(card).length === 3 && resets(card).every((b) => b.disabled));
    check("no environment note when no variable is set",
          !texts(card).includes("CLAUNCH_WEBDEBUG_INSECURE_TLS is set"));
  }

  {
    const { w, calls } = build([
      { status: 200, body: BASE },
      { status: 200, body: { ...BASE, values: { ...BASE.values, insecure_tls: true } } },
    ]);
    await w.refreshWebdebugSetting();
    const box = inputById(w.webdebugSettingCard(), "webdebug-insecure_tls");
    box.checked = true;
    await box.on.change[0]();
    check("a switch PUTs that field only as a boolean",
          calls[1].method === "PUT"
          && JSON.stringify(calls[1].body) === '{"insecure_tls":true}', calls[1].body);
    const card = w.webdebugSettingCard();
    check("redrawn from the answer",
          inputById(card, "webdebug-insecure_tls").checked === true);
    check("Reset is enabled for the changed field",
          resets(card)[2].disabled === false && resets(card)[0].disabled === true);
    await resets(card)[2].on.click[0]();
    check("Reset sends null", calls[2].body.insecure_tls === null, calls[2].body);
  }

  {
    const { w, calls } = build([
      { status: 200, body: BASE },
      { status: 200, body: { ...BASE, values: { ...BASE.values, max_windows: 3 } } },
      { status: 200, body: BASE },
    ]);
    await w.refreshWebdebugSetting();
    const count = inputById(w.webdebugSettingCard(), "webdebug-max-windows");
    count.value = "3";
    await count.on.change[0]();
    check("a typed count PUTs that field only as a number",
          calls[1].method === "PUT" && JSON.stringify(calls[1].body) === '{"max_windows":3}',
          calls[1].body);
    w.takeWebdebugSetting({ ...BASE, values: { ...BASE.values, max_windows: 3 } });
    const count2 = inputById(w.webdebugSettingCard(), "webdebug-max-windows");
    count2.value = "";
    await count2.on.change[0]();
    check("an emptied count sends null", calls[2].body.max_windows === null, calls[2].body);
  }

  {
    const { w } = build([
      { status: 200, body: BASE },
      { status: 200, body: BASE },
      { status: 400, body: { error: "'max_windows' must be between 1 and 8" } },
    ]);
    await w.refreshWebdebugSetting();
    await w.saveWebdebugSetting("max_windows", null);
    await w.saveWebdebugSetting("max_windows", 99);
    const card = w.webdebugSettingCard();
    check("a refused PUT shows the error",
          card.children.some((c) => c.cls === "error"
                             && c.text.includes("between 1 and 8")));
    check("a refused PUT keeps the saved value",
          inputById(card, "webdebug-max-windows").value === "1");
    check("a refused PUT leaves the input usable",
          !inputById(card, "webdebug-max-windows").disabled);
  }

  {
    // The operator set the variable: the stored value is not what the daemon
    // uses, and the card has to say which variable is deciding.
    const overridden = {
      ...BASE,
      values: { ...BASE.values, insecure_tls: false },
      effective: { ...BASE.effective, insecure_tls: true },
      env: { ...BASE.env, insecure_tls: { name: "CLAUNCH_WEBDEBUG_INSECURE_TLS", set: true } },
    };
    const { w } = build([{ status: 200, body: overridden }]);
    await w.refreshWebdebugSetting();
    const card = w.webdebugSettingCard();
    const body = texts(card);
    check("the overridden value is named as in effect",
          body.includes("In effect now: on"), body);
    check("the variable that overrides it is named",
          body.includes("CLAUNCH_WEBDEBUG_INSECURE_TLS"), body);
    check("the stored value is still what the switch shows",
          inputById(card, "webdebug-insecure_tls").checked === false);
  }

  if (failures) {
    console.log(`webdebugsetting_check: ${failures} failure(s)`);
    process.exit(1);
  }
  console.log("webdebugsetting_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
