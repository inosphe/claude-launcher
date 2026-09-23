const assert = require("node:assert/strict");
require("../../src/claude_launcher/web/static/session-grid.js");
const values = new Map();
const storage = { getItem: k => values.get(k), setItem: (k, v) => values.set(k, v) };
const reload = () => new SessionGridLayout(storage, "grid");
const cells = (g) => g.rows.map(r => [r.name, ...r.cells]);

// A fresh layout starts with one row named by the phonetic alphabet.
const grid = reload();
assert.deepEqual(cells(grid), [["alpha"]]);
assert.equal(grid.addRow().name, "bravo");
assert.ok(grid.isDefault("r1"), "the first row starts as the default row");
assert.ok(grid.setDefault("r2"));

// New sessions land in the default row, whatever spawned them.
grid.place([{ name: "a" }, { name: "b" }, { name: "c" }, { name: "d" }]);
assert.deepEqual(cells(grid), [["alpha"], ["bravo", "a", "b", "c", "d"]]);
grid.place([{ name: "a1", parent: "x" }]);
assert.deepEqual(cells(grid)[1], ["bravo", "a", "b", "c", "d", "a1"]);
assert.equal(grid.place([{ name: "a" }]), false, "a placed session is not placed again");

// Positions are fixed: a session leaving the view leaves its cell as it was,
// and the sessions after it do not shift left.
const present = new Set(["a", "b", "d", "a1"]);   // c went away
grid.place([...present].map(name => ({ name })));
assert.deepEqual(grid.positionOf("d"), { row: 1, col: 3 }, "(r,3) stays at 3 after (r,2) goes");
assert.equal(grid.at(1, 2), "c", "the absent session keeps its assignment");
assert.deepEqual(cells(reload()), cells(grid), "placement survives a reload");

// A cell still assigned to a session out of view is not a hole.
grid.place([{ name: "e" }]);
assert.deepEqual(cells(grid)[1], ["bravo", "a", "b", "c", "d", "a1", "e"]);

// Moving onto an empty cell leaves a hole behind; nothing else moves.
assert.ok(grid.move("a", "r1", 2, present));
assert.deepEqual(cells(grid), [["alpha", null, null, "a"], ["bravo", null, "b", "c", "d", "a1", "e"]]);
assert.equal(grid.columns(), 7, "one empty column past the longest row");

// Moving onto a present session swaps the two.
assert.ok(grid.move("e", "r2", 1, present));
assert.deepEqual(cells(grid)[1], ["bravo", null, "e", "c", "d", "a1", "b"]);

// Moving onto an out-of-view assignment takes the cell; that session is
// placed afresh when it returns -- into the default row's first empty cell.
assert.ok(grid.move("b", "r2", 2, present));
assert.deepEqual(cells(grid)[1], ["bravo", null, "e", "b", "d", "a1"]);
grid.place([{ name: "c" }]);
assert.deepEqual(grid.positionOf("c"), { row: 1, col: 0 });
// Holes outside the default row are never reused.
assert.ok(grid.move("c", "r1", 5, present));
grid.place([{ name: "c2" }]);
assert.deepEqual(cells(grid)[0], ["alpha", null, null, "a", null, null, "c"]);
assert.deepEqual(grid.positionOf("c2"), { row: 1, col: 0 });

// Keyboard steps: refused at the edges, no wrapping.
assert.equal(grid.moveBy("e", 0, -1, present), true);
assert.equal(grid.moveBy("e", 0, -1, present), false, "left edge");
assert.equal(grid.moveBy("e", 1, 0, present), false, "last row");
assert.ok(grid.moveBy("e", -1, 0, present));
assert.deepEqual(grid.positionOf("e"), { row: 0, col: 0 });

// Rows are renamed freely but names stay unique and non-empty.
assert.equal(grid.renameRow("r1", "  build   farm "), null);
assert.equal(grid.rows[0].name, "build farm");
assert.match(grid.renameRow("r2", "BUILD FARM"), /already named/);
assert.match(grid.renameRow("r2", "   "), /empty/);
assert.equal(reload().rows[0].name, "build farm");
assert.equal(grid.addRow().name, "alpha", "the first unused default name is reused");

// A row with a present session in it cannot be removed; an empty one can,
// unless it is the default row.
assert.equal(grid.removeRow("r1", present.add("e")), false);
assert.equal(grid.removeRow("r3", present), true);
grid.rows[1].cells = [];
assert.equal(grid.removeRow("r2", present), false, "the default row stays");
assert.deepEqual(grid.rows.map(r => r.id), ["r1", "r2"]);
assert.equal(grid.addRow().id, "r4", "row ids are not reused");

