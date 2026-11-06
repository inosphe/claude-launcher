/* The preset picker on the Search settings card (search-anything.js
   settingsCard). The card is preset-only since
   claunch-rag-preset-always-hn5bc: the picker addresses one preset, its
   values load into the fields below, and 저장 writes them back over that
   preset -- selecting and editing are the same gesture. Six things must
   hold. The option label names the preset id, its embedding model, and a
   model_key only when that key adds information beyond the model name. The
   summary line a reader sees under an active preset names the model and
   where it lives (profile name or custom endpoint). The per-bucket state
   line on the RAG card (app.js) distinguishes running / waiting / normal /
   failed from one index row and shows the row's own progress, and the
   queue line names the depth, the waiting buckets in order, the last sync
   and the board watch. And the card wiring is the contract: there is no
   "직접 설정" option, switching the picker only loads the preset's values
   into the fields (no PUT -- activation happens on save), the fieldsets stay
   editable under a preset, a save replaces the selected preset's row with
   the screen values and carries the pointer, a new preset appends through
   the settings PUT and becomes active, model_key survives an edit only while
   the embedding model is untouched, the connection-test button POSTs the
   screen rows and the daemon probes those (its /api/rag/test forces the body
   to no preset), the RAG card polls /api/rag/status while work remains (a
   bounded timer, not a promise the click was received), the pause/resume
   toggle POSTs the backfill endpoints, a click reports what the daemon
   answered, and the Selftest button
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
/* The RAG card itself lives in app.js (claunch-i2i95 moved the per-collection
   status, the queue line and the pause control there, under the DB heading
   the settings card also names), so its pure helpers are read from there. */
const app = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "app.js"), "utf8");
const css = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");

