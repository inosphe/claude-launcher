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
   pre-empts the ceiling instead of erroring after the fact. */
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

/* ---- the card wiring (source-text contract) ------------------------------ */
check("the picker has no direct-rows option",
      src.includes('new Option("직접 설정'), false);
check("the picker lists the saved presets",
      src.includes("for (const p of presets) presetSelect.append(new Option(ragPresetOptionLabel(p), p.id))"),
      true);
check("switching the picker only loads values — no PUT on change",
      !slice("settingsCard").includes("presetSelect.onchange = async"),
      true);
check("switching reloads the fields from the chosen preset",
      [src.includes("presetSelect.onchange = () => {"),
       src.includes("loadPreset(presets.find(p => p.id === presetSelect.value));")],
      [true, true]);
check("the fieldsets stay editable under a preset",
      !src.includes("sourceBox.disabled = customBox.disabled = profileBox.disabled = Boolean(active)"),
      true);
check("a save replaces the selected preset row with the screen values",
      src.includes("body.presets = presets.map(p => p.id === id ? editedPreset(id) : p);"),
      true);
check("a save carries the pointer too",
      src.includes("out.preset = presetSelect.value;"),
      true);
check("a save with no preset selected is refused with a hint",
      src.includes("선택된 프리셓이 없습니다"),
      true);
check("model_key survives an edit only while the model is untouched",
      src.includes('old.embedding_model === preset.embedding_model'),
      true);
check("a new preset appends through the settings PUT and activates",
      src.includes("request(\"api/rag/settings\", { ...values(), presets: [...presets, preset], preset: id }, \"PUT\")"),
      true);
check("the save flow uses the replace-row body",
      src.includes("testOnly ? values() : saveBody()"),
      true);
check("a save restarts the progress watch",
      slice("afterSave").includes("backfillPolls = 0; watchBackfill();"),
      true);
check("the connection test posts the screen rows",
      src.includes('request(testOnly ? "api/rag/test" : "api/rag/settings"'),
      true);
check("the progress line polls the status endpoint",
      [src.includes('request("api/rag/status")'), src.includes("setTimeout(watchBackfill, 3000)")],
      [true, true]);
check("the poll gives up after a bounded number of rounds",
      src.includes("backfillPolls < 200"),
      true);
check("the pause toggle posts the backfill endpoints",
      [src.includes('"api/rag/backfill/pause"'), src.includes('"api/rag/backfill/resume"')],
      [true, true]);
check("the pause button reflects the polled state",
      src.includes('pauseBtn.textContent = backfillPaused ? "재개" : "일시정지"'),
      true);
check("the selftest button posts the selftest endpoint with threshold",
      src.includes('request("api/rag/selftest", {threshold: 0.5, top_k: 5, sample: 20})'),
      true);
check("the selftest result renders in its own line",
      [src.includes('node("p", "", "rag-selftest-result")'), src.includes("selftestResult.hidden = false;")],
      [true, true]);
check("the /v1 correction's opt-out ships as a root-serving checkbox in the custom box",
      src.includes('field("root_serving", "루트 경로에 직접 서빙 (TEI 등 — /v1 경로 없음)", "checkbox", customBox)'),
      true);
check("selecting a preset restores its root-serving flag",
      src.includes("fields.root_serving.checked = p.root_serving === true;"),
      true);
check("the flag is saved on the preset row only when checked",
      slice("editedPreset").includes("if (fields.root_serving.checked) preset.root_serving = true;"),
      true);
check("the picker shows the saved count against the server ceiling",
      [src.includes('node("span", ` ${presets.length}/${presetMax}`, "rag-preset-count")'),
       src.includes("Number(cfg.preset_max) > 0 ? Number(cfg.preset_max) : 20")],
      [true, true]);
check("a delete button sits next to the picker and asks first",
      [src.includes('node("button", "삭제", "rag-preset-delete")'), src.includes("window.confirm")],
      [true, true]);
check("deletion writes the list minus the row through the settings PUT",
      src.includes('request("api/rag/settings", { ...values(), presets: remaining, preset: next }, "PUT")'),
      true);