// Storage that is corrupt, duplicated or unavailable does not break it.
values.set("bad", "{nope");
assert.deepEqual(cells(new SessionGridLayout(storage, "bad")), [["alpha"]]);
values.set("dup", JSON.stringify({ rows: [
  { id: "r1", name: "x", cells: ["s", "s", 3] },
  { id: "r2", name: "X", cells: ["t"] },
  { id: "r1", name: "y", cells: [] },
  { name: "z", cells: [] },
] }));
const dup = new SessionGridLayout(storage, "dup");
assert.deepEqual(cells(dup), [["x", "s"]], "duplicate sessions, names and ids are dropped");
assert.equal(dup.addRow().id, "r2");
const denied = { getItem() { throw Error("denied"); }, setItem() { throw Error("denied"); } };
const ephemeral = new SessionGridLayout(denied, "grid");
ephemeral.place([{ name: "q" }]);
assert.deepEqual(cells(ephemeral), [["alpha", "q"]]);
// Wrapped rows: whole lines of perLine cells, with room for one more.
values.delete("wrap");
const wrap = new SessionGridLayout(storage, "wrap");
wrap.setDefault(wrap.addRow().id);
wrap.place(["a", "b", "c", "d", "e"].map(name => ({ name })));   // bravo: a..e
assert.equal(wrap.span(0, 4), 4, "an empty row is one line");
assert.equal(wrap.span(1, 4), 8, "five cells plus one free wrap onto two lines of four");
assert.equal(wrap.span(1, 3), 6);
assert.equal(wrap.span(1, 5), 10, "a full line of five still leaves a free cell below");
// Up/down walk lines inside a row before leaving it, keeping the slot.
assert.deepEqual(wrap.neighbor(1, 1, 1, 0, 4), { row: 1, col: 5 });
assert.equal(wrap.neighbor(1, 5, 1, 0, 4), null, "past the last line of the last row");
assert.deepEqual(wrap.neighbor(1, 5, -1, 0, 4), { row: 1, col: 1 });
assert.deepEqual(wrap.neighbor(1, 2, -1, 0, 4), { row: 0, col: 2 }, "into the last line of the row above");
assert.deepEqual(wrap.neighbor(0, 3, 1, 0, 4), { row: 1, col: 3 }, "into the first line of the row below");
assert.equal(wrap.neighbor(0, 3, -1, 0, 4), null);
assert.equal(wrap.neighbor(1, 7, 0, 1, 4), null, "right stops at the end of the drawn cells");
assert.deepEqual(wrap.neighbor(1, 4, 0, -1, 4), { row: 1, col: 3 }, "left runs back along the row");
// Alt+down from the first line lands one line lower in the same row.
const wrapPresent = new Set(["a", "b", "c", "d", "e"]);
assert.ok(wrap.moveBy("b", 1, 0, wrapPresent, 4));
assert.deepEqual(cells(wrap)[1], ["bravo", "a", null, "c", "d", "e", "b"]);
// Conditions and organize.
values.delete("org");
const org = new SessionGridLayout(storage, "org");            // r1 alpha = default
const ws = org.addRow(), both = org.addRow(), mesh = org.addRow(), plain = org.addRow();
assert.match(org.setRule("r1", { mesh: "m" }), /default row/);
assert.equal(org.setRule(ws.id, { workspace: " /repo ", mesh: "" }), null);
assert.deepEqual(org.row(ws.id).rule, { workspace: "/repo" }, "blank parts are dropped");
org.setRule(both.id, { mesh: "m1", workspace: "/other" });
org.setRule(mesh.id, { mesh: "m1" });
const facts = {
  p: { meshes: ["m1"], workspace: "/repo" },     // ws row comes first
  q: { meshes: ["m1"], workspace: "/other" },    // both row, before the mesh row
  r: { meshes: ["m1"], workspace: "/x" },        // mesh row only
  s: { meshes: [], workspace: "/nowhere" },      // nothing matches
  t: { meshes: ["m1"], workspace: "/repo" },     // out of view: left alone
};
const matches = (name, rule) => (!rule.mesh || facts[name].meshes.includes(rule.mesh))
  && (!rule.workspace || facts[name].workspace === rule.workspace);
org.place(Object.keys(facts).map(name => ({ name })));
org.move("p", plain.id, 0, new Set(Object.keys(facts)));
org.place([{ name: "u" }]);                                   // takes p's old hole
facts.u = { meshes: ["m1"], workspace: "/repo" };
const orgPresent = new Set(["q", "r", "s", "u"]);
assert.equal(org.organize(matches, orgPresent), 3);
assert.deepEqual(cells(org), [
  ["alpha", null, null, null, "s", "t"],
  ["bravo", "u"], ["charlie", "q"], ["delta", "r"], ["echo", "p"],
]);
assert.equal(org.organize(matches, orgPresent), 0, "sessions outside the default row are never moved");
assert.ok(org.setDefault(mesh.id));
assert.equal(org.row(mesh.id).rule, undefined, "a row made default loses its condition");
assert.deepEqual(new SessionGridLayout(storage, "org").row(ws.id).rule, { workspace: "/repo" }, "conditions persist");
assert.equal(new SessionGridLayout(storage, "org").defaultId, mesh.id, "the default row persists");
console.log("sessiongrid_check: ok");
