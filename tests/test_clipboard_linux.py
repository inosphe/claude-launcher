"""The host clipboard's text is read on Linux too, WSL included.

Under WSL the clipboard a person copies into is the Windows one, so it is read
through powershell.exe -- one process kept running and asked a line per read,
because the history samples every second and a PowerShell costs about 0.3 s to
start. Elsewhere on Linux it is the display server's, read with wl-paste or
xclip/xsel.

What is pinned here is which route a machine gets, what each route's answers
mean (text, the same text again, too long, an error, silence, an exit), and
that a PowerShell which fails is not restarted every second. The routes are
not run against a real clipboard: it is shared with the person using the
machine, and what is on it is not the suite's to know.
"""

from __future__ import annotations

import base64
import queue
import subprocess
import sys
from pathlib import Path

import pytest

from claude_launcher.daemon import clipboard


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# ---- which route --------------------------------------------------------


def test_a_platform_with_no_reader_names_itself():
    with pytest.raises(OSError) as exc:
        clipboard.read_text("darwin")
    assert "darwin" in str(exc.value)


def test_linux_under_wsl_reads_through_the_bridge(monkeypatch):
    class Bridge:
        def read(self):
            return "from windows"

    monkeypatch.setattr(clipboard, "_wsl_bridge", lambda: Bridge())
    monkeypatch.setattr(clipboard, "read_command", lambda argv: pytest.fail("ran a tool"))
    assert clipboard.read_text("linux") == "from windows"


def test_linux_without_wsl_runs_the_display_servers_tool(monkeypatch):
    # conftest already answers None for the bridge.
    monkeypatch.setattr(clipboard, "native_text_command", lambda: ["xclip", "-o"])
    monkeypatch.setattr(clipboard, "read_command", lambda argv: f"via {argv[0]}")
    assert clipboard.read_text("linux") == "via xclip"


