"""app.js가 같은 이름의 최상위 함수를 두 번 선언하지 않는지 확인한다.

한 스크립트 안에서 같은 이름의 최상위 함수 선언이 둘이면 뒤의 선언이 앞의
선언을 대체하고, 앞 선언을 부르려던 호출부는 조용히 뒤 선언을 부른다.
경고도 예외도 없다.

실제로 발생한 사례가 이 테스트의 근거다(claunch-sktu): Settings의 프로필
탭이 ``refreshProfiles``를 추가하면서 new-session 폼의 동명 함수를 덮었고,
그 결과 폼의 Profile 목록이 비어서 표시되었다. ``tests/web/*_check.js``는
함수를 이름으로 하나씩 잘라내 단독 실행하므로, 파일 전체를 평가할 때만
드러나는 이 충돌을 잡지 못한다.
"""

from __future__ import annotations

import collections
import re
from pathlib import Path

APP = (
    Path(__file__).resolve().parent.parent
    / "src" / "claude_launcher" / "web" / "static" / "app.js"
)

#: 들여쓰기가 없는 줄에서 시작하는 선언만 최상위다. 중첩 함수는 이 규칙이
#: 다루는 대상이 아니며, 서로 가려도 스코프가 다르므로 문제가 되지 않는다.
TOP_LEVEL = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")


def _declarations() -> dict[str, list[int]]:
    where: dict[str, list[int]] = collections.defaultdict(list)
    for number, line in enumerate(
        APP.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = TOP_LEVEL.match(line)
        if match:
            where[match.group(1)].append(number)
    return where


def test_app_js_has_no_duplicate_top_level_functions() -> None:
    duplicates = {
        name: lines for name, lines in _declarations().items() if len(lines) > 1
    }
    assert not duplicates, (
        "app.js에 같은 이름의 최상위 함수가 여러 번 선언되어 있습니다. "
        "뒤의 선언이 앞의 선언을 대체하므로 이름을 구분해야 합니다: "
        + ", ".join(
            f"{name} (줄 {', '.join(str(n) for n in lines)})"
            for name, lines in sorted(duplicates.items())
        )
    )


def test_the_two_profile_refreshers_are_named_apart() -> None:
    """이 결함이 실제로 났던 두 함수가 각각 한 번씩만 선언되어 있다."""
    where = _declarations()
    assert len(where.get("refreshProfiles", [])) == 1
    assert len(where.get("refreshProfileSettings", [])) == 1
