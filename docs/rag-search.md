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

Top-level in `~/.claunch.yaml`, beside `llm:` (which stays what it is: the
backend the briefing posts to — a profile, or a `chat/completions` URL and key
typed in; edited in the dashboard's **Settings ▸ Briefing model** card). `store.rag_config()` is
the one reader; every key has a default and a malformed block is the
disabled default, never an error.

```yaml
rag:
  base_url: https://omlx.inosphe.work/v1     # OpenAI-compatible root
  api_key: <key>                             # or CLAUNCH_RAG_API_KEY in the env
  embedding_model: mlx-community--Qwen3-Embedding-4B-mxfp8
  rerank_model: mlx-community--Qwen3-Reranker-4B-mxfp8   # optional
  verify_tls: false     # this endpoint's chain fails Python's verification
  dimensions: 1024      # 0 = the model's width (2560 here); Matryoshka truncation
  timeout: 120          # seconds per endpoint call
  batch: 16             # texts per embeddings request
  candidates: 40        # vector hits widened before reranking
  rerank_top: 12        # of those, how many the reranker scores
  watch_interval: 30    # seconds between checks of each board's .beads files; 0 = off
```

The feature is on when `base_url`, `api_key` and `embedding_model` are all
set; `rerank_model` empty means vector order only. The key leaves the
process only as the `Authorization` header; status and error messages never
carry it. `sync.sections` does not include `rag` by default, so a synced
config does not carry the key unless the operator lists the section.

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
   is discarded rather than mixed. Machine-local derived data, like
   `briefings.json`.
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
| `GET /api/rag/status` | configured, host, models, each loaded index's coverage and root, the queue (depth, pending keys, last consume, watcher interval) |
| `POST /api/rag/reindex` `{kind, cwd, force}` | 202, starts a sync |
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
| **Session rail** | search box above the state filters (`#session-search`) | typing narrows by substring over name, identity, issue, branch, task and the briefing one-liner; Enter ranks the fleet by meaning and shows only the sessions the daemon named; Esc clears; a note under the box says "N of M match" and the index coverage |
| **Beads page** | search box in the filter bar (`.beads-search`) | Enter replaces the lanes with a ranked list — score chip (reranker's when it ran, amber when the id/title contains the query), status/priority badges, excerpt — with a head line that counts hits and states coverage; one request per board root the page shows, merged best-first; clear/Esc restores the lanes |
| **Issue detail pane** | "Related by meaning" block | the focused issue's nearest neighbours, score-chipped, fetched with the detail |
| **New-session form picker** | the existing "filter the board" box | typing stays substring; Enter asks the board of the chosen directory (or the parent session's) by meaning and lists the daemon's ranking first, then the substring matches it did not name; lead row says "by meaning" |
| **Spawn modal picker** | the same box | same behaviour, asked of the board the candidates came from |
| **Settings** | "Semantic search (RAG)" card | configured or not (with the yaml to fill in), endpoint host, models, TLS, and each loaded index's coverage with Sync and Rebuild buttons |

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
