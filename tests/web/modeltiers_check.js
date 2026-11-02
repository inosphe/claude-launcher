/* Model tiers: the Model select is built per (profile, entry harness) from
   the server's catalog, which is the list model_tiers.validate applies.
   validate() reads the entry's own harness -- the profile's effective harness
   only when the entry names none -- so one list per profile would hide valid
   presets for a claude entry on a codex-default profile and offer codex-only
   names to it (claunch-61vj3 review). */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
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

const presets = ["default", "small", "large", "xlarge", "subagent"];
const ctx = vm.createContext({
  modelTiers: {
    // One list per profile is not enough: an explicit entry harness has its own.
    models: { cx: ["default", "luna"], work: ["default", "haiku"] },
    models_by_harness: {
      cx: {
        "": ["default", "luna"],
        claude: ["default", "small", "haiku"],
        codex: ["default", "luna"],
      },
      work: {
        "": ["default", "small", "haiku"],
        codex: ["default", "luna", "gpt6-sol"],
      },
    },
    presets,
  },
});
vm.runInContext(
  `${slice("modelTiersEntryModelChoices")}; this.choices = modelTiersEntryModelChoices;`,
  ctx
);
const choices = (profile, harness) => JSON.parse(vm.runInContext(
  `JSON.stringify(choices(${JSON.stringify(profile)}, ${JSON.stringify(harness)}))`,
  ctx
));

// An entry without a harness gets the profile's effective harness list.
assert.deepEqual(choices("cx", ""), ["default", "luna"]);
// An explicit harness gets its own list, not the profile default's: on a
// codex-default profile a claude entry still sees claude's presets...
assert.deepEqual(choices("cx", "claude"), ["default", "small", "haiku"]);
// ...and on a claude-default profile a codex entry does not see claude-only ones.
assert.deepEqual(choices("work", "codex"), ["default", "luna", "gpt6-sol"]);
assert.equal(choices("work", "codex").includes("haiku"), false);
// A harness the server did not list falls back to the profile's own list.
assert.deepEqual(choices("cx", "pi"), ["default", "luna"]);
// With no catalog at all the presets are the last resort.
assert.deepEqual(choices("ghost", ""), presets);
console.log("model tiers: per-harness Model choices passed");
