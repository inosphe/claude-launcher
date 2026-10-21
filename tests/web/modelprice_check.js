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
     against a hand-computed value. */

const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
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
  ["priceCurrency", "priceRateFor", "readingCost", "costText",
   "costBreakdown", "ctxTokensLine", "ctxCostDetailRow"].map(slice).join("\n") + `
let modelPrices = null;
exports.setPrices = (p) => { modelPrices = p; };
exports.rateFor = priceRateFor;
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
api.setPrices({ prices: { "claude-opus-5": RATES }, currency: "$" });
check("estimate is the bucket-by-bucket multiplication",
      api.cost(READING, api.rateFor(READING.model)), EXPECTED);
check("a rate the row omits counts as 0",
      api.cost(READING, api.rateFor("partial-only") ??
                    { cache_read: 1.5 }), 0.225);
check("no rate row means no estimate, not a free one",
      api.cost(READING, null), null);
check("an unpriced model has no rate row",
      api.rateFor("deepseek-v4p1-flash-1017"), null);
check("the table keys on the recorded id, not the display form",
      api.rateFor("claude opus 5"), null);

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
api.setPrices({ prices: { "claude-opus-5": RATES }, currency: "$" });
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
api.setPrices({ prices: { "claude-opus-5": RATES }, currency: "$" });
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

if (failures) {
  console.error(`${failures} failure(s)`);
  process.exit(1);
}
console.log("modelprice_check: ok");
