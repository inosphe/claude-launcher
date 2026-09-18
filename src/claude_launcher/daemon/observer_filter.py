"""Conservative display/input filter for routine communication receipts.

Unrecognized or mixed content stays visible. This does not delete source
records, and explicit questions and attachments always remain available.
"""
import re


_RESULT = re.compile(
    r"실패|오류|결함|취약|장애|충돌|발견|해결|수정|구현|삭제|복구|배포|부족|불가|차단|중단|거부|거절|"
    r"--no-ff|(?<![-\w])[a-f0-9]{7,40}(?![a-f0-9])|(?:리뷰|검토).{0,12}(?:pass|통과)|"
    r"(?:테스트|검증|시험).{0,40}(?:통과|완료)|"
    r"(?:병합|머지|커밋)(?:이|을)?\s*(?:완료|됐|되었|했|됨)|"
    r"(?:승인|선택|결정).{0,12}(?:필요|해\s*주|하십시오)|"
    r"\b(?:failed|failure|error|blocked|fixed|implemented|deployed|merged)\b|"
    r"\b\d+\s+(?:tests?\s+)?passed\b|\?|？",
    re.I,
)
_RECEIPT = re.compile(
    r"(?:확인|접수|수신|인지)(?:했습니다|하였습니다|했음|함|됨|완료)|"
    r".*(?:메시지|요청|공지|알림|응답).*(?:확인|접수|수신)(?:했습니다|하였습니다|했음|함|완료)|"
    r"(?:(?:메시지|내용|사실|요청|공지|알림|결과|상태|리더|세션|상대|s\d+).*)"
    r"(?:전달|공유|회신|통지|알림|재촉|송신|전송|응답)(?:했습니다|하였습니다|했음|함|완료)|"
    r"(?:.*(?:대기|동결)(?:\s*상태)?(?:를)?\s*(?:중입니다|중|유지합니다|유지|합니다))|"
    r"(?:변경\s*사항|추가\s*조치)(?:은|는|이|가)?\s*없습니다|"
    r"(?:acknowledged|received|noted|standing by|waiting for (?:a |the )?(?:reply|response)|"
    r"(?:message|notification|ack|fyi) (?:sent|received)|sent msg-[\w-]+(?: to .*)?)",
    re.I,
)


def communication_only(text):
    if not isinstance(text, str) or not text.strip() or _RESULT.search(text):
        return False
    # Require every sentence to be a known receipt; a result in the same
    # paragraph must never disappear just because it mentions a message.
    parts = [p.strip(" \t\r\n.!·-*") for p in re.split(r"[\n.!]+", text)]
    return all(_RECEIPT.fullmatch(p) for p in parts if p) and any(parts)


def visible(event):
    return bool(event.get("question") or event.get("attachments")) or not communication_only(event.get("text"))
