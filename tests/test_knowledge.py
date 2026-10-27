import sqlite3
from pathlib import Path

import pytest

from claude_launcher import knowledge, workspaces

REPO = Path(__file__).resolve().parents[1]


def _ws(tmp_path, name="alpha"):
    root = tmp_path / name
    root.mkdir()
    return workspaces.add(str(root), name)


def _file(ws, name, text):
    folder = knowledge.files_dir(ws)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def test_db_records_crud_supersede_and_isolation(home, tmp_path):
    a, b = _ws(tmp_path, "alpha"), _ws(tmp_path, "beta")
    first = knowledge.create(a, {"kind": "decision", "title": "PR 착지가 기본", "body": "이유: 리뷰",
                                 "tags": ["landing"], "refs": ["claunch-1"]}, author="s1")
    assert first["origin"] == "db" and first["author"] == "s1" and first["status"] == "active"
    assert knowledge.entries(b) == []
    second = knowledge.create(a, {"kind": "decision", "title": "로컬 착지로 변경",
                                  "body": "PR 불가", "supersedes": first["id"]})
    assert [r["id"] for r in knowledge.entries(a)] == [second["id"]]
    assert knowledge.entries(a, status="superseded")[0]["id"] == first["id"]
    edited = knowledge.update(a, second["id"], {"body": "PR 불가 — gh 없음"})
    assert edited["body"] == "PR 불가 — gh 없음" and edited["title"] == "로컬 착지로 변경"
    knowledge.delete(a, second["id"])
    with pytest.raises(knowledge.KnowledgeError) as err:
        knowledge.delete(a, second["id"])
    assert err.value.status == 404


@pytest.mark.parametrize("body", [
    {"kind": "rumor", "title": "t", "body": "b"},
    {"kind": "need", "title": "", "body": "b"},
    {"kind": "need", "title": "t", "body": ""},
    {"kind": "need", "title": "t", "body": "b", "tags": "x" * 300},
    {"kind": "need", "title": "t", "body": "b", "status": "maybe"},
])
def test_invalid_records_are_refused(home, tmp_path, body):
    with pytest.raises(knowledge.KnowledgeError) as err:
        knowledge.create(_ws(tmp_path), body)
    assert err.value.status == 400


def test_file_records_are_read_only_and_bad_files_skipped(home, tmp_path):
    ws = _ws(tmp_path)
    _file(ws, "need-x.md", "---\nkind: need\ntitle: 사용자는 한국어 보고를 원한다\n"
                           "tags: [korean]\ndate: 2026-10-01\n---\n본문\n")
    _file(ws, "broken.md", "no front matter")
    _file(ws, "badkind.md", "---\nkind: nope\ntitle: x\n---\nbody\n")
    [row] = knowledge.entries(ws)
    assert row["id"] == "md:need-x" and row["origin"] == "file"
    assert row["path"] == ".claunch/knowledge/need-x.md" and row["body"] == "본문"
    for action in (lambda: knowledge.update(ws, row["id"], {"body": "x"}),
                   lambda: knowledge.delete(ws, row["id"])):
        with pytest.raises(knowledge.KnowledgeError) as err:
            action()
        assert err.value.status == 409


def test_search_ranks_by_word_hits_then_field(home, tmp_path):
    ws = _ws(tmp_path)
    body_only = knowledge.create(ws, {"kind": "lesson", "title": "기타", "body": "카테고리를 갱신한다"})
    title = knowledge.create(ws, {"kind": "decision", "title": "카테고리 즉시 등록", "body": "x"})
    both = knowledge.create(ws, {"kind": "decision", "title": "카테고리 배치", "body": "즉시 반영",
                                 "tags": ["category"]})
    hits = knowledge.search(ws, "카테고리 즉시")
    # Two words hit before one; with both words, the title outweighs the body.
    assert [h["id"] for h in hits] == [title["id"], both["id"], body_only["id"]]
    assert hits[0]["matched"] == ["카테고리", "즉시"]
    assert knowledge.search(ws, "CATEGORY")[0]["id"] == both["id"]
    assert knowledge.search(ws, "없는낱말") == []
    assert [h["id"] for h in knowledge.search(ws, "카테고리", kind="lesson")] == [body_only["id"]]
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.search(ws, "x", limit=0)


def test_export_groups_by_kind(home, tmp_path):
    ws = _ws(tmp_path)
    knowledge.create(ws, {"kind": "need", "title": "니즈 하나", "body": "본문 N"})
    knowledge.create(ws, {"kind": "vision", "title": "비전 하나", "body": "본문 V"})
    text = knowledge.export_markdown(ws)
    assert text.index("## 비전 (vision)") < text.index("## 니즈 (need)")
    assert "### 니즈 하나" in text and "본문 V" in text


def test_this_repositorys_seed_records_all_parse():
    folder = REPO / ".claunch" / "knowledge"
    files = sorted(folder.glob("*.md"))
    assert files, "seed records are missing"
    parsed = [knowledge.parse_file(p) for p in files]
    assert [p.name for p, r in zip(files, parsed) if r is None] == []
    assert {r["kind"] for r in parsed} == set(knowledge.KINDS)


