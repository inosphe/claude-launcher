// The clone lineage tree (vm-setup-8s6): built from the VMs' clone records and the presets,
// never linked by guess. Slices buildLineage out of vm.js.
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/vm.js"), "utf8");
const from = src.indexOf("function lineageGraph");
const to = src.indexOf("const LIFE_TEXT");
assert(from > 0 && to > from, "buildLineage is in vm.js");
const ctx = { Map, Set, Array };
vm.createContext(ctx);
vm.runInContext(src.slice(from, to) + "\nthis.build = buildLineage; this.chain = lineageChain;", ctx);

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

// the one-line lineage of the selected VM: ancestors up to where the records stop, never filled in
const chain = (vms, presets, id) => ctx.chain(vms, presets, id);
const gold = { name: "gold", source: "base", created_at: 100 }, silver = { name: "silver", source: "t01", created_at: 200 };
const chainVms = [{ id: "base" }, { id: "t01", cloned_from: rec("gold") }, { id: "t03", cloned_from: rec("silver", 200) }];
assert.strictEqual(chain(chainVms, [gold, silver], "t03"), "t03 ← 프리셋 silver ← t01 ← 프리셋 gold ← base(출처 기록 없음)");
assert.strictEqual(chain(chainVms, [gold, silver], "t01"),
  "t01 ← 프리셋 gold ← base(출처 기록 없음) · 이 VM에서 만든 프리셋: silver → t03");
assert.strictEqual(chain(chainVms, [gold, silver], "base"),
  "base: 출처 기록 없음(직접 등록했거나 이 기능 이전에 만든 VM, 추측하지 않음) · 이 VM에서 만든 프리셋: gold → t01");
assert.strictEqual(chain([{ id: "lone" }], [], "lone"), "lone: 출처 기록 없음(직접 등록했거나 이 기능 이전에 만든 VM, 추측하지 않음)");
assert.strictEqual(chain([{ id: "x", cloned_from: rec("gone", 5) }], [], "x"), "x ← 삭제된 프리셋 gone(이 프리셋 이전의 계보는 알 수 없음)");
assert.strictEqual(chain([{ id: "v", cloned_from: rec("p", 1) }], [{ name: "p", source: "removed", created_at: 1 }], "v"),
  "v ← 프리셋 p ← 원본 VM removed(등록 해제됨)");
assert.strictEqual(chain([{ id: "v", cloned_from: rec("p", 1) }], [{ name: "p", source: "", created_at: 1 }], "v"), "v ← 프리셋 p(원본 기록 없음)");
// a registered copy of a preset is named as such, then followed to the preset's source
assert.strictEqual(chain([{ id: "base" }, { id: "tmpl", template: "gold" }], [gold], "tmpl"),
  "tmpl(프리셋 gold의 복사본: 프리셋 템플릿, 일반 VM이 아님) ← base(출처 기록 없음)");
assert.strictEqual(chain([{ id: "tmpl", template: "gone" }], [], "tmpl"),
  "tmpl(프리셋 gone의 복사본: 프리셋 템플릿, 일반 VM이 아님) ← 그 프리셋의 기록이 없음");
assert.strictEqual(chain([], [], "none"), "");
console.log("vmlineage_check ok");
