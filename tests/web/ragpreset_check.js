/* The preset picker on the Search settings card (search-anything.js
   settingsCard, the claunch-7m5v8 follow-up). Six things must hold.
   The option label names the preset id, its embedding model, and a
   model_key only when that key adds information beyond the model name.
   The summary line a reader sees under an active preset names the model
   and where it lives (profile name or custom endpoint). The backfill
   totals sum pending/total across the daemon's index rows and tolerate
   rows that carry no counts. The backfill state line distinguishes
   running / idle / done from the status payload, and the active line
   names the preset (or says the direct rows are in force) with the model
   and model_key, staying empty when the daemon has no active record.
   And the card wiring is the contract: switching the picker PUTs only
   the preset pointer and restores the previous selection on failure,
   the connection fieldsets go read-only under a preset, values() carries
   the pointer on a normal save, the progress line polls /api/rag/status
   until nothing is pending, and the Selftest button POSTs the selftest
   endpoint with threshold/top_k/sample and renders the verdict in its
   own result line. */
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "search-anything.js"), "utf8");
const css = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");

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

const api = {};
new Function("exports",
  slice("ragPresetOptionLabel") + "\n" + slice("ragPresetSummary") + "\n" +
  slice("ragBackfillTotals") + "\n" + slice("ragBackfillStateLine") + "\n" +
  slice("ragActiveText") + "\n" +
  "exports.label = ragPresetOptionLabel; exports.summary = ragPresetSummary;\n" +
  "exports.totals = ragBackfillTotals; exports.stateLine = ragBackfillStateLine;\n" +
  "exports.active = ragActiveText;")(api);

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) return;
  failures++;
  console.log(`FAIL ${label}\n  got  ${g}\n  want ${w}`);
}

/* ---- option labels ------------------------------------------------------ */
check("the label names id and model",
      api.label({ id: "qwen8b", embedding_model: "qwen3-embedding-8b" }),
      "qwen8b · qwen3-embedding-8b");
check("a model_key that differs from the model name is shown",
      api.label({ id: "mx8b", embedding_model: "Qwen3-Embedding-8B", model_key: "qwen3-embedding-8b" }),
      "mx8b · Qwen3-Embedding-8B · model_key qwen3-embedding-8b");
check("a model_key equal to the model name adds nothing and is dropped",
      api.label({ id: "qwen8b", embedding_model: "qwen3-embedding-8b", model_key: "qwen3-embedding-8b" }),
      "qwen8b · qwen3-embedding-8b");
check("a preset with no model still has a label",
      api.label({ id: "draft" }), "draft");

/* ---- the active-preset summary ------------------------------------------ */
check("a profile preset says which profile",
      api.summary({ id: "p1", source: "profile", profile: "fireworks", embedding_model: "qwen3-embedding-8b" }),
      "p1 — qwen3-embedding-8b (profile fireworks)");
check("a custom preset says its endpoint",
      api.summary({ id: "c1", source: "custom", base_url: "http://localhost:9999/v1", embedding_model: "m1" }),
      "c1 — m1 (http://localhost:9999/v1)");
check("an unmodelled preset says so rather than omitting the model",
      api.summary({ id: "d1", source: "custom" }),
      "d1 — 모델 미지정 (custom endpoint)");

/* ---- backfill totals ----------------------------------------------------- */
check("pending and total sum across index rows",
      api.totals([{ kind: "sessions", pending: 2902, total: 2906 },
                  { kind: "all", pending: 33615, total: 109440 }]),
      { pending: 36517, total: 112346 });
check("rows without counts and a missing list are tolerated",
      [api.totals([{ kind: "all" }, { kind: "sessions", pending: 2 }]), api.totals(undefined)],
      [{ pending: 2, total: 0 }, { pending: 0, total: 0 }]);

/* ---- backfill state line ------------------------------------------------- */
check("a syncing row means running regardless of pending",
      api.stateLine([{ kind: "sessions", pending: 0, total: 100, syncing: true }],
                    { preset: "qwen8b", embedding_model: "m1", model_key: "m1" }),
      { state: "running", text: "소급 임베딩 진행 중 · 남은 문서 0 / 100건" });
