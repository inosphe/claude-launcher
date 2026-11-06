/* The billed-bucket line under the context gauge and its price, run against
   the real functions from app.js.

   The line shows how the reading splits into read (fresh input), cached
   (replayed from cache) and write (written to cache), and — only where the
   person priced this model — what that request cost. The rules these
   checks hold:

   - Abbreviation is the usageShort scale (k from 1k, M from 1M), so a
     figure means the same thing here and on the usage line below.
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
   "costBreakdown", "ctxTokensLine", "ctxCostDetailRow"].map(slice).join("\n") + `
let modelPrices = null;
exports.setPrices = (p) => { modelPrices = p; };
exports.rateFor = priceRateFor;
exports.provider = sessionPriceProvider;
exports.cost = readingCost;
exports.text = costText;
exports.breakdown = costBreakdown;
exports.line = ctxTokensLine;
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
check("the rail line prices the session under its provider",
      texts(api.line({ name: "p", harness: "claude", profile: "ds4",
                       context: READING })).slice(-1),
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

/* ---- the rail line ---- */
function texts(line) {
  return line.kids.map((k) => k.textContent);
}
api.setPrices({ prices: { "*": { "claude-opus-5": RATES } }, currency: "$" });
const priced = api.line({ name: "a", harness: "claude", context: READING });
check("priced line: the three buckets abbreviated, then the estimate",
      texts(priced),
      ["read 2.00k", "·", "cached 150k", "·", "write 4.00k", "·",
       "≈ $0.5550"]);
check("priced line tooltip carries the multiplication",
      priced.title.includes("estimated from Settings → Model prices") &&
      priced.title.includes("cached read 150,000 × $1.5/1M"), true);

api.setPrices({ prices: {}, currency: "$" });
const unpriced = api.line({ name: "b", harness: "claude", context: READING });
check("unpriced line keeps the buckets and drops the estimate",
      texts(unpriced), ["read 2.00k", "·", "cached 150k", "·", "write 4.00k"]);
check("unpriced line tooltip says how to price it, not that it is free",
      unpriced.title.includes("no per-1M rates for this model"), true);

check("no reading means no line at all",
      api.line({ name: "c", harness: "claude" }), null);
check("an empty reading still draws its zeros, like the usage line would",
      texts(api.line({ name: "d", harness: "claude",
                       context: { tokens: 0, input: 0, cache_read: 0,
                                  cache_write: 0, output: 0,
                                  model: "m" } })),
      ["read 0", "·", "cached 0", "·", "write 0"]);

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

/* ---- the abbreviation scale itself ---- */
check("the scale abbreviates at K and M as the task asks",
      api.line({ context: { tokens: 1, input: 999, cache_read: 1000,
                            cache_write: 1500000, output: 0,
                            model: "m-no-price" } })
        .kids.map((k) => k.textContent).filter((_, i) => i % 2 === 0),
      ["read 999", "cached 1.00k", "write 1.50M"]);

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
