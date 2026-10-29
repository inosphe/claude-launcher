/* Context items beside the create form's opening task (gds6-bfu7).

   Held here: both the New and the Spawn payloads let `opening_contexts`
   through to the daemon; blank rows are dropped and titles trimmed; the
   form refuses, before any request, what the daemon would refuse (too many
   items, an item or the total over its byte ceiling, counted in UTF-8);
   a spawn request's spec puts its items back when the person opens it with
   Manual; and the detail panel's section is guarded for harnesses that
   slice sessTask without it. */
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

function constLine(name) {
  const m = new RegExp(`const ${name} = [^;]*;`).exec(src);
  if (!m) throw new Error(`cannot locate const ${name}`);
  return m[0];
}

/* ---- the payload lets the items through --------------------------------- */
{
  const config = new Function(`${slice("sessionFormConfig")}; return sessionFormConfig;`)();
  for (const mode of ["new", "spawn"]) {
    assert.ok(config(mode).payload.includes("opening_contexts"),
      `the ${mode} payload carries opening_contexts`);
  }
  assert.ok(!config("fork").payload.includes("opening_contexts"),
    "Quick fork takes a task only");
}

/* ---- what is sent, and what is refused before sending -------------------- */
{
  const make = new Function(
    [constLine("OPENING_CONTEXTS_MAX"), constLine("OPENING_CONTEXT_MAX_BYTES"),
     constLine("OPENING_CONTEXTS_MAX_BYTES"), "const openingContexts = [];",
     slice("humanSize"), slice("utf8Bytes"), slice("openingContextsPayload"),
     slice("openingContextsProblem"),
     "return { openingContexts, openingContextsPayload, openingContextsProblem," +
     " OPENING_CONTEXTS_MAX, OPENING_CONTEXT_MAX_BYTES, OPENING_CONTEXTS_MAX_BYTES };"].join("\n"));
  const m = make();
  m.openingContexts.push(
    { title: "  pytest log ", text: "Traceback\n" },
    { title: "blank", text: "   \n" },
    { title: "", text: "notes" },
  );
  assert.deepStrictEqual(m.openingContextsPayload(), [
    { title: "pytest log", text: "Traceback\n" },
    { title: "", text: "notes" },
  ], "blank rows are dropped and titles trimmed");
  assert.strictEqual(m.openingContextsProblem(m.openingContextsPayload()), "");

  const many = Array.from({ length: m.OPENING_CONTEXTS_MAX + 1 }, () => ({ title: "", text: "x" }));
  assert.match(m.openingContextsProblem(many), /at most 20 context items/);

  // 3 bytes a character: under the ceiling in characters, over it in bytes.
  const wide = "가".repeat(Math.floor(m.OPENING_CONTEXT_MAX_BYTES / 3) + 1);
  assert.match(m.openingContextsProblem([{ title: "log", text: wide }]),
    /^context 1 \(log\) is 256\.0 KiB; .*attach a larger log as a file$/);

  const half = "x".repeat(m.OPENING_CONTEXT_MAX_BYTES);
  assert.match(m.openingContextsProblem([{ title: "", text: half }, { title: "", text: half }]),
    /together they are at most 384\.0 KiB/);
}

/* ---- the submit sends them, and a launch spends them ---------------------- */
{
  const at = src.indexOf("if (f.task.value.trim()) body.task = f.task.value.trim();");
  const body = src.slice(at, at + 400);
  assert.ok(/body\.opening_contexts = contextItems/.test(body),
    "the create body carries the items next to the task");
  const check = src.indexOf("const contextProblem = openingContextsProblem(contextItems);");
  assert.ok(check > at && check < src.indexOf("createBusy = true;", at),
    "the size check runs before the request is marked busy and sent");
  const spent = src.indexOf("clearOpeningFiles();\n    clearOpeningContexts();");
  assert.ok(spent > at, "a launch that went through clears the items with the files");
}

/* ---- Manual puts a request's items back ------------------------------------ */
{
  const seedOf = new Function(`${slice("spawnRequestSeed")}; return spawnRequestSeed;`)();
  const items = [{ title: "log", text: "boom" }];
  assert.deepStrictEqual(seedOf({ task: "t", opening_contexts: items }, { scope: "child" }).opening_contexts,
    items, "the spec's items are in the seed");
  assert.ok(!("opening_contexts" in seedOf({ task: "t" }, { scope: "child" })),
    "a spec without items adds no key");
  assert.ok(/clearOpeningContexts\(\)/.test(slice("spawnGateManual")),
    "Manual drops a draft's items before the request's are put back");
  assert.ok(/setOpeningContexts\(seed\.opening_contexts\)/.test(slice("applySessionModalSeed")),
    "the modal seed hands the items to the held list");
}

/* ---- the detail panel's section is optional for a sliced sessTask ---------- */
assert.ok(/typeof sessOpeningContexts === "function"/.test(slice("sessTask")),
  "sessTask guards the context items section");

console.log("openingcontexts_check: ok");
