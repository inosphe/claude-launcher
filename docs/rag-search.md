# Semantic search over the board and the fleet — design and UI plan

Branch `s479-rag-search`, issue `claunch-omgf`. What was built, what it was
measured against, and the plan for the search and filter surfaces the
dashboard still lacks — the first slice is delivered here, the rest is
listed so it lands as follow-up issues rather than as folklore.

## The idea in one paragraph

Two corpora already exist as text: the repository board (every beads issue,
with its title, labels and the structured description the workflows make
agents write) and the fleet (every session's opening task, identity, linked
issue and the cached LLM briefing). Both are searched today by scrolling or
by a substring over id and title. An embedding endpoint turns "the issue
about the kanban in_ready lane I filed last week" and "which session is on
the relay" into ranked answers, in either language, and a reranker settles
the order of the dozen best. The daemon owns the index and the key; the
dashboard, the CLI and any agent ask it the same `GET /api/search`.

## Configuration — the `rag:` block

Top-level in `~/.claude-launcher/.claunch.yaml`, beside `llm:` (which stays what it is: the
backend the briefing posts to — a profile, or a `chat/completions` URL and key
typed in, held either in the block itself or in one of its saved `presets:`;
edited in the dashboard's **Settings ▸ Briefing model** card). `store.rag_config()` is
the one reader; every key has a default and a malformed block is the
disabled default, never an error.

```yaml
rag:
  source: custom        # which row below is in force: custom or profile
  sources:              # one row per connection path, each with its own models
    custom:
      base_url: https://omlx.inosphe.work/v1   # OpenAI-compatible root
      api_key: <key>                           # or CLAUNCH_RAG_API_KEY in the env
      embedding_model: mlx-community--Qwen3-Embedding-4B-mxfp8
      rerank_model: mlx-community--Qwen3-Reranker-4B-mxfp8   # optional
    profile:
      profile: ds4         # its provider's endpoints.openai + key
      embedding_model: accounts/fireworks/models/qwen3-embedding-8b
      rerank_model: ""
  verify_tls: false     # this endpoint's chain fails Python's verification
  dimensions: 1024      # 0 = the model's width (2560 here); Matryoshka truncation
  timeout: 120          # seconds per endpoint call
  batch: 16             # texts per embeddings request
  candidates: 40        # vector hits widened before reranking
  rerank_top: 12        # of those, how many the reranker scores
  watch_interval: 30    # seconds between checks of each board's .beads files; 0 = off
```

`source` picks which row is in force (claunch-dkrqf, claunch-pxxn4).
`custom` (the default, and what a block without `source` means) uses the
typed `base_url` and `api_key` — a local server such as oMLX. `profile`
uses the named claunch profile's backend instead: its provider's
`endpoints.openai` as the base and the profile's stored token (else the
provider's `api_key`) as the key, the same resolution the Observer and the
briefing use — e.g. `ds4` → Fireworks.

Each row keeps its own `embedding_model` and `rerank_model` (claunch-pxxn4).
The two paths name models differently — a local oMLX server takes a
converted model id, a provider takes its own — so one shared pair meant that
choosing a path overwrote the other path's ids. Switching the radio in
**Settings ▸ Search settings** now shows that row's ids, and saving writes
only the row in force (the other row is carried unchanged). Only the row in
force is validated, so a path with no address yet does not block the other.

A block written before `sources` existed has one set of values at the root.
That set was what both paths meant then, so `store.rag_source_rows()` reads
it as both rows' initial values; the first save writes the rows and drops the
flat keys. `CLAUNCH_RAG_API_KEY` is a key for the typed endpoint and applies
to `custom` only. The embedding and rerank model ids are always typed in: a
profile's `models` are chat models. Changing the source row (or a preset's
model to one of another identity) changes which collection answers; the old
one is kept on disk, so switching back is cheap.

### presets — switching models without re-embedding

Saved connections under an id, the same shape `llm:` uses for its briefing
backends (claunch-7m5v8). `preset` names the active one; empty (or naming a
row that no longer exists) falls back to the block's own rows above.