function sliceFrom(text, name) {
  let start = text.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  // An `async` function keeps its keyword: a slice that starts at `function`
  // turns every `await` inside it into a syntax error.
  if (text.slice(Math.max(0, start - 6), start) === "async ") start -= 6;
  const body = text.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < text.length; j++) {
    if (text[j] === "{") depth++;
    else if (text[j] === "}") { depth--; if (!depth) return text.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
function slice(name) { return sliceFrom(src, name); }

const api = {};
new Function("exports",
  slice("ragPresetOptionLabel") + "\n" + slice("ragPresetSummary") + "\n" +
  slice("ragDbSlug") + "\n" +
  slice("ragPresetDb") + "\n" +
  slice("ragPresetIdProblem") + "\n" +
  slice("ragDbGroups") + "\n" +
  slice("ragCorpusLabel") + "\n" +
  "exports.dbSlug = ragDbSlug; exports.presetDb = ragPresetDb;\n" +
  "exports.idProblem = ragPresetIdProblem; exports.dbGroups = ragDbGroups;\n" +
  "exports.corpusLabel = ragCorpusLabel;\n" +
  "exports.label = ragPresetOptionLabel; exports.summary = ragPresetSummary;\n")(api);

const cardApi = {};
new Function("exports, beadsCache",
  sliceFrom(app, "ragBucketState") + "\n" + sliceFrom(app, "ragQueueLine") + "\n" +
  sliceFrom(app, "ragBucketLabel") + "\n" + sliceFrom(app, "beadsNameOfRoot") + "\n" +
  sliceFrom(app, "ragDiskDue") + "\n" + sliceFrom(app, "ragPlaceholderRows") + "\n" +
  sliceFrom(app, "ragPlaceholderLabel") + "\n" + sliceFrom(app, "ragPlaceholderState") + "\n" +
  sliceFrom(app, "ragDbGroups") + "\n" +
  "exports.bucketState = ragBucketState; exports.queueLine = ragQueueLine;\n" +
  "exports.bucketLabel = ragBucketLabel; exports.diskDue = ragDiskDue;\n" +
  "exports.placeholderRows = ragPlaceholderRows;\n" +
  "exports.placeholderLabel = ragPlaceholderLabel;\n" +
  "exports.placeholderState = ragPlaceholderState;\n" +
  "exports.dbGroups = ragDbGroups;")(cardApi, null);

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

/* ---- per-bucket state on the RAG card (app.js) --------------------------- */
check("the combined corpus is named by what it holds, under either of its names",
      [cardApi.bucketLabel({ kind: "unified" }), cardApi.bucketLabel({ kind: "all" })],
      ["Search anything (통합: 이슈·코멘트·세션)", "Search anything (통합: 이슈·코멘트·세션)"]);
check("the fleet bucket keeps its own name",
      cardApi.bucketLabel({ kind: "sessions" }), "sessions (세션)");
check("a bucket with no problem reads as normal",
      cardApi.bucketState({ kind: "sessions", indexed: 2906, total: 2906, pending: 0 }),
      { state: "ok", text: "정상" });
check("a bucket with unembedded documents reads as waiting with its count",
      cardApi.bucketState({ kind: "sessions", indexed: 4, total: 2906, pending: 2902 }),
      { state: "pending", text: "대기 2902건" });
check("a syncing bucket draws its own progress, not a queue-wide total",
      cardApi.bucketState({ kind: "all", indexed: 120, total: 480, pending: 360, syncing: true }),
      { state: "busy", text: "색인 중 120/480 (25%)" });
check("a cancelled pass and a queued rebuild each say so on the busy line",
      [cardApi.bucketState({ kind: "all", indexed: 1, total: 4, syncing: true, cancel_requested: true }).text,
       cardApi.bucketState({ kind: "all", indexed: 1, total: 4, syncing: true, force_pending: true }).text],
      ["색인 중 1/4 (25%) · 취소 요청됨", "색인 중 1/4 (25%) · 재색인 예약"]);
check("an error outranks every other state and keeps the daemon's words",
      cardApi.bucketState({ kind: "all", error: "dims 4096 != 768", syncing: true, pending: 9 }),
      { state: "failed", text: "dims 4096 != 768" });
check("a failed row carries the time of the pass that failed",
      cardApi.bucketState({ kind: "all", error: "dims 4096 != 768",
                            finished_at: "2026-11-04T08:05:09+00:00" }),
      { state: "failed", text: "dims 4096 != 768 · 마지막 실패 2026-11-04 08:05:09" });
check("a bucket with no counts still draws a percentage of zero",
      cardApi.bucketState({ kind: "all", indexed: 0, total: 0, syncing: true }),
      { state: "busy", text: "색인 중 0/0 (0%)" });

/* ---- the queue line ------------------------------------------------------ */
check("an idle queue names the depth, the last sync and the board watch",
      cardApi.queueLine({ depth: 0, pending: [], last_consumed_at: "2026-11-04T08:05:09+00:00",
                          watch_interval: 60 }),
      "대기 0 · 마지막 sync 2026-11-04 08:05:09 · 보드 확인 60초");
check("a queue that never ran says so, and a paused watch says it is off",
      cardApi.queueLine({ depth: 0 }),
      "대기 0 · 아직 sync 없음 · 보드 감시 꺼짐");
check("waiting buckets keep their order on the line",
      cardApi.queueLine({ depth: 2, pending: ["sessions", "beads-abc"], watch_interval: 30 }),
      "대기 2 · 순서: sessions → beads-abc · 아직 sync 없음 · 보드 확인 30초");

/* ---- claunch-c7vjm: collections on disk that no index has opened ---------- */
/* The card drew only /api/rag/status's indexes, so a collection on disk that
   no search or reindex pass had touched yet was invisible there while the DB
   panel listed it. The placeholder rows close that gap, and the disk listing
   they come from must not ride the 3-second status poll -- /api/rag/dbs walks
   every collection to total its files. */
const statusWithOneOpen = {
  db: "embed", dimensions: 768,
  indexes: [{ kind: "sessions", path: "/idx/sessions.embed.zvec", db: "embed",
              db_configured: "embed", dims: 768, indexed: 5, total: 5 }],
};
const diskRows = [
  { name: "sessions.embed", corpus: "sessions", identity: "embed",
    path: "/idx/sessions.embed.zvec", dims: 768, model: "embed" },
  { name: "unified.embed", corpus: "unified", identity: "embed",
    path: "/idx/unified.embed.zvec", dims: 4096, model: "embed" },
  { name: "beads-abc123.sessions", corpus: "beads-abc123", identity: "sessions",
    path: "/idx/beads-abc123.sessions.zvec", dims: 0, model: "" },
];
const unopened = cardApi.placeholderRows(statusWithOneOpen, diskRows);
check("only the collections no index has opened become placeholders",
      unopened.map(r => r.name), ["unified.embed", "beads-abc123.sessions"]);
check("the difference is taken by directory, so a legacy path is not counted twice",
      cardApi.placeholderRows(
        { indexes: [{ path: "/idx/all.embed.zvec", kind: "unified" }] },
        [{ name: "all.embed", path: "/idx/all.embed.zvec" }]).length, 0);
check("a placeholder names its corpus, not the DB it folds under",
      unopened.map(r => cardApi.placeholderLabel(r)),
      ["Search anything (통합: 이슈·코멘트·세션)", "board beads-abc123"]);
check("a width the running model cannot use is said before the open, not after",
      [cardApi.placeholderState(unopened[0], statusWithOneOpen),
       cardApi.placeholderState(unopened[1], statusWithOneOpen)],
      [{ state: "unopened", text: "아직 열리지 않음 · 열리면 실패 예상 — 지금 설정은 768차원" },
       { state: "unopened", text: "아직 열리지 않음" }]);
const placeholderGroups = cardApi.dbGroups(statusWithOneOpen, unopened);
check("a placeholder sits under its own DB beside the rows that are running",
      placeholderGroups.map(g => [g.name, g.rows.map(r => Boolean(r.placeholder))]),
      [["embed", [false, true]], ["sessions", [true]]]);
check("a DB drawn only from the disk listing carries no Sync/Rebuild",
      placeholderGroups.filter(g => !g.rows.some(r => !r.placeholder)).map(g => g.name),
      ["sessions"]);
check("the disk listing rides its own minute, never the status poll's 3 seconds",
      [cardApi.diskDue(30, 10, 60), cardApi.diskDue(70, 10, 60), cardApi.diskDue(1, 0, 60),
       /RAG_POLL_MS = 3000/.test(app), /RAG_DISK_POLL_MS = 60000/.test(app),
       // the 3-second refresh only asks the disk for a listing when the
       // minute is up -- the call itself lives outside it
       sliceFrom(app, "refreshRagStatus")
         .includes("ragDiskDue(Date.now(), ragDiskAt, RAG_DISK_POLL_MS)"),
       sliceFrom(app, "refreshRagStatus").includes("rag/dbs")],
      [false, true, true, true, true, true, false]);
check("the disk listing asks for the rows without the size walk",
      sliceFrom(app, "refreshRagDisk").includes('"/api/rag/dbs?sizes=0"'), true);
check("the two listings are compared by the directory, the only name they share",
      [sliceFrom(app, "ragPlaceholderRows").includes("row.path"),
       sliceFrom(app, "ragPlaceholderRows").includes("row.name")],
      [true, false]);

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
check("the RAG card polls the status endpoint while work remains, bounded",
      [app.includes('api("/api/rag/status")'), app.includes("ragPolls >= RAG_POLL_LIMIT"),
       app.includes("const RAG_POLL_MS = 3000"),
       app.includes('setTimeout(() => { if (wsOpen && wsSection === "") refreshRagStatus(); }, RAG_POLL_MS)')],
      [true, true, true, true]);
check("the pause toggle posts the backfill endpoints and mirrors the polled state",
      [app.includes('"/api/rag/backfill/pause"'), app.includes('"/api/rag/backfill/resume"'),
       app.includes('st.backfill_paused ? "재개" : "일시정지"')],
      [true, true, true]);
check("a click on the card reports what the daemon answered, before and after",
      [app.includes("ragNotice = `${group.name} ${label} 요청 중…`;"),
       app.includes("ragNotice = `${group.name} ${label} — ${outcomes.join(\" · \")}`;"),
       app.includes("이미 색인 중 — 실행하지 않음"), app.includes("재색인 시작"),
       app.includes('el("p", "wf-note rag-notice", ragNotice)')],
      [true, true, true, true, true]);
check("a syncing bucket can be cancelled from its own row",
      [app.includes('api("/api/rag/cancel"'), app.includes('row.cancel_requested ? "취소 요청됨" : "취소"'),
       app.includes('el("button", "wf-btn clear", row.cancel_requested ? "취소 요청됨" : "취소")')],
      [true, true, true]);
check("the card draws the request's notice and lock before the first await",
      [app.includes("let ragBusy = false;"), app.includes("ragBusy = true;"),
       app.includes("renderWorkspaces();  // the notice and the locked buttons, before the first await"),
       app.includes("renderWorkspaces();  // drawn before the request, like ragDbAction"),
       app.includes("} finally {\n    ragBusy = false;\n  }")],
      [true, true, true, true, true]);

/* ---- the click's first frame, run rather than read -----------------------
   A source check cannot tell "the notice is set" from "the notice is drawn";
   the peer review found exactly that gap. So `ragDbAction` is run here
   against stubs: the render count and ragBusy are read straight after the
   call (before its first await resolves) and again after it settles. */
const clickApi = {};
new Function("exports", `
  let ragPolls = 0, ragBusy = false, ragNotice = "";
  let renders = 0;
  const renderWorkspaces = () => { renders++; };
  const ragBucketLabel = row => "bucket-" + row.kind;
  async function ragReindex(row, force) { await Promise.resolve(); return { started: true }; }
  async function refreshRagStatus() { renders += 0; }
  ${sliceFrom(app, "ragDbAction")}
  exports.click = (group) => {
    const pending = ragDbAction({}, group, false);
    const during = { renders, busy: ragBusy, notice: ragNotice };
    return { during, done: pending.then(() => ({ renders, busy: ragBusy, notice: ragNotice })) };
  };
`)(clickApi);
const click = clickApi.click({ name: "kept", rows: [{ kind: "all" }, { kind: "sessions" }] });
check("a click renders before the request is awaited, with the notice and the lock",
      [click.during.renders, click.during.busy, click.during.notice],
      [1, true, "kept Sync 요청 중…"]);
const laterChecks = [];
function checkLater(label, promise, want) {
  laterChecks.push(promise.then(got => check(label, got, want)));
}
checkLater("the lock and the notice settle into the daemon's answer",
           click.done.then(a => [a.busy, a.notice]),
           [false, "kept Sync — bucket-all: 색인 시작 · bucket-sessions: 색인 시작"]);

check("every control is disabled while a request is in flight",
      [app.includes("pause.disabled = ragBusy;"), app.includes("sync.disabled = ragBusy;"),
       app.includes("force.disabled = ragBusy;"),
       app.includes("cancel.disabled = Boolean(row.cancel_requested) || ragBusy;")],
      [true, true, true, true]);
check("the card draws one block per DB, with Sync and Rebuild on its heading",
      [app.includes("for (const group of ragDbGroups(st, placeholders))"), app.includes("`DB ${group.name}`"),
       app.includes('el("button", "wf-btn clear", "Sync")'), app.includes('el("button", "wf-btn clear", "Rebuild")')],
      [true, true, true, true]);
check("the two buttons hang off a DB that has an open collection to act on",
      sliceFrom(app, "ragCard").includes("group.rows.some(row => !row.placeholder)"), true);
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
       card.includes("del.disabled = occupied;"), card.includes("이 DB를 읽는 프리셋이 있어 삭제할 수 없습니다")],
      [true, true, true, true]);
