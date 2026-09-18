/* The spawn modal's size: the operator's, and remembered.

   The grip itself is the stylesheet's (`resize: both`), so half of this file
   reads style.css — a rule dropped there takes the feature away just as
   surely as a deleted function would, and nothing else in the suite would
   notice. The other half drives the JS pair that carries the size across
   opens, and pins the thing that makes that pair delicate: #modal-overlay's
   .modal-box is ONE element shared with the confirm dialogs, so a form
   dragged to 900px must not leave a 900px confirm dialog behind it. */
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(root, "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "style.css"), "utf8");

/* ---- slice helpers, the same shape the other harnesses use -------------- */
function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
function sliceStmt(decl) {
  const start = src.indexOf(decl);
  if (start < 0) throw new Error("missing " + decl);
  const end = src.indexOf(";", start);
  return src.slice(start, end + 1);
}

let fails = 0;
function ok(cond, what) {
  if (!cond) { fails++; console.log("FAIL:", what); }
}
function eq(got, want, what) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a !== b) { fails++; console.log(`FAIL: ${what}\n  got  ${a}\n  want ${b}`); }
}

/* ---- the stylesheet half ----------------------------------------------- */
/* The block the spawn modal owns, isolated so a rule found here is a rule
   that applies to the spawn box and not to some other dialog. */
function cssBlock(selector) {
  const at = css.indexOf(selector);
  if (at < 0) return null;
  const open = css.indexOf("{", at);
  const close = css.indexOf("}", open);
  return open < 0 || close < 0 ? null : css.slice(open + 1, close);
}

const boxRules = cssBlock("#modal-overlay.spawn-open .modal-box");
ok(boxRules !== null, "the spawn box has its own rule block");
/* The grip. `resize` is silently ignored on a box whose overflow is visible,
   so the pair is one fact, not two — asserting only `resize` would pass a
   stylesheet where the handle never draws. */
ok(/resize:\s*both/.test(boxRules || ""), "the spawn box is resizable");
ok(/overflow:\s*hidden/.test(boxRules || ""),
   "the spawn box hides overflow — resize does not take without it");
/* Floors, so the grip cannot be dragged into a box with no form in it. */
ok(/min-width:\s*\d+px/.test(boxRules || ""), "the spawn box has a width floor");
ok(/min-height:\s*\d+px/.test(boxRules || ""), "the spawn box has a height floor");

/* Dragging taller must show more FORM. Two things stand between the grip and
   that: a body that keeps its content height, and the form's own vh cap. */
const bodyRules = cssBlock("#modal-overlay.spawn-open #modal-body") || "";
ok(/flex:\s*1/.test(bodyRules),
   "the body takes the slack a taller box creates");
const formRules = cssBlock("#modal-overlay.spawn-open .sess-spawn") || "";
ok(/max-height:\s*none/.test(formRules),
   "the form's own height cap is lifted while the box owns the height");

/* The native grip is drawn in the corner the actions sit in. */
const actionRules = cssBlock("#modal-overlay.spawn-open #new-session-actions") || "";
ok(/padding-right:\s*\d+px/.test(actionRules),
   "the actions clear the grip's corner");

/* The floors the JS clamps to must be the floors the sheet draws, or the grip
   stops at one number and a reopen snaps to another. */
const cssMinW = Number((/min-width:\s*(\d+)px/.exec(boxRules || "") || [])[1]);
const cssMinH = Number((/min-height:\s*(\d+)px/.exec(boxRules || "") || [])[1]);

/* ---- the JS half ------------------------------------------------------- */
const store = {};
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
const window = { innerWidth: 1600, innerHeight: 1000 };

/* A box that records what was written to style, and reports whatever size the
   test says the grip left it at. */
function boxNode(rect) {
  return {
    style: { width: "", height: "" },
    getBoundingClientRect: () => rect,
  };
}

const ctx = {};
new Function(
  "exports", "localStorage", "window",
  "const BASE = '/';\n"
  + sliceStmt("const SPAWN_SIZE_KEY =")
  + sliceStmt("const SPAWN_W_MIN =")
  + sliceStmt("const SPAWN_H_MIN =")
  + sliceStmt("const spawnWMax =")
  + sliceStmt("const spawnHMax =")
  + slice("clampSpawnSize") + slice("spawnSizeRecall")
  + slice("spawnSizeApply") + slice("spawnSizeRemember")
  + `
Object.assign(exports, {
  clampSpawnSize, spawnSizeRecall, spawnSizeApply, spawnSizeRemember,
  KEY: SPAWN_SIZE_KEY, W_MIN: SPAWN_W_MIN, H_MIN: SPAWN_H_MIN,
});
`
)(ctx, localStorage, window);

