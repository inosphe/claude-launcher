/* The card's cost lines and the detail panel's per-request price, run
   against the real functions from app.js.

   The card draws what the conversation itself has spent, and — where the
   session has subagents — what they spent, each priced at the rates the
   person entered. The detail panel keeps the last request's billed buckets.
   The rules these checks hold:

   - The card's figure is the cumulative reading (`token_usage`), not the
     single last request (`context`): those are different spends, and the
     cumulative one is the figure the terminal overlay's chart prices.
   - Subagent spend gets its own row. Folding it into the conversation
     figure would hide which half came from subagents.
   - Abbreviation is the usageShort scale (k from 1k, M from 1M), so a
     figure means the same thing here and on the usage line above.
   - "Not priced" never reads as "costs nothing": no rate row means no
     estimate, and the counts still show.
   - The estimate is the multiplication the tooltip claims it is — checked
     against a hand-computed value.
   - The Settings card's save leaves an empty cache-write cell out of the
     PUT body, so the read-rate fallback survives a save.
   - Rates are keyed by provider, then model: a session is priced under the
     provider its profile (or its lender) resolves to, and a model that
     provider does not price falls back to the "*" row. */

const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

// A top-level `const NAME = ...;` line, copied as app.js has it.
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .*;$`, "m"));
  if (!m) throw new Error(`cannot locate const ${name} in app.js`);
  return m[0];
}

function slice(name) {
  let start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  if (src.slice(start - 6, start) === "async ") start -= 6;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

const api = {};
new Function(
  "exports", "el", "usageShort", "usageCost",
  ["priceCurrency", "priceAnyProvider", "sessionPriceProvider",
   "priceRateFor", "cacheWriteRate", "readingCost", "costText",
   "costBreakdown", "railCostLines", "ctxCostDetailRow"].map(slice).join("\n") + `
