from types import SimpleNamespace

from claude_launcher.daemon import search_keywords as keywords


def test_body_filename_and_partial_query_terms_are_searchable():
    index = keywords.KeywordIndex()
    index.put("target", {"title": "프로필 지침", "excerpt": "`.beads/ISSUES.jsonl`을 직접 수정 금지"})
    index.put("other", {"title": "다른 지침", "excerpt": "읽기만 수행"})
    assert index.rank("issues.jsonl", 2)[0][0] == "target"
    assert index.rank("issues.jsonl 프롬프트", 2)[0][0] == "target"
    assert index.rank("직접", 2)[0][0] == "target"
    assert index.rank("unrelated", 2) == []
    assert index.rank("issues.jsonl", 2, exclude={"target"}) == []


def test_keyword_update_delete_and_length_normalization():
    index = keywords.KeywordIndex()
    index.put("short", {"excerpt": "needle"})
    index.put("long", {"excerpt": "needle " + "padding " * 100})
    assert index.rank("needle", 2)[0][0] == "short"
    index.put("short", {"excerpt": "replacement"})
    assert [d for d, _ in index.rank("needle", 2)] == ["long"]
    index.drop("long")
    assert index.rank("needle", 2) == []
    assert "needle" not in index.postings
    assert index.rank("replacement", 1)[0][0] == "short"


def test_fusion_counts_each_issue_once_and_preserves_other_boards():
    entries = {d: SimpleNamespace(meta={"kind": "comment", "root": "A", "issue": "x"})
               for d in ("c1:0", "c2:0", "c3:0")}
    entries["other:0"] = SimpleNamespace(meta={"kind": "beads", "root": "B", "issue": "x"})
    index = SimpleNamespace(entries=entries)
    order, scores, matches = keywords.fuse(index,
        [(d, 0.9) for d in entries], [(d, 4) for d in entries], [], 5)
    assert order == ["c1:0", "other:0"]
    assert scores["c1:0"] == 2 / 61
    assert scores["other:0"] == 2 / 62
    assert matches["c1:0"] == ["c1:0", "c2:0", "c3:0"]


def test_distinct_events_on_one_session_are_not_combined():
    meta = {"kind": "report", "sessions": [{"name": "s1"}]}
    assert keywords.group_key("event:s1:a:0", meta) == keywords.group_key("event:s1:a:1", meta)
    assert keywords.group_key("event:s1:a:0", meta) != keywords.group_key("event:s1:b:0", meta)
