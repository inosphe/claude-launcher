"""기착 게이트와 재심의 순서 (claunch-eyz0s.2).

``land``의 착지 게이트(확인 넷, 열린 제동 마커 없음)는 ``submit``보다 앞이고,
그 마커를 풀 수 있는 재심(``peer-review``)은 ``submit`` 안에 있다. 그래서
재작업 회차는 자기 리뷰 지적이 남아 있는 한 게이트를 통과할 수 없었고, 고칠
수 있는 유일한 세션(리뷰어)은 그 게이트를 통과해야만 불렸다 — 실측에서 이
런이 그 자리에서 사흘 가까이 사람 결정을 기다렸다.

이 파일이 고정하는 것 셋:

1. **순서** — 실제 ``land`` 본문을 재작업 회차로 몰아, 재심이 통합 요청보다
   앞에 선다는 것. 그 순서가 재작업 대기 판정의 방어선이다: 새 리뷰 판정이
   요청보다 먼저 보드에 서고, 다시 ``changes``면 요청이 나가지 않는다.
2. **마커 표기** — 리뷰어의 ``CHANGES REQUESTED``가 심사한 tip을 함께 적는다.
   tip이 없으면 어느 회차의 판정인지 보드에서 갈리지 않는다.
3. **게이트 문구** — 확인 4가 세 마커를 구별하고, 재작업 회차의 「재심 대기」를
   「미해소」와 가르며, 그 판정이 명령의 출력 셋으로 선다는 것. 상위의 제동
   (``HOLD``, tip 없는 ``CHANGES REQUESTED``)은 그대로 escalate다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import claude_launcher
from claude_launcher.cflow2 import frontend, layers, runtime

V2 = Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"

#: ``land``가 부르는 잎 중 셸·데몬·보드가 필요한 것. 본문·게이트·재심 ·
#: 대기는 파일 자신의 것을 쓴다 — 스텁은 판정 대상이 아니다.
STUB = {
    "merge-ready": {"prompt": "merge-ready", "returns": {"ok": {"code": "int"}}},
    "rebase": {"prompt": "rebase", "returns": {"ok": {"tip": "str?"}}},
    "reviewer-ensure": {"prompt": "reviewer-ensure", "returns": {"ok": {"code": "int"}}},
    "confirm-landed": {"prompt": "confirm-landed",
                       "returns": {"ok": {"merge": "str", "tip": "str?"}}},
    "integration-request": {"prompt": "integration-request",
                            "returns": {"ok": {"tip": "str", "issues": "list"},
                                        "blocked": {"reason": "str"}}},
    "pr-publish": {"prompt": "pr-publish", "returns": {"ok": {}}},
    "ensure-issue": {"prompt": "ensure-issue", "returns": {"ok": {"issue": "str"}}},
    "landing-nudge": {"prompt": "landing-nudge", "returns": {"ok": {}}},
    "landing-review": {"prompt": "landing-review",
                       "returns": {"ok": {}, "hold": {"reason": "str?"}}},
    "landing-review-leader": {"prompt": "landing-review-leader",
                              "returns": {"ok": {}, "hold": {"reason": "str?"}}},
    "peer-review": {"prompt": "peer-review",
                    "returns": {"ok": {}, "changes": {"what": "str?"},
                                "unrouted": {"reason": "str"}}},
    "landed": {"prompt": "landed",
               "returns": {"ok": {"tip": "str", "merge": "str", "target": "str"}}},
}

VALUES = {
    "merge-ready": {"code": 0},
    "rebase": {"tip": "abc"},
    "reviewer-ensure": {"code": 0},
    "confirm-landed": {"merge": "m"},
    "integration-request": {"tip": "abc", "issues": ["i"]},
    "landed": {"tip": "abc", "merge": "m", "target": "master"},
}


def _land_chart(tmp_path):
    """``land`` 하나를 시작 본문으로 세운 차트. 절차 본문·submit의 순서·
    대기 갈래는 정의 파일 그대로다."""
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    for name, node in STUB.items():
        doc["nodes"][name] = node
    doc["body"] = [{"id": "land-call", "call": "land", "bind": "land_out"}]
    doc.pop("spec", None)
    # 착지 요청에 실을 이슈는 이미 귀속됐다고 둔다 — ensure-issue가 대신 설
    # 자리를 없애 그 노드가 경로에 끼어들지 않게 한다.
    doc["data"] = {"claim": {"issue": "probe-issue", "how": "claimed"},
                   "landing": {"medium": "local"}}
    doc["returns"] = {"landed": {"merge": "str", "tip": "str?"},
                      "held": {"reason": "str?", "rework": "bool?"},
                      "rework": {"reason": "str?", "rework": "bool"},
                      "blocked": {"reason": "str"}}
    return frontend.compile_text(json.dumps(doc, ensure_ascii=False),
                                 resolve_include=layers.resolver(str(tmp_path)))


def _real_review_outcome(answer: str) -> str:
    """이 파일의 스텁이 그 선택지에 대해 보고해야 하는 outcome 이름.

    스텁은 이 파일이 세운 것이지만 **무엇을 보고**하는지는 정본에서 읽는다.
    스텁이 ``"changes"``를 하드코딩하면 ``options.changes.outcome``을 ``ok``로
    바꾼 변이가 살아남는다(재심 지적, s1262-rev): 그러면 실제 워크플로에서는
    재심이 고칠 것을 골라도 요청이 나가는데 테스트는 green이다. 그래서 선택지
    → outcome 매핑은 정의 파일에서 읽고, 그 이름이 선언돼 있지 않으면 그
    자리에서 죽는다(스텁이 낼 수 없는 이름을 지어내지 않는다).
    """
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    options = (doc["nodes"]["peer-review"].get("ask") or {}).get("options") or {}
    declared = [str((o or {}).get("outcome") or "") for o in options.values()]
    named = {str(name): str((o or {}).get("outcome") or "")
             for name, o in options.items()}
    # ``answer``는 이 파일이 쓰는 답의 이름이다. 옵션 이름이면 그 옵션의
    # outcome을, 이미 outcome 이름이면 그대로 쓴다.
    outcome = named.get(answer) or (answer if answer in declared else "")
    assert outcome, f"peer-review declares no such option or outcome: {answer!r} ({named})"
    return outcome


def _drive(tmp_path, monkeypatch, review_answer):
    """실제 ``land``를 몰고 지나간 라벨을 순서대로 낸다.

    ``review_answer``는 ``peer-review``가 낼 답이다. 게이트에서는 ``request``를
    고른다 — 재작업 회차가 스스로 낼 수 있는 유일한 갈래이고, 이 파일이
    검증하는 것은 그 선택이 사람을 세우지 않고 재심까지 가는가다.
    """
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    # 매핑을 정본에서 먼저 읽는다 — 스텁이 낼 수 없는 이름이면 그 자체가
    # 드리프트이므로 아래 report가 거절하고 테스트가 그 자리에서 죽는다.
    review_outcome = _real_review_outcome(review_answer)
    st = runtime.start(_land_chart(tmp_path), caps={}, **kw)
    path, humans = [], []
    for _ in range(60):
        if st.get("asks"):
            # 이 자리에 사람이 서면 그 사실 자체가 실패다 (landing-review는
            # 상위 세션의 질문이고, ask-user는 사람의 문이다).
            humans.append(st["asks"][0]["label"].rsplit("/", 1)[-1])
            break
        if st.get("choices"):
            names = [o["name"] for o in st["choices"][0]["options"]]
            pick = "request" if "request" in names else names[0]
            path.append("CHOICE:" + pick)
            st = runtime.select(st["choices"][0]["instance"], pick, "확인 셋이 참", **kw)
            continue
        if not st.get("tasks"):
            path.append("END:" + str(st.get("outcome")))
            break
        task = st["tasks"][0]
        label = task["label"].rsplit("/", 1)[-1]
        path.append(label)
        if label == "peer-review":
            st = runtime.report(task["instance"], review_outcome,
                                {"what": "1) 표적 테스트가 범위를 덮지 않는다"} if
                                review_outcome == "changes" else {}, **kw)
        else:
            st = runtime.report(task["instance"], "ok", VALUES.get(label, {}), **kw)
    return path, humans


def test_the_rereview_runs_before_the_request_on_a_rework_round(tmp_path, monkeypatch):
    """재작업 회차가 게이트를 통과하면 사람을 세우지 않고, 재심이 통합 요청
    앞에 선다. 이 순서가 「옛 마커가 있는 채로 요청이 나가지 않는다」의
    근거다 — 리뷰어의 새 판정이 요청보다 먼저 보드에 선다."""
    path, humans = _drive(tmp_path, monkeypatch, "ok")
    assert humans == [], f"이 경로에 사람의 문이 섰다: {humans}"
    assert "CHOICE:request" in path, path
    for label in ("merge-ready", "reviewer-ensure", "peer-review", "integration-request"):
        assert label in path, (label, path)
    assert (path.index("reviewer-ensure") < path.index("peer-review")
            < path.index("integration-request")), path
    # 정렬이 재심보다 앞이다 — 리뷰어가 보는 트리가 상위가 머지할 트리다
    assert path.index("merge-ready") < path.index("peer-review"), path


def test_a_second_changes_verdict_stops_the_request(tmp_path, monkeypatch):
    """재심이 다시 고칠 것을 고르면 통합 요청은 나가지 않고 배치가 되감긴다.
    게이트를 재심 대기로 읽은 것이 방어선을 지운 것이 아님을 이 한 줄이
    고정한다: 요청은 재심의 판정 뒤에만 있다."""
    path, _ = _drive(tmp_path, monkeypatch, "changes")
    assert "peer-review" in path, path
    assert "integration-request" not in path, path
    assert path[-1] == "END:rework", path


# --------------------------------------------------------------------------- #
# 문구: 마커 표기와 확인 4의 판정
# --------------------------------------------------------------------------- #
def _land_prompt() -> str:
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    prompt = doc["procedures"]["land"]["body"]
    return " ".join(str(o) for o in _walk(prompt))


def _walk(o):
    if isinstance(o, dict):
        for key, value in o.items():
            if key == "prompt" and isinstance(value, str):
                yield value
            else:
                yield from _walk(value)
    elif isinstance(o, list):
        for item in o:
            yield from _walk(item)


def _words(text: str) -> str:
    return " ".join(text.split())


def test_check_four_names_the_reviewers_marker_form():
    """``CHANGES REQUESTED``가 심사한 tip을 함께 적지 않으면 워커가 그 마커를
    이번 회차의 판정인지 가를 수 없다. 표기가 정본 문구에 있어야 리뷰어가
    그대로 쓰고, 게이트가 그 형태로 읽는다."""
    prompt = _words(_land_prompt())
    assert "CHANGES REQUESTED @ <tip>: <무엇을>" in prompt
    # tip 없는 형태는 어느 회차의 것인지 갈리지 않는다 → escalate
    assert "@ <tip>`이 없는 `CHANGES REQUESTED`" in prompt


def test_check_four_separates_pending_rereview_from_unresolved():
    """재작업 회차의 핵심: 마커가 남아 있다는 것과 그 마커가 아직 유효하다는
    것은 다르다. 판정은 셋이고 전부 명령의 출력이다."""
    prompt = _words(_land_prompt())
    assert "재심 대기" in prompt
    assert "git merge-base --is-ancestor <tip> HEAD" in prompt
    assert "git rev-list --count <tip>..HEAD" in prompt
    # 셋이 전부 참일 때만 request다 — 「아마 고쳤다」는 판정이 아니다
    assert "셋 중 하나라도 안 서면 escalate다" in prompt
    # 그리고 그 판정이 안전한 이유가 순서다 — 이 문장이 빠지면 다음 편집자가
    # 재심을 요청 뒤로 옮겨도 문구가 항변하지 않는다
    assert "`integration-request` **앞에서** 돈다" in prompt


def test_the_gate_reads_a_tip_wherever_the_marker_puts_it():
    """tip의 자리는 형태가 아니다. 이 런의 판정 자체가 ``CHANGES REQUESTED:
    <무엇을> @ <tip>`` 형태였으므로(s162), 판독이 ``@ <tip>:`` 접두 형태만
    인정하면 고치려던 그 마커를 못 읽는다(s404-rev 지적). 두 형태를 같은
    판정으로 읽는다는 규칙이 정본에 있어야 한다."""
    prompt = _words(_land_prompt())
    assert "**tip의 자리는 둘 중 하나다 — 그 둘만 tip이다.**" in prompt
    assert "`CHANGES REQUESTED: <무엇을> @ <tip>`" in prompt
    assert "그 줄의 **마지막 토큰**" in prompt


def test_the_gate_does_not_read_a_hash_quoted_inside_the_line():
    """이 이슈(claunch-eyz0s.2.2)가 닫는 구멍: 「줄 어디에서 찾든 tip」이라는
    판독은 상위 세션이 거절 문면에 근거로 인용한 해시를 그 마커의 tip으로
    읽는다. 실측 — 리더가 쓴 ``CHANGES REQUESTED`` 첫 줄 29건에 ``@<hex>``가
    있고, ``improv-leader-v2.yaml``에는 거절 마커의 꼴을 정한 문장이 없었다
    (``grep -c`` → 0). 그러면 상위의 제동이 「재심 대기」로 통과한다.

    판독을 둘(마커 낱말 바로 뒤, 줄의 마지막 토큰)로 좁히고 본문 안의
    ``@ <hex>``를 배제한다. 이 테스트는 그 배제 문장이 정본에 있는지를
    고정한다 — 배제를 지우면 상위 거절이 다시 리뷰 판정으로 분류된다."""
    prompt = _words(_land_prompt())
    # 본문 안의 해시는 tip이 아니다 — 이 문장이 규칙 그 자체다
    assert "**줄 본문 안의 다른 `@ <hex>`는 tip이 아니다.**" in prompt
    # 그리고 그 귀결: 그런 줄은 tip 없는 마커이므로 escalate
    assert "그 줄은 tip 없는 마커다" in prompt
    assert "리뷰 판정이 아니라 상위의 제동이므로 escalate다" in prompt
    # 두 자리가 서로 다른 해시를 가리키면 판독 불가
    assert "**서로 다른 해시**를 가리키면 그 줄은 판독 불가다" in prompt


def test_the_gate_separates_the_upper_by_comment_author_not_by_prefix():
    """The gate used to separate the upper's brake from a reviewer's verdict by
    the line's *prefix* (``PR-REVIEW #<번호>``). Three measured lines written by
    the same upper (s29) carry no prefix and were therefore read as reviewer
    verdicts: ``CHANGES REQUESTED @ 568b0d19…`` (claunch-o8pb7, the (i)
    position), ``CHANGES REQUESTED @ acc16d7b (s29):`` (claunch-u9m6z, (i) with
    the author named inline) and a trailing ``@ b606bc15`` (claunch-75kfl, the
    (ii) position). The canon already said the ground is *who wrote the line*
    (claunch-eyz0s.2.1.1).

    The first implementation of this rewrite read the author head but then
    narrowed "upper" to the first ``landing-review`` responder group, which
    fails in a parent-worker configuration (the first group is worker; the
    leader is in no group and is not a reviewer, so its lines fell back to the
    prefix rule and became reviewer verdicts again). It also looked ``[sNNN]``
    up in the mesh roster's *handle* column, though ``[sNNN]`` is a session
    name. So the default is flipped: only the three review-verdict branches
    are reviews, everything else is a brake, and the prefix is the last
    fallback."""
    prompt = _words(_land_prompt())
    # the flipped default, stated as the rule
    assert "기본값은 「제동」이고, 리뷰어만 뺀다" in prompt
    # where the subject is read from, and that it is a session name
    assert "`[sNNN] at <시각>` 꼴의 머리" in prompt
    assert "그 `[sNNN]`은 **세션 이름**이다" in prompt
    # the roster comparison is the third column, and the two differ — pinning
    # the concrete pairs keeps a handle-based lookup from coming back
    assert "<핸들> <role> <데몬>/<세션>`" in prompt
    assert "첫 열(핸들)이 아니라 셋째 열의 세션 이름" in prompt
    for pair in ("`s404-rev`의 세션은 `s573`", "`s511-rev`는 `s523`",
                 "`s5874-indep-leader`는 `s5878`"):
        assert pair in prompt, pair
    # the three review-verdict branches, and that review is tested first
    assert "리뷰 판정 셋 중 하나에" in prompt and "먼저 걸리면" in prompt
    assert "(a) 그 세션의 role이 `reviewer`다" in prompt
    assert "`claunch cflow2 responders peer-review --json`" in prompt
    assert "`groups[].asks`에 (핸들→세션 이름으로 옮겨) 들어 있다" in prompt
    # (c) is a fallback, not a peer of (a)/(b) — a reviewer-shaped line form
    # alone must not promote an upper's brake to a verdict
    assert "는 **role과 응답자 여부를 읽지 못했을 때의 대체**다" in prompt
    assert "상위의 제동 줄은 그 형태로 쓰이지 않으므로" in prompt
    assert "꼴만으로는 리뷰 판정이라 하지 않는다" in prompt
    # everything else is a brake, including a session absent from the roster
    assert "**그 밖의 전부 — 상위의 제동이다.**" in prompt
    assert "명부에 없는 세션" in prompt
    # the `resolves` fact the first implementation got wrong: it is top-level
    assert "`resolves`는 최상위에" in prompt
    assert "그룹마다의 `resolves` 필드는 없다" in prompt
    # the parent-worker anchor — a parentless stack makes the first group the
    # leader (a sibling), which is why the first-group reading looked correct
    assert "부모 워커가 답하는 편성에서 첫 그룹은 worker(ancestor)이고" in prompt
    # the prefix survives only as the fallback, both directions stated
    assert "**줄 머리 접두가 마지막 대체다**" in prompt
    assert "`PR-REVIEW #<번호>`로 시작하면 제동이고" in prompt
    assert "주체가 리뷰어인 줄은 접두가 붙어 있어도 리뷰 판정이고" in prompt
    assert "주체가 리뷰어가 아닌 줄은 접두가 없어도 제동이다" in prompt
    # the three measured lines, their measured author
    assert "claunch-o8pb7" in prompt and "claunch-u9m6z" in prompt and "claunch-75kfl" in prompt
    assert "셋 다 상위(s29)가 쓴 줄이다" in prompt
    # the ordering claim survives the rewrite
    assert "이 규칙이 자리 규칙보다 앞선다" in prompt
    # the form that *was* caught by the prefix still sits in the canon
    assert "`PR-REVIEW #530 CHANGES REQUESTED @ e6daa14b: …`" in prompt


def test_the_leader_canon_names_the_declines_marker_form():
    """워커의 판독이 tip으로 두 출처를 가르는 이상, 상위가 남기는 거절의
    꼴도 리더 쪽 정본에 있어야 한다. 없으면 리더는 tip을 붙일 수도, 붙이지
    말아야 한다는 것을 알 수도 없다 — 이 이슈 이전에 그 파일에는
    ``CHANGES REQUESTED``가 0건이었다(claunch-eyz0s.2.2 실측)."""
    leader = V2.parent / "improv-leader-v2.yaml"
    text = _words(leader.read_text(encoding="utf-8"))
    assert "`CHANGES REQUESTED: <무엇을>`" in text, text[:400]
    assert "**그 코멘트에는 `@ <hash>`를 붙이지 않는다.**" in text
    # 거절 사유에 해시를 인용해야 할 때의 꼴도 정해져 있어야 한다 —
    # 그러지 않으면 리더가 다시 본문 안에 @ <hex>를 쓴다
    assert "`tip: <hash>`" in text
    # PR 절차 3번에도 같은 지시가 있어야 한다 — 실측 29건을 낳은 자리가
    # 그 절차이고, 큐 거절 자리에만 지시가 있으면 리더는 그 줄에 해시를 쓴다
    assert "**이 코멘트에도 판정 낱말 뒤에 `@ <hash>`를 붙이지 않는다**" in text
    assert "PR-REVIEW #<번호> <판정>: <근거>" in text


def test_the_order_claim_is_scoped_to_an_answered_review():
    """순서는 리뷰어가 **답했을 때**의 보장이다: 응답이 없으면
    ``otherwise: self:pass``로 통과하고 ``review-unrouted``의 ``skip``도
    리뷰 없이 요청으로 간다. 그 두 경우 옛 마커가 남으므로, 문구가 「경로는
    없다」로 총칭하면 거짓이 된다(s490-rev·s511-rev·s404-rev 지적)."""
    prompt = _words(_land_prompt())
    assert "**다만 그 순서는 리뷰어가 답했을 때의 보장이다**" in prompt
    assert "otherwise: self:pass" in prompt
    assert "옛 마커가 그대로인 채로 보드에 요청이 실리는 경로는 없다" not in prompt


def test_the_gate_pins_its_three_load_bearing_sentences():
    """세 문장이 변이에서 살아남았다(s490-rev, claunch-eyz0s.2.1 재작업).

    정본과 프로젝트 레이어를 같은 바이트로 변이해도 4모듈이 250 passed로
    같았다 — 어느 테스트도 이 셋을 물지 않았다는 뜻이다.

    1. tip 없는 `CHANGES REQUESTED`는 「보드에서 갈리지 않는다 — 그때는
       escalate다」. 재심 대기로 뒤집으면 상위의 거절이 대기 판정이 된다.
    2. `BLOCKED`는 **같은 tip**의 `CLEARED`만 푼다. 그 조건을 지우면 앞
       tip의 낡은 해소가 지금 제동을 푼다.
    3. 증거 묶음의 `미해소 마커: …` / `미해소 마커 없음` 줄. 지우면 상위가
       심사 있음으로 읽는다.

    단언은 문장 자체를 가리킨다 — 문장을 지우거나 뒤집는 변이가 여기서
    죽는다."""
    prompt = _words(_land_prompt())
    # 1. tip 없는 마커 → escalate
    assert "판정인지 보드에서 갈리지 않는다 — 그때는 escalate다" in prompt
    # 2. CLEARED는 같은 tip만 푼다
    assert "같은 tip의 `CLEARED: ... @ <tip>`가 풀고" in prompt
    assert "다른 tip의 CLEARED는 풀지 않는다" in prompt
    # 3. 묶음의 미해소 마커 줄 — 두 꼴 다
    assert "`미해소 마커: CHANGES REQUESTED @ <tip> — 재심은 <무응답 통과|skip>`" in prompt
    assert "`미해소 마커 없음`" in prompt


def test_the_bundle_must_record_a_marker_the_rereview_did_not_clear():
    """무응답 통과·skip으로 요청이 나가는 경우, 남은 마커를 상위가 알 자리는
    증거 묶음의 한 줄뿐이다. 이 줄이 없으면 상위는 심사 있음으로 읽는다."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    text = _words(" ".join(str(v) for v in _walk(doc["nodes"]["integration-request"])))
    assert "**미해소 제동 마커**" in text, text[:400]
    assert "미해소 마커 없음" in text