check("deleting the active preset re-points at the first remaining row",
      src.includes('remaining.length ? remaining[0].id : ""'),
      true);
check("the last preset cannot be deleted — the button refuses up front",
      [src.includes("presets.length <= 1"), src.includes("마지막 프리셓은 삭제할 수 없습니다")],
      [true, true]);
check("the delete success line no longer claims a direct-input default",
      src.includes("직접 연결 값 입력이 기본이 됩니다"),
      false);
check("the add button pre-empts the ceiling with a reason",
      src.includes("프리셓은 ${presetMax}개까지 저장됩니다"),
      true);
check("the count line refreshes after an add and a delete",
      (src.match(/presetCount\.textContent = /g) || []).length >= 2,
      true);
check("the stylesheet ships the state line rule",
      css.includes(".search-settings .rag-preset-state"), true);
check("the preset row keeps the picker, count and delete button on one line",
      [css.includes(".search-settings .rag-preset-row"),
       src.includes('node("label", "프리셋", "rag-preset-row")')],
      [true, true]);
check("the stylesheet ships the selftest result line rule",
      css.includes(".search-settings .rag-selftest-result"), true);

/* ---- claunch-fhs7f.1: what a save writes, renaming, and named stores ----- */
check("the save button says it saves the selected preset",
      src.includes('node("button", "선택한 프리셓 저장")'),
      true);
check("the save result names the row it overwrote",
      src.includes("을 덮어써 저장했습니다"),
      true);
check("a rename control sits with the presets and refuses a taken name",
      [src.includes('node("button", "이름 변경")'), src.includes("이미 있습니다 — 다른 이름을 쓰세요.")],
      [true, true]);
check("a rename carries the old id so the daemon can move the saved secret",
      src.includes("renamed_from: from"),
      true);
check("a rename moves the active pointer only when that preset was the active one",
      [src.includes("body.preset = activeId === from ? to : activeId"),
       src.includes('request("api/rag/settings", body, "PUT")')],
      [true, true]);
check("the card tracks the active preset apart from the picker's selection",
      [src.includes('let activeId = typeof cfg.preset === "string" ? cfg.preset : ""'),
       src.includes("presetSelect.value = data.preset; activeId = data.preset;")],
      [true, true]);
check("a rename says whether the active preset moved",
      [src.includes("활성 프리셓도 ${to}입니다"), src.includes("그대로입니다")],
      [true, true]);
check("the preset row carries a DB name field",
      [src.includes('dbInput.placeholder = "DB 이름 (비우면 모델 이름에서 유도)"'),
       slice("editedPreset").includes("if (db) preset.db = db;")],
      [true, true]);
check("selecting a preset restores its DB name",
      slice("loadPreset").includes('dbInput.value = p.db || "";'),
      true);
check("the card lists the DBs the daemon has on disk",
      [src.includes('request("api/rag/dbs", undefined, "GET")'), src.includes("loadDbs();")],
      [true, true]);
check("a DB row carries a delete control that asks first",
      [src.includes('node("button", "삭제", "rag-db-delete")'), src.includes("window.confirm(`DB ${name}")],
      [true, true]);
check("a DB a preset reads cannot be deleted — the button is disabled and says why",
      [src.includes("del.disabled = occupied"),
       src.includes("이 DB를 쓰는 프리셓이 있어 삭제할 수 없습니다")],
      [true, true]);
check("deleting a DB posts its directory name and reloads the list",
      [src.includes('request("api/rag/dbs/delete", { name: row.name }, "POST")'), src.includes("await loadDbs();")],
      [true, true]);
check("the stylesheet ships the rename, db and list rules",
      [css.includes(".search-settings .rag-preset-rename"),
       css.includes(".search-settings .rag-preset-db"),
       css.includes(".search-settings .rag-db-list"),
       css.includes(".search-settings .rag-db-entry"),
       css.includes(".search-settings .rag-db-delete")],
      [true, true, true, true, true]);

if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log("ragpreset ok");
