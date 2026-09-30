"""Where the tests that start a real psmux-relay look for its binary.

Four test modules run a relay process: federation, one daemon on two relays,
two daemon processes in one mesh, and the relay login gate. Each used to carry
its own copy of this lookup, and the copies drifted. claunch-wt12o.1.1 taught
the federation copy to look under the home directory on POSIX; the other three
kept only the ``F:/`` roots, which on POSIX are paths relative to the cwd, so on
Linux they skipped unless ``PSMUX_RELAY_EXE`` was set -- and the targeted gate
and the sweep do not set it (claunch-wt12o.1.1.1). They share this one now.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

#: Names a binary directly; it wins over every root.
ENV = "PSMUX_RELAY_EXE"
EXE = "psmux-relay.exe" if os.name == "nt" else "psmux-relay"


def relay_roots(
    os_name: str | None = None, home: PurePath | None = None
) -> list[PurePath]:
    """The ``target`` directories a local mux-relay build is looked for in.

    The main mux-relay checkout first: peering landed on its backend-peering
    branch, so older worktree builds may predate PEER_OPEN (the federation
    tests still check CAP_PEERING at runtime and skip if the binary is too
    old). ``F:/...`` is where that checkout sits on the Windows host; on POSIX
    it would be a path relative to the cwd, and the checkout sits under the
    home directory instead (claunch-wt12o.1.1).

    ``os_name`` and ``home`` default to this host's. They are arguments, and
    the result is pure paths of the named host's flavour, so a test can ask
    for the other host's roots: patching ``os.name`` instead makes ``Path``
    build the other flavour's concrete class, which reads this host's HOME
    with the other host's rules (and on Python < 3.12 cannot be built at all).
    """
    if (os.name if os_name is None else os_name) == "nt":
        checkout = PureWindowsPath("F:/works/mux-relay")
    else:
        checkout = PurePosixPath(Path.home() if home is None else home)
        checkout = checkout / "works" / "mux-relay"
    return [
        checkout / "target",
        checkout / ".claude" / "worktrees" / "relay-tunnel" / "target",
    ]


def find_relay_exe(roots: Sequence[PurePath] | None = None) -> Path | None:
    """The binary ``PSMUX_RELAY_EXE`` names, else the first build found under
    ``roots`` in their order (default :func:`relay_roots`), debug before
    release. None when there is neither."""
    env = os.environ.get(ENV)
    if env and Path(env).is_file():
        return Path(env)
    for root in relay_roots() if roots is None else roots:
        for prof in ("debug", "release"):
            p = Path(root) / prof / EXE
            if p.is_file():
                return p
    return None


def skip_reason(roots: Sequence[PurePath] | None = None) -> str:
    """The skip reason of a test that found no binary: where it looked, and
    how to point it at one."""
    roots = relay_roots() if roots is None else roots
    return (
        f"psmux-relay binary not found (set {ENV}, or build it under one of: "
        + ", ".join(str(root) for root in roots)
        + ")"
    )