def test_the_shared_marker_list_shows_the_tip_form():
    """공통 조각이 tip 없는 형태를 정본으로 보여 주면, 그 조각을 읽는 리뷰어는
    tip 없는 마커를 쓴다 — 새 확인 4는 그것을 escalate한다. 표기를 의무화한
    변경이 공통 규칙에서도 서야 재작업 회차의 원래 결함이 그 경로로 남지
    않는다(s597-rev·s1304·s1389-rev 지적)."""
    common = V2.parent / "improv-common.yaml"
    doc = yaml.safe_load(common.read_text(encoding="utf-8"))
    block = _words(doc["fragments"]["beads-common"])
    assert "`CHANGES REQUESTED @ <hash>: <무엇을>`" in block
    # 그리고 상위의 거절과의 구별이 그 목록에도 있어야 한다 — 없으면 다음
    # 독자가 tip 없는 형태를 옛 표기로만 읽는다
    assert "리뷰 판정이 아니라 상위의 제동이다" in block


def test_the_gate_still_escalates_on_the_uppers_brake():
    """상위 세션의 제동은 워커가 풀 수 없다. ``HOLD``와 tip 없는
    ``CHANGES REQUESTED``가 그 자리에 남아야 재작업 대기 규칙이 상위의
    동결을 우회하는 문이 되지 않는다."""
    prompt = _words(_land_prompt())
    assert "`HOLD:` — 상위 세션 또는 사람의 제동이다" in prompt
    assert "이 세션은 풀 수 없다" in prompt
    assert "상위 세션이 착지를 거절하며 남긴 문면일 수 있고" in prompt


