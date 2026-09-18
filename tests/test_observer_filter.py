import pytest

from claude_launcher.daemon.observer_filter import communication_only, visible


@pytest.mark.parametrize("text", [
    "확인했습니다.", "리더에게 메시지를 전달했습니다.",
    "리더가 병합 요청을 확인했습니다. 브랜치 동결 상태를 유지합니다.",
    "변경 사항 없이 세션 종료 승인 대기 중입니다.",
    "병합 심사 대기 상태를 유지합니다.",
    "결과를 공유했습니다. 추가 조치는 없습니다.",
    "Acknowledged.", "Message sent.", "sent msg-123abc to s469 [ack]",
])
def test_receipts_are_suppressed(text):
    assert communication_only(text)


@pytest.mark.parametrize("text", [
    "리더에게 메시지를 전달했습니다. 테스트 24개 통과했습니다.",
    "테스트 실패를 리더에게 전달했습니다.",
    "사용자 승인 요청: 운영 배포를 승인해 주십시오.",
    "병합 완료 사실을 공유했습니다.",
    "연결 오류 때문에 응답 대기 중입니다.",
    "접근 권한 부족으로 작업 대기 중입니다.",
    "master ab6aa8d9로 이동, 브랜치 동결 유지.",
    "동료 리뷰 pass 처리, 브랜치 동결 유지.",
    "사용자가 거부하고 인터럽트함 — 사용자 지시 대기 중",
    "리더에게 수정 내용을 전달했습니다.",
    "메시지 전달 기능을 구현했습니다.",
    "작업 결과: 인덱스 크기 42MB.",
    "Message sent. 24 tests passed.",
    "배포 환경을 선택하십시오.",
    "확인했습니까?", "", None,
])
def test_results_questions_and_unknown_content_stay(text):
    assert not communication_only(text)


def test_explicit_questions_and_attachments_stay_visible():
    assert visible({"text": "확인했습니다.", "question": True})
    assert visible({"text": "결과를 공유했습니다.", "attachments": [{"id": "image"}]})
    assert not visible({"text": "확인했습니다.", "needs_action": True})
