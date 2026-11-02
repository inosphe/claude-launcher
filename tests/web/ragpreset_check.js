/* The preset picker on the Search settings card (search-anything.js
   settingsCard). The card is preset-only since
   claunch-rag-preset-always-hn5bc: the picker addresses one preset, its
   values load into the fields below, and 저장 writes them back over that
   preset -- selecting and editing are the same gesture. Six things must
   hold. The option label names the preset id, its embedding model, and a
   model_key only when that key adds information beyond the model name. The
   summary line a reader sees under an active preset names the model and
   where it lives (profile name or custom endpoint). The backfill totals
   sum pending/total across the daemon's index rows and tolerate rows that
   carry no counts. The backfill state line distinguishes running / idle /
   done / paused from the status payload, and the active line names the
   preset (or says the direct rows are in force) with the model and
   model_key, staying empty when the daemon has no active record. And the
   card wiring is the contract: there is no "직접 설정" option, switching
   the picker only loads the preset's values into the fields (no PUT --
   activation happens on save), the fieldsets stay editable under a preset,
   a save replaces the selected preset's row with the screen values and
   carries the pointer, a new preset appends through the settings PUT and
   becomes active, model_key survives an edit only while the embedding
   model is untouched, the connection-test button POSTs the screen rows and
   the daemon probes those (its /api/rag/test forces the body to no preset),
   the progress line polls /api/rag/status until nothing is pending, the
   pause/resume toggle POSTs the backfill endpoints, and the Selftest button
   POSTs the selftest endpoint with threshold/top_k/sample and renders the
   verdict in its own result line. And the follow-ups: the /v1 correction's
   opt-out (claunch-rag-preset-always-hn5bc.1) ships as a root-serving
   checkbox in the Custom API box whose flag rides the preset row and the
   test probe; and the preset list is deletable (claunch-rag-preset-always-
   hn5bc.2) — a 삭제 button next to the picker confirms, then writes the list
   minus the row with the pointer moved to the first remaining preset, the
   last preset cannot be deleted at all (the preset-only card keeps at
   least one — an empty list would be re-wrapped by the read-side
   migration and the delete would silently do nothing, so the button
   refuses up front and points at edit-in-place), the picker shows the
   saved count against the server's preset_max, and the add button
   pre-empts the ceiling instead of erroring after the fact. The redesign
   (claunch-r4796) adds: the DB name slug shown before a save, the preset id
   rule checked as typed, and the DB panel folded by name -- one row per
   named DB across its corpora, a name that fills the DB field when clicked,
   and rename/delete acting on the whole DB. */
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
  slice("ragActiveText") + "\n" + slice("ragDbSlug") + "\n" + slice("ragPresetDb") + "\n" +
  slice("ragPresetIdProblem") + "\n" + slice("ragDbGroups") + "\n" +
  "exports.dbSlug = ragDbSlug; exports.presetDb = ragPresetDb;\n" +
  "exports.idProblem = ragPresetIdProblem; exports.dbGroups = ragDbGroups;\n" +
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
check("no index rows under a preset says that preset has no index yet",
      api.stateLine([], { preset: "qwen8b", embedding_model: "m1", model_key: "m1" }),
      { state: "idle", text: "프리셓 qwen8b의 색인은 아직 없습니다 — 소급 임베딩이 시작되면 진행률이 여기에 표시됩니다." });
check("no index rows with no active record means idle",
      api.stateLine([], null),
      { state: "idle", text: "색인 미구동 — 아직 임베딩된 문서가 없습니다." });
check("zero total on every row with no active record means idle",
      api.stateLine([{ kind: "sessions", pending: 0, total: 0 }], null),
      { state: "idle", text: "색인 미구동 — 아직 임베딩된 문서가 없습니다." });
check("a finished backfill with an active record appends the active line",
      api.stateLine([{ kind: "sessions", pending: 0, total: 150 }],
                    { preset: "qwen8b", embedding_model: "qwen3-embedding-8b", model_key: "qwen3-embedding-8b" }),
      { state: "done", text: "활성: 프리셋 qwen8b · qwen3-embedding-8b · model_key qwen3-embedding-8b · 색인 갱신 완료" });
check("a finished backfill with no active record is plain done",
      api.stateLine([{ kind: "sessions", pending: 0, total: 150 }], null),
      { state: "done", text: "색인 갱신 완료" });
check("a paused backfill names the hold and keeps the counts",
      api.stateLine([{ kind: "sessions", pending: 12, total: 200 }], null, true),
      { state: "paused", text: "소급 임베딩 일시정지됨 · 남은 문서 12 / 200건" });

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

