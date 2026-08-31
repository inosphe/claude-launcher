"""`sweep-run`: 한 트리를 한 번 스윕하고 세션을 반납하는 일회성 런.

이 파일이 지키는 것은 워크플로의 문구가 아니라 **회수 가능성**이다. 데몬은
run 이 ``done`` 에 이른 것을 보고 그 세션을 죽인다
(``daemon/cflow_clock.py`` 의 kill-on-end). 발화 조건 네 가지 중 둘은
워크플로 파일이 지고 있고, 그 둘은 문법으로는 아무 문제 없이 깨진다:

* ``recur`` 를 선언하면 스캔이 ``not payload.get("recur")`` 에서 걸러 낸다.
* 끝나는 스텝에 ``escalate`` 를 선언하면 pending start 가 생기고, 스캔이
  ``not pending_by`` 에서 걸러 낸다.

둘 다 이 워크플로를 더 쓸모 있게 만들려는 편집으로 자연스럽게 들어온다
("끝나고 리뷰로 넘기자", "계속 돌자"). 그때 잃는 것은 기능이 아니라 슬롯
반납이고, 증상은 몇 시간 뒤 사용자가 세션 목록을 하나씩 확인하며 죽이는
일로 나타난다 — 워크플로를 고친 사람에게는 보이지 않는다. 그래서 종료
코드로 못박는다.

세 번째 축은 조건이 아니라 도달이다. 조건 1·2 를 지켜도 run 이 ``done`` 에
닿지 못하면 세션은 그대로 남는다. 가장 흔한 형태가 초록에서만 통과하는
``verify`` 를 스윕 스텝에 거는 것이고(빨강인 날은 그 스텝을 떠날 수 없다),
그다음이 거절 목적지 없는 ``ask`` 다(거절이 사람을 기다리며 제자리에
선다). 아래 테스트들이 그 두 형태를 각각 잡는다.
"""

from __future__ import annotations

import pytest

from claude_launcher.cflow import model, state as state_mod

SWEEP_RUN = "sweep-run"


def _bundled(name: str) -> "model.Workflow":
    """패키지 사본을 레이어 해석 없이 단독으로 읽는다."""
    bundled = dict(state_mod.bundled_workflows())
    assert name in bundled, f"{name} 이 번들 워크플로에 없다"
    return model.load(bundled[name])


@pytest.fixture
def wf() -> "model.Workflow":
    return _bundled(SWEEP_RUN)


def test_sweep_run_is_a_bundled_workflow():
    assert SWEEP_RUN in dict(state_mod.bundled_workflows())


def test_it_parses_without_warnings(wf):
    """순환·도달 불가·폐기된 철자가 없다."""
    assert wf.warnings == []
    assert wf.deprecations == []


# --- 회수 조건 1: recur 를 선언하지 않는다 ------------------------------- #

def test_it_does_not_recur_so_the_daemon_reaps_the_session(wf):
    assert wf.recur is False, (
        "recur 를 선언하면 kill-on-end 스캔이 이 run 을 건너뛴다"
        " (cflow_clock.scan 의 `not payload.get(\"recur\")`) — 스윕이 끝나도"
        " 세션이 남는다"
    )
    assert wf.recur_auto is False


# --- 회수 조건 2: 어떤 스텝도 escalate 하지 않는다 ----------------------- #

def test_no_step_escalates_so_nothing_holds_a_pending_start(wf):
    escalating = sorted(s.id for s in wf.steps.values() if s.escalate is not None)
    assert escalating == [], (
        f"{escalating} 이 escalate 를 선언한다. escalate 는 pending start 를"
        " 남기고, kill-on-end 스캔은 `not pending_by` 에서 그런 run 을 건너뛴다"
        " — 세션이 반납되지 않는다. 다음 워크플로로 넘길 일이 생기면 이 런을"
        " 끝낸 뒤 요청하는 쪽이 새 런을 시작한다"
    )


# --- 회수 조건 3: 모든 길이 end 에 닿는다 -------------------------------- #

def _reachable(wf: "model.Workflow") -> set:
    seen, stack = set(), [wf.start]
    while stack:
        sid = stack.pop()
        if sid is None or sid in seen:
            continue
        seen.add(sid)
        stack.extend(wf.steps[sid].successors())
    return seen


def test_every_reachable_step_can_reach_a_termination(wf):
    """막다른 길이 없다 — 어느 스텝에 서 있어도 end 로 가는 길이 있다."""
    reachable = _reachable(wf)
    # None(종료)에 닿는 스텝을 고정점까지 넓힌다.
    terminating = {
        sid for sid in reachable if None in wf.steps[sid].successors()
    }
    changed = True
    while changed:
        changed = False
        for sid in reachable - terminating:
            if any(nxt in terminating for nxt in wf.steps[sid].successors()):
                terminating.add(sid)
                changed = True
    stranded = sorted(reachable - terminating)
    assert stranded == [], (
        f"{stranded} 에서 end 로 가는 길이 없다. run 이 done 에 닿지 못하면"
        " kill-on-end 는 발화하지 않고 세션이 남는다"
    )


