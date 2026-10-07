"""A claude session's auto-compact answer, as the env claude reads at launch.

Claude Code takes both halves from its environment, beside its own
``autoCompactEnabled`` setting: ``DISABLE_AUTO_COMPACT`` switches the
automatic compaction off (``DISABLE_COMPACT`` also switches off ``/compact``),
and ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` is the threshold it compacts at. A
session's answer lives in its definition's own env, which is the last layer of
the spawn env (``daemon/harness.py``), so it overrides the profile's
``auto_compact_at`` and survives a daemon restart.

Pure functions only: the create form, the spawn policy (``spawn.check``) and
the restart (``SessionManager.auto_compact``) all write the env through
:func:`with_auto_compact`, and the context gauge reads it through
:func:`auto_compact_off`.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

AUTO_COMPACT_OFF_ENV = "DISABLE_AUTO_COMPACT"
COMPACT_OFF_ENV = "DISABLE_COMPACT"
COMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"

#: The values claude reads as "set" for a boolean env switch.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def auto_compact_off(env: Dict[str, str]) -> bool:
    """Whether ``env`` switches claude's automatic compaction off."""
    return any(
        str(env.get(key, "")).strip().lower() in _TRUTHY
        for key in (AUTO_COMPACT_OFF_ENV, COMPACT_OFF_ENV)
    )


def with_auto_compact(
    env: Dict[str, str], *, enabled: bool, window: Optional[int]
) -> Dict[str, str]:
    """``env`` with a session's auto-compact answer written into it.

    ``enabled=False`` sets ``DISABLE_AUTO_COMPACT=1``; ``True`` removes the
    session's own switch (and ``DISABLE_COMPACT``, the one other key that
    would keep the answer from holding), which leaves the decision to
    claude's setting and to the profile's env. ``window`` pins
    ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``; ``None`` removes the pin, so the
    profile's ``auto_compact_at`` applies again.
    """
    out = {str(k): str(v) for k, v in env.items()}
    if enabled:
        out.pop(AUTO_COMPACT_OFF_ENV, None)
        out.pop(COMPACT_OFF_ENV, None)
    else:
        out[AUTO_COMPACT_OFF_ENV] = "1"
    if window is None:
        out.pop(COMPACT_WINDOW_ENV, None)
    else:
        out[COMPACT_WINDOW_ENV] = str(int(window))
    return out


def parse(value) -> Tuple[bool, Optional[int]]:
    """``{"enabled": bool, "window": int|None}`` from a request body.

    Raises :class:`ValueError` naming what is wrong; ``window`` omitted is
    ``None`` (the profile's threshold).
    """
    if not isinstance(value, dict):
        raise ValueError(
            "auto_compact is {'enabled': true|false, 'window': <tokens>|null}"
        )
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError(
            "auto_compact.enabled is true to compact automatically, false not to"
        )
    window = value.get("window")
    if window is not None and (
        isinstance(window, bool) or not isinstance(window, int) or window <= 0
    ):
        raise ValueError(
            "auto_compact.window is a positive token count, or null for the "
            "profile's auto_compact_at"
        )
    return enabled, window
