# Search anything

The session rail's **Search anything** button and `/` open a search dialog.
Enter submits a semantic search, and Escape closes the dialog and restores
focus. `/` remains ordinary input in text fields, editors and terminals.
Results are drawn as two labelled lists, **세션** and **그 외 항목**, each headed by
how many rows it holds; the answer's counts of both are repeated in the status
line above them. The ranking still orders every answer, and it orders it within
the list the row belongs to: whether a row is a session is the first thing a
reader needs from it. A session row is headed by the session's own name and the
state it is in now, and carries an accent edge; a record row is headed by the
source kind it came from. Both keep the matching passage and the sessions they
concern, and **원문 보기** retrieves the original issue (including comments) or
event. This feature retrieves records; it does not generate answers.

The state shown on a session — on the row that is a session and on every chip a
record carries — is read from the session registry when the answer is built, not
from the index: the index is embedded in the background, so a state stored in it
would be as old as its last sync. `status` is the registry's own word
(`starting`, `busy`, `idle`, `exited`); a record that was paused or archived
says so instead, since its process is gone in either case. A name the registry
does not hold keeps whatever the index had, because there is no live state to
put beside it.

**종류 필터** sits between the status line and the rows: `전체` and one chip per
kind the answer holds, each with how many rows it would leave, pressed-state
shown on the chip. Choosing one narrows the two lists to that kind; `전체`
restores them. A chip is built from the rows in hand rather than from a fixed
list of kinds, so a kind the corpus gains later becomes a chip with no change
here, and a chip never promises rows the answer does not have. The filter
narrows what arrived, and does not ask the daemon again — so it can only show
what the answer's window (`limit=30`) already holds: a kind whose matches all
rank below that window has no chip on this answer. Filling the window with one
kind instead would take a server-side `only=` filter, which this change does not
add. A new search starts unfiltered: the chips count the answer they belong to,
so the line above them and the rows below them agree when it arrives.

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
session links, session state at answer time, settings and retrieval.
`tests/search_anything_browser.cjs` uses an isolated fixture server to exercise
actual browser focus, keyboard, stale-response handling, the two result lists
with their states, source display and settings forms. Set `CLAUNCH_PLAYWRIGHT`
and `CLAUNCH_CHROMIUM` to local installations if needed.
