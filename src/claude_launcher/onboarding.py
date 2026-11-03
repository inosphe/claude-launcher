"""Mark a profile's Claude Code first-run onboarding as done.

A profile is its own ``CLAUDE_CONFIG_DIR``. When its ``.claude.json`` lacks
``hasCompletedOnboarding`` (no seed source at ``create``, or ``--no-seed``),
Claude Code opens the first-run theme/login screens even though ``run``
injects a working token. ``claunch onboard`` writes that one flag so a profile
with a credential starts straight into the session; ``run`` and ``validate``
only read it and point at that command (claunch-1lpzm.1).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import lineage, providers
from .profile import Profile

#: The key Claude Code reads to skip its first-run screens.
FLAG = "hasCompletedOnboarding"

CONFIG_FILENAME = ".claude.json"

DONE = "done"
MISSING = "missing"


class OnboardingError(Exception):
    """The profile's ``.claude.json`` cannot be read as a JSON object."""


def config_path(profile: Profile) -> Path:
    return profile.config_dir / CONFIG_FILENAME


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OnboardingError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OnboardingError(f"{path} does not hold a JSON object")
    return data


def state(profile: Profile) -> str:
    """``done`` when the flag is ``true``, ``missing`` otherwise.

    An unreadable file counts as missing: ``run`` only prints a hint from
    this, and ``onboard`` reports the read error itself through :func:`mark`.
    """
    try:
        data = _read(config_path(profile))
    except OnboardingError:
        return MISSING
    return DONE if data.get(FLAG) is True else MISSING


def has_credential(profile: Profile) -> bool:
    """Whether a claude-harness launch of ``profile`` carries auth.

    The same test ``validate`` applies before its heartbeat: an Anthropic
    OAuth profile needs a token (own, inherited or native), a provider
    profile its stored launcher token.
    """
    if providers.uses_anthropic_oauth(providers.resolve_name(profile)):
        return lineage.lookup_token(profile) is not None
    return lineage.stored_auth_token(profile) is not None


def mark(profile: Profile) -> bool:
    """Set the flag; ``True`` when the file changed, ``False`` if already set.

    Every other key is kept. The file is replaced atomically, because a
    running Claude Code may be writing the same file.
    """
    path = config_path(profile)
    data = _read(path)
    if data.get(FLAG) is True:
        return False
    data[FLAG] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=CONFIG_FILENAME + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        if path.is_file():
            # mkstemp makes the file 0600; keep the mode the file had.
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def hint(profile: Profile) -> str:
    return (
        f"profile {profile.name!r} has not completed Claude Code onboarding "
        f"({FLAG} missing in {config_path(profile)}); "
        f"run 'claunch onboard {profile.name}' to skip the first-run screens"
    )
