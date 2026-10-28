"""The wrapup quotes the ``round-report`` skill instead of carrying the report
spec inline (claunch-mxxm).

The wrapup instructions are re-emitted from the front at
``_INSTRUCTIONS_LIMIT`` (1200) characters, and the inline spec was the reason
most of the step never reached the reminder. So the canonical text lives in
``report_spec.SKILL_MD`` and the yaml carries only the quote — these tests pin
both sides of that arrangement, the way the layer-composition tests pin the
project layer against the package bytes.
"""

from pathlib import Path

import claude_launcher
import yaml as pyyaml

from claude_launcher import report_spec
from claude_launcher.cflow import model, state as state_mod

_INSTRUCTIONS_LIMIT = 1200


def _worker_path() -> Path:
    return Path(dict(state_mod.bundled_workflows())["improv-worker"])


def _worker_yaml() -> str:
    return _worker_path().read_text(encoding="utf-8")


def _wrapup_instructions() -> str:
    wf = model.load(_worker_path())
    return wf.steps["wrapup"].instructions


def test_wrapup_quotes_the_skill_before_writing():
    # (가) the quote sits in the first paragraph, as an imperative.
    text = _wrapup_instructions()
    head = text[:_INSTRUCTIONS_LIMIT]
    assert "round-report" in head
    assert "스킬을 열어라" in head
    # the imperative names the skill, and names it before the spec detail below
    assert head.index("round-report") < head.index("스킬을 열어라")


def test_wrapup_keeps_the_two_line_gist():
    # (나) even with the skill unreachable, the gist survives the reminder window.
    head = _wrapup_instructions()[:_INSTRUCTIONS_LIMIT]
    assert "두괄식" in head
    assert "산출물 자체" in head


def test_the_spec_itself_left_the_yaml():
    # phrases only the full spec carried must not linger as a half-copy
    text = _worker_yaml()
    for orphan in ["자기 칭찬이 아니라", "파일 이름이 곧 색인이다", "단독 파일로 쓴다"]:
        assert orphan not in text


def test_the_skill_canonically_holds_what_the_yaml_names():
    # (다) the quote must resolve to a canonical that really carries the spec
    body = report_spec.SKILL_MD
    assert "round-report" in body.split("\n", 1)[0] or "name: round-report" in body
    for load_bearing in [
        "한 것과 나온 것",
        "산출물 자체",
        "두괄식",
        "claunch report path",
        "claunch report ls",
    ]:
        assert load_bearing in body, load_bearing


def test_the_yaml_instruction_phrases_come_from_the_skill():
    # the gist the yaml keeps must be the skill's own phrases, not a fork
    skill = report_spec.SKILL_MD
    text = _wrapup_instructions()
    assert "한 것과 나온 것" in skill
    assert "한 것과 나온 것" in text
    assert "산출물 자체" in skill
    assert "산출물 자체" in text


_V2 = (
    Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"
)


def test_the_v2_report_node_quotes_the_skill_too():
    # the v2 port carries the same report node — it quotes, it does not fork
    text = _V2.read_text(encoding="utf-8")
    assert "round-report" in text
    assert "스킬을 열어라" in text
    for orphan in ["자기 칭찬이 아니라", "파일 이름이 곧 색인이다", "단독 파일로 쓴다"]:
        assert orphan not in text


def test_write_skill(tmp_path):
    path = report_spec.write_skill(tmp_path)
    assert path == tmp_path / "round-report" / "SKILL.md"
    assert path.read_text(encoding="utf-8") == report_spec.SKILL_MD


def test_install_registers_the_skill():
    from claude_launcher import install

    src = Path(install.__file__).read_text(encoding="utf-8")
    assert "report_spec.write_skill" in src


def test_skill_frontmatter_is_valid():
    front = report_spec.SKILL_MD.split("---\n")[1]
    meta = pyyaml.safe_load(front)
    assert meta["name"] == "round-report"
    assert "스킬" in meta["description"] or "skill" in meta["description"].lower()


def test_the_skill_limits_the_product_to_what_is_already_recorded():
    # claunch-igkbo.2: "a command's output" read as licence to re-run the
    # harnesses; the product section quotes recorded bytes, it does not remake them
    body = " ".join(report_spec.SKILL_MD.split())
    assert "a command's output" not in body
    assert "이미 기록된 출력" in body
    assert "다시 만들지 말고 커밋이나 기록에서 잘라 싣는다" in body
    # the code-only allowance stays: the section is not mandatory
    assert "this section may be empty" in body


def test_the_skill_keeps_verification_out_of_the_report():
    body = " ".join(report_spec.SKILL_MD.split())
    assert "산출물과 검증은 다른 절이다" in body
    assert "테스트·스윕을 다시 돌리지 않고" in body
    assert "원인을 조사하지 않는다" in body
    assert "기록에 없으면 없다고 적는다" in body


def test_the_v2_report_node_says_what_it_does_not_do():
    text = _V2.read_text(encoding="utf-8")
    node = text.split("\n  round-report:\n", 1)[1].split("\n    done_when:", 1)[0]
    node = " ".join(node.split())
    assert "기록에 없으면 없다고 적는다" in node
    assert "기록에 없는 것을 기억이나 추측으로 채우지 않는다" not in node
    assert "이 노드가 하지 않는 일: 검증" in node
    assert "다시 돌리지 않고" in node
    assert "이미 기록된 출력만" in node
    assert "다시 만들지 말고 커밋이나 기록에서 잘라 싣는다" in node
    assert "그 절을 비워 둔다" in node