/* The sheet and the script agree on the floors. */
eq(ctx.W_MIN, cssMinW, "the JS width floor is the stylesheet's");
eq(ctx.H_MIN, cssMinH, "the JS height floor is the stylesheet's");

/* --- nothing remembered: the box keeps whatever the sheet gave it -------- */
{
  const box = boxNode({ width: 700, height: 600 });
  ctx.spawnSizeApply(box);
  eq([box.style.width, box.style.height], ["", ""],
     "a first-ever open is left to the stylesheet's 700px");
}

/* --- the round trip ------------------------------------------------------ */
{
  const box = boxNode({ width: 900, height: 740 });
  ctx.spawnSizeRemember(box);
  /* Written down... */
  eq(JSON.parse(store[ctx.KEY]), { w: 900, h: 740 }, "the dragged size is written down");
  /* ...and the inline pair given back, or the next confirm dialog — the SAME
     element — opens 900px wide holding two lines of prose. */
  eq([box.style.width, box.style.height], ["", ""],
     "closing hands the box back to the stylesheet");

  const next = boxNode({ width: 700, height: 600 });
  ctx.spawnSizeApply(next);
  eq([next.style.width, next.style.height], ["900px", "740px"],
     "the next open comes up at the remembered size");
}

/* --- clamped on the way in, not just on the way out ---------------------- */
/* The operator may have dragged it wide on a monitor they are no longer at. */
{
  store[ctx.KEY] = JSON.stringify({ w: 5000, h: 4000 });
  const box = boxNode({ width: 700, height: 600 });
  ctx.spawnSizeApply(box);
  eq([box.style.width, box.style.height],
     [`${window.innerWidth - 32}px`, `${Math.round(window.innerHeight * 0.88)}px`],
     "a size from a bigger screen is clamped to this one");
}
{
  store[ctx.KEY] = JSON.stringify({ w: 10, h: 10 });
  const box = boxNode({ width: 700, height: 600 });
  ctx.spawnSizeApply(box);
  eq([box.style.width, box.style.height], [`${ctx.W_MIN}px`, `${ctx.H_MIN}px`],
     "a size below the floors is clamped up to them");
}

/* --- a row that is not a size is not a size ------------------------------ */
/* localStorage is shared by every daemon behind one relay and outlives any
   release, so the stored row is untrusted input, not a value. */
for (const bad of ["", "null", "{", "{}", '{"w":"wide","h":3}', '{"w":null,"h":null}', "[]"]) {
  store[ctx.KEY] = bad;
  const box = boxNode({ width: 700, height: 600 });
  let threw = false;
  try { ctx.spawnSizeApply(box); } catch { threw = true; }
  ok(!threw, `a stored row of ${JSON.stringify(bad)} does not throw`);
  eq([box.style.width, box.style.height], ["", ""],
     `a stored row of ${JSON.stringify(bad)} is ignored`);
}

/* --- a measurement that is not a size is not written down ---------------- */
/* showModal() hides the overlay without removing the spawn-open class, so a
   confirm dialog raised over an open spawn form can leave the box unlaid-out
   when close finally measures it: 0×0. The stylesheet's floors mean a VISIBLE
   spawn box can never measure under them, so anything under is not a size the
   operator chose. Flooring it would silently shrink the modal they had; the
   size they last chose has to survive instead. */
{
  store[ctx.KEY] = JSON.stringify({ w: 880, h: 700 });
  const box = boxNode({ width: 0, height: 0 });
  ctx.spawnSizeRemember(box);
  eq(JSON.parse(store[ctx.KEY]), { w: 880, h: 700 },
     "a 0x0 measurement leaves the remembered size standing");
  eq([box.style.width, box.style.height], ["", ""],
     "the box is handed back even when nothing was worth remembering");
}
/* ...and a first-ever close that measures nothing writes nothing, rather than
   seeding the floor as if it were a choice. */
{
  delete store[ctx.KEY];
  ctx.spawnSizeRemember(boxNode({ width: 0, height: 0 }));
  eq(store[ctx.KEY], undefined, "nothing measurable, nothing written");
}

/* --- the key is scoped, like every other remembered pixel ---------------- */
/* Daemons behind one relay share this localStorage; an unscoped key would let
   one daemon's dialog size another's. */
ok(ctx.KEY.includes("/"), "the size key is scoped by BASE");

console.log(fails ? `\nspawnsize_check: ${fails} FAILED` : "\nspawnsize_check: all ok");
process.exit(fails ? 1 : 0);
