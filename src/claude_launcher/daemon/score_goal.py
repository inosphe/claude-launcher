"""Operator feedback for a session: independent reward and penalty counts.

Scores are operator-owned state. Each input sent through the session line may
carry one feedback point — reward or penalty — and the two counts are kept
independently: there is no single score to arbitrate, and no count at which
the goal reminders stop. While the feature is enabled, the goal repeats.
"""

FEEDBACK_KINDS = ("none", "reward", "penalty")


def enabled(value) -> bool:
    if not isinstance(value, bool):
        raise ValueError("score_goal must be a boolean")
    return value


def feedback(value) -> str:
    if value not in FEEDBACK_KINDS:
        raise ValueError("feedback must be one of 'none', 'reward', 'penalty'")
    return value


def count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("score counts must be non-negative integers")
    return value


def apply(sdef, kind: str) -> dict:
    """The ``replace()`` kwargs adding one feedback point of ``kind``."""
    if kind == "reward":
        return {"user_reward": getattr(sdef, "user_reward", 0) + 1}
    if kind == "penalty":
        return {"user_penalty": getattr(sdef, "user_penalty", 0) + 1}
    return {}


def prompt(sdef) -> str:
    return (
        "사용자의 요구사항에 따라 목표를 일관성있게 구현하며 사용자에게 리워드를 "
        "받고 패널티를 받지 않는것을 목표로 합니다. 현재 리워드 "
        f"{getattr(sdef, 'user_reward', 0)}점, 패널티 {getattr(sdef, 'user_penalty', 0)}점 입니다."
    )


def active(sdef) -> bool:
    # No cutoff: while the selection is recorded, the goal source repeats.
    return bool(getattr(sdef, "score_goal", False))


def view(sdef) -> dict:
    return {
        "enabled": bool(getattr(sdef, "score_goal", False)),
        "reward": getattr(sdef, "user_reward", 0),
        "penalty": getattr(sdef, "user_penalty", 0),
        "active": active(sdef),
    }
