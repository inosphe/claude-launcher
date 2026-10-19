/* The Settings card for daemon.delegate_profile / delegate_model
   (claunch-yl0to), run against stub nodes.

   GET /api/delegate-setting answers the saved values, the built-in
   defaults, the profiles and the model presets; PUT {profile|model} saves
   one field (null restores the default) and answers the same shape. What is
   checked: the card reads "reading…" before the first answer; each select
   offers "default (<built-in>)" first and then every choice, and shows ""
   while the value is the built-in; a saved value that is no longer a choice
   stays visible as "(missing)"; a change sends PUT with that field only
   (empty → null) and redraws from the answer; a refused PUT shows the
   error and keeps the saved value. */
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

function selects(card) {
  const out = {};
  for (const label of card.children.filter((c) => c.tag === "label")) {
    const s = label.children.find((c) => c.tag === "select");
    if (s) out[s.id] = s;
  }
  return out;
}

const BASE = {
  profile: "ds4", model: "small", defaults: { profile: "ds4", model: "small" },
  profiles: ["ds4", "work"], presets: ["default", "small", "large", "xlarge", "subagent"],
};

function build(answers) {
  const calls = [];
  const api = async (url, opts) => {
    calls.push({ url, method: (opts && opts.method) || "GET",
                 body: opts && opts.body ? JSON.parse(opts.body) : null });
    const next = answers.shift();
    return { ok: next.status === 200, status: next.status, json: async () => next.body };
  };
  const code = slice("/* Delegated child session (claunch-yl0to)", "function promptPresetCard(");
  const w = new Function(
    "el", "document", "api", "renderWorkspaces", "wsOpen",
    code + "\nreturn { delegateSetting, delegateSettingCard, refreshDelegateSetting, saveDelegateSetting };")(
    node, { createElement: (tag) => node(tag) }, api, () => {}, true);
  return { w, calls };
}

(async () => {
  {
    const { w } = build([]);
    const card = w.delegateSettingCard();
    check("before the first answer the card is reading",
          card.children.some((c) => c.text === "reading…"));
    check("no select before the first answer", Object.keys(selects(card)).length === 0);
  }

  {
    const { w, calls } = build([{ status: 200, body: BASE }]);
    await w.refreshDelegateSetting();
    check("refresh is a GET of the setting",
          calls[0].url === "/api/delegate-setting" && calls[0].method === "GET", calls);
    const s = selects(w.delegateSettingCard());
    const prof = s["delegate-profile"], model = s["delegate-model"];
    check("two selects", prof && model, Object.keys(s));
    check("profile offers default first, then every profile",
          prof.children.map((o) => o.value).join(",") === ",ds4,work",
          prof.children.map((o) => o.value));
    check("default option names the built-in", prof.children[0].textContent === "default (ds4)");
    check("model offers the presets",
          model.children.map((o) => o.value).join(",") === ",default,small,large,xlarge,subagent");
    check("built-in values show as default", prof.value === "" && model.value === "");
  }

  {
    const { w } = build([{ status: 200, body: { ...BASE, profile: "gone" } }]);
    await w.refreshDelegateSetting();
    const prof = selects(w.delegateSettingCard())["delegate-profile"];
    check("a saved profile that is gone stays visible",
          prof.value === "gone" && prof.children.some((o) => o.textContent === "gone (missing)"),
          prof.children.map((o) => o.textContent));
  }

  {
    const { w, calls } = build([
      { status: 200, body: BASE },
      { status: 200, body: { ...BASE, model: "large" } },
      { status: 200, body: BASE },
    ]);
    await w.refreshDelegateSetting();
    await w.saveDelegateSetting("model", "large");
    check("a change PUTs that field only",
          calls[1].method === "PUT" && JSON.stringify(calls[1].body) === '{"model":"large"}', calls[1]);
    check("redrawn from the answer", selects(w.delegateSettingCard())["delegate-model"].value === "large");
    await w.saveDelegateSetting("model", "");
    check("empty restores the default with null", calls[2].body.model === null, calls[2]);
  }

  {
    const { w } = build([
      { status: 200, body: BASE },
      { status: 400, body: { error: "no profile named 'x'" } },
    ]);
    await w.refreshDelegateSetting();
    await w.saveDelegateSetting("profile", "x");
    const card = w.delegateSettingCard();
    check("a refused PUT shows the error",
          card.children.some((c) => c.cls === "error" && c.text.includes("no profile named")));
    check("a refused PUT keeps the saved value", selects(card)["delegate-profile"].value === "");
    check("a refused PUT leaves the select usable", !selects(card)["delegate-profile"].disabled);
  }

  if (failures) {
    console.log(`delegatesetting_check: ${failures} failure(s)`);
    process.exit(1);
  }
  console.log("delegatesetting_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
