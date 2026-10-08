/* Images attached to the create form's opening task (claunch-jmrmc).

   Held here: which harnesses the form offers images for (a declared
   `image_paste_keys`; an unknown harness counts as yes and the daemon answers
   for itself), what an attach keeps (images only, at most ten), that both the
   New and the Spawn payloads let `opening_images` through to the daemon, and
   that the upload goes one image at a time, in order, and stops with the
   daemon's reason on the first refusal. */
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

const kindsBlock = (() => {
  const start = src.indexOf("const PASTE_IMAGE_KINDS = {");
  return src.slice(start, src.indexOf("};", start) + 2);
})();

/* ---- which harness takes images --------------------------------------- */
{
  const takes = new Function("harnessDetails", "newProfileHarnessName",
    `${slice("newFormTakesImages")}; return newFormTakesImages;`);
  const declared = { claude: { image_paste_keys: ["M-v"] }, pi: { image_paste_keys: [] },
    codex: { image_paste_keys: [], image_paste_transport: "path" } };
  assert.deepStrictEqual(takes(declared, () => "codex")({}), { ok: true, harness: "codex" },
    "Codex path transport offers opening images without a clipboard key");
  assert.deepStrictEqual(takes(declared, () => "claude")({}), { ok: true, harness: "claude" });
  assert.deepStrictEqual(takes(declared, () => "pi")({}), { ok: false, harness: "pi" },
    "a harness that declares no image key is not offered images");
  assert.strictEqual(takes(declared, () => "custom")({}).ok, true,
    "an unknown harness counts as yes -- the daemon answers for itself");
  assert.strictEqual(takes({ codex: {} }, () => "codex")({}).ok, true,
    "an older daemon that publishes no image_paste_keys counts as yes");
  assert.strictEqual(takes(declared, () => "")({}).harness, "claude");
}

/* ---- what an attach keeps ---------------------------------------------- */
{
  const notes = [];
  let renders = 0;
  const env = new Function("$", "URL", "renderOpeningImages", "openingImagesNote",
    "syncOpeningImages", "newFormTakesImages",
    `${kindsBlock}
     ${slice("imageKindOf")}
     ${constLine("OPENING_IMAGES_MAX")}
     const openingImages = [];
     ${slice("addOpeningImages")}
     return { addOpeningImages, openingImages };`)(
    () => ({}), { createObjectURL: (f) => `blob:${f.name}` },
    () => { renders++; }, (t) => notes.push(t), () => {}, () => ({ ok: true }));
  const file = (name, type) => ({ name, type });
  env.addOpeningImages([file("a.png", "image/png"), file("notes.txt", "text/plain"),
    file("b.JPG", "")]);
  assert.deepStrictEqual(env.openingImages.map((i) => i.file.name), ["a.png", "b.JPG"],
    "images are kept in order, the type read off the extension when empty");
  assert.ok(/1 file\(s\) not a PNG/.test(notes.at(-1)), notes.at(-1));
  env.addOpeningImages(Array.from({ length: 12 }, (_, i) => file(`s${i}.webp`, "image/webp")));
  assert.strictEqual(env.openingImages.length, 10, "an opening takes at most ten images");
  assert.ok(/at most 10/.test(notes.at(-1)), notes.at(-1));
  assert.ok(renders >= 2);
}

/* ---- a refused harness attaches nothing --------------------------------- */
{
  let synced = 0;
  const env = new Function("$", "URL", "renderOpeningImages", "openingImagesNote",
    "syncOpeningImages", "newFormTakesImages",
    `${kindsBlock}
     ${slice("imageKindOf")}
     ${constLine("OPENING_IMAGES_MAX")}
     const openingImages = [];
     ${slice("addOpeningImages")}
     return { addOpeningImages, openingImages };`)(
    () => ({}), { createObjectURL: () => "blob:" }, () => {}, () => {},
    () => { synced++; }, () => ({ ok: false, harness: "pi" }));
  env.addOpeningImages([{ name: "a.png", type: "image/png" }]);
  assert.strictEqual(env.openingImages.length, 0);
  assert.strictEqual(synced, 1, "the note says why instead");
}

/* ---- both payloads carry the ids ---------------------------------------- */
{
  const config = new Function(`${slice("sessionFormConfig")}; return sessionFormConfig;`)();
  for (const mode of ["new", "spawn"]) {
    assert.ok(config(mode).payload.includes("opening_images"),
      `${mode}: opening_images must reach the daemon`);
  }
}

/* ---- the upload: one at a time, in order, stopping on a refusal --------- */
(async () => {
  const sent = [];
  const answers = [
    { ok: true, status: 201, doc: { id: "aaaaaaaaaaaaaaaaaaaa.png" } },
    { ok: true, status: 201, doc: { id: "bbbbbbbbbbbbbbbbbbbb.jpg" } },
  ];
  const make = (api) => new Function("api", "openingImages",
    `${kindsBlock}
     ${slice("imageKindOf")}
     async ${slice("uploadOpeningImages")}
     return uploadOpeningImages;`)(api, [
      { file: { name: "a.png", type: "image/png" } },
      { file: { name: "b.jpg", type: "image/jpeg" } },
    ]);
  const api = async (url, opts) => {
    sent.push([url, opts.headers["Content-Type"], opts.body.name]);
    const a = answers.shift();
    return { ok: a.ok, status: a.status, json: async () => a.doc };
  };
  const ids = await make(api)();
  assert.deepStrictEqual(ids, ["aaaaaaaaaaaaaaaaaaaa.png", "bbbbbbbbbbbbbbbbbbbb.jpg"]);
  assert.deepStrictEqual(sent, [
    ["/api/opening-images", "image/png", "a.png"],
    ["/api/opening-images", "image/jpeg", "b.jpg"],
  ]);

  let calls = 0;
  const refusing = async () => {
    calls++;
    return { ok: false, status: 413, json: async () => ({ error: "the image is larger than 24 MiB" }) };
  };
  await assert.rejects(make(refusing)(), /image 1 \(a\.png\): the image is larger than 24 MiB/);
  assert.strictEqual(calls, 1, "the first refusal stops the batch");
  console.log("openingimages_check: ok");
})().catch((err) => { console.error(err); process.exit(1); });