@pytest.mark.parametrize("where", ["peer-review", "reviewer-ensure"])
def test_the_rereview_is_told_to_write_the_tip(where):
    """리뷰어가 그 표기를 실제로 쓰도록 시키는 자리는 둘이다 — 질문(peer-review)과
    스폰된 리뷰어의 과제(reviewer-ensure). 한쪽만 고치면 나머지 경로가 tip 없는
    마커를 남긴다."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    text = _words(" ".join(str(v) for v in _walk(doc["nodes"][where])))
    assert "CHANGES REQUESTED @ <tip>: <무엇을>" in text, text[:400]


def test_the_uppers_decline_is_the_marker_without_a_tip():
    """게이트가 tip으로 두 출처를 가르는 이상, 상위의 거절이 어느 쪽인지도
    적혀 있어야 한다. ``landing-declined``가 상위의 거절을 받는 자리이고,
    리뷰 판정은 거기 오지 않는다 — 그 구별이 문구에 없으면 다음 편집자가
    한쪽 표기를 다른 쪽으로 맞춘다."""
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    text = _words(" ".join(str(v) for v in _walk(doc["nodes"]["landing-declined"])))
    assert "이 마커에는 `@ <tip>`을 붙이지 않는다" in text, text[:400]
    assert "리뷰 판정은 `submit`의 `peer-review`가 `changes`로 답하며 끝나고" in text
