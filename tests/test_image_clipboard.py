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
    with pytest.raises(clipboard.ClipboardError) as exc:
        asyncio.run(clipboard.put_image(target, "image/png"))
    assert "claunch-no-such-tool" in str(exc.value)


def test_a_command_that_fails_reports_its_last_stderr_line(tmp_path):
    target = tmp_path / "a.png"
    target.write_bytes(b"x")
    script = "import sys; sys.stderr.write('first\\nclipboard is locked\\n'); sys.exit(3)"
    argv = [sys.executable, "-c", script]
    with pytest.raises(clipboard.ClipboardError) as exc:
        asyncio.run(_put_with(argv, target))
    # The last line, because that is where a tool puts the thing that went
    # wrong; the operator reads this in the web session line.
    assert "clipboard is locked" in str(exc.value)


def test_a_command_that_hangs_is_killed_and_reported(tmp_path):
    target = tmp_path / "a.png"
    target.write_bytes(b"x")
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(clipboard.ClipboardError) as exc:
        asyncio.run(_put_with(argv, target, timeout=0.5))
    assert "0.5s" in str(exc.value)


async def _put_with(argv, target, timeout=15.0):
    """Run :func:`put_image` against a stand-in command."""
    real = clipboard.image_command
    clipboard.image_command = lambda *a, **k: clipboard.ClipboardCommand(list(argv))
    try:
        await clipboard.put_image(target, "image/png", timeout=timeout)
    finally:
        clipboard.image_command = real
