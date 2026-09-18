# Search anything

The session rail's **Search anything** button and `/` open a search dialog.
Enter submits a semantic search, and Escape closes the dialog and restores
focus. `/` remains ordinary input in text fields, editors and terminals.
Results show the source type, date, matching passage and related sessions.
**원문 보기** retrieves the original issue (including comments) or event.
This feature retrieves records; it does not generate answers.

The unified corpus covers all known/registered repository boards, issue
descriptions, design/acceptance/notes fields, labels and comments; session
tasks and summaries; Observer events and direct reports/answers; briefing
and checks; and session lifecycle/borrow/worktree events. Long records are
split into independently searchable passages without the older eight-chunk
cap. Embedding and optional reranking use the existing oMLX-compatible
client. `/api/search?kind=all&q=...` exposes the same search to API clients.
If reranking fails, unified search still returns embedding/exact-match results
and explicitly displays that reranking was unavailable.
The response reports indexed/total passages and indexing errors. Initial
indexing runs in the background, so early searches cover a partial corpus.

Settings → **Search settings** edits the API address, credential, embedding
model and optional rerank model. Advanced controls cover candidate counts,
batch size, timeout, watch interval and TLS verification. An empty credential
field preserves the existing key; keys are never returned by this API.
The existing `CLAUNCH_RAG_API_KEY` environment override still takes priority.
**연결 테스트** tests unsaved settings without storing them, checks both models
when reranking is enabled, and reports the actual embedding width.
There is no editable dimension field. Saving removes the old `dimensions`
override and subsequent requests use the model's default output width.
Changing the embedding model, endpoint or requested-width configuration
invalidates previous vectors; changing only the reranker reuses them.
Actual widths and indexing progress appear in the index status card.

Observer keeps its bounded display history. A separate daemon-local SQLite
archive, `search-records.sqlite3`, retains source records for search beyond
that display limit, including records from sessions later removed from the
registry. Re-importing a source ID updates that record instead of duplicating
it. New briefing/check snapshots are recorded on write, independently of
whether automatic model observation or semantic search is enabled. Existing
retained Observer records and latest briefing/check snapshots are imported;
history already discarded before this feature cannot be reconstructed.
Search indexes remain derived data under the daemon's `rag/` directory.

Validation: `tests/test_search_anything.py` covers durability, source coverage,
session links, settings and retrieval. `tests/search_anything_browser.cjs`
uses an isolated fixture server to exercise actual browser focus, keyboard,
stale-response handling, source display and settings forms. Set
`CLAUNCH_PLAYWRIGHT` and `CLAUNCH_CHROMIUM` to local installations if needed.
