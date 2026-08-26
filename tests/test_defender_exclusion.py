"""Machine-scoped installs ask Defender to skip the trees claunch works in.

Two things are pinned here, and the second is the one that was asked for.

1. *What* is excluded is derived from what claunch already knows -- its own
   home and the registered workspaces -- so no user's directory layout is
   baked into the source.
2. *A refusal is reported, not swallowed.* ``Add-MpPreference`` needs an
   elevated shell; on an ordinary one it fails, and the failure has to reach
   the person reading the install output together with the command to paste
   into an admin shell. Silence here is the worst outcome available: the
   install succeeds, the exclusion never happens, and the slowness it was
   meant to fix is now attributed to something else.

Nothing in this file runs Defender. ``_powershell`` is replaced everywhere --
the real cmdlet needs privileges no test can assume, and its answer differs
per machine policy.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from claude_launcher import defender, install as install_mod, workspaces


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def on_windows(monkeypatch):
    """Take the platform out of the test's hands; every case below is Windows."""
    monkeypatch.setattr(sys, "platform", "win32")


class Calls(list):
    """The scripts that would have gone to PowerShell, plus canned answers.

    A list so a test can assert on what was attempted, with ``replies``
    mapping a substring of the script to the process (or exception) it should
    answer with. Anything unmatched succeeds silently, which keeps the cases
    that are not about failure free of setup.
    """

    def __init__(self):
        super().__init__()
        self.replies = {}


@pytest.fixture
def ran(monkeypatch):
    calls = Calls()

    def fake(script):
        calls.append(script)
        for needle, proc in calls.replies.items():
            if needle in script:
                if isinstance(proc, Exception):
                    raise proc
                return proc
        return FakeProc()

    monkeypatch.setattr(defender, "_powershell", fake)
    return calls


DENIED = FakeProc(
    returncode=1,
    stderr="Add-MpPreference : Requested operation requires elevation\nAccess is denied.",
)

#: What this machine actually answers when a non-admin reads the list -- the
#: case that makes "no exclusions" and "you may not look" identical.
UNREADABLE = FakeProc(stdout="N/A: Administrators are not allowed to view exclusions")


def test_nothing_happens_off_windows(monkeypatch, ran, home):
    monkeypatch.setattr(sys, "platform", "linux")
    assert defender.lines() == []
    assert ran == []


def test_the_launcher_home_and_every_workspace_are_covered(on_windows, home, tmp_path):
    a = tmp_path / "repos"
    b = tmp_path / "other"
    a.mkdir()
    b.mkdir()
    workspaces.add(str(a))
    workspaces.add(str(b))
    wanted = [str(p) for p in defender.wanted_paths()]
    assert str(home) in wanted
    assert any(str(a).lower() == w.lower() for w in wanted)
    assert any(str(b).lower() == w.lower() for w in wanted)


def test_a_workspace_registered_twice_is_asked_for_once(on_windows, home, tmp_path):
    ws = tmp_path / "repos"
    ws.mkdir()
    workspaces.add(str(ws))
    # The launcher home itself as a workspace: the one duplicate a real setup
    # produces, since `claunch workspace add .` is run from anywhere.
    workspaces.add(str(home))
    wanted = [str(p).lower() for p in defender.wanted_paths()]
    assert len(wanted) == len(set(wanted))


def test_a_workspace_on_an_unmounted_drive_is_still_asked_for(on_windows, home, tmp_path):
    ws = tmp_path / "removable"
    ws.mkdir()
    workspaces.add(str(ws))
    ws.rmdir()  # drive unplugged between `workspace add` and this install
    assert any(str(ws).lower() == str(p).lower() for p in defender.wanted_paths())


def test_success_reports_every_path_it_registered(on_windows, ran, home):
    out = defender.lines()
    assert any(str(home) in line for line in out)
    assert not any("not registered" in line for line in out)
    assert any("Add-MpPreference" in s for s in ran)


def test_a_refusal_is_printed_verbatim(on_windows, ran, home):
    ran.replies = {"Add-MpPreference": DENIED}
    out = defender.lines()
    joined = "\n".join(out)
    assert "not registered" in joined
    # Defender's own words, not a sentence written here in advance.
    assert "requires elevation" in joined
    assert "Access is denied" in joined


def test_a_refusal_hands_back_the_command_to_re_run_elevated(on_windows, ran, home):
    ran.replies = {"Add-MpPreference": DENIED}
    out = defender.lines()
    hint = [line for line in out if defender.ELEVATED_HINT in line]
    assert hint, "a refusal must say how to finish the job"
    # The pasted command must be the command that was tried -- if these drift,
    # the hint sends the user somewhere that was never exercised.
    attempted = [s for s in ran if "Add-MpPreference" in s][0]
    assert attempted in hint[0]


def test_a_refusal_does_not_fail_the_install(on_windows, ran, home):
    ran.replies = {"Add-MpPreference": DENIED}
    # No exception, and the caller still gets its report.
    assert defender.lines()


def test_a_missing_powershell_is_reported_not_raised(on_windows, ran, home):
    ran.replies = {"Add-MpPreference": OSError("powershell not found")}
    out = defender.lines()
    assert any("could not run powershell" in line for line in out)
    assert any(defender.ELEVATED_HINT in line for line in out)


def test_a_wedged_defender_times_out_into_a_line(on_windows, ran, home):
    ran.replies = {"Add-MpPreference": subprocess.TimeoutExpired("powershell", 60)}
    out = defender.lines()
    assert any("did not answer" in line for line in out)
    assert any(defender.ELEVATED_HINT in line for line in out)


def test_a_path_already_excluded_is_said_so_and_not_re_added(on_windows, ran, home):
    ran.replies = {"Get-MpPreference": FakeProc(stdout=str(home))}
    out = defender.lines()
    assert any(str(home) in line and "already present" in line for line in out)
    added = [s for s in ran if "Add-MpPreference" in s]
    assert not any(str(home) in s for s in added)


def test_an_unreadable_exclusion_list_means_unknown_not_empty(on_windows, ran, home):
    """The machine that refuses to list must not be read as 'nothing excluded'.

    It is the same answer either way, so the only safe move is to add anyway --
    ``Add-MpPreference`` is idempotent -- and never to claim a state that was
    not read.
    """
    ran.replies = {"Get-MpPreference": UNREADABLE}
    out = defender.lines()
    assert not any("already present" in line for line in out)
    assert any("Add-MpPreference" in s for s in ran)


def test_the_os_temp_gap_is_stated_rather_than_left_to_be_assumed(on_windows, ran, home):
    """A clean exclusion list must not read as 'scanning is ruled out'."""
    out = defender.lines()
    assert any("temp" in line.lower() and "NOT excluded" in line for line in out)


def test_the_machine_scoped_installs_ask_and_a_project_install_does_not(
    on_windows, ran, home, tmp_path, monkeypatch
):
    """The scope rule install.py states about itself, applied to this too.

    An antivirus exclusion is machine state, so it rides with the installs
    that already write machine state -- not with the one whose whole promise
    is that it writes inside its project.
    """
    assert any("defender exclusion" in line for line in install_mod.install_into_user())

    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    project_lines = install_mod.install_into_project(proj)
    assert not any("defender exclusion" in line for line in project_lines)


def test_the_command_quotes_paths_with_spaces(on_windows):
    cmd = defender.add_command([Path(r"C:\Program Files\x"), Path(r"D:\a b")])
    assert "'C:\\Program Files\\x'" in cmd
    assert "'D:\\a b'" in cmd
    assert cmd.startswith("Add-MpPreference -ExclusionPath ")