/* ---- claunch-r4796: DB names, preset ids and the DB panel's rows ---------- */
check("a DB name slugs the way the daemon stores it",
      [api.dbSlug("My DB"), api.dbSlug("bambocher--embeddinggemma-2-oQ8"), api.dbSlug("  -x-  "), api.dbSlug("!!!"),
       api.dbSlug("a".repeat(60)).length],
      ["my-db", "bambocher-embeddinggemma-2-oq8", "x", "", 48]);
check("a preset's DB is its db, else its model key, else its model's slug",
      [api.presetDb({ db: "Kept", model_key: "k", embedding_model: "m" }),
       api.presetDb({ model_key: "k", embedding_model: "m" }),
       api.presetDb({ embedding_model: "Qwen/Qwen3-8B" }), api.presetDb(null)],
      ["kept", "k", "qwen-qwen3-8b", ""]);
check("a preset id follows the daemon's rule and the list's limits",
      [api.idProblem("ok-1", [], 20), api.idProblem("", [], 20) !== "",
       api.idProblem("Bad", [], 20) !== "", api.idProblem("-x", [], 20) !== "",
       api.idProblem("a".repeat(33), [], 20) !== "",
       api.idProblem("p", [{ id: "p" }], 20), api.idProblem("q", [{ id: "p" }], 1),
       api.idProblem("p", [{ id: "p" }], 0, "p"), api.idProblem("q", [{ id: "p" }], 0, "p")],
      ["", true, true, true, true,
       "프리셋 p이 이미 있습니다. 다른 이름을 쓰세요.", "프리셋은 1개까지 저장됩니다. 프리셋을 삭제한 뒤 다시 시도하세요.",
       "", ""]);
const groups = api.dbGroups([
  { name: "sessions.embed", identity: "embed", dims: 768, model: "embed", presets: ["a"], in_use: true, bytes: 10 },
  { name: "all.embed", identity: "embed", dims: 768, model: "embed", presets: ["a", "b"], in_use: false, bytes: 5 },
  { name: "sessions.old", identity: "old", dims: 1024, model: "x", presets: [], in_use: false, bytes: 1 },
  { name: "sessions", identity: "", dims: 768, model: "Legacy M", model_key: "", presets: [], bytes: 2 },
  null]);
check("a named DB's corpora fold into one row; a legacy collection keeps its own",
      groups.map(g => [g.name, g.legacy, g.fill, g.collections.length, g.dims, g.presets, g.in_use, g.bytes]),
      [["embed", false, "embed", 2, [768], ["a", "b"], true, 15],
       ["old", false, "old", 1, [1024], [], false, 1],
       ["sessions", true, "legacy-m", 1, [768], [], false, 2]]);
check("a missing list folds to no rows", api.dbGroups(undefined), []);

/* ---- the card wiring (source-text contract) ------------------------------ */
const card = slice("settingsCard");
check("the picker has no direct-rows option", src.includes('new Option("직접 설정'), false);
check("switching the picker only loads values — no PUT on change",
      [card.includes("presetSelect.onchange = async"), card.includes("presetSelect.onchange = () => {"),
       card.includes("loadPreset(presets.find(p => p.id === presetSelect.value));")],
      [false, true, true]);
check("the card shows the active preset apart from the picker's selection",
      [card.includes('let activeId = typeof cfg.preset === "string" ? cfg.preset : ""'),
       card.includes('"활성 프리셋", "rag-summary-key"'), card.includes('selected.id === activeId ? "활성" : "비활성"')],
      [true, true, true]);
check("a save replaces the selected preset row with the screen values and carries the pointer",
      [card.includes("body.presets = presets.map(p => p.id === id ? editedPreset(id) : p);"),
       card.includes("out.preset = presetSelect.value;"), card.includes("testOnly ? values() : saveBody()")],
      [true, true, true]);
check("the save button names the preset it writes and that it activates it",
      card.includes("saveBtn.textContent = selected ? `${selected.id} 저장하고 활성화` : \"저장하고 활성화\";"), true);
check("a save with no preset selected is refused with a hint", card.includes("선택된 프리셋이 없습니다"), true);
check("model_key survives an edit only while the model is untouched",
      slice("editedPreset").includes("old.embedding_model === preset.embedding_model"), true);
check("a new preset appends through the settings PUT and activates",
      card.includes('request("api/rag/settings", { ...values(), presets: [...presets, preset], preset: id }, "PUT")'), true);
check("preset ids are checked as typed, before any request",
      [card.includes("ragPresetIdProblem(id, presets, presetMax)"), card.includes("sub.input.oninput")], [true, true]);
