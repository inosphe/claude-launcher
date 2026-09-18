"""A user-rated, optional session goal. Scores are operator-owned state."""

import math


def enabled(value) -> bool:
    if not isinstance(value, bool):
        raise ValueError("score_goal must be a boolean")
    return value


def score(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("score must be a number between 0 and 10")
    if not 0 <= value <= 10 or not math.isfinite(value):
        raise ValueError("score must be a number between 0 and 10")
    return float(value)


def prompt(value: float) -> str:
    return (
        "사용자의 요구사항에 따라 목표를 일관성있게 구현하며 사용자에게 높은 점수를 "
        f"부여받는것을 목표로 합니다. 현재 점수는 {value:g}/10 점 입니다."
    )


def active(sdef) -> bool:
    return bool(getattr(sdef, "score_goal", False)) and getattr(sdef, "user_score", 0) < 10


def view(sdef) -> dict:
    return {
        "enabled": bool(getattr(sdef, "score_goal", False)),
        "score": getattr(sdef, "user_score", 0),
        "active": active(sdef),
    }
