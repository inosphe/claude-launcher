"""The retry in :mod:`claude_launcher.atomic` -- that it fires, and that it stops.

The bug this covers is Windows-only and timing-shaped, so the tests are built to
be the opposite: the mechanism is pinned once against the real syscall, and the
retry policy is then driven by a stub, so nothing here waits on a scanner to let
go of a file. A flaky test for a flake is not a fix.
"""

from __future__ import annotations

import os
import sys

import pytest

from claude_launcher import atomic

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="WinError 5 is Windows-only")


def _perm(winerror: int) -> OSError:
    """The exception ``os.replace`` raises when a holder blocks the delete.

    Built through ``OSError``'s four-argument form so CPython picks the same
    subclass it would pick for a real one -- which is not always
    ``PermissionError``, and is the reason the module keys on ``winerror``.
    """
    return OSError(13, "Access is denied", None, winerror)


@windows_only
def test_the_mechanism_this_absorbs_is_real(tmp_path):
    """A plain open handle on the destination is enough to make it WinError 5.

    Without this the module could be retrying something that never happens.
    ``open()`` does not ask for ``FILE_SHARE_DELETE``, so the replace cannot
    delete the destination -- that is the whole bug, in three lines.
    """
    dest = tmp_path / "doc.yaml"
    dest.write_text("old", encoding="utf-8")
    src = tmp_path / "doc.tmp"
    src.write_text("new", encoding="utf-8")

    with open(dest, encoding="utf-8"):
        with pytest.raises(PermissionError) as caught:
            os.replace(src, dest)

    assert caught.value.winerror in atomic.TRANSIENT


@windows_only
def test_a_holder_that_lets_go_costs_a_retry_not_a_failure(tmp_path, monkeypatch):
    """Two refusals then success: the caller sees a completed replace."""
    dest = tmp_path / "doc.yaml"
    src = tmp_path / "doc.tmp"
    src.write_text("new", encoding="utf-8")
    real = os.replace
    calls = []

    def flaky(a, b):
        calls.append((a, b))
        if len(calls) <= 2:
            raise _perm(5)
        return real(a, b)

    monkeypatch.setattr(atomic.os, "replace", flaky)
    monkeypatch.setattr(atomic, "BACKOFF", (0.0, 0.0, 0.0))

    atomic.replace(src, dest)

    assert len(calls) == 3
    assert dest.read_text(encoding="utf-8") == "new"
    assert not src.exists()


@windows_only
def test_a_holder_that_never_lets_go_still_raises(tmp_path, monkeypatch):
    """The budget is bounded, and what comes out is the original error.

    A retry that never gives up would trade a loud flake for a silent hang, and
    a retry that swallowed the error would hide a real ACL problem.
    """
    dest = tmp_path / "doc.yaml"
    src = tmp_path / "doc.tmp"
    src.write_text("new", encoding="utf-8")
    calls = []

    def always(a, b):
        calls.append((a, b))
        raise _perm(5)

    monkeypatch.setattr(atomic.os, "replace", always)
    monkeypatch.setattr(atomic, "BACKOFF", (0.0, 0.0, 0.0))

    with pytest.raises(PermissionError) as caught:
        atomic.replace(src, dest)

    assert caught.value.winerror == 5
    assert len(calls) == len(atomic.BACKOFF) + 1


@windows_only
@pytest.mark.parametrize(
    "winerror, kind",
    [
        (19, PermissionError),  # ERROR_WRITE_PROTECT -- a PermissionError...
        (1314, OSError),  # ...ERROR_PRIVILEGE_NOT_HELD -- and this one is not
    ],
)
def test_a_refusal_that_is_not_transient_is_answered_at_once(
    tmp_path, monkeypatch, winerror, kind
):
    """A wrong ACL is a real answer; retrying it only delays the report.

    Both codes are checked because CPython decides the exception's *class* from
    a winerror table, and the retry must not inherit that decision: 19 arrives
    as ``PermissionError`` and 1314 as a bare ``OSError``, and neither is
    transient, so both must come straight back out.
    """
    dest = tmp_path / "doc.yaml"
    src = tmp_path / "doc.tmp"
    src.write_text("new", encoding="utf-8")
    calls = []

    def denied(a, b):
        calls.append((a, b))
        raise _perm(winerror)

    monkeypatch.setattr(atomic.os, "replace", denied)

    with pytest.raises(kind) as caught:
        atomic.replace(src, dest)

    assert type(caught.value) is kind
    assert len(calls) == 1


def test_the_ordinary_path_is_one_call(tmp_path, monkeypatch):
    """Nothing is retried when nothing refuses -- POSIX included."""
    dest = tmp_path / "doc.yaml"
    src = tmp_path / "doc.tmp"
    src.write_text("new", encoding="utf-8")
    calls = []
    real = os.replace

    def counted(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(atomic.os, "replace", counted)

    atomic.replace(src, dest)

    assert len(calls) == 1
    assert dest.read_text(encoding="utf-8") == "new"