let modelPrices = null;
exports.setPrices = (p) => { modelPrices = p; };
exports.rateFor = priceRateFor;
exports.provider = sessionPriceProvider;
exports.cost = readingCost;
exports.text = costText;
exports.breakdown = costBreakdown;
exports.line = railCostLines;
exports.detail = ctxCostDetailRow;
`
)(api,
  (tag, cls, text) => ({
    tag, className: cls || "", textContent: text ?? "", title: "",
    kids: [],
    append(...cs) { cs.forEach((c) => this.kids.push(c)); },
  }),
  // usageShort, copied from app.js's own definition so the check fails if
  // the scale changes under the line (ctxsize_check holds ctxShort; this
  // line rides usageShort).
  (n) => {
    if (!Number.isFinite(n) || n < 0) return "?";
    if (n < 1000) return String(Math.round(n));
    const units = [[1e9, "B"], [1e6, "M"], [1e3, "k"]];
    for (const [size, unit] of units) {
      if (n >= size) {
        const v = n / size;
        return (v < 10 ? v.toFixed(2) : v < 100 ? v.toFixed(1) : String(Math.round(v))) + unit;
      }
    }
    return String(n);
  },
  (v) => v >= 100 ? v.toFixed(0) : v >= 1 ? v.toFixed(2) : v.toFixed(4));

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const READING = {
  tokens: 156000, input: 2000, cache_read: 150000, cache_write: 4000,
  output: 3000, model: "claude-opus-5",
};
const RATES = { input: 15, cache_read: 1.5, cache_write: 18.75, output: 75 };
// hand-computed: (2000·15 + 150000·1.5 + 4000·18.75 + 3000·75) / 1e6
const EXPECTED = 0.555;

/* ---- the estimate's arithmetic ---- */
api.setPrices({ prices: { "*": { "claude-opus-5": RATES } }, currency: "$" });
check("estimate is the bucket-by-bucket multiplication",
      api.cost(READING, api.rateFor(READING.model)), EXPECTED);
check("a rate the row omits counts as 0",
      api.cost(READING, api.rateFor("partial-only") ??
                    { cache_read: 1.5 }), 0.225);
check("an unset cache-write rate prices written tokens at the read rate",
      api.cost(READING, { input: 15, cache_read: 1.5, output: 75 }),
      (2000 * 15 + 150000 * 1.5 + 4000 * 15 + 3000 * 75) / 1e6);
check("a cache-write rate set to 0 stays 0, not the read rate",
      api.cost(READING, { input: 15, cache_read: 1.5, cache_write: 0,
                          output: 75 }),
      (2000 * 15 + 150000 * 1.5 + 3000 * 75) / 1e6);
check("the tooltip names the read rate it fell back to",
      api.breakdown(READING, { input: 15 }).includes(
        "cache write 4,000 × $15/1M"), true);
check("no rate row means no estimate, not a free one",
      api.cost(READING, null), null);
check("an unpriced model has no rate row",
      api.rateFor("deepseek-v4p1-flash-1017"), null);
check("the table keys on the recorded id, not the display form",
      api.rateFor("claude opus 5"), null);

/* ---- the provider tier ---- */
const DS = { input: 0.3, cache_read: 0.006, output: 1.2 };
api.setPrices({
  prices: { "*": { "claude-opus-5": RATES }, deepseek: { "claude-opus-5": DS } },
  profiles: { ds4: "deepseek", sr: "default" }, any: "*", currency: "$",
});
check("a provider's own row wins over the * row",
      api.rateFor("claude-opus-5", "deepseek"), DS);
check("a provider without a row for the model takes the * row",
      api.rateFor("claude-opus-5", "default"), RATES);
check("no provider at all takes the * row",
      api.rateFor("claude-opus-5", ""), RATES);
api.setPrices({
  prices: { "*": { "claude-opus-5": RATES }, deepseek: { "claude-opus-5": {} } },
  profiles: { ds4: "deepseek", sr: "default" }, any: "*", currency: "$",
});
check("a provider row with no rates does not hide the * row",
      api.rateFor("claude-opus-5", "deepseek"), RATES);
api.setPrices({
  prices: { "*": { "claude-opus-5": RATES }, deepseek: { "claude-opus-5": DS } },
  profiles: { ds4: "deepseek", sr: "default" }, any: "*", currency: "$",
});
check("a session is priced under the provider its profile resolves to",
      api.provider({ profile: "ds4:claude" }), "deepseek");
check("a borrowing session is priced under its lender's provider",
      api.provider({ profile: "sr", borrow: "ds4" }), "deepseek");
check("a profile the table does not know has no provider",
      api.provider({ profile: "gone" }), "");
check("the same reading costs what each provider charges",
      [api.text(api.cost(READING, api.rateFor(READING.model,
         api.provider({ profile: "ds4" })))),
       api.text(api.cost(READING, api.rateFor(READING.model,
         api.provider({ profile: "sr" }))))],
      [api.text((2000 * 0.3 + 150000 * 0.006 + 4000 * 0.3 + 3000 * 1.2) / 1e6),
       api.text(EXPECTED)]);
check("the card prices the session under its provider",
      api.line({ name: "p", harness: "claude", profile: "ds4",
                 token_usage: { ...READING, total: 156000, requests: 1 } })
        .kids[0].kids.slice(-1).map((k) => k.textContent),
      [api.text((2000 * 0.3 + 150000 * 0.006 + 4000 * 0.3 + 3000 * 1.2) / 1e6)]);

/* ---- the text of it ---- */
check("a sub-dollar estimate keeps four decimals",
      [api.text(0.0042), api.text(EXPECTED)],
      ["≈ $0.0042", "≈ $0.5550"]);
check("a dollar-plus estimate keeps two", api.text(1.5), "≈ $1.50");
check("the currency is the one the table carries",
      (api.setPrices({ prices: {}, currency: "₩" }), api.text(1.5)),
      "≈ ₩1.50");
check("no table at all falls back to $",
      (api.setPrices(null), api.text(1.5)), "≈ $1.50");

/* ---- the card's cost lines ---- */
function rows(box) {
  return box.kids.map((r) => r.kids.map((k) => k.textContent));
}
const USAGE = {
  input: 2000, cache_read: 150000, cache_write: 4000, output: 3000,
  total: 159000, requests: 4, model: "claude-opus-5",
};
api.setPrices({ prices: { "*": { "claude-opus-5": RATES } }, currency: "$" });
const priced = api.line({ name: "a", harness: "claude", token_usage: USAGE });
check("the conversation row: its label and the estimate",
      rows(priced), [["conversation", "≈ $0.5550"]]);
check("the conversation row's tooltip carries the multiplication",
      priced.kids[0].title.includes(
        "conversation, estimated from Settings → Model prices") &&
      priced.kids[0].title.includes("cached read 150,000 × $1.5/1M"), true);
check("the conversation row says which spend it is",
      priced.kids[0].title.includes("the conversation's own requests"), true);

const WITH_SUB = { ...USAGE, subagents: {
  input: 500, cache_read: 10000, cache_write: 0, output: 1000,
  total: 11500, requests: 2 } };
const two = api.line({ name: "e", harness: "claude", token_usage: WITH_SUB });
check("subagent spend gets its own row, not folded into the conversation's",
      rows(two),
      [["conversation", "≈ $0.5550"], ["subagents", "≈ $0.0975"]]);
check("a session with no subagent requests draws no subagent row",
      rows(api.line({ name: "f", harness: "claude",
                      token_usage: { ...USAGE, subagents: { requests: 0 } } }))
        .length,
      1);

api.setPrices({ prices: {}, currency: "$" });
check("unpriced: no block at all — the usage line still shows the counts, " +
      "and no row claims zero",
      api.line({ name: "b", harness: "claude", token_usage: USAGE }), null);

api.setPrices({ prices: { "*": { "claude-opus-5": RATES } }, currency: "$" });
check("no cumulative reading means no line at all — the last request's " +
      "reading is not a spend",
      api.line({ name: "c", harness: "claude", context: READING }), null);
check("a partial reading marks the block",
      api.line({ name: "g", harness: "claude",
                 token_usage: { ...USAGE, partial: true } }).className,
      "rail-usage-cost partial");
check("a zero-request reading still prices its zeros",
      rows(api.line({ name: "d", harness: "claude",
                      token_usage: { input: 0, cache_read: 0, cache_write: 0,
                                     output: 0, total: 0, requests: 1,
                                     model: "claude-opus-5" } })),
      [["conversation", "≈ $0.0000"]]);

/* ---- the detail-panel row ---- */
function detailTexts(s) {
  const dl = { kids: [], appendChild(c) { this.kids.push(c); } };
  api.detail(dl, s);
  return dl.kids.map((k) => `${k.tag}:${k.textContent}`);
}
api.setPrices({ prices: { "*": { "claude-opus-5": RATES } }, currency: "$" });
check("detail row priced", detailTexts({ context: READING }),
      ["dt:last request",
       `dd:read 2.00k · cached 150k · write 4.00k · ≈ $0.5550`]);
api.setPrices({ prices: {}, currency: "$" });
check("detail row unpriced says so instead of estimating",
      detailTexts({ context: READING }),
      ["dt:last request",
       "dd:read 2.00k · cached 150k · write 4.00k · not priced"]);
check("detail row absent without a reading", detailTexts({}), []);

/* ---- the estimate's own scale ---- */
api.setPrices({ prices: { "*": { "claude-opus-5": RATES } }, currency: "$" });
check("a sub-dollar conversation keeps four decimals",
      rows(api.line({ token_usage: {
        input: 999, cache_read: 1000, cache_write: 0, output: 50,
        total: 2000, requests: 1, model: "claude-opus-5" } })),
      [["conversation", "≈ $0.0202"]]);

/* ---- the Settings card: its header and what Save sends ---- */
// A second sandbox for the card and its save, which reach for the DOM and
// the daemon: el/document build plain nodes, api records the PUT body.
function node(tag, cls, text) {
  return {
    tag, className: cls || "", textContent: text ?? "", title: "",
    kids: [],
    append(...cs) { cs.forEach((c) => this.kids.push(c)); },
    appendChild(c) { this.kids.push(c); return c; },
    // Listeners are kept so a check can press a button the card drew.
    addEventListener(type, fn) { (this.on = this.on || {})[type] = fn; },
    setAttribute() {},
  };
}
const card = {};
new Function(
  "exports", "el", "document", "api", "renderWorkspaces",
  [constLine("MP_RATE_KEYS"),
   ...["priceAnyProvider", "modelPricesProviderOptions",
       "modelPricesDraftValues", "modelPricesSave", "modelPriceRateId",
       "modelPricesCard"]
     .map(slice)].join("\n") + `