/* The table used to be one row per DB with a single "쓰는 프리셋" cell holding
   the union of its collections' readers, which read as "the presets that
   write into this DB". A preset adopts one collection per corpus, so the same
   presets appear on each of that DB's rows; the table now draws one row per
   collection and names the cell for what it answers (claunch-rk0od). */
check("a board collection's corpus key shows as beads, the rest as themselves",
      [api.corpusLabel("sessions"), api.corpusLabel("unified"),
       api.corpusLabel("beads-0123456789ab"), api.corpusLabel(""), api.corpusLabel(undefined)],
      ["sessions", "unified", "beads", "?", "?"]);
check("the table's header names corpus, 형식 and the adopting presets",
      card.includes('["DB 이름", "corpus", "형식", "이 컬렉션을 채택하는 프리셋", "폭", "모델", ""]'),
      true);
check("each collection gets its own row, its own presets and its own width",
      [card.includes("const rows = g.collections.length ? g.collections : ["),
       card.includes('row.presets.length ? row.presets.join(", ")'),
       card.includes('row.dims ? String(row.dims) : "—"')],
      [true, true, true]);
check("the DB cell and the actions span the group's rows, and rename opens under the last",
      [card.includes("nameCell.rowSpan = rows.length"), card.includes("actions.rowSpan = rows.length"),
       card.includes("renameDbRow(rows.length > 1 ? lastTr : tr, g)")],
      [true, true, true]);
check("the RAG card's DB header lists every width, not the first row's",
      [app.includes("if (row.dims && !g.dims.includes(row.dims)) g.dims.push(row.dims);"),
       app.includes('group.dims.length ? ` · ${group.dims.join("/")}차원` : ""')],
      [true, true]);
check("no node() call passes a class name as its text (the 'rag-preset-db' label leak)",
      /node\("(?:p|div|span)", "rag-[a-z-]+"\)/.test(src), false);
check("the stylesheet ships the card's rules",
      [".search-settings .rag-summary", ".search-settings .rag-hint", ".search-settings .rag-preset-row",
       ".search-settings .rag-preset-state", ".search-settings .rag-selftest-result", ".search-settings .rag-db-table",
       ".search-settings .rag-db-use", ".search-settings .rag-db-scroll"].map(rule => css.includes(rule)),
      [true, true, true, true, true, true, true, true]);
check("the stale per-entry flex rule is gone (it broke the table rows)",
      css.includes(".search-settings .rag-db-entry { display: flex"), false);

Promise.all(laterChecks).then(() => {
  if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
  console.log("ragpreset ok");
});
