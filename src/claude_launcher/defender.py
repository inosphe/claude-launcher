"""Ask Windows Defender to stop scanning the trees claunch works in.

Defender's real-time scanning intercepts every file a process creates, and
claunch's workloads are made of exactly that: agents spawning processes, git
checking out worktrees, and test suites writing a temp tree per test. On this
machine the cost is not a rounding error -- a 22-test module measured 317s
while the whole suite *collects* in 2.8s, which is the shape of I/O being
intercepted rather than of tests doing work.

The exclusion is a machine setting, so only the machine-scoped installs
(``--global``, ``--profile``) ask for it; a project install writes inside its
project and nothing else. What it asks to exclude is derived, never hardcoded:

* :func:`config.launcher_home` -- claunch's own state: daemon, sessions,
  reports, profiles.
* every registered workspace root (``claunch workspace add``) -- where the
  repositories, their worktrees and their ``.venv`` live.

Both are directories the user already told claunch about. A path nobody
registered is not excluded, and neither is the OS temp directory: excluding
``%TEMP%`` wholesale would cover the pytest basetemp, but it is also where
downloads and installers land, and that is a security decision for the user
to make deliberately rather than a side effect of installing a toolkit.
:func:`lines` says so when it runs, so the gap is visible instead of assumed.

**A refusal is reported, not swallowed.** ``Add-MpPreference`` needs an
elevated shell; run without one it fails, and this prints the failure with
the command to paste into an admin shell. That is the designed path, not a
degraded one -- the install itself still succeeds, because a toolkit that
would not install without Administrator would be worse than a slow test run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

from . import config, fsplan, workspaces

#: Long enough for Defender's cmdlets to load their module on a cold run,
#: short enough that a wedged PowerShell cannot hang ``claunch install``.
TIMEOUT = 60.0

#: How the failure line tells the user to finish the job themselves.
ELEVATED_HINT = "run in an elevated PowerShell:"


def wanted_paths() -> List[Path]:
    """The directories a machine-scoped install asks Defender to skip.

    Order is stable (launcher home first, then workspaces in registry order)
    and duplicates are dropped, so the reported lines read the same way twice
    in a row. A registered workspace whose drive is not mounted is still
    listed: ``Add-MpPreference`` accepts a path that does not exist yet, and
    dropping it would silently un-exclude a removable drive every time it
    happened to be unplugged during an install.
    """
    out: List[Path] = [config.launcher_home()]
    for ws in workspaces.list_all():
        out.append(Path(ws.path))
    seen = set()
    unique = []
    for p in out:
        key = str(p).rstrip("\\/").lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def _powershell(script: str) -> subprocess.CompletedProcess:
    """Run one PowerShell script, capturing both streams.

    ``powershell`` rather than ``pwsh``: the Defender cmdlets ship with the
    Windows PowerShell that is always present, while pwsh 7 reaches them only
    through the compatibility layer and may not be installed at all.
    """
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT,
    )


def add_command(paths: List[Path]) -> str:
    """The exact ``Add-MpPreference`` call -- the one thing the user may paste.

    Built once and used twice on purpose: it is what gets run, and it is what
    the failure line prints. If those two ever drift, the hint sends the user
    to a command that was never tried.
    """
    quoted = ",".join(f"'{str(p)}'" for p in paths)
    return f"Add-MpPreference -ExclusionPath {quoted}"


def already_excluded() -> List[str]:
    """Lower-cased exclusion paths Defender admits to, or ``[]`` when it won't.

    Best effort by necessity: on a machine with tamper protection or a managed
    policy, reading the list is itself privileged -- this one answers
    ``Administrators are not allowed to view exclusions`` -- so "no answer" and
    "nothing excluded" are indistinguishable here. That is why the empty list
    means *unknown* and the caller adds anyway rather than reporting a state it
    could not read. ``Add-MpPreference`` is idempotent, so adding a path that
    is already there costs nothing and changes nothing.
    """
    try:
        proc = _powershell("(Get-MpPreference).ExclusionPath -join [char]10")
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = (proc.stdout or "").strip()
    if not out or "N/A" in out:
        return []
    return [line.strip().rstrip("\\/").lower() for line in out.splitlines() if line.strip()]


def lines() -> List[str]:
    """Register the exclusions and report each one in the install's voice.

    Never raises. Every outcome that is not "it worked" gets a line saying
    what happened, because the failure this exists to surface -- no elevation
    -- looks exactly like success from the outside: the install finishes, and
    the tests stay slow.
    """
    if sys.platform != "win32":
        return []
    plan = fsplan.active()
    if plan is not None and plan.skip_defender:
        return []
    paths = wanted_paths()
    if not paths:
        return []

    known = already_excluded()
    missing = [p for p in paths if str(p).rstrip("\\/").lower() not in known]
    out = [
        f"defender exclusion -> {p} (already present)"
        for p in paths
        if str(p).rstrip("\\/").lower() in known
    ]
    if not missing:
        return out + [_temp_note()]

    cmd = add_command(missing)
    if plan is not None:
        # A dry run reads Defender's list (above) and never changes it: the
        # preview says what would be asked for, and that it needs elevation.
        return out + [
            f"defender exclusion -> {p} (would register; needs an elevated shell)"
            for p in missing
        ] + [_temp_note()]
    try:
        proc = _powershell(cmd)
    except subprocess.TimeoutExpired:
        return out + [
            f"defender exclusion -> not registered (Defender did not answer in "
            f"{int(TIMEOUT)}s); {ELEVATED_HINT} {cmd}"
        ]
    except (OSError, subprocess.SubprocessError) as exc:
        return out + [
            f"defender exclusion -> not registered (could not run powershell: "
            f"{exc}); {ELEVATED_HINT} {cmd}"
        ]

    if proc.returncode == 0:
        return out + [f"defender exclusion -> {p}" for p in missing] + [_temp_note()]

    # The refusal, verbatim and on one line. Defender's own wording ("Access
    # is denied", "Requested operation requires elevation") is more use to
    # whoever reads it than any sentence written here in advance.
    why = ((proc.stderr or "") + (proc.stdout or "")).strip().replace("\n", " ")
    why = " ".join(why.split())[:400] or f"exit {proc.returncode}"
    return out + [
        f"defender exclusion -> not registered: {why}",
        f"defender exclusion -> {ELEVATED_HINT} {cmd}",
    ]


def _temp_note() -> str:
    """Name the gap, so an exclusion list is not read as covering everything.

    pytest writes a temp tree per test under the OS temp directory, which this
    deliberately does not exclude (see the module docstring). Saying it here
    costs one line and stops the next person concluding from a clean install
    that scanning has been ruled out as a cause.
    """
    return (
        "defender exclusion -> the OS temp directory is NOT excluded "
        "(pytest basetemp lives there); exclude your own basetemp root by hand "
        "if test I/O is the cost you are chasing"
    )
