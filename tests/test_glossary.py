from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from claude_launcher import glossary as g, workspaces


@pytest.fixture
def ws(home, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return workspaces.add(str(root), "alpha")


def term(name="기반 모델", **kw):
    return {"term": name, "definition": "학습을 시작할 때 사용하는 모델", "aliases": ["base model"], **kw}


def test_workspace_isolation_worktree_resolution_and_rename(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    other = workspaces.add(str(other), "beta")
    g.create(ws, term())
    assert g.entries(other) == []
    assert g.workspace(cwd=str(tmp_path / "repo" / ".claude" / "worktrees" / "one")) == ws
    renamed = workspaces.add(ws.path, "renamed")
    assert g.db_path(renamed) == g.db_path(ws)
    assert g.entries(renamed)[0]["term"] == "기반 모델"
    assert not (tmp_path / "repo" / ".glossary").exists()


def test_crud_normalized_duplicates_and_aliases(ws):
    row = g.create(ws, term("Base Model", aliases=["BASE-model", "기반 모델", "기반모델"]))
    assert row["aliases"] == ["기반 모델"]
    with pytest.raises(g.GlossaryError) as exc:
        g.create(ws, term("ＢＡＳＥ-model"))
    assert exc.value.status == 409
    updated = g.update(ws, row["id"], {"definition": "새 정의", "term": "foundation model"})
    assert updated["id"] == row["id"]
    assert updated["reference"] == row["reference"]
    assert updated["definition"] == "새 정의"
    g.delete(ws, row["id"])
    assert g.entries(ws) == []
    with pytest.raises(g.GlossaryError) as exc:
        g.update(ws, row["id"], {"definition": "missing"})
    assert exc.value.status == 404


def test_suggestions_are_pending_idempotent_and_reviewed_once(ws):
    proposal = g.suggest(ws, term(), "s94")
    assert g.suggest(ws, term(), "s94")["id"] == proposal["id"]
    assert g.search(ws, "base model") == []
    assert g.resolve(ws, "[[term:base model]]")["references"][0]["status"] == "missing"
    accepted = g.review(ws, proposal["id"], "accept")
    assert accepted["suggestion"]["term_id"] == accepted["term"]["id"]
    assert g.suggestions(ws) == []
    assert len(g.search(ws, "기반모델")) == 1
    with pytest.raises(g.GlossaryError) as exc:
        g.review(ws, proposal["id"], "reject")
    assert exc.value.status == 409
    rejected = g.suggest(ws, term("다른 모델"), "s94")
    assert g.review(ws, rejected["id"], "reject")["term"] is None
    assert g.suggestions(ws, "rejected")[0]["reviewed_at"]
    assert len(g.entries(ws)) == 1


def test_duplicate_accept_rolls_back_without_losing_the_proposal(ws):
    g.create(ws, term())
    proposal = g.suggest(ws, term(definition="다른 정의"))
    with pytest.raises(g.GlossaryError) as exc:
        g.review(ws, proposal["id"], "accept")
    assert exc.value.status == 409
    assert g.suggestions(ws)[0]["id"] == proposal["id"]
    assert len(g.entries(ws)) == 1


def test_concurrent_identical_suggestions_return_one_pending_proposal(ws):
    g.entries(ws)  # Initialize an empty DB before concurrent requests.
    start = Barrier(12)
    def propose(_):
        start.wait(timeout=10)
        return g.suggest(ws, term(source="same context"), "s94")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(propose, range(12)))
    assert len({row["id"] for row in results}) == 1
    assert len(g.suggestions(ws)) == 1
    assert all(row["status"] == "pending" for row in results)
    assert g.entries(ws) == []


def test_concurrent_review_creates_one_term(ws):
    proposal = g.suggest(ws, term())
    def accept():
        try:
            return g.review(ws, proposal["id"], "accept")
        except g.GlossaryError as exc:
            return exc.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: accept(), range(2)))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert 409 in results
    assert len(g.entries(ws)) == 1


def test_fuzzy_search_exact_alias_substring_and_korean_typo(ws):
    target = g.create(ws, term("checkpoint", aliases=["체크포인트", "학습 체크포인트"]))
    g.create(ws, term("checklist", aliases=[]))
    assert g.search(ws, "CHECK-point")[0]["match"] == "exact"
    assert g.search(ws, "체크 포인트")[0]["match"] == "exact"
    assert g.search(ws, "checkpiont")[0]["id"] == target["id"]
    assert g.search(ws, "체크포인드")[0]["id"] == target["id"]
    assert g.search(ws, "학습 체크")[0]["match"] == "substring"
    assert g.search(ws, "qqqqqqqq") == []
    assert g.search(ws, "!!!") == []


