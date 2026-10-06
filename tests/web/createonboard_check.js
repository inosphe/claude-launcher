/* What a create/spawn answer says failed beside the session it made.

   The daemon reports each onboarding leg separately, so a partial success
   stays legible: the session exists, and the mesh join or the workflow start
   beside it may not have happened. The CLI has printed the failing one since
   it grew them ("mesh join failed: ..."), but the browser only ever checked
   the HTTP status — and a refused mesh join is a 200 with the session in it.

   So a Create that asked for a mesh and got none closed the box, opened the
   new terminal, and said nothing. The operator read that as the mesh picker
   being broken. Measured on s59/s60 (2026-10-06): both were refused with
   "handle 's59' is already taken in mesh 'gds6@shared-daemon-main'" — a
   handle an older, exited session on the other daemon still held — and
   neither the operator nor the sessions were told why.

   `onboardFailureNotices` is the pure half (the caller turns each entry into
   a sticky card), so it is what is worth holding here: which legs count as
   failed, what the sentence is, and that a leg which merely PENDED is not
   reported as a membership. */
const assert = require("assert");
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

const onboardFailureNotices =
  new Function(`${slice("onboardFailureNotices")}; return onboardFailureNotices;`)();

/* ---- a refused mesh join is the error, verbatim ------------------------ */
{
  const said = onboardFailureNotices({
    mesh: {
      ok: false,
      error: "handle 's59' is already taken in mesh 'gds6@shared-daemon-main'",
    },
    workflow: { ok: true, scope: "s59" },
  });
  assert.strictEqual(said.length, 1, JSON.stringify(said));
  assert.strictEqual(said[0].title, "mesh join failed");
  assert.strictEqual(said[0].sub,
    "handle 's59' is already taken in mesh 'gds6@shared-daemon-main'");
}

/* ---- a join that only PENDED is a wait, not a failure ------------------ */
{
  const said = onboardFailureNotices({
    mesh: { ok: false, pending: { pending: true, primary: "shared-daemon-main" } },
  });
  assert.strictEqual(said.length, 1, JSON.stringify(said));
  assert.strictEqual(said[0].title, "mesh join pending",
    "a pended join has not failed -- the primary has not answered yet");
  assert.ok(/shared-daemon-main/.test(said[0].sub), said[0].sub);
  assert.ok(/approve/.test(said[0].sub), said[0].sub);
  assert.ok(!/taken|error/.test(said[0].sub), said[0].sub);

  // An error wins over a pend: `_join` returns one or the other, and if both
  // ever arrive the reader needs the failure, not the wait.
  const both = onboardFailureNotices({
    mesh: { ok: false, error: "relay refused", pending: { primary: "m" } },
  });
  assert.strictEqual(both[0].title, "mesh join failed", JSON.stringify(both));
  assert.strictEqual(both[0].sub, "relay refused", JSON.stringify(both));
}

/* ---- a failure with nothing to say still says something ---------------- */
{
  const said = onboardFailureNotices({ mesh: { ok: false } });
  assert.strictEqual(said[0].sub, "no reason given");
}

/* ---- a workflow that did not start is its own sentence ----------------- */
{
  const said = onboardFailureNotices({
    mesh: { ok: true, mesh: "claunch", handle: "s120" },
    workflow: { ok: false, error: "no active cflow run" },
  });
  assert.strictEqual(said.length, 1, JSON.stringify(said));
  assert.strictEqual(said[0].title, "workflow did not start");
  assert.strictEqual(said[0].sub, "no active cflow run");
}

/* ---- both legs can fail at once, and both are said --------------------- */
{
  const said = onboardFailureNotices({
    mesh: { ok: false, error: "mesh is gone" },
    workflow: { ok: false, error: "workflow is gone" },
  });
  assert.deepStrictEqual(said.map((n) => n.title),
    ["mesh join failed", "workflow did not start"]);
}

/* ---- a clean answer is silent ------------------------------------------ */
{
  assert.deepStrictEqual(onboardFailureNotices({
    mesh: { ok: true, mesh: "claunch", handle: "s120", connected_to: ["s29"] },
    workflow: { ok: true, scope: "s120" },
  }), []);
  // A plain create asks for neither leg, so neither key is there.
  assert.deepStrictEqual(onboardFailureNotices({ name: "s120" }), []);
  assert.deepStrictEqual(onboardFailureNotices(null), []);
  assert.deepStrictEqual(onboardFailureNotices(undefined), []);
}

/* ---- the create/spawn flow actually asks it ---------------------------- */
{
  // A pure function that is right is worth nothing if the box never calls it:
  // this line is the whole fix — the one that turns a 200 carrying a failed
  // leg into a sentence the operator reads before the page leaves.
  assert.ok(/for \(const bad of onboardFailureNotices\(info\)\)/.test(src),
    "the create/spawn flow must report the legs the answer says failed");
  assert.ok(/notify\(bad\.title, bad\.sub, \{ kind: "warn", sticky: true \}\)/.test(src),
    "each failed leg is shown as a sticky card");
}

console.log("createonboard_check ok");
