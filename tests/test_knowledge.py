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