def _which(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_wayland_reads_with_wl_paste_without_a_trailing_newline():
    argv = clipboard.native_text_command({"WAYLAND_DISPLAY": "wayland-0"},
                                         _which("wl-paste", "xclip"))
    assert argv == ["wl-paste", "--no-newline", "--type", "text"]


def test_x11_reads_with_xclip_then_xsel():
    env = {"DISPLAY": ":0"}
    assert clipboard.native_text_command(env, _which("xclip", "xsel"))[0] == "xclip"
    assert clipboard.native_text_command(env, _which("xsel"))[0] == "xsel"


def test_wayland_without_wl_paste_falls_back_to_xwayland():
    env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    assert clipboard.native_text_command(env, _which("xclip"))[0] == "xclip"


def test_no_display_and_no_tool_are_told_apart():
    with pytest.raises(OSError) as exc:
        clipboard.native_text_command({}, _which("wl-paste", "xclip"))
    assert "WAYLAND_DISPLAY" in str(exc.value) and "DISPLAY" in str(exc.value)
    with pytest.raises(OSError) as exc:
        clipboard.native_text_command({"DISPLAY": ":0"}, _which())
    assert "wl-clipboard" in str(exc.value) and "xclip" in str(exc.value)


# ---- finding powershell.exe under WSL -------------------------------------


def _wsl(tmp_path, *, release="6.18.33.2-microsoft-standard-WSL2", interop="enabled\n",
         mounts=""):
    osrelease = tmp_path / "osrelease"
    osrelease.write_text(release)
    binfmt = tmp_path / "binfmt_misc"
    binfmt.mkdir()
    if interop is not None:
        (binfmt / "WSLInterop").write_text(interop)
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(mounts)
    return {"osrelease": str(osrelease), "binfmt": str(binfmt), "mounts": str(proc_mounts)}


def _drive(tmp_path, name):
    root = tmp_path / name
    exe = root / clipboard._POWERSHELL
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return root, exe


def test_a_kernel_that_is_not_wsl_has_no_bridge(tmp_path):
    paths = _wsl(tmp_path, release="6.8.0-45-generic")
    assert clipboard.wsl_powershell(**paths, which=_which("powershell.exe")) is None


def test_wsl_with_interop_switched_off_has_no_bridge(tmp_path):
    for interop in ("disabled\n", None):
        sub = tmp_path / str(interop is None)
        sub.mkdir()
        paths = _wsl(sub, interop=interop)
        assert clipboard.wsl_powershell(**paths, which=_which("powershell.exe")) is None


def test_powershell_on_the_path_is_taken_as_it_is(tmp_path):
    paths = _wsl(tmp_path)
    found = clipboard.wsl_powershell(**paths, which=_which("powershell.exe"))
    assert found == "/usr/bin/powershell.exe"


def test_a_daemon_without_windows_on_its_path_finds_powershell_on_the_drive(tmp_path):
    # Measured on the machine this was written for: the daemon's PATH had no
    # /mnt/c entry at all, so `which` alone would have said there is no route.
    root, exe = _drive(tmp_path, "c drive")
    mounts = (
        "none /usr/lib/wsl/drivers 9p ro,aname=drivers;fmask=222 0 0\n"
        f"C:\\134 {str(root).replace(' ', chr(92) + '040')} 9p "
        "rw,noatime,aname=drvfs;path=C:\\;uid=1000 0 0\n"
    )
    paths = _wsl(tmp_path, mounts=mounts)
    assert clipboard.wsl_powershell(**paths, which=_which()) == str(exe)


def test_no_drive_holding_powershell_means_no_bridge(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    paths = _wsl(tmp_path, mounts=f"D:\\134 {root} drvfs rw 0 0\n")
    assert clipboard.wsl_powershell(**paths, which=_which()) is None


def test_the_bridge_script_goes_in_encoded_and_single_threaded():
    argv = clipboard.bridge_command("/mnt/c/ps.exe")
    assert argv[0] == "/mnt/c/ps.exe"
    # -STA: the clipboard API is single-threaded-apartment only.
    assert "-STA" in argv and "-NonInteractive" in argv
    # Encoded, because WSL rebuilds a Windows command line from the argv and
    # the script's quotes and $ would not survive it.
    script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "[System.Windows.Forms.Clipboard]::GetText" in script
    assert f"-gt {clipboard.MAX_TEXT}" in script
    for kind in ("text", "same", "long", "error"):
        assert f"'{kind}'" in script
    # A progress record would reach the pipe as serialized XML.
    assert "$ProgressPreference = 'SilentlyContinue'" in script


# ---- the bridge's conversation ---------------------------------------------


class _Out:
    def __init__(self):
        self.lines: queue.Queue = queue.Queue()

    def readline(self):
        return self.lines.get()

    def close(self):
        pass


class _In:
    def __init__(self, proc):
        self.proc = proc
        self.closed = False

    def write(self, data):
        if self.proc.returncode is not None:
            raise BrokenPipeError("gone")
        self.proc.requests += 1
        for line in self.proc.answer(self.proc.requests):
            if line is None:
                self.proc.exit(1)
                return
            self.proc.stdout.lines.put(line)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakePowerShell:
    """A PowerShell that answers from a script and never exists."""

    def __init__(self, answer, startup=()):
        self.answer = answer
        self.requests = 0
        self.returncode = None
        self.killed = False
        self.stdout = _Out()
        self.stdin = _In(self)
        for line in startup:
            self.stdout.lines.put(line)

    def exit(self, code):
        self.returncode = code
        self.stdout.lines.put(b"")

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.exit(-9)

    def wait(self, timeout=None):
        return self.returncode


class _Spawner:
    def __init__(self, *procs):
        self.procs = list(procs)
        self.spawned = []

    def __call__(self, argv, **kw):
        assert kw["stderr"] is subprocess.DEVNULL
        proc = self.procs.pop(0)
        if isinstance(proc, Exception):
            raise proc
        self.spawned.append(proc)
        return proc


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


_open_bridges = []


@pytest.fixture(autouse=True)
def _close_bridges():
    """End each fake process, so its pump thread does not outlive the test."""
    yield
    while _open_bridges:
        _open_bridges.pop().close()


def _bridge(*procs, timeout=5.0):
    spawner = _Spawner(*procs)
    clock = _Clock()
    bridge = clipboard.PowerShellBridge(["ps"], timeout=timeout, retry_after=10.0,
                                        popen=spawner, clock=clock)
    _open_bridges.append(bridge)
    return bridge, spawner, clock


def test_text_is_decoded_and_same_hands_back_what_was_read():
    replies = {1: [b"#< CLIXML\r\n", b"clip:text:" + _b64("한글\r\nline").encode() + b"\r\n"],
               2: [b"clip:same\r\n"]}
    proc = _FakePowerShell(lambda n: replies[n])
    bridge, spawner, _ = _bridge(proc)
    # A line that is not a reply (here the head of a serialized progress
    # record) is skipped rather than taken for one.
    assert bridge.read() == "한글\r\nline"
    assert bridge.read() == "한글\r\nline"
    assert len(spawner.spawned) == 1


def test_empty_text_is_an_empty_clipboard():
    bridge, _, _ = _bridge(_FakePowerShell(lambda n: [b"clip:text:\r\n"]))
    assert bridge.read() == ""


def test_too_long_and_a_busy_clipboard_are_errors_that_keep_the_process():
    replies = {1: [b"clip:long\r\n"],
               2: [b"clip:error:" + _b64("OpenClipboard failed").encode() + b"\r\n"],
               3: [b"clip:text:" + _b64("after").encode() + b"\r\n"]}
    proc = _FakePowerShell(lambda n: replies[n])
    bridge, spawner, _ = _bridge(proc)
    with pytest.raises(OSError) as exc:
        bridge.read()
    assert str(exc.value) == clipboard.TOO_LONG
    with pytest.raises(OSError) as exc:
        bridge.read()
    assert "OpenClipboard failed" in str(exc.value)
    assert bridge.read() == "after"
    assert len(spawner.spawned) == 1 and not proc.killed


def test_silence_kills_the_process_and_the_next_one_waits_out_the_retry():
    """A late answer would be read as the next read's, so the process that
    missed its deadline is killed, and the text it was holding is dropped:
    the next process starts with nothing cached, as its script does."""
    first = _FakePowerShell(lambda n: [b"clip:text:" + _b64("old").encode() + b"\r\n"] if n == 1 else [])
    second = _FakePowerShell(lambda n: [b"clip:same\r\n"])
    bridge, spawner, clock = _bridge(first, second, timeout=0.05)
    assert bridge.read() == "old"
    with pytest.raises(OSError) as exc:
        bridge.read()
    assert "did not answer within 0.05s" in str(exc.value)
    assert first.killed and first.stdin.closed
    with pytest.raises(OSError) as again:
        bridge.read()
    assert str(again.value) == str(exc.value)
    assert len(spawner.spawned) == 1, "restarted inside the retry window"
    clock.now += 10.0
    assert bridge.read() == ""
    assert len(spawner.spawned) == 2


def test_a_script_that_cannot_start_says_why_then_is_not_restarted_every_second():
    startup_failure = [b"clip:error:" + _b64("Add-Type: assembly not found").encode() + b"\r\n"]
    proc = _FakePowerShell(lambda n: [], startup=startup_failure)
    bridge, spawner, clock = _bridge(proc)
    with pytest.raises(OSError) as exc:
        bridge.read()
    assert "assembly not found" in str(exc.value)
    proc.exit(1)  # the script's `exit 1` after its reply
    with pytest.raises(OSError) as exc:
        bridge.read()
    assert "exited (code 1)" in str(exc.value)
    clock.now += 5.0
    with pytest.raises(OSError):
        bridge.read()
    assert len(spawner.spawned) == 1


def test_a_process_that_ends_mid_read_is_reported_with_its_code():
    proc = _FakePowerShell(lambda n: [None])
    bridge, _, _ = _bridge(proc)
    with pytest.raises(OSError) as exc:
        bridge.read()
    assert "exited (code 1) without answering" in str(exc.value)


def test_powershell_that_cannot_be_started_is_a_reason_and_is_retried_later():
    later = _FakePowerShell(lambda n: [b"clip:text:" + _b64("ok").encode() + b"\r\n"])
    bridge, spawner, clock = _bridge(OSError(8, "Exec format error"), later)
    with pytest.raises(OSError) as exc:
        bridge.read()
    assert "could not be started" in str(exc.value) and "Exec format error" in str(exc.value)
    with pytest.raises(OSError):
        bridge.read()
    clock.now += 10.0
    assert bridge.read() == "ok"


def test_an_unreadable_reply_restarts_rather_than_desynchronises():
    first = _FakePowerShell(lambda n: [b"clip:text:%%%\r\n"])
    bridge, spawner, clock = _bridge(first)
    with pytest.raises(OSError) as exc:
        bridge.read()
    assert "unreadable" in str(exc.value)
    assert first.killed


def test_close_ends_the_process_and_a_later_read_starts_another():
    first = _FakePowerShell(lambda n: [b"clip:text:" + _b64("a").encode() + b"\r\n"])
    second = _FakePowerShell(lambda n: [b"clip:text:" + _b64("b").encode() + b"\r\n"])
    bridge, spawner, _ = _bridge(first, second)
    assert bridge.read() == "a"
    bridge.close()
    assert first.killed and first.stdin.closed
    assert bridge.read() == "b"
    assert len(spawner.spawned) == 2


# ---- the same conversation over real pipes --------------------------------

_FAKE_SERVER = r"""
import base64, sys, time
n = 0
for line in sys.stdin:
    n += 1
    if n == 1:
        reply = "clip:text:" + base64.b64encode("한글\nline".encode()).decode()
    elif n == 2:
        reply = "clip:same"
    else:
        time.sleep(30)
        continue
    sys.stdout.write("noise before the reply\r\n" + reply + "\r\n")
    sys.stdout.flush()
"""


@pytest.mark.skipif(sys.platform == "win32", reason=(
    "a real child process in the same worker as the winpty session tests is "
    "what crashed the suite before (see test_image_clipboard._FakeProc); the "
    "bridge runs on Linux only"))
def test_the_bridge_over_real_pipes_reads_skips_noise_and_kills_on_silence():
    bridge = clipboard.PowerShellBridge([sys.executable, "-c", _FAKE_SERVER], timeout=5.0)
    try:
        assert bridge.read() == "한글\nline"
        assert bridge.read() == "한글\nline"
        proc = bridge._proc
        bridge.timeout = 0.3
        with pytest.raises(OSError) as exc:
            bridge.read()
        assert "did not answer" in str(exc.value)
        assert proc.poll() is not None, "the silent process was left running"
    finally:
        bridge.close()


# ---- the tools of a Linux desktop ------------------------------------------


class _Done:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_a_tool_that_prints_text_answers_it_as_utf8():
    run = lambda argv, **kw: _Done(0, "한글\nline".encode())
    assert clipboard.read_command(["wl-paste"], run=run) == "한글\nline"


@pytest.mark.parametrize("stderr", [
    b"Nothing is copied\n",                         # wl-paste, empty clipboard
    b"No suitable type of content copied\n",        # wl-paste, an image
    b"Error: target UTF8_STRING not available\n",   # xclip
])
def test_a_clipboard_with_no_text_is_empty_rather_than_an_error(stderr):
    run = lambda argv, **kw: _Done(1, stderr=stderr)
    assert clipboard.read_command(["tool"], run=run) == ""


def test_a_tool_that_fails_reports_its_last_stderr_line_or_its_code():
    run = lambda argv, **kw: _Done(1, stderr=b"warming up\nFailed to connect to a Wayland server\n")
    with pytest.raises(OSError) as exc:
        clipboard.read_command(["wl-paste"], run=run)
    assert str(exc.value) == "wl-paste: Failed to connect to a Wayland server"
    with pytest.raises(OSError) as exc:
        clipboard.read_command(["xsel"], run=lambda argv, **kw: _Done(3))
    assert "exit 3" in str(exc.value)


def test_a_tool_that_hangs_or_is_missing_is_a_reason():
    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    with pytest.raises(OSError) as exc:
        clipboard.read_command(["xclip"], timeout=5.0, run=hang)
    assert "did not finish within 5s" in str(exc.value)

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    with pytest.raises(OSError) as exc:
        clipboard.read_command(["xclip"], run=missing)
    assert "could not be started" in str(exc.value)


def test_output_past_the_limit_is_refused_before_it_is_decoded():
    run = lambda argv, **kw: _Done(0, b"x" * (clipboard.MAX_TEXT * 4 + 1))
    with pytest.raises(OSError) as exc:
        clipboard.read_command(["xclip"], run=run)
    assert str(exc.value) == clipboard.TOO_LONG