def test_quasiquotation_preserves_literal_text_ambiguity_and_no_recursive_evaluation(ws):
    first = g.create(ws, term("first", aliases=["shared"], definition="[[term:second]] $(echo secret)"))
    g.create(ws, term("second", aliases=["shared"]))
    text = "  literal [[term:first]] / [[term:shared]] / [[term:frist]] / " + first["reference"] + r" / \[[term:second]]  "
    resolved = g.resolve(ws, text)
    assert resolved["text"] == text
    refs = resolved["references"]
    assert [r["status"] for r in refs] == ["resolved", "ambiguous", "missing", "resolved"]
    assert len(refs[1]["candidates"]) == 2
    assert refs[2]["candidates"][0]["id"] == first["id"]
    for ref in refs:
        assert text[ref["start"]:ref["end"]] == ref["literal"]
    assert refs[0]["term"]["definition"] == first["definition"]
    g.delete(ws, first["id"])
    assert g.resolve(ws, first["reference"])["references"][0]["status"] == "missing"


@pytest.mark.parametrize("body", [None, [], {}, {"term": "!!!", "definition": "a"},
    term(aliases="foo"), term(aliases=[42]), term(aliases=["!!!"]), term(definition=""),
    term(source=4), term("x" * 201)])
def test_invalid_inputs_rejected(ws, body):
    with pytest.raises(g.GlossaryError):
        g.create(ws, body)


def test_workspace_and_search_validation(ws):
    with pytest.raises(g.GlossaryError):
        g.workspace("missing")
    with pytest.raises(g.GlossaryError):
        g.search(ws, "a", True)
    with pytest.raises(g.GlossaryError):
        g.suggestions(ws, "invalid")
    with pytest.raises(g.GlossaryError):
        g.review(ws, "missing", "approve")


def test_composer_detects_multiple_meanings_and_only_expands_explicit_choices(ws):
    explain = g.create(ws, term("자세히 · 구체적 설명", aliases=["자세히"],
        definition="일의 목적과 동기, 실행 방법을 구체적으로"))
    research = g.create(ws, term("자세히 · 추가 탐색", aliases=["자세히"],
        definition="현재 모르는 일에 대해 브라우저 검색을 포함해 추가 탐색"))
    raw = "😀 자세히 설명하고 자세히 탐색해주세요"
    found = g.analyze(ws, raw)
    assert len(found["mentions"]) == 2
    for mention in found["mentions"]:
        assert raw[mention["start"]:mention["end"]] == "자세히"
        assert {c["id"] for c in mention["candidates"]} == {explain["id"], research["id"]}
    assert g.compose(ws, raw)["delivery"] == raw
    assert not g.compose(ws, "[[term:자세히]] 설명해주세요")["ready"]
    chosen = "😀 " + explain["reference"] + " 설명해주세요"
    delivered = g.compose(ws, chosen)
    assert delivered["ready"]
    assert delivered["delivery"].startswith(chosen + "\n\n")
    assert explain["definition"] in delivered["delivery"]
    assert research["definition"] not in delivered["delivery"]
    assert g.analyze(ws, chosen)["mentions"] == []
    # Each send re-reads definitions, including deletion since a preview.
    g.delete(ws, explain["id"])
    assert not g.compose(ws, chosen)["ready"]


def test_composer_literals_overlaps_and_incomplete_references(ws):
    g.create(ws, term("RAG", aliases=[]))
    g.create(ws, term("base model", aliases=[]))
    g.create(ws, term("model", aliases=[]))
    text = r"paragraph RAG base model \[[term:RAG]]"
    assert [r["literal"] for r in g.analyze(ws, text)["mentions"]] == ["RAG", "base model"]
    assert g.compose(ws, r"\[[term:RAG]]")["delivery"] == r"\[[term:RAG]]"
    assert not g.compose(ws, "[[term:RAG")["ready"]
    assert not g.compose(ws, "[[term:unknown]]")["ready"]
    assert not g.compose(ws, "[[term:]")["ready"]
    assert g.compose(ws, "ordinary text")["ready"]