check("the add button is disabled at the ceiling and says why",
      [card.includes("createOpen.disabled = presets.length >= presetMax;"), card.includes("개까지 저장됩니다. 먼저 하나를 삭제하세요.")],
      [true, true]);
check("the last preset cannot be deleted — disabled with a reason and refused",
      [card.includes("deletePresetBtn.disabled = presets.length <= 1;"), card.includes("마지막 프리셋은 삭제할 수 없습니다")],
      [true, true]);
check("deletion writes the list minus the row, re-pointing only when the active one went",
      [card.includes('request("api/rag/settings", { ...values(), presets: remaining, preset: next }, "PUT")'),
       card.includes('wasActive ? (remaining.length ? remaining[0].id : "") : activeId')],
      [true, true]);
check("a preset rename carries the old id and moves the pointer only when it was active",
      [card.includes("renamed_from: from"), card.includes("body.preset = activeId === from ? to : activeId")],
      [true, true]);
check("the connection test posts the screen rows and keeps the width it measured",
      [card.includes('request(testOnly ? "api/rag/test" : "api/rag/settings"'), card.includes("testedDims = Number(data.dimensions) || 0;")],
      [true, true]);
check("the progress line polls the status endpoint, bounded",
      [card.includes('request("api/rag/status")'), card.includes("setTimeout(watchBackfill, 3000)"), card.includes("backfillPolls < 200")],
      [true, true, true]);
check("the pause toggle posts the backfill endpoints and mirrors the polled state",
      [card.includes('"api/rag/backfill/pause"'), card.includes('"api/rag/backfill/resume"'),
       card.includes('pauseBtn.textContent = backfillPaused ? "재개" : "일시정지"')],
      [true, true, true]);
check("the selftest button posts threshold/top_k/sample and renders its own line",
      [card.includes('request("api/rag/selftest", {threshold: 0.5, top_k: 5, sample: 20})'),
       card.includes('node("p", "", "rag-selftest-result")')],
      [true, true]);
check("the root-serving opt-out sits in the custom box and rides the preset row",
      [card.includes('field("root_serving", "루트 경로에 직접 서빙 (TEI 등 — /v1 경로 없음)", "checkbox", customBox,'),
       slice("loadPreset").includes("fields.root_serving.checked = p.root_serving === true;"),
       slice("editedPreset").includes("if (fields.root_serving.checked) preset.root_serving = true;")],
      [true, true, true]);
check("the DB field restores per preset and shows the name a save will store",
      [slice("loadPreset").includes('dbInput.value = p.db || "";'), slice("editedPreset").includes("if (db) preset.db = db;"),
       slice("showDbResolved").includes("ragDbSlug(fields.embedding_model.value)")],
      [true, true, true]);
check("the DB list is read from the daemon and folded by name",
      [card.includes('request("api/rag/dbs", undefined, "GET")'), card.includes("dbGroups = ragDbGroups(data.dbs);")],
      [true, true]);
check("clicking a DB name fills the DB field",
      [card.includes('button(g.legacy ? `${g.name} (이전 형식)` : g.name, "rag-db-use")'),
       card.includes("dbInput.value = g.fill; showDbResolved();")],
      [true, true]);
check("a DB rename posts from/to and the field follows the DB the edited preset reads",
      [card.includes('request("api/rag/dbs/rename", { from: g.name, to }, "POST")'),
       card.includes("const screenDb = ragDbSlug(dbInput.value) || ragDbSlug(fields.embedding_model.value);") &&
       card.includes("if (screenDb === g.name || repointed) dbInput.value = data.db;")],
      [true, true]);
check("a DB delete asks first, takes the whole named DB and is disabled while read",
      [card.includes("window.confirm(`DB ${g.name}"), card.includes("g.legacy ? { name: g.name } : { db: g.name }"),
       card.includes("del.disabled = occupied;"), card.includes("이 DB를 쓰는 프리셋이 있어 삭제할 수 없습니다")],
      [true, true, true, true]);
check("no node() call passes a class name as its text (the 'rag-preset-db' label leak)",
      /node\("(?:p|div|span)", "rag-[a-z-]+"\)/.test(src), false);
check("the stylesheet ships the card's rules",
      [".search-settings .rag-summary", ".search-settings .rag-hint", ".search-settings .rag-preset-row",
       ".search-settings .rag-preset-state", ".search-settings .rag-selftest-result", ".search-settings .rag-db-table",
       ".search-settings .rag-db-use", ".search-settings .rag-db-scroll"].map(rule => css.includes(rule)),
      [true, true, true, true, true, true, true, true]);
check("the stale per-entry flex rule is gone (it broke the table rows)",
      css.includes(".search-settings .rag-db-entry { display: flex"), false);

if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log("ragpreset ok");