check("pending above zero means running",
      api.stateLine([{ kind: "sessions", pending: 12, total: 200 }],
                    { preset: "qwen8b", embedding_model: "m1", model_key: "m1" }),
      { state: "running", text: "소급 임베딩 진행 중 · 남은 문서 12 / 200건" });
check("no index rows at all means idle",
      api.stateLine([], { preset: "qwen8b", embedding_model: "m1", model_key: "m1" }),
      { state: "idle", text: "색인 미구동 — 아직 임베딩된 문서가 없습니다." });
check("zero total on every row means idle",
      api.stateLine([{ kind: "sessions", pending: 0, total: 0 }], null),
      { state: "idle", text: "색인 미구동 — 아직 임베딩된 문서가 없습니다." });
check("a finished backfill with an active record appends the active line",
      api.stateLine([{ kind: "sessions", pending: 0, total: 150 }],
                    { preset: "qwen8b", embedding_model: "qwen3-embedding-8b", model_key: "qwen3-embedding-8b" }),
      { state: "done", text: "활성: 프리셋 qwen8b · qwen3-embedding-8b · model_key qwen3-embedding-8b · 색인 갱신 완료" });
check("a finished backfill with no active record is plain done",
      api.stateLine([{ kind: "sessions", pending: 0, total: 150 }], null),
      { state: "done", text: "색인 갱신 완료" });

/* ---- the active line ----------------------------------------------------- */
check("a preset active record names the preset, model and key",
      api.active({ preset: "qwen8b", embedding_model: "qwen3-embedding-8b", model_key: "qwen3-embedding-8b" }),
      "활성: 프리셋 qwen8b · qwen3-embedding-8b · model_key qwen3-embedding-8b");
check("a direct (legacy) active record says so",
      api.active({ preset: "", embedding_model: "nomic-embed-text", model_key: "nomic-embed-text" }),
      "활성: 직접 설정 · nomic-embed-text · model_key nomic-embed-text");
check("no active record means no line",
      [api.active(null), api.active({}), api.active({ preset: "", embedding_model: "", model_key: "" })],
      ["", "", ""]);

/* ---- the card wiring (source-text contract) ------------------------------ */
check("switching the picker PUTs only the preset pointer",
      src.includes('request("api/rag/settings", {preset: presetSelect.value}, "PUT")'), true);
check("a failed switch restores the previous selection",
      src.includes("presetSelect.value = savedPreset;"), true);
check("the connection fieldsets go read-only while a preset is in force",
      src.includes("sourceBox.disabled = customBox.disabled = profileBox.disabled = Boolean(active)"), true);
check("a normal save carries the pointer too",
      src.includes("out.preset = presetSelect.value;"), true);
check("the progress line polls the status endpoint",
      [src.includes('request("api/rag/status")'), src.includes("setTimeout(watchBackfill, 3000)")],
      [true, true]);
check("the poll gives up after a bounded number of rounds",
      src.includes("backfillPolls < 200"), true);
check("a save restarts the progress watch",
      src.includes("backfillPolls = 0; watchBackfill();"), true);
check("the picker offers the direct rows alongside the saved presets",
      src.includes('new Option("직접 설정 (아래 연결 값 사용)", "")'), true);
check("the selftest button posts the selftest endpoint with threshold",
      src.includes('request("api/rag/selftest", {threshold: 0.5, top_k: 5, sample: 20})'), true);
check("the selftest result renders in its own line",
      [src.includes('node("p", "", "rag-selftest-result")'), src.includes("selftestResult.hidden = false;")],
      [true, true]);
check("the stylesheet ships the state line rule",
      css.includes(".search-settings .rag-preset-state"), true);
check("the stylesheet ships the selftest result line rule",
      css.includes(".search-settings .rag-selftest-result"), true);

if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log("ragpreset ok");