```yaml
rag:
  preset: local-qwen
  presets:
    - id: local-qwen
      source: custom
      base_url: https://omlx.inosphe.work/v1
      api_key: <key>
      embedding_model: mlx-community--Qwen3-Embedding-4B-mxfp8
      model_key: qwen3-embedding-4b   # optional; see below
    - id: cloud-qwen
      source: custom
      base_url: https://api.fireworks.ai/inference/v1
      api_key: <key>
      embedding_model: accounts/fireworks/models/qwen3-embedding-8b
```

Which collection a corpus opens is decided by the store's **identity**, not
by the `(endpoint, model)` pair: each preset may carry a `model_key`, and
without one the key derives from the `embedding_model` name (lower-cased,
non-alphanumerics folded to hyphens). Two presets that reach the same real
model through different providers or name aliases — a local oMLX id and a
provider's id for the same weights — share one collection by sharing one
`model_key`, so switching between them answers from the vectors already on
disk instead of embedding the corpus again. A genuinely different model
opens its own collection under `<corpus>.<model-key>.zvec` and the normal
sync backfills it, paced by `rate_limit`; switching back reuses what the
first pass stored. Without a preset, paths and behavior are unchanged
(`<corpus>.zvec`), and a preset naming the model a pre-preset collection was
built under adopts that directory rather than starting a second copy.

The daemon still manages one embedding model at a time — the single sync
queue is unchanged; a preset switch is a configuration choice, not a second
concurrent embedder.

`rate_limit` caps the embedding requests a five-minute sliding window may
carry (default 120, 0 = unlimited). It paces document backfills so a corpus
catch-up does not land on the endpoint as one burst; query embeddings are
metered but never delayed by it.

The engine records the resolved selection — which preset is in force, which
model and store identity it resolved to, through which connection, when — in
`<daemon dir>/rag/active.json`, and shows it in `rag status` and the
dashboard's search card, so which model answered is a record rather than a
guess.

After switching, `POST /api/rag/selftest` checks the new store against its
own entries: it samples the corpus, queries with each picked entry's text,
and reports how many come back within `top_k` at a similarity of at least
`threshold` (both parameters, with `sample`, in the JSON body; corpus
`kind` and a board `cwd` for beads). Identical text embeds to the unit
vector, so a healthy index scores self-hits near 1.0 — the failures list
names what the index answered instead and the score it gave.

**Settings ▸ Search settings** is preset-only (claunch-rag-preset-always-hn5bc):
the picker addresses one preset, its values load into the connection fields,
and 저장 writes them back over that preset — editing a preset and switching
which is active are the same save. There is no "direct rows" mode left; a
block whose `preset` pointer resolves to nothing is migrated on the first
settings read — the pre-preset connection becomes a preset named after its
`embedding_model` (a taken id gets a `-2` suffix), or, with nothing stored,
the pointer wakes at the first saved preset. The connection test
(`POST /api/rag/test`) probes the values on the card rather than the saved
preset, so an edit is tested before it is written. A bare-host `base_url`
is called with a `/v1` root appended — the OpenAI-compatible paths hang off
it — while the stored spelling is left as typed. The `/v1` correction has
one exclusion: a **root-serving endpoint** (TEI — Text Embeddings Inference
— spells `/embeddings` at the host root, so `<host>/v1/embeddings` is 404)
opts out with the preset row's `root_serving: true` flag, set from the
card's "루트 경로에 직접 서빙 (TEI 등)" checkbox in the Custom API box; the
call target is then the bare host with nothing appended. The flag also
rides the test probe, so the connection test exercises the same escape
spelling. The preset list travels
through the settings API (`presets` in the payload, secrets reported as
`api_key_set` only). A preset is deleted by writing the list without that
row (`presets` minus the row, `preset` re-pointed at the first remaining
row) — the card's 삭제 button does exactly that, moving the deleted row's
screen values into the legacy fallback rows. The last preset cannot be
deleted: the preset-only card keeps at least one, and the button refuses
up front with that reason. Deletion is honest about the read-side safety
net — if an empty list ever reached the server, the migration would
immediately re-wrap the still-alive legacy connection as a model-named
preset, so a "delete to zero" would silently do nothing; editing the last
preset in place is the supported path for changing it. The saved list holds at most
`store.RAG_MAX_PRESETS` (20) presets; the card shows the count as `N/20`
and the server refuses a longer list, which keeps the settings PUT body two
orders of magnitude under its measured breaking point (~205 bytes per
preset row, so the aiohttp 1 MiB default body ceiling falls at roughly
5,100 presets — the 20-preset ceiling binds far earlier).

