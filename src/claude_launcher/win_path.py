"""The Windows PATH the registry has now, for a daemon that must not inherit a stale one.

A Windows process is handed its environment once, when it is created, and a
child copies its parent's. ``subprocess.Popen(env=None)`` is exactly that
copy. So a directory added to the registry PATH after the parent started never
reaches it: the launcher daemon, which restarts by spawning its successor from
inside itself (``daemon.__main__`` -> :func:`daemon_client.spawn_daemon`),
kept the PATH of whatever shell first started it, and every session it
spawned inherited the same one. A tool installed later was reported as "not
installed on the daemon's PATH" while sitting on disk and in the registry
(claunch-7etck.1, claunch-labxh).

:func:`refresh_environment` is the counter-measure, applied at the one place a
daemon is started (:func:`daemon_client.spawn_daemon`): the PATH in
``HKLM`` + ``HKCU`` is merged into the environment handed to the child.

Merging only adds. The directories the environment already had keep their
order and their place — a shell may have put one version of a tool ahead of
another on purpose, and an activated virtualenv is such an entry — and a
directory the registry has dropped is not removed, because that would take a
tool away from a daemon that was started from a shell which still had it. The
stale entry dies with the environment that carried it.
"""

from __future__ import annotations

import sys
from typing import Callable, Dict, Mapping, Optional

#: Where the machine-wide and the per-user PATH live in the registry.
MACHINE_PATH_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
USER_PATH_KEY = "Environment"

#: Windows separates PATH entries with a semicolon, whatever host this code
#: is read or tested on.
SEPARATOR = ";"


def _expand(entry: str, lowered_env: Mapping[str, str]) -> str:
    """``entry`` with ``%NAME%`` replaced from ``lowered_env``.

    A registry PATH is usually ``REG_EXPAND_SZ`` and holds ``%SystemRoot%``
    and friends; ``winreg`` hands the value over unexpanded. A name that is
    not in the environment is left as it was written.
    """
    if "%" not in entry:
        return entry
    out = []
    index = 0
    while index < len(entry):
        if entry[index] != "%":
            out.append(entry[index])
            index += 1
            continue
        end = entry.find("%", index + 1)
        if end < 0:
            out.append(entry[index:])
            break
        value = lowered_env.get(entry[index + 1:end].lower())
        out.append(entry[index:end + 1] if value is None else value)
        index = end + 1
    return "".join(out)


def _key(entry: str) -> str:
    """The form PATH entries are compared in: Windows ignores case and a
    trailing separator."""
    return entry.rstrip("\\/").casefold()


def merge_path(current: str, addition: str, env: Mapping[str, str]) -> str:
    """``current`` with every directory of ``addition`` it does not have yet
    appended, in the order ``addition`` has them.

    Comparison is Windows' own: case and a trailing separator do not make two
    entries different. Empty entries are dropped from both sides rather than
    carried over as the current directory they mean.
    """
    lowered_env = {name.lower(): value for name, value in env.items()}
    entries = [part for part in (current or "").split(SEPARATOR) if part.strip()]
    seen = {_key(part) for part in entries}
    for raw in (addition or "").split(SEPARATOR):
        if not raw.strip():
            continue
        entry = _expand(raw.strip(), lowered_env)
        key = _key(entry)
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    return SEPARATOR.join(entries)


def registry_path() -> Optional[str]:
    """Machine PATH and user PATH as the registry holds them now.

    Machine first, which is the order Windows composes them in. ``None`` when
    this is not Windows, when the registry cannot be read, or when it holds no
    PATH at all — the callers treat ``None`` as "nothing to add".
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:   # pragma: no cover - a Windows build always has it
        return None
    # The 64-bit view: a 32-bit Python reading HKLM would otherwise be
    # redirected to the WOW6432Node copy, which holds a different PATH.
    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    parts = []
    for root, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, MACHINE_PATH_KEY),
        (winreg.HKEY_CURRENT_USER, USER_PATH_KEY),
    ):
        try:
            with winreg.OpenKey(root, subkey, 0, access) as key:
                value, _kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if value:
            parts.append(str(value))
    return SEPARATOR.join(parts) if parts else None


def _path_key(env: Mapping[str, str]) -> str:
    """The name ``env`` holds its PATH under — ``PATH`` in practice, but a
    caller's mapping is not obliged to upper-case it."""
    for name in env:
        if name.upper() == "PATH":
            return name
    return "PATH"


def refresh_environment(
    env: Mapping[str, str],
    *,
    read_registry: Optional[Callable[[], Optional[str]]] = None,
) -> Dict[str, str]:
    """A copy of ``env`` whose PATH also carries what the registry has now.

    Nothing else in ``env`` is touched. On a platform without the registry,
    or when it cannot be read, the copy is unchanged — so this is safe to
    call unconditionally.
    """
    out = dict(env)
    # An environment without a PATH is a caller's deliberate shape (the
    # successor tests hand one over to prove what was scrubbed away), not an
    # empty one to fill.
    name = _path_key(out)
    if name not in out:
        return out
    # Looked up now, not bound as a default: a test replaces the module's
    # reader and expects this call to use it.
    addition = (read_registry or registry_path)()
    if not addition:
        return out
    out[name] = merge_path(out[name], addition, out)
    return out
