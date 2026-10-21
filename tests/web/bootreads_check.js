/* The first paint's reads, ahead of the rest of the dashboard's (claunch-e303z).

   boot() used to ask for twelve reads in one batch and wait for all of them
   before anything could be drawn. The rail builds every row itself, so its
   first row waited for the slowest read in that batch -- `/api/cflow2/rail`,
   measured at 1.3 s where `/api/sessions` measured 0.55 s on a fleet of
   1,100 sessions. The rail's two reads now leave first, each `direct`
   (noBatch: the batch window has nothing to gather for them), and the other
   ten still travel together behind them.

   The mesh list goes before the sessions read because the rail's group
   headers are drawn from it; answered together, the rail would be drawn
   ungrouped and rebuilt once the mesh list landed.

   What this pins is the order and the option, not the timing: the timing is
   measured in the report, and a source that keeps this shape cannot put the
   rail back behind a slower pane's read. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

const boot = slice("boot");
const sessions = slice("refreshSessions");
const meshes = slice("refreshMeshList");

// The rail's mesh list and its sessions, in that order, and before the batch
// that carries everything else.
const meshAt = boot.indexOf("await refreshMeshList({ direct: true });");
const sessionsAt = boot.indexOf("await refreshSessions({ direct: true });");
const batchAt = boot.indexOf("await Promise.all([");
assert.ok(meshAt >= 0, "boot asks for the mesh list direct");
assert.ok(sessionsAt >= 0, "boot asks for the rail's sessions direct");
assert.ok(batchAt >= 0, "boot still gathers the rest in one batch");
assert.ok(meshAt < sessionsAt, "the mesh list is asked for before the sessions");
assert.ok(sessionsAt < batchAt, "the rail's reads are not inside the batch");

// ... and neither of them is still in that batch, where it would be held
// behind the slowest read again.
const inBatch = boot.slice(batchAt);
assert.ok(!/refreshSessions\(/.test(inBatch), "refreshSessions is not in the batch");
assert.ok(!/refreshMeshList\(/.test(inBatch), "refreshMeshList is not in the batch");

// `direct` reaches the read as noBatch: that is what skips the batch window.
assert.ok(/if \(options && options\.direct\) readOpts\.noBatch = true;/.test(sessions),
          "refreshSessions passes direct on as noBatch");
assert.ok(/Object\.keys\(readOpts\)\.length \? readOpts : undefined/.test(sessions),
          "refreshSessions sends the options it built (and nothing when empty)");
assert.ok(/options && options\.direct \? \{ noBatch: true \} : undefined/.test(meshes),
          "refreshMeshList passes direct on as noBatch");

// A plain poll still batches: direct is the first boot's option, not the
// poll's. Nothing in either function may set noBatch outside that guard.
const noBatchLines = (sessions.match(/noBatch/g) || []).length;
assert.strictEqual(noBatchLines, 1,
                   "only the direct guard sets noBatch in refreshSessions");
const meshNoBatch = (meshes.match(/noBatch/g) || []).length;
assert.strictEqual(meshNoBatch, 1,
                   "only the direct guard sets noBatch in refreshMeshList");

console.log("bootreads_check: ok");