def test_the_slot_request_routes_its_decline_instead_of_parking(wf):
    """거절이 목적지를 가진다 — 없으면 사람을 기다리며 제자리에 선다."""
    ask = wf.steps["sweep"].ask
    assert ask is not None, "스윕 진입은 슬롯 승인 게이트를 지나야 한다"
    assert ask.on_decline, (
        "on_decline 이 없는 거절은 run 을 그 자리에 세우고 사람을 기다린다"
        " (model.Step.successors 의 주석) — 아무도 답하지 않으면 세션이 남는다"
    )
    assert ask.on_decline in wf.steps, (
        f"거절 목적지 {ask.on_decline!r} 가 스텝이 아니다"
    )


def test_an_unanswered_slot_request_proceeds_instead_of_parking(wf):
    """무응답이 사람 대기로 떨어지지 않는다.

    ``otherwise: human`` 이면 리더가 자리에 없는 밤에 이 세션은 게이트 앞에서
    살아 있는다 — 이 워크플로가 없애려는 바로 그 증상이다. self 는 저널에
    '승인 아님, 무응답'으로 남으므로 승인으로 둔갑하지 않는다.
    """
    delegate = wf.steps["sweep"].ask.delegate
    assert delegate.otherwise == model.OTHERWISE_SELF
    assert delegate.timeout, "무응답을 판정할 시한이 없으면 self 로 떨어지지 않는다"
    assert [c.role for c in delegate.candidates] == ["leader"], (
        "슬롯은 권한 판단이라 리더에게 묻는다"
    )


# --- 회수 조건 3의 가장 흔한 파괴 형태: 스윕 스텝의 초록 전용 게이트 ---- #

def test_the_sweep_step_is_not_gated_on_a_green_result(wf):
    """빨강도 스텝을 떠날 수 있어야 한다.

    스윕 스텝에 ``sweep.py check`` 를 걸면 빨강인 날 그 스텝을 떠날 수 없고,
    보고도 회수도 일어나지 않는다. 초록·빨강의 분기는 그다음 select 가 하고,
    초록 주장의 재판정은 handoff 의 verify 가 한다.
    """
    assert wf.steps["sweep"].verify is None, (
        "스윕 스텝에 verify 를 걸면 빨강인 실행이 막다른 길이 된다"
    )
    assert wf.steps["failures"].verify is None, (
        "빨강 보고 스텝에 verify 를 걸면 같은 막다른 길이 된다"
    )


def test_the_red_branch_reports_and_ends(wf):
    select = wf.steps["verdict"].select
    assert select is not None
    assert set(select.options) == {"green", "red"}
    assert select.options["red"].next == "failures"
    assert wf.steps["failures"].next is None or wf.steps["failures"].next == model.END


# --- 게이트가 실제로 재는 것 -------------------------------------------- #

def test_the_green_claim_is_rechecked_by_a_receipt_not_by_a_suite(wf):
    """handoff 의 verify 는 영수증을 읽지, 스위트를 다시 돌리지 않는다.

    improv-leader 가 겪은 사고와 같은 자리다: verify 는 스텝을 떠날 때 동기로
    돌기 때문에, 여기에 pytest 를 두면 드라이버의 next 가 스윕 시간만큼
    막히고 저널에는 스윕이 돌았다는 사실조차 남지 않는다.
    """
    verify = wf.steps["handoff"].verify
    assert verify is not None, "초록 주장을 되잡는 것이 없으면 select 는 자기신고다"
    assert "sweep.py check" in verify.command
    assert "pytest" not in verify.command
    assert "sweep.py run" not in verify.command, (
        "check 가 아니라 run 이면 스위트를 한 번 더 돌린다"
    )


def test_the_receipt_gate_asks_about_the_tip_it_is_standing_on(wf):
    """``--branch HEAD`` — verify 는 정적 문자열이라 context 를 끼울 수 없고,
    영수증은 커밋 sha 로 철해지므로 HEAD 로 물어도 같은 파일에 닿는다."""
    assert "--branch HEAD" in wf.steps["handoff"].verify.command


# --- 누가 모는가 -------------------------------------------------------- #

def test_only_a_worker_may_drive_it(wf):
    """리더가 직접 몰면 자기 턴이 스윕 시간만큼 막힌다."""
    assert wf.filter_roles is not None
    assert wf.filter_roles.type == model.FILTER_WHITELIST
    assert "worker" in wf.filter_roles.roles


def test_it_volunteers_for_no_role(wf):
    """default_role 을 선언하면 워커 역할의 기본 워크플로(improv-worker)와
    다투게 된다. 이 워크플로는 이름을 대고 시작하는 것이다."""
    assert wf.default_role is None


def test_it_pairs_children_with_nothing(wf):
    """일회성 측정 런은 자식을 두지 않는다."""
    assert wf.default_child_cflow is None


# --- 모든 스텝이 완료 기준을 선언한다 ------------------------------------ #

def test_every_non_select_step_states_when_it_is_done(wf):
    missing = sorted(
        s.id for s in wf.steps.values()
        if not s.is_select and s.verify is None and not s.done_when
    )
    assert missing == [], f"{missing} 에 완료 기준이 없다"
