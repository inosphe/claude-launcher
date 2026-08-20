"""What the wizard remembers between runs: the last submitted answers.

``new-session --wizard`` and ``spawn --wizard`` tend to be filled in the same
way every time — same profile, same role, same mesh — and a form that opens
blank makes the human re-answer questions whose answers have not changed. So
a successful submit records the answers worth repeating, and the next form
opens with them as its defaults.

This is *recall*, not configuration: the file is machine-local convenience
state under :func:`config.launcher_home` (``~/.claude-launcher/
wizard-recall.yaml``), deliberately not ``~/.claunch.yaml`` — the canonical
store is hand-edited and synced, and the last thing somebody typed into a
form is neither. Losing the file costs nothing but retyping once.

Precedence is fixed: a flag typed on the command line beats the recall, and
the recall beats the form's built-in fallback. Only *answers* are remembered
— per-launch identity (name, worktree), per-conversation state (resume,
fork) and directions (cwd, workspace) are excluded, the last because the
form promises to start where the command was typed, not where the previous
launch went. A remembered choice whose option no longer exists (a removed
profile, a policy-locked row) is ignored by the form's own ``select``, so a
stale file cannot make a form lie.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from . import config

FILENAME = "wizard-recall.yaml"


def path() -> Path:
    """The recall file (under the launcher home, so tests isolate it free)."""
    return config.launcher_home() / FILENAME


def _read() -> dict:
    """The whole file, ``{}`` for anything unreadable.

    Tolerant on purpose: this file is convenience state, and a corrupt one
    must cost a blank form, never a failed launch.
    """
    p = path()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def load(kind: str) -> Dict[str, Any]:
    """The remembered answers for one form (``"new"`` / ``"spawn"``)."""
    if not kind:
        return {}
    section = _read().get(kind)
    if not isinstance(section, dict):
        return {}
    return {str(k): v for k, v in section.items()}


def save(kind: str, values: Dict[str, Any]) -> None:
    """Replace ``kind``'s section with this submit's answers, wholesale.

    Wholesale, because the section IS the last submit: merging would keep a
    role the user just deselected and recall it forever. Best-effort — a
    disk that refuses this write must not refuse the session it remembers.
    """
    if not kind:
        return
    doc = _read()
    doc[kind] = dict(values)
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            yaml.safe_dump(doc, sort_keys=True, allow_unicode=True),
            encoding="utf-8",
        )
    except OSError:
        pass


#: Attributes where ``False`` is an answer a person can actually type
#: (``--no-restore``), rather than just a ``store_true`` flag left alone.
#: For these the recall yields only to ``None``/absent, so an explicit "no"
#: on the command line is never overruled by what was remembered.
TRISTATE = frozenset({"restore"})


class Defaults:
    """The wizard's ``defaults`` argument with the recall behind the flags.

    Reads like the argparse namespace it wraps: an attribute the command
    line answered comes back as typed, and an unanswered one falls through
    to the remembered value. Unanswered means falsy, because that is how the
    forms already read their defaults (every ``get(...)`` there guards with
    ``or``) and because a ``store_true`` flag left alone is indistinguishable
    from one typed as false — except for :data:`TRISTATE`, where the command
    line can say ``False`` outright and must win.
    """

    def __init__(self, args: Any, remembered: Dict[str, Any]) -> None:
        self._args = args
        self._remembered = remembered

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._args, name, None)
        if value:
            return value
        if value is False and name in TRISTATE:
            return value
        return self._remembered.get(name, value)


def defaults(args: Any, remembered: Dict[str, Any]) -> Any:
    """``args`` as the form's defaults, recall included when there is any."""
    return Defaults(args, remembered) if remembered else args
