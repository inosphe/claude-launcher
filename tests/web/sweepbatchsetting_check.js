/* The Settings card for daemon.delayed_sweep_batch_size / delayed_sweep_batch_settle,
   run against stub nodes.

   GET /api/sweep-batch-setting answers the effective values and the built-in
   defaults; PUT {size|settle} saves one field (null restores the default) and
   answers the same shape. What is checked: the card reads "reading…" before
   the first answer; each input shows the effective value and the default; a
   change sends PUT with that field only and redraws from the answer; an empty
   input and the Reset button send null; Reset is disabled while the value is
   the default; a refused PUT shows the error and keeps the saved value. */
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
    children: [], on: {}, disabled: false, id: "", value: "", textContent: "",
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { for (const c of cs) this.children.push(c); },
    addEventListener(ev, fn) { (this.on[ev] = this.on[ev] || []).push(fn); },
  };
}

function find(n, pred, out = []) {
  if (pred(n)) out.push(n);
  for (const c of n.children || []) find(c, pred, out);
  return out;
}
const input = (card, field) => find(card, (n) => n.id === `sweep-batch-${field}`)[0];
const resets = (card) => find(card, (n) => n.tag === "button" && n.text === "Reset");

const BASE = { size: 5, settle: 600, defaults: { size: 5, settle: 600 } };

function build(answers) {
  const calls = [];
  const api = async (url, opts) => {
    calls.push({ url, method: (opts && opts.method) || "GET",
                 body: opts && opts.body ? JSON.parse(opts.body) : null });
    const next = answers.shift();
    return { ok: next.status === 200, status: next.status, json: async () => next.body };
  };
  const code = slice("/* Post-merge sweep batching", "/* Model tiers (claunch-cvife)");
  const w = new Function(
    "el", "document", "api", "renderWorkspaces", "wsOpen",
    code + "\nreturn { sweepBatchSetting, sweepBatchSettingCard, refreshSweepBatchSetting, saveSweepBatchSetting };")(
    node, { createElement: (tag) => node(tag) }, api, () => {}, true);
  return { w, calls };
}

(async () => {
  {
    const { w } = build([]);
    const card = w.sweepBatchSettingCard();
    check("before the first answer the card is reading",
          card.children.some((c) => c.text === "reading…"));
    check("no input before the first answer", !input(card, "size"));
  }

  {
    const { w, calls } = build([{ status: 200, body: BASE }]);
    await w.refreshSweepBatchSetting();
    check("refresh is a GET of the setting",
          calls[0].url === "/api/sweep-batch-setting" && calls[0].method === "GET", calls);
    const card = w.sweepBatchSettingCard();
    check("inputs show the effective values",
          input(card, "size").value === "5" && input(card, "settle").value === "600");
    check("both inputs allow 0 and up", input(card, "size").min === "0" && input(card, "settle").min === "0");
    check("the default is named beside each input",
          find(card, (n) => n.text.includes("default 5")).length > 0
          && find(card, (n) => n.text.includes("default 600")).length > 0);
    check("Reset is disabled while the value is the default",
          resets(card).length === 2 && resets(card).every((b) => b.disabled));
  }

  {
    const { w, calls } = build([
      { status: 200, body: BASE },
      { status: 200, body: { ...BASE, settle: 900 } },
      { status: 200, body: BASE },
    ]);
    await w.refreshSweepBatchSetting();
    const first = input(w.sweepBatchSettingCard(), "settle");
    first.value = "900";
    await first.on.change[0]();
    check("a change PUTs that field only as a number",
          calls[1].method === "PUT" && JSON.stringify(calls[1].body) === '{"settle":900}', calls[1]);
    const card = w.sweepBatchSettingCard();
    check("redrawn from the answer", input(card, "settle").value === "900");
    check("Reset is enabled for the changed field",
          resets(card)[1].disabled === false && resets(card)[0].disabled === true);
    await resets(card)[1].on.click[0]();
    check("Reset sends null", calls[2].body.settle === null, calls[2]);
  }

  {
    const { w, calls } = build([
      { status: 200, body: BASE },
      { status: 200, body: BASE },
    ]);
    await w.refreshSweepBatchSetting();
    const field = input(w.sweepBatchSettingCard(), "size");
    field.value = "";
    await field.on.change[0]();
    check("an emptied input sends null", calls[1].body.size === null, calls[1]);
  }

  {
    const { w } = build([
      { status: 200, body: BASE },
      { status: 400, body: { error: "'size' must be between 0 and 100" } },
    ]);
    await w.refreshSweepBatchSetting();
    await w.saveSweepBatchSetting("size", 500);
    const card = w.sweepBatchSettingCard();
    check("a refused PUT shows the error",
          card.children.some((c) => c.cls === "error" && c.text.includes("between 0 and 100")));
    check("a refused PUT keeps the saved value", input(card, "size").value === "5");
    check("a refused PUT leaves the input usable", !input(card, "size").disabled);
  }

  if (failures) {
    console.log(`sweepbatchsetting_check: ${failures} failure(s)`);
    process.exit(1);
  }
  console.log("sweepbatchsetting_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