let modelPrices = null, modelPricesKey = "", modelPricesError = "";
let modelPricesDraft = null, modelPricesNotice = "", modelPricesBusy = false;
let modelPricesAdd = { provider: "", model: "" };
let sessionsCache = [];
exports.load = (p) => { modelPrices = p; modelPricesDraft = null; };
exports.draft = modelPricesDraftValues;
exports.save = modelPricesSave;
exports.card = modelPricesCard;
exports.error = () => modelPricesError;
`
)(card, node, { createElement: (tag) => node(tag) },
  async (url, opts) => {
    card.sent = JSON.parse(opts.body);
    return { ok: true, json: async () => card.sent };
  },
  () => {});

function findAll(n, pred, out = []) {
  if (pred(n)) out.push(n);
  (n.kids || []).forEach((k) => findAll(k, pred, out));
  return out;
}

card.load({ prices: { "*": { "m": { input: 0.3, cache_read: 0.006, output: 1.2 } } },
            profiles: { ds4: "deepseek" }, any: "*", currency: "$" });
const drawn = card.card();
check("the table header names the cache-write column",
      findAll(drawn, (n) => n.className === "mp-rate-head")
        .map((n) => n.textContent),
      ["read", "cached read", "cache write", "output", ""]);
check("each row names its provider before its model",
      findAll(drawn, (n) => n.className === "mp-provider" ||
                            n.className === "mp-model")
        .map((n) => n.textContent),
      ["*", "m"]);
check("the add row offers * first, then each profile's provider",
      findAll(drawn, (n) => n.tag === "option" && n.value !== undefined &&
                            n.textContent)
        .map((n) => n.value),
      ["*", "deepseek"]);

async function saveChecks() {
  // A saved row without cache_write opens with that cell empty, and saving
  // it untouched must send no cache_write key: an empty cell sent as 0 would
  // turn the read-rate fallback off on the next reading.
  card.load({ prices: { deepseek: { "m": { input: 0.3, cache_read: 0.006,
                                           output: 1.2 } } },
              currency: "$" });
  check("an unset cache write opens as an empty cell",
        card.draft().prices.deepseek.m.cache_write, "");
  await card.save();
  check("saving an empty cache-write cell sends no cache_write key",
        card.sent, { currency: "$", prices: { deepseek: {
          m: { input: 0.3, cache_read: 0.006, output: 1.2 } } } });

  card.load({ prices: { "*": { "z": { input: 0.3, cache_write: 0 } } },
              currency: "$" });
  await card.save();
  check("a cache-write rate typed as 0 is sent as 0",
        card.sent.prices["*"].z, { input: 0.3, cache_write: 0 });

  card.load({ prices: { "*": { "a": { input: 1 } },
                        deepseek: { "b": { input: 2 } } }, currency: "$" });
  card.draft().prices.deepseek.b.input = "";
  await card.save();
  check("a provider whose every row was emptied is not sent",
        card.sent.prices, { "*": { a: { input: 1 } } });
}

/* ---- the add row: the picked provider is the tier the row lands under ---- */
// Draws the card, fills the add row's provider select and model input, and
// presses "Add model" — the path the Settings page takes to make a new row.
function addThroughCard(provider, model) {
  const drawnCard = card.card();
  const [sel] = findAll(drawnCard, (n) => n.id === "mp-add-provider");
  const [inp] = findAll(drawnCard, (n) => n.id === "mp-add-model");
  const [btn] = findAll(drawnCard, (n) => n.textContent === "Add model");
  sel.value = provider;
  inp.value = model;
  btn.on.click();
}

async function addRowChecks() {
  card.load({ prices: {}, profiles: { ds4: "deepseek" }, any: "*",
              currency: "$" });
  addThroughCard("deepseek", "deepseek-v4-pro");
  check("adding under deepseek makes a deepseek row",
        Object.keys(card.draft().prices.deepseek || {}), ["deepseek-v4-pro"]);
  check("adding under deepseek leaves * without that row",
        (card.draft().prices["*"] || {})["deepseek-v4-pro"], undefined);
  addThroughCard("*", "claude-opus-5");
  check("adding under * makes a * row",
        Object.keys(card.draft().prices["*"] || {}), ["claude-opus-5"]);
  card.draft().prices.deepseek["deepseek-v4-pro"].input = "0.3";
  card.draft().prices["*"]["claude-opus-5"].input = "15";
  await card.save();
  check("the added rows are saved under the provider they were added to",
        card.sent.prices, { deepseek: { "deepseek-v4-pro": { input: 0.3 } },
                            "*": { "claude-opus-5": { input: 15 } } });
}

saveChecks().then(addRowChecks).then(() => {
  if (failures) {
    console.error(`${failures} failure(s)`);
    process.exit(1);
  }
  console.log("modelprice_check: ok");
}, (err) => {
  console.error(err);
  process.exit(1);
});