def test_an_update_keeps_the_previous_version_and_its_editor(home, tmp_path):
    ws = _ws(tmp_path)
    first = knowledge.create(ws, {"kind": "decision", "title": "결정", "body": "# 처음\n- 이유"}, author="s1")
    assert first["version"] == 1
    second = knowledge.update(ws, first["id"], {"body": "## 고침"}, author="s2")
    assert second["version"] == 2 and second["body"] == "## 고침"
    versions = knowledge.history(ws, first["id"])
    assert [v["version"] for v in versions] == [1, 2]
    assert [v["body"] for v in versions] == ["# 처음\n- 이유", "## 고침"]
    assert versions[0]["archived_by"] == "s2" and versions[0]["updated_at"]
    assert [v["current"] for v in versions] == [False, True]
    # Saving the same content again is not a new version.
    assert knowledge.update(ws, first["id"], {"body": "## 고침"}, author="s2")["version"] == 2
    assert len(knowledge.history(ws, first["id"])) == 2


def test_superseding_is_a_version_of_the_old_record(home, tmp_path):
    ws = _ws(tmp_path)
    old = knowledge.create(ws, {"kind": "decision", "title": "옛 결정", "body": "이유"}, author="s1")
    knowledge.create(ws, {"kind": "decision", "title": "새 결정", "body": "새 이유",
                          "supersedes": old["id"]}, author="s2")
    versions = knowledge.history(ws, old["id"])
    assert [(v["version"], v["status"]) for v in versions] == [(1, "active"), (2, "superseded")]
    assert versions[0]["archived_by"] == "s2" and versions[1]["archived_by"] == ""
    # A second supersede of an already superseded record adds no version.
    knowledge.create(ws, {"kind": "decision", "title": "셋째 결정", "body": "이유",
                          "supersedes": old["id"]}, author="s3")
    assert len(knowledge.history(ws, old["id"])) == 2


def test_a_superseded_record_can_be_revived_with_a_version(home, tmp_path):
    ws = _ws(tmp_path)
    old = knowledge.create(ws, {"kind": "decision", "title": "옛 결정", "body": "이유"}, author="s1")
    knowledge.create(ws, {"kind": "decision", "title": "새 결정", "body": "새 이유",
                          "supersedes": old["id"]}, author="s2")
    revived = knowledge.update(ws, old["id"], {"status": "active"}, author="s3")
    assert revived["status"] == "active" and revived["version"] == 3
    assert [(v["version"], v["status"]) for v in knowledge.history(ws, old["id"])] == [
        (1, "active"), (2, "superseded"), (3, "active")]


def test_history_limit_and_unknown_records(home, tmp_path):
    ws = _ws(tmp_path)
    row = knowledge.create(ws, {"kind": "lesson", "title": "t", "body": "v1"}, author="s1")
    for i in range(2, 5):
        knowledge.update(ws, row["id"], {"body": f"v{i}"}, author="s1")
    assert [v["version"] for v in knowledge.history(ws, row["id"], limit=2)] == [3, 4]
    assert [v["version"] for v in knowledge.history(ws, row["id"], limit=1)] == [4]
    for bad in (0, 101, True):
        with pytest.raises(knowledge.KnowledgeError):
            knowledge.history(ws, row["id"], limit=bad)
    with pytest.raises(knowledge.KnowledgeError) as err:
        knowledge.history(ws, "nope")
    assert err.value.status == 404


def test_history_of_a_file_record_is_its_single_version(home, tmp_path):
    ws = _ws(tmp_path)
    _file(ws, "need-x.md", "---\nkind: need\ntitle: 니즈\n---\n본문\n")
    [version] = knowledge.history(ws, "md:need-x")
    assert version["version"] == 1 and version["current"] is True and version["origin"] == "file"


def test_a_store_written_before_versions_gains_the_column(home, tmp_path):
    ws = _ws(tmp_path)
    path = knowledge.db_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
               "title TEXT NOT NULL, body TEXT NOT NULL, tags TEXT NOT NULL, refs TEXT NOT NULL, "
               "source TEXT NOT NULL, author TEXT NOT NULL, status TEXT NOT NULL, "
               "supersedes TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.execute("INSERT INTO records VALUES ('old1','decision','옛 결정','본문','[]','[]','',"
               "'s1','active','','2026-10-01','2026-10-01')")
    db.commit()
    db.close()
    assert knowledge.entries(ws)[0]["version"] == 1
    knowledge.update(ws, "old1", {"body": "새 본문"}, author="s2")
    assert [(v["version"], v["body"]) for v in knowledge.history(ws, "old1")] == [
        (1, "본문"), (2, "새 본문")]


def test_a_page_save_records_the_editor_not_the_author(home, tmp_path):
    ws = _ws(tmp_path)
    row = knowledge.create(ws, {"kind": "decision", "title": "t", "body": "v1"}, author="s1")
    # The tab sends the record's fields without its author: the author is who
    # wrote the record, not who changed this version (claunch-qgdfc review).
    knowledge.update(ws, row["id"], {"kind": "decision", "title": "t", "body": "v2",
                                     "tags": [], "refs": [], "source": "",
                                     "status": "active", "supersedes": ""})
    versions = knowledge.history(ws, row["id"])
    assert versions[0]["author"] == "s1" and versions[0]["body"] == "v1"
    assert versions[0]["archived_by"] == "web"
    # A supersede made on the page carries the same editor label, not a tool name.
    old = knowledge.create(ws, {"kind": "decision", "title": "old", "body": "b"}, author="s1")
    knowledge.create(ws, {"kind": "decision", "title": "new", "body": "b",
                          "supersedes": old["id"]})
    assert knowledge.history(ws, old["id"])[0]["archived_by"] == "web"


def test_delete_drops_the_versions_too(home, tmp_path):
    ws = _ws(tmp_path)
    row = knowledge.create(ws, {"kind": "decision", "title": "t", "body": "v1"}, author="s1")
    knowledge.update(ws, row["id"], {"body": "v2"}, author="s1")
    knowledge.delete(ws, row["id"])
    with knowledge.connection(ws) as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