The card's storage follow-ups (claunch-fhs7f.1) make three things explicit.
The save button reads **"<id> 저장하고 활성화"** (claunch-r4796; it was
"선택한 프리셋 저장" before) and its result line names the row it overwrote:
the form writes the *selected preset's* row (`saveBody`) and makes it the
active preset in the same PUT, so the label says both. The backfill
line under the title tells the two empty states apart — "프리셓 <id>의 색인은
아직 없습니다 …" when a preset is in force and has no collection of its own,
and "색인 미구동 — 아직 임베딩된 문서가 없습니다." only when no preset is
active. The line right after a save is the first case, which is what the old
single sentence got wrong: it read as "there are no documents". A preset can
be **renamed in place**: the card writes the renamed row with
`renamed_from: <old id>`, and the daemon carries the saved `api_key` over
from the old id. The secret is filed under the preset id, so without that
hint a rename would silently drop the key. The active pointer follows the
rename only when the renamed preset is the one the daemon is running with;
the picker's selection is not the active preset, and a settings PUT starts
the pointed-at preset's model on the whole corpus, so moving the pointer for
a name change alone would activate a preset the reader only renamed.
`renamed_from` is a save-time input and is dropped before the row is written;
it never appears in the settings file.

A preset may also **name the collection** it reads and writes: `db:
<store-name>`. Omitting it keeps the previous rule — the preset's `model_key`,
which itself derives from `embedding_model` — so every preset written before
the field existed resolves to exactly the directory it did before. Naming one
decouples the vectors from the model: point a second preset at the same `db`
— another provider, another model alias — and it answers from the vectors
already on disk, which is what makes a provider change cheap without moving
the data. The directory is `<corpus>.<db>.zvec`; its sidecar
(`<corpus>.<db>.meta.json`) records the model and width the vectors were made
with, and the resolved name is in `active.json` as `db`. `GET /api/rag/dbs`
lists what is on disk — name, width, model, byte size, and the presets that
read it — and the card shows that list under the DB field as a table with
**one row per DB name** (claunch-r4796): the collections of every corpus
that share an identity fold into one row (`ragDbGroups`), and a pre-preset
`<corpus>.zvec` keeps a row of its own marked legacy. The table carries the
name, the presets that read it, and the rename/delete actions; the widths,
models and sizes it used to repeat are drawn on the Semantic search card
instead, so one screen owns each number (claunch-i2i95). Clicking a row's
name fills the DB field with it (for a legacy row, the slug of the model it
was made with, which is the name a preset adopts it under); when the last
connection test measured a different width the status line says so before
anything is saved, and the save sends that measured width as `probe_dims` so
the daemon refuses a collection it cannot fill. The
field shows the name it will actually be stored under (the same slug rule as
`store.rag_model_key_slug`) while it is typed. Each row has 이름 변경 and 삭제.
`POST /api/rag/dbs/rename` with `{"from": "<db>", "to": "<new name>"}`
renames a DB: `to` is slugged, every `<corpus>.<from>.zvec` and sidecar is
moved to `<corpus>.<to>…` after the RAG service is shut down (the index
holds a LOCK file on its directory), every preset whose resolved DB is
`from` gets `db: <to>` written, and the service starts again on the result.
It is refused (400) for an unknown `from`, an empty `to`, the same name, a
`to` that already exists on disk, or a `to` another preset already names as
its `db` before anything was embedded there (the moved vectors would answer
that preset's model); a failed move or save puts every moved file
back. The zvec directory holds no absolute paths, so a moved directory
opens as it was. `POST /api/rag/dbs/delete` with
`{"name": "<directory stem>"}` removes a collection and its sidecar;
`{"db": "<db name>"}` removes every collection of that DB name across corpora
(the table's 삭제 sends this for a named row, `name` for a legacy row). A store
a preset reads, or the one the active config reads, is refused: its vectors
would have to be asked for from the provider again, and the row already
names the readers, so the preset is pointed elsewhere first. "Reads" is the
path `_index_path` would open, not the name a preset carries — a preset that
adopts a pre-preset `<corpus>.zvec` (its sidecar matching the model) reads
that directory and is named as its reader. Deleting a collection nothing
reads is the only case where nothing downstream changes.

A collection's width is fixed when its vectors are written, so pointing a
model at a `db` of a different width is refused before the query reaches the
storage engine, with both numbers named and `claunch rag reindex` given as the
way out. The settings PUT refuses that save instead. Both come from sidecars
already on disk — the target's, and one this same model built earlier; when
the model's width is not recorded anywhere yet, the save goes through and the
connection test is what reports it. The width the index checks against is the
one it reads from the collection itself (`col.schema.vectors[0].dimension`),
not the sidecar's `dims`: a sidecar is a file a later run can rewrite beside
vectors it did not write, and a rewritten one made a 768-dimension query look
compatible with a 4096-dimension collection until the engine answered
`dimension mismatch` (claunch-rzepk.1). `save` writes `dims` from the
collection's width for the same reason.

The feature is on when the chosen source yields a base URL and a key and
`embedding_model` is set; `rerank_model` empty means vector order only. The key leaves the
process only as the `Authorization` header; status and error messages never
carry it. `sync.sections` does not include `rag` by default, so a synced
config does not carry the key unless the operator lists the section.

### provider의 요청 헤더 규칙

`source: profile`이면 호출은 그 프로필의 provider를 통해 나간다. provider가
`session_header`나 `anthropic_auth_header`를 선언했으면 그 규칙이 **임베딩과
리랭크 호출에도** 적용된다. 인덱서는 상류를 직접 부르고 shim이 경로에 없으므로,
규칙을 여기서 적용하지 않으면 OpenCode Go 같은 상류가 모든 동기화를
`400 MissingSessionID`로 거절한다(브리핑 호출에서 2026-10-25에 같은 원인으로
재현됐다, `claunch-acy2f`).

- 규칙의 정본은 `src/claude_launcher/routing.py`의 `apply_session_header`와
  `apply_anthropic_auth`이고, shim·`daemon/briefing.py`·`daemon/observer.py`가
  같은 함수를 부른다.
- `daemon/rag.py:request_headers()`가 `RagClient._session()`마다 헤더를 만든다.
  설정을 매번 다시 읽으므로 provider 편집이 다음 동기화에 바로 반영된다.
- `session_header`에 보내는 값은 **`claunch-rag`** 이다 (`rag.RAG_SESSION_ID`).
  색인 대상 세션의 이름을 쓰지 않는다: 이 호출은 세션의 한 턴이 아니라 데몬의
  색인 작업이고, 상류에서 그것이 구분되어 보여야 한다.
- `provider`는 `store.rag_config()`가 `source: profile`일 때만 채운다.
  `base_url`/`api_key`를 직접 적은 `custom` 블록은 어느 provider 항목에도
  속하지 않으므로 규칙이 적용되지 않는다.
- 값이 잘못된 경우(헤더 이름 규칙 위반) 호출은 `RagError`로 실패하며 메시지에
  설정 키가 들어간다. shim처럼 헤더를 건너뛰고 계속 보내는 선택지는 여기 없다 —
  상류가 어차피 거절하므로 건너뛰면 원인을 감춘다.

`verify_tls` exists because the target endpoint's certificate chain fails
Python verification ("Basic Constraints of CA cert not marked critical");
the daemon's relay block already needed the same switch for the same domain.

## What was measured (2026-09-02, the endpoint above)

| axis | value |
|---|---|
| embeddings, default width | 2560 dims, L2-normalised (norm 1.0000) |
| `dimensions` parameter | honoured (1024, 512) |
| embedding throughput | 1 doc 2.3 s · 16 docs 11.9 s (11365 tok) · 64 docs 54.4 s (59520 tok) ≈ 1000 tok/s |
| longest input accepted | 30002 tokens (55.7 s) |
| rerank shape | Cohere: `results[{index, relevance_score, document.text}]`, `top_n` honoured; `/v1/reranking` is 404 |
| rerank latency | 3 short docs 1.8 s · 20 short docs 4.5 s · 30 full descriptions 10.8 s ≈ 0.2–0.35 s per document |
| cross-lingual | query "kanban in ready status": Korean doc 0.992 > English 0.891 > unrelated 1e-6 |
| board | 778 issues (open 348, closed 427); title+description chars p50 1459 / p90 3435 / max 32037 / sum 1.40 M |
| `User-Agent: Python-urllib` | Cloudflare 403 error 1010; aiohttp's default passes |

Consequences baked into the design: a first index of the whole board is a
10–12 minute background job and a search reports its coverage instead of
waiting; the reranker scores `rerank_top` candidates on title + 600 chars,
not the board; documents are chunked (3000 chars, 8 per document) so a long
write-up matches on any part.

## The pipeline (`daemon/rag.py`)

1. **Corpus → documents.** `issue_doc` (title + `[labels]` head on every
   chunk, description chunked) and `session_doc` (name, identity, issue,
   directory, task, and the cached briefing's one-line/goal/now/progress).
   Each document carries a content hash of the text it was built from; a
   status or assignee change refreshes the stored metadata without a new
   embedding.
2. **Index.** `VectorIndex`: one JSON file per corpus under
   `<daemon dir>/rag/` (`beads-<root hash>.json`, `sessions.json`), float32
   vectors base64-encoded, model and width recorded so another model's file
   is discarded rather than mixed. Machine-local derived data, like the
   per-session briefing cache (`sessions/<name>/briefing.json`).
3. **Sync.** `RagService.ensure_sync` diffs documents against the index,
   drops what is gone, embeds what changed in batches, saves after every
   batch (an interrupted sync keeps what it did). One task per corpus; a
   search starts one if none is running and waits at most `?wait=` seconds
   (default 2) before answering from what is indexed.
3a. **The queue.** The index does not wait for a search: producers call
   `RagService.enqueue(kind, root)` when a corpus changed, and one consumer
   task drains the queue a key at a time, running `ensure_sync` and waiting
   for it. A key already waiting is joined (five enqueues of one board is
   one pass); a key enqueued while its own pass is running is queued once
   more behind it, so a change that landed mid-sync gets its own pass. A
   pass is the same content-hash diff, so repeated passes embed nothing new
   (idempotent). The producers:
   - the daemon's board writes — `Board.br` calls its `write_hooks` after
     every write that succeeded (create, update, close, comments add, dep
     add; `create_for`, `assign`, `ensure_issue`, the exit sweep all go
     through it), and the service re-stamps the board files there so the
     watcher does not queue the same write again;
   - the writes the daemon did not make — `claunch beads …` runs `br`
     itself, so a watcher stats `<root>/.beads/beads.db` and `issues.jsonl`
     every `watch_interval` seconds for every board the daemon knows (the
     roots sessions' directories resolve to, plus any a search or a producer
     named), drops the board's listing cache (`Board.invalidate`) and
     enqueues when the mtime or size moved;
   - the fleet — `SessionManager.change_hooks` fires after every `persist`
     (a session created, exited, re-parented), `exit_hooks` on an ending,
     and `briefing.persist_hooks` when a briefing is cached.
   With the `rag:` block unfilled every producer is a no-op and the queue
   stays empty; the watcher still ticks (every 30 s) so a block filled in
   later is caught on its next tick, which queues every known board and the
   fleet — the same catch-up a daemon start runs. `app.on_shutdown` cancels
   the watcher, the consumer and any pass in flight. `GET /api/rag/status`
   carries the queue (`depth`, `pending`, `consumer`, `watcher`,
   `watch_interval`, `watched`, `consumed`, `last_consumed_at`,
   `last_consumed_key`) and the settings card shows it in one line.
4. **Search.** Embed the query once → cosine over every stored vector, best
   chunk per document → widen with lexical hits (id or title containing
   every query word — the exact-match half an embedding is weakest at) →
   rerank the top `rerank_top` when a reranker is configured → return
   `limit` rows with `score` (vector), `rerank_score` (when it ran),
   `lexical`, the display metadata, and the index's coverage. The state of
   every session a row names — the row itself when it is one, and each entry
   of its `sessions` — is then read from the registry and put on the answer
   (`status`, plus `paused`/`archived` for a record whose process is gone
   either way). Step 1's metadata refresh is what keeps the index from
   re-embedding on a status change; this is what keeps the *answer* from
   reporting the state the sync happened to see.
5. **Related.** `RagService.related`: an issue's nearest neighbours by its
   first chunk's vector — the "is this a duplicate" question. An issue not
   yet indexed is embedded on the spot.

## API

| route | what |
|---|---|
| `GET /api/search?q=&kind=beads\|sessions&cwd=\|parent=&limit=&rerank=&wait=` | ranked results + `index: {total, indexed, pending, syncing, error}` + `timing` |
| `GET /api/beads/{id}/related?cwd=&limit=` | nearest issues |
| `GET /api/rag/status` | configured, host, models, each loaded index's coverage, root, and the DB it belongs to (`db` from the collection's path, `db_configured` from the preset, `legacy` when the path predates presets), the queue (depth, pending keys, last consume, watcher interval) |
| `GET /api/rag/dbs` | every collection on disk: name, corpus, identity, width, model, bytes, the presets that read it, and which one is in use (claunch-fhs7f.1) |
| `POST /api/rag/dbs/delete` | `{"name": "<stem>"}` — removes a collection and its sidecar; refused (400) when a preset or the active config reads it ; `{"db": "<db>"}` removes every collection of that DB name (claunch-r4796) |
| `POST /api/rag/dbs/rename` | `{"from": "<db>", "to": "<name>"}` — moves every collection of a DB to the slugged new name and re-points the presets that read it; 400 for unknown/empty/same names and for a name taken on disk or by another preset (claunch-r4796) |
| `POST /api/rag/reindex` `{kind, cwd, force}` | 202, starts a sync and says what the request did: `started`, `running` (a pass was already going), `force_applied`, `force_pending` (a forced request that arrived mid-pass and will run as one more pass) — claunch-i2i95 |
| `POST /api/rag/cancel` `{kind, cwd}` | 200 when a pass was running and is now asked to stop at its next embedding group; 409 when nothing was running. Vectors already embedded stay; a forced pass queued behind the stopped one is dropped (claunch-i2i95) |
| `GET /api/sessions` | now also `rag_configured` |

400 when the block is not configured or `q` is empty, 404 for a directory
with no board, 502 when the endpoint fails.

## CLI

```
claunch search "<query>" [--kind beads|sessions] [--limit N] [--no-rerank] [--wait S] [-C dir] [--json]
claunch rag status [--json]
claunch rag reindex [--kind beads|sessions] [--force] [-C dir]
```

`claunch search` is what an agent's "is there already an issue about X"
should become; the workflows' `claunch beads search` line is `br`'s own
substring search and is untouched here (changing the canonical workflow
text is its own change — see follow-ups).

## UI plan

The dashboard is one hand-written `app.js`; every search surface follows
the two conventions it already had — the issue picker's substring box
(`issueSearchMatches`) and the Reports filter bar (chips, a select derived
from real rows, a "N of M" count). Semantic search is always **Enter**, never
per keystroke: the endpoint answers in about a second and the reranker in a
few, so typing narrows by substring at once and Enter asks by meaning.

### Delivered in this slice

| surface | control | behaviour |
|---|---|---|
| **Session rail** | search box above the state filters (`#session-search`) | typing narrows by substring over name, identity, issue, branch, task and the briefing one-liner; Enter ranks the fleet by meaning and shows only the sessions the daemon named; Esc clears, and so does the header's `⇱ card` — a narrowed rail would otherwise hide the card that press jumps to; a note under the box says "N of M match" and the index coverage |
| **Beads page** | search box in the filter bar (`.beads-search`) | Enter replaces the lanes with a ranked list — score chip (reranker's when it ran, amber when the id/title contains the query), status/priority badges, excerpt — with a head line that counts hits and states coverage; one request per board root the page shows, merged best-first; clear/Esc restores the lanes |
| **Issue detail pane** | "Related by meaning" block | the focused issue's nearest neighbours, score-chipped, fetched with the detail |
| **New-session form picker** | the existing "filter the board" box | typing stays substring; Enter asks the board of the chosen directory (or the parent session's) by meaning and lists the daemon's ranking first, then the substring matches it did not name; lead row says "by meaning" |
| **Spawn modal picker** | the same box | same behaviour, asked of the board the candidates came from |
| **Settings** | "Semantic search (RAG)" card | configured or not (with the yaml to fill in), endpoint host, models, TLS; the queue line with pause/resume; then one block per DB holding the collections it reads (`Search anything`, `sessions`, one row per board) with a state badge (정상/색인 중/실패/대기), the row's own progress, and its physical path when it is the pre-preset form. `Sync` and `Rebuild` act on the whole DB; a syncing row offers `취소`. A click reports what the daemon answered, and the card re-reads the status every 3 s while work remains (bounded at 200 polls) — claunch-i2i95 |

Interaction rules the code keeps: a search box that is focused pauses the
page's poll-driven re-render (`formInUse`), so Enter blurs the box before
rendering; a semantic answer is dropped if the box's text changed while it
was in flight; a ranking is only ever applied to the exact query it was
made for.

### Follow-ups (separate issues; not in this branch)

1. **Transcript search** — `#/log/<name>` has cursor paging only. Corpus:
   the harness jsonl (`transcript_view` already keeps a byte-offset index
   to build on). Needs its own chunking and retention policy; a busy
   session's transcript is megabytes.
2. **Mesh history search** — `#/mesh/<name>` filters by current/archived
   only. Corpus: `<daemon dir>/mesh/<name>/log.jsonl`.
3. **Queues tab filter** — a text filter over the lane cards (client-side,
   the cards are already loaded) and a "show only sessions whose queue
   matches" toggle.
4. **Reports filter by meaning** — the Reports bar has state/session/issue
   selects; a query box over report titles and the issue text they cite.
5. **RAG-augmented briefing** — put the session's nearest issues into the
   briefing prompt so "goal" is grounded in the board. Explicitly out of
   this slice (leader ruling, 2026-09-02).
6. **Workflow text** — point the intake/`issue-search` steps' duplicate
   check at `claunch search` instead of `br search`. Canonical workflow yaml
   is its own change with its own tests.
7. **Command palette** — a global Ctrl+K over sessions, issues and pages.
   Nothing global exists today; the rail box is the nearest thing.
8. **CLI wizard picker** — `wizard.py`'s issue list has no search box at
   all; a query line there would use the same endpoint.
9. **Filter chip consistency** — three "on" styles exist (`.seq-tab.on`,
   `.mesh-message-filter.on`, `.session-filters [aria-pressed]`); the search
   surfaces reuse `.seq-tab` and add no fourth.

## Tests

- `tests/test_rag.py` — config parsing and env override, chunking, document
  hashes, index round-trip/diff/rank/neighbours/lexical, the client's wire
  shapes against a local aiohttp endpoint (bearer header, `dimensions`,
  batching, error text without the key), the service's incremental sync and
  ranking, the sessions corpus, the routes' contract, the CLI's formatting;
  the queue — one key enqueued five times runs once, an enqueue mid-sync
  runs once more, a second pass embeds nothing, the board-file watcher
  catches a write made behind the daemon's back, `Board.br` writes reach
  the queue and reads do not, an unconfigured block queues nothing, the
  app's shutdown hook cancels the tasks.
- `tests/web/ragsearch_check.js` — the rail matcher and note, the pickers'
  semantic order and lead row, the Beads result list and multi-board merge,
  the markup and the mobile font rule.
