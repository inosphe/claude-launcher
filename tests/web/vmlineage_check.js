// The clone lineage tree (vm-setup-8s6): built from the VMs' clone records and the presets,
// never linked by guess. Slices buildLineage out of vm.js.
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/vm.js"), "utf8");
const from = src.indexOf("function buildLineage");
const to = src.indexOf("const LIFE_TEXT");
assert(from > 0 && to > from, "buildLineage is in vm.js");
const ctx = { Map, Set, Array };
vm.createContext(ctx);
vm.runInContext(src.slice(from, to) + "\nthis.build = buildLineage;", ctx);

const shape = (n) => `${n.kind}:${n.key.split(":").slice(1).join(":")}` + (n.children.length ? `[${n.children.map(shape).join(",")}]` : "");
const tree = (vms, presets) => ctx.build(vms, presets).map(shape).join(" | ");
const rec = (preset, created = 100) => ({ kind: "preset", preset, preset_created_at: created, at: 1 });

// the chain: a hand-registered VM -> a preset saved from it -> VMs made from the preset -> a preset of one of those
assert.strictEqual(
  tree([{ id: "base", cloned_from: {} }, { id: "t01", cloned_from: rec("gold") }, { id: "t02", cloned_from: rec("gold") }, { id: "t03", cloned_from: rec("silver", 200) }],
       [{ name: "gold", source: "base", created_at: 100 }, { name: "silver", source: "t01", created_at: 200 }]),
  "vm:base[preset:gold[vm:t01[preset:silver[vm:t03]],vm:t02]]");

// no record: a root, not guessed at
assert.strictEqual(tree([{ id: "a" }, { id: "b", cloned_from: {} }], []), "vm:a | vm:b");

// the preset was deleted: the VM sits under a "deleted preset" node, the same preset name for two VMs shares it
assert.strictEqual(
  tree([{ id: "x", cloned_from: rec("gone", 5) }, { id: "y", cloned_from: rec("gone", 5) }], []),
  "gone-preset:gone:5[vm:x,vm:y]");

// deleted and saved again under the same name (another created_at): not the same preset
assert.strictEqual(
  tree([{ id: "base" }, { id: "x", cloned_from: rec("gold", 5) }], [{ name: "gold", source: "base", created_at: 99 }]),
  "vm:base[preset:gold] | gone-preset:gold:5[vm:x]");

// a preset whose source VM is no longer registered, and one with no source recorded
const orphan = ctx.build([], [{ name: "p", source: "removed", created_at: 1 }, { name: "q", source: "", created_at: 2 }]);
assert.deepStrictEqual(Array.from(orphan, (n) => n.kind).sort(), ["gone-vm", "preset"]);
assert.strictEqual(orphan.find((n) => n.kind === "gone-vm").children[0].data.name, "p");

// an old record without the preset's time still links by name
assert.strictEqual(tree([{ id: "v", cloned_from: { kind: "preset", preset: "gold" } }], [{ name: "gold", source: "", created_at: 7 }]), "preset:gold[vm:v]");

// a loop in the records cannot hide nodes: every node is drawn once
const loop = ctx.build([{ id: "a", cloned_from: rec("p") }], [{ name: "p", source: "a", created_at: 100 }]);
const count = (ns) => ns.reduce((n, x) => n + 1 + count(x.children), 0);
assert.strictEqual(count(loop), 2);
console.log("vmlineage_check ok");
