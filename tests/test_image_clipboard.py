"""The daemon puts an image on its own machine's clipboard.

This is the half of the web session line's image paste that leaves the
daemon: the file is already stored, and the harness reads images from the
clipboard, so the clipboard is what has to be filled before the keystroke is
sent.

There is no portable way to do it, so the module answers with a command per
platform. What is pinned here is that mapping and its refusals, because the
refusals are what the operator sees in place of an image: a platform with no
route, an image type the platform's clipboard has no class for, and a tool
that is not installed each have to say which one it was.

The commands themselves are not run. Running them would write to the
clipboard of whatever machine the suite is on, which is shared with the
person using it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from claude_launcher.daemon import clipboard


def test_windows_runs_powershell_in_an_sta_with_the_path_quoted():
    cmd = clipboard.image_command(Path(r"C:\tmp\a b.png"), "image/png", platform="win32")
    assert cmd.stdin_path is None
    assert cmd.argv[0] == "powershell.exe"
    # -STA is not decoration: the clipboard APIs are single-threaded-apartment
    # only, and without it SetImage throws instead of copying.
    assert "-STA" in cmd.argv
    script = cmd.argv[-1]
    assert "[System.Windows.Forms.Clipboard]::SetImage" in script
    assert r"'C:\tmp\a b.png'" in script


def test_a_quote_in_a_windows_path_cannot_end_the_powershell_string():
    target = Path("C:/tmp/it's.png")
    cmd = clipboard.image_command(target, "image/png", platform="win32")
    # Doubling is PowerShell's escape inside a single-quoted string. Anything
    # else here would let a file name run on into the command.
    assert "'" + str(target).replace("'", "''") + "'" in cmd.argv[-1]


@pytest.mark.parametrize(
    "media_type, klass",
    [("image/png", "PNGf"), ("image/jpeg", "JPEG"), ("image/gif", "GIFf")],
)
def test_macos_names_the_pasteboard_class_for_the_type(media_type, klass):
    # The path is built the way the module will see it. On the suite's own
    # platform a POSIX-looking literal is not what str(Path(...)) produces,
    # and the assertion would be about Windows, not about osascript.
    target = Path("/tmp/a.png")
    cmd = clipboard.image_command(target, media_type, platform="darwin")
    assert cmd.argv[0] == "osascript"
    assert klass in cmd.argv[-1]
    escaped = str(target).replace("\\", "\\\\")
    assert f'(POSIX file "{escaped}")' in cmd.argv[-1]


def test_macos_refuses_webp_instead_of_naming_a_class_that_does_not_exist():
    with pytest.raises(clipboard.ClipboardError) as exc:
        clipboard.image_command(Path("/tmp/a.webp"), "image/webp", platform="darwin")
    assert "image/webp" in str(exc.value)


def test_linux_uses_xclip_when_wayland_is_not_the_session(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    target = Path("/tmp/a.png")
    cmd = clipboard.image_command(target, "image/png", platform="linux")
    assert cmd.argv[:2] == ["xclip", "-selection"]
    assert cmd.argv[-2:] == ["-i", str(target)]
    assert "image/png" in cmd.argv


def test_linux_prefers_wl_copy_under_wayland_and_feeds_it_on_stdin(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    cmd = clipboard.image_command(Path("/tmp/a.png"), "image/png", platform="linux")
    assert cmd.argv == ["wl-copy", "--type", "image/png"]
    # wl-copy takes the image on stdin rather than as a path, so the caller
    # has to know to open the file.
    assert cmd.stdin_path == Path("/tmp/a.png")


def test_linux_with_neither_tool_says_so_rather_than_failing_at_exec(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    with pytest.raises(clipboard.ClipboardError) as exc:
        clipboard.image_command(Path("/tmp/a.png"), "image/png", platform="linux")
    assert "wl-copy" in str(exc.value) and "xclip" in str(exc.value)


def test_an_unknown_platform_is_a_reason_not_a_crash():
    with pytest.raises(clipboard.ClipboardError) as exc:
        clipboard.image_command(Path("/tmp/a.png"), "image/png", platform="aix")
    assert "aix" in str(exc.value)


def test_a_command_that_is_not_installed_comes_back_as_a_clipboard_error(
    monkeypatch, tmp_path
):
    target = tmp_path / "a.png"
    target.write_bytes(b"x")
    monkeypatch.setattr(
        clipboard,
        "image_command",
        lambda *a, **k: clipboard.ClipboardCommand(["claunch-no-such-tool"]),
    )

    async def missing(*argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    with pytest.raises(clipboard.ClipboardError) as exc:
        asyncio.run(clipboard.put_image(target, "image/png"))
    assert "claunch-no-such-tool" in str(exc.value)


def test_a_command_that_fails_reports_its_last_stderr_line(monkeypatch, tmp_path):
    proc = _FakeProc(returncode=3, stderr=b"first\nclipboard is locked\n")
    _stub(monkeypatch, proc)
    with pytest.raises(clipboard.ClipboardError) as exc:
        asyncio.run(clipboard.put_image(tmp_path / "a.png", "image/png"))
    # The last line, because that is where a tool puts the thing that went
    # wrong; the operator reads this in the web session line.
    assert "clipboard is locked" in str(exc.value)


def test_a_command_that_says_nothing_still_reports_its_exit_code(monkeypatch,
                                                                 tmp_path):
    _stub(monkeypatch, _FakeProc(returncode=9, stderr=b""))
    with pytest.raises(clipboard.ClipboardError) as exc:
        asyncio.run(clipboard.put_image(tmp_path / "a.png", "image/png"))
    assert "exit 9" in str(exc.value)


def test_a_command_that_hangs_is_killed_reaped_and_reported(monkeypatch, tmp_path):
    """Killing only asks. Until the process is waited for, the child stays
    around and its transport stays open, and a loop closing over a live
    subprocess transport takes the whole process down with it on Windows —
    which is how this was found: a pytest worker crashed inside winpty."""
    proc = _FakeProc(returncode=0, stderr=b"", hang=True)
    _stub(monkeypatch, proc)
    with pytest.raises(clipboard.ClipboardError) as exc:
        asyncio.run(clipboard.put_image(tmp_path / "a.png", "image/png", timeout=0.05))
    assert "0.05s" in str(exc.value)
    assert proc.killed is True
    assert proc.reaped is True


def test_a_wl_copy_style_command_is_fed_the_file_on_stdin(monkeypatch, tmp_path):
    target = tmp_path / "a.png"
    target.write_bytes(b"png bytes")
    proc = _FakeProc(returncode=0, stderr=b"")
    monkeypatch.setattr(
        clipboard,
        "image_command",
        lambda *a, **k: clipboard.ClipboardCommand(["wl-copy"], stdin_path=target),
    )
    _record = {}

    async def fake_exec(*argv, **kw):
        # Read here, while the handle is still the caller's: put_image closes
        # its own copy right after the spawn.
        _record["stdin"] = kw["stdin"].read()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(clipboard.put_image(target, "image/png"))
    assert _record["stdin"] == b"png bytes"


class _FakeProc:
    """A subprocess that never exists.

    These tests are about how :func:`put_image` translates what a command
    did, not about the platform's ability to start one. Spawning a real
    child for that turned out to cost more than it proved: an asyncio
    subprocess left in the same pytest process ahead of the winpty-backed
    session tests produced a heap-corruption crash on Windows.
    """

    def __init__(self, returncode, stderr, hang=False):
        self.returncode = returncode
        self._stderr = stderr
        self._hang = hang
        self.killed = False
        self.reaped = False

    async def communicate(self):
        if self._hang and not self.killed:
            await asyncio.sleep(3600)
        if self.killed:
            self.reaped = True
        return b"", self._stderr

    def kill(self):
        self.killed = True


def _stub(monkeypatch, proc):
    monkeypatch.setattr(
        clipboard,
        "image_command",
        lambda *a, **k: clipboard.ClipboardCommand(["a-clipboard-tool"]),
    )

    async def fake_exec(*argv, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
