"""Raise the daemon's open-file soft limit to the hard limit at startup.

A daemon that hosts one PTY pair per live session holds two descriptors per
session before it does any work of its own, and the descriptor count grows
with sessions, browser sockets, sweep subprocesses and the files a request
walks (:mod:`claude_launcher.daemon.artifact_cleanup` alone opens every path
it fingerprints). On 2026-10-25 a daemon serving 103 sessions held 705
descriptors against a soft limit of 1024 while the hard limit was 1048576,
and ``artifact_cleanup`` reported every item as ``cleanup_pending`` with
``[Errno 24] Too many open files`` (claunch-wo1zj.1). Nothing in this package
raised the limit; the daemon used whatever the shell that started it passed
down.

The soft limit is the one a process hits at ``open()``; the hard limit is the
ceiling only a privileged process may raise. A process may always raise its
own soft limit as far as the hard one, so the daemon can take the whole of
what it was already allowed.

POSIX only. Windows has no per-process descriptor ceiling of this shape —
handles are bounded by memory and the system-wide table — so
:func:`raise_soft_limit` reports ``None`` there rather than pretending.

Failure is not fatal. A daemon that cannot raise its limit is a daemon with
the limit it was started with, which is what every daemon ran with before
this module existed; refusing to start would turn a resource ceiling into an
outage.
"""

from __future__ import annotations

from typing import Optional

#: ``resource.RLIM_INFINITY`` when the module exists, ``None`` elsewhere.
_INFINITY: Optional[int] = None
try:  # pragma: no cover - import guard, exercised on Windows only
    import resource as _resource

    _INFINITY = int(_resource.RLIM_INFINITY)
    _NOFILE = _resource.RLIMIT_NOFILE
except (ImportError, AttributeError):  # pragma: no cover - Windows
    _resource = None  # type: ignore[assignment]


def supported() -> bool:
    """Whether this platform has an open-file limit this module can raise."""
    return _resource is not None


def limits() -> Optional[tuple]:
    """``(soft, hard)`` for open files, or ``None`` where there is no such limit."""
    if _resource is None:
        return None
    soft, hard = _resource.getrlimit(_NOFILE)
    return int(soft), int(hard)


def raise_soft_limit() -> Optional[dict]:
    """Set the open-file soft limit to the hard limit; report what changed.

    Returns ``None`` where the platform has no such limit, otherwise a record
    with ``before`` (the soft/hard pair as it was), ``after`` (as it is), and
    ``why`` when the attempt did not happen or did not hold. ``why`` is empty
    on success, and also when there was nothing to do — the soft limit already
    equalled the hard one.

    Never raises: a ceiling the process cannot lift is a condition to log,
    not a reason to stop serving.
    """
    if _resource is None:
        return None
    try:
        before = limits()
    except (OSError, ValueError) as exc:
        return {"before": None, "after": None, "why": f"cannot read the limit: {exc}"}
    assert before is not None  # supported() is true here
    record = {"before": before, "after": before, "why": ""}
    soft, hard = before
    if soft >= hard:
        return record
    try:
        _resource.setrlimit(_NOFILE, (hard, hard))
    except (OSError, ValueError) as exc:
        # ``hard`` can be RLIM_INFINITY on a host that reports one but refuses
        # to hand it out, and a container may cap the soft limit below the
        # hard one. Either way the daemon keeps what it has.
        record["why"] = f"setrlimit failed: {exc}"
        return record
    try:
        record["after"] = limits()
    except (OSError, ValueError) as exc:  # pragma: no cover - read-back guard
        record["why"] = f"cannot read the limit back: {exc}"
    return record


def describe(record: Optional[dict]) -> str:
    """One line for the daemon log, or ``""`` when there is nothing to say."""
    if record is None:
        return ""
    before = record.get("before")
    after = record.get("after")
    if record.get("why"):
        held = f"{before[0]}/{before[1]}" if before else "unknown"
        return f"open-file limit unchanged ({held}): {record['why']}"
    if not before or not after:
        return ""
    if before[0] == after[0]:
        return f"open-file limit already at its maximum ({after[0]}/{after[1]})"
    return (
        f"open-file soft limit raised {before[0]} -> {after[0]} "
        f"(hard {after[1]})"
    )


#: ``EMFILE``/``ENFILE``: the process or the system ran out of descriptors.
EXHAUSTION_ERRNOS = frozenset({24, 23})


def exhaustion_note(exc: BaseException) -> str:
    """A hint to append to a descriptor-exhaustion error, or ``""``.

    ``[Errno 24] Too many open files`` says nothing about which limit was hit
    or by which process, so an operator reading it has to guess whether the
    daemon is capped, the system is, or one walk is holding everything. The
    caller's own numbers answer that; the errno alone does not.
    """
    if getattr(exc, "errno", None) not in EXHAUSTION_ERRNOS:
        return ""
    pair = limits()
    if pair is None:
        return " (this process is out of file descriptors)"
    return (
        f" (this process is out of file descriptors at a soft limit of "
        f"{pair[0]} of {pair[1]}; sessions it hosts are what consume them)"
    )
