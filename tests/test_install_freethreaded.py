"""The free-threaded install script decides p2p before the long zvec build.

``tools/install_freethreaded.sh`` used to install ``.[p2p]``, and if that failed
it silently reinstalled without the extra (claunch-fsn9f): the user got a
``claunch-nogil`` with no P2P support and one stderr line explaining it. The
script now decides p2p *before* the nine-minute build and either asks, stops,
or continues -- never a silent downgrade.

These cases run the real script against a stub ``uv`` that records what the
script asked for, so the decision is what is measured. A stub ``uv`` makes the
build a no-op and a stub tool python answers the post-install zvec check. The
macOS branch of the same script is selected through a stub ``uname`` (every
other case pins ``Linux``, so the host's OS does not change what they measure).
The Windows counterpart (``install_freethreaded.ps1``) is run the same way when a
``pwsh`` is on PATH -- skipped otherwise; see the last two cases and
claunch-fsn9f.1.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

def _posix_bash() -> str | None:
    """The bash that runs the script under test.

    On Windows a bare ``bash`` resolves to ``System32\bash.exe``, the WSL
    launcher, which cannot see a Windows path and fails when no distribution
    is set up. Git for Windows ships a bash that can, so it is looked up
    through ``git`` itself; where neither exists the cases are skipped.
    """
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if git:
        for root in Path(git).resolve().parents:
            candidate = root / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    return None


BASH = _posix_bash()

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "install_freethreaded.sh"
PS1 = Path(__file__).resolve().parents[1] / "tools" / "install_freethreaded.ps1"

# Logs its argv, answers `tool dir`, and can be told to fail the install the
# way a real missing header fails it -- after the check said it was there.
# `build` records and succeeds without producing a wheel, which is enough to
# tell "the decision came first" from "the build came first".
# `tool run` stands in for the pylibsrtp build: `pip wheel --wheel-dir D` leaves
# an abi3 wheel in D and `wheel tags --remove F` renames F to the cp314t tag, so
# the script's own lookups see what the real tools leave behind. `tool install`
# copies its --overrides file next to the log, because the script deletes it on
# exit.
STUB_UV = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_UV_LOG"
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  printf 'CPPFLAGS=%s LDFLAGS=%s\\n' "${CPPFLAGS:-}" "${LDFLAGS:-}" >> "$STUB_UV_LOG.env"
fi
if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then echo "$STUB_TOOL_DIR"; exit 0; fi
if [ "$1" = "tool" ] && [ "$2" = "run" ]; then
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[$i]}" in
      --wheel-dir) : > "${args[$((i + 1))]}/pylibsrtp-1.0.0-cp310-abi3-linux_x86_64.whl" ;;
      --remove) f="${args[$((i + 1))]}"; [ -f "$f" ] && mv "$f" "${f/cp310-abi3/cp314-cp314t}" ;;
    esac
  done
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  prev=""
  for a in "$@"; do
    [ "$prev" = "--overrides" ] && cat "$a" >> "$STUB_UV_LOG.overrides"
    prev="$a"
  done
fi
if [ "$1" = "tool" ] && [ "$2" = "install" ] && [ -n "${STUB_FAIL_INSTALL:-}" ]; then
  echo "SIMULATED: the p2p build failed" >&2
  exit 1
fi
exit 0
"""

STUB_TOOL_PYTHON = """#!/usr/bin/env bash
cat >/dev/null
echo "python: 3.14.0 (stub) | Py_GIL_DISABLED = 1"
echo "zvec: 0.8.0 (stub) | GIL enabled with PYTHON_GIL=0: False"
"""

# Enough of git for the zvec build path to run: the script only ever clones,
# checks out and updates submodules. Exists so the ordering case below does not
# depend on the host having git or a C++ compiler.
STUB_GIT = """#!/usr/bin/env bash
exit 0
"""

STUB_CXX = """#!/usr/bin/env bash
exit 0
"""

# Answers `getent passwd <uid>` with the sandbox home, so the script's
# $HOME-unset fallback can be exercised without resolving to the developer's
# real home -- which is how this defect was first reached by hand, and running
# the install there for real is how it did damage (claunch-a0ero).
STUB_GETENT = """#!/usr/bin/env bash
[ -n "${STUB_GETENT_EMPTY:-}" ] && exit 2
printf '%s:x:1000:1000::%s:/bin/bash\\n' "sandbox" "$STUB_HOME_ANSWER"
"""

# Answers `--exists libsrtp2` with {exit_code} and `--exists libcrypto` with
# {crypto_code}: 0 means the development files are present. The flag queries
# answer the way a Homebrew pkg-config does on Apple Silicon.
STUB_PKG_CONFIG = """#!/usr/bin/env bash
[ "$1" = "--exists" ] && [ "$2" = "libsrtp2" ] && exit {exit_code}
[ "$1" = "--exists" ] && [ "$2" = "libcrypto" ] && exit {crypto_code}
[ "$1" = "--cflags" ] && { echo "-I/opt/homebrew/include"; exit 0; }
[ "$1" = "--libs-only-L" ] && { echo "-L/opt/homebrew/lib"; exit 0; }
exit 1
"""

# `uname -s` answers $STUB_UNAME, so each case picks the Linux or macOS branch.
STUB_UNAME = """#!/usr/bin/env bash
echo "${STUB_UNAME:-Linux}"
"""

# `xcode-select -p` succeeds unless STUB_NO_XCODE is set.
STUB_XCODE_SELECT = """#!/usr/bin/env bash
[ -n "${STUB_NO_XCODE:-}" ] && exit 2
echo /Library/Developer/CommandLineTools
"""

# `dscl . -read /Users/<name> NFSHomeDirectory`, answered with the sandbox
# home for the same reason getent is stubbed (claunch-a0ero).
STUB_DSCL = """#!/usr/bin/env bash
printf 'NFSHomeDirectory: %s\\n' "$STUB_HOME_ANSWER"
"""


def _overrides_tail(line: str) -> str:
    """The --overrides path of a logged `tool install` line.

    It is a temp file name, so assertions about the target strip it.
    """
    marker = "--overrides "
    return line.split(marker, 1)[1] if marker in line else ""


def _without_pkg_config(path: str, shadow_root: Path) -> str:
    """``path`` with every ``pkg-config`` on it hidden.

    Hiding the host's copy, rather than only skipping the stub, is what makes
    "the host has no pkg-config" a real condition: a sandbox that merely skips
    writing a stub still inherits the host's copy, and a developer who followed
    the README's `apt install pkg-config` advice would then get different
    results from CI or from a machine that skipped it (caught in review,
    claunch-fsn9f).

    A directory that holds one is replaced by a directory under
    ``shadow_root`` that links everything else in it. Dropping the directory
    outright also dropped ``/usr/bin`` -- where Ubuntu puts pkg-config next to
    bash and coreutils -- and the run then failed with FileNotFoundError
    before the script started.
    """
    names = ("pkg-config", "pkg-config.exe")
    kept = []
    for index, entry in enumerate(path.split(os.pathsep)):
        if not entry:
            continue
        if not any(os.access(os.path.join(entry, name), os.X_OK) for name in names):
            kept.append(entry)
            continue
        shadow = shadow_root / f"path-{index}"
        shadow.mkdir(parents=True, exist_ok=True)
        try:
            children = os.listdir(entry)
        except OSError:
            continue
        for child in children:
            if child in names:
                continue
            link = shadow / child
            if not os.path.lexists(link):
                os.symlink(os.path.join(entry, child), link)
        kept.append(str(shadow))
    return os.pathsep.join(kept)


@pytest.fixture
def sandbox(tmp_path: Path):
    """A stub PATH, a stub tool directory, and a wheel the script accepts."""
    if BASH is None:
        pytest.skip("no POSIX bash that can run the install script on this host")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(STUB_UV, encoding="utf-8")
    uv.chmod(0o755)

    # The zvec build path shells out to git and looks for a C++ compiler.
    # Stubbing both keeps the ordering case independent of the host toolchain.
    # `getent` is stubbed so the $HOME fallback answers with a sandbox path
    # instead of the developer's real home (claunch-a0ero).
    for name, body in (("git", STUB_GIT), ("c++", STUB_CXX), ("getent", STUB_GETENT),
                       ("uname", STUB_UNAME), ("xcode-select", STUB_XCODE_SELECT),
                       ("dscl", STUB_DSCL)):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)

    tool_python = tmp_path / "tools" / "claude-launcher" / "bin" / "python"
    tool_python.parent.mkdir(parents=True)
    tool_python.write_text(STUB_TOOL_PYTHON, encoding="utf-8")
    tool_python.chmod(0o755)

    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    wheel = wheel_dir / "zvec-0.8.0-cp314t-cp314t-linux_x86_64.whl"
    wheel.write_text("", encoding="utf-8")

    log = tmp_path / "uv.log"

    def pkg_config(exit_code: int | None, crypto_code: int | None = None) -> None:
        path = bin_dir / "pkg-config"
        if exit_code is None:
            if path.exists():
                path.unlink()
            return
        crypto = exit_code if crypto_code is None else crypto_code
        body = STUB_PKG_CONFIG.replace("{exit_code}", str(exit_code)).replace("{crypto_code}", str(crypto))
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run(*args: str, stdin: str = "", pkg: int | None = 1, crypto: int | None = None,
            fail_install: bool = False, wheel_path: str | None = "__default__",
            home: str | None = "__default__", no_overrides: bool = False,
            os_name: str = "Linux", extra_env: dict[str, str] | None = None):
        """Run the script. ``pkg=None`` leaves pkg-config unreachable.

        ``crypto`` answers ``pkg-config --exists libcrypto``; ``None`` answers
        it the same as libsrtp2.

        ``wheel_path=None`` clears CLAUNCH_FT_ZVEC_WHEEL so the script takes the
        real build path -- the only configuration in which "the check ran before
        the build" is observable.

        ``home`` sets HOME; ``home=None`` removes it entirely, which is the
        condition claunch-a0ero is about.

        ``no_overrides`` clears CLAUNCH_FT_ROOT, CLAUNCH_FT_SHIM_DIR and
        CLAUNCH_FT_WHEELS. Those three bypass the defaults that read HOME, so
        with them set this defect cannot happen at all -- the cases that
        measure it have to run without them. The values still land inside
        tmp_path: the defaults are computed from whichever HOME the run gets,
        and the script answers from the sandbox's own home directory.
        """
        pkg_config(pkg, crypto)
        env = dict(os.environ)
        inherited = env.get("PATH", "")
        if pkg is None:
            inherited = _without_pkg_config(inherited, tmp_path / "path-shadow")
        env.update(
            {
                "PATH": os.pathsep.join([str(bin_dir), inherited]),
                "STUB_UV_LOG": str(log),
                "STUB_TOOL_DIR": str(tmp_path / "tools"),
                "STUB_HOME_ANSWER": str(tmp_path / "fallback-home"),
            }
        )
        if no_overrides:
            for name in ("CLAUNCH_FT_ROOT", "CLAUNCH_FT_SHIM_DIR", "CLAUNCH_FT_WHEELS"):
                env.pop(name, None)
        else:
            env.update(
                {
                    "CLAUNCH_FT_ROOT": str(tmp_path / "ft-root"),
                    "CLAUNCH_FT_SHIM_DIR": str(tmp_path / "shim"),
                    "CLAUNCH_FT_WHEELS": str(tmp_path / "wheels"),
                }
            )
        if home is None:
            env.pop("HOME", None)
        elif home != "__default__":
            env["HOME"] = home
        if wheel_path is None:
            env.pop("CLAUNCH_FT_ZVEC_WHEEL", None)
        elif wheel_path == "__default__":
            env["CLAUNCH_FT_ZVEC_WHEEL"] = str(wheel)
        else:
            env["CLAUNCH_FT_ZVEC_WHEEL"] = wheel_path
        if fail_install:
            env["STUB_FAIL_INSTALL"] = "1"
        else:
            env.pop("STUB_FAIL_INSTALL", None)
        env["STUB_UNAME"] = os_name
        for name in ("STUB_NO_XCODE", "STUB_GETENT_EMPTY", "CPPFLAGS", "LDFLAGS"):
            env.pop(name, None)
        env.update(extra_env or {})
        return subprocess.run(
            [BASH, SCRIPT.as_posix(), *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(SCRIPT.parents[1]),
        )

    def calls() -> list[str]:
        """Every argv line the stub uv recorded, in order."""
        if not log.exists():
            return []
        return log.read_text(encoding="utf-8").splitlines()

    def installs() -> list[str]:
        """The `tool install` calls the script actually issued, target kept."""
        return [line for line in calls() if line.startswith("tool install")]

    def install_env() -> list[str]:
        """CPPFLAGS/LDFLAGS as each `tool install` saw them."""
        env_log = Path(str(log) + ".env")
        return env_log.read_text(encoding="utf-8").splitlines() if env_log.exists() else []

    def overrides() -> list[str]:
        """The --overrides lines every `tool install` was handed, in order."""
        path = Path(str(log) + ".overrides")
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    return {"run": run, "installs": installs, "calls": calls, "overrides": overrides,
            "install_env": install_env, "wheels": tmp_path / "wheels"}


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def _targets(sandbox) -> list[str]:
    """Just the --editable targets, in the order they were asked for."""
    targets = []
    for line in sandbox["installs"]():
        parts = line.split()
        if "--editable" in parts:
            targets.append(parts[parts.index("--editable") + 1])
    return targets


def test_no_p2p_does_not_ask_and_installs_without_the_extra(sandbox):
    proc = sandbox["run"]("--no-p2p", pkg=None)
    out = _out(proc)
    assert proc.returncode == 0, out
    # No question and no install guide: --no-p2p already answered it.
    assert "번호를 고르세요" not in out
    assert "고치려면" not in out
    assert _targets(sandbox) == ["."]


def test_summary_states_the_feature_is_out(sandbox):
    proc = sandbox["run"]("--no-p2p", pkg=None)
    assert proc.returncode == 0, _out(proc)
    assert "빠른 직접 연결 기능: 포함되지 않음" in _out(proc)


def test_summary_states_the_feature_is_in_and_installs_the_extra(sandbox):
    proc = sandbox["run"](pkg=0)
    assert proc.returncode == 0, _out(proc)
    assert "빠른 직접 연결 기능: 포함됨" in _out(proc)
    assert _targets(sandbox) == [".[p2p]"]


def test_require_p2p_stops_when_libsrtp2_is_missing(sandbox):
    proc = sandbox["run"]("--require-p2p", pkg=1)
    out = _out(proc)
    assert proc.returncode != 0
    assert "sudo apt install -y libsrtp2-dev libssl-dev pkg-config" in out
    assert "tools/install_freethreaded.sh --rebuild" in out
    assert sandbox["installs"]() == []


def test_the_first_line_says_what_breaks_before_naming_the_technical_term(sandbox):
    """The user who does not know what p2p is still learns what they lose."""
    proc = sandbox["run"]("--require-p2p", pkg=None)
    out = _out(proc)
    assert proc.returncode != 0
    first = next(line for line in out.splitlines() if line.strip())
    assert "브라우저와 데몬" in first
    # The technical name is given parenthetically, not as the subject.
    assert "(pkg-config)" in first
    assert not first.startswith("pkg-config")
    assert not first.startswith("libsrtp2")


def test_no_terminal_and_no_flag_stops_rather_than_installing_without_it(sandbox):
    """The accident: a script run without a terminal used to install without
    p2p and mention it in one line on the way out."""
    proc = sandbox["run"](pkg=1, stdin="")
    out = _out(proc)
    assert proc.returncode != 0
    assert "물어볼 수 없습니다" in out
    # It names the flag that makes the non-interactive choice explicit.
    assert "--no-p2p" in out
    assert sandbox["installs"]() == []


def test_a_failed_p2p_build_does_not_fall_back_silently(sandbox):
    """The check passing does not promise the build succeeds. When it does
    not, the script fails: there is no second install without the extra."""
    proc = sandbox["run"](pkg=0, fail_install=True)
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "SIMULATED: the p2p build failed" in out
    # One attempt, and it was the one with the extra -- not a downgrade.
    assert _targets(sandbox) == [".[p2p]"]


def test_the_decision_is_reached_with_HOME_unset(sandbox):
    """claunch-a0ero: `set -u` made an empty HOME fatal on the first default.

    Three branches, all with HOME gone and no CLAUNCH_FT_* overrides -- the
    overrides bypass the defaults, so with them set this cannot fail.
    """
    stop = sandbox["run"]("--require-p2p", pkg=1, home=None, no_overrides=True)
    out = _out(stop)
    assert stop.returncode == 1, out
    assert "HOME: unbound variable" not in out, out
    assert "멈춥니다 — --require-p2p" in out, out
    assert sandbox["installs"]() == []

    # HOME is gone and there is no terminal: it stops, rather than quietly
    # installing without the feature or dying on the way there.
    blind = sandbox["run"](pkg=1, home=None, no_overrides=True, stdin="")
    out = _out(blind)
    assert blind.returncode == 1, out
    assert "HOME: unbound variable" not in out, out
    assert "물어볼 수 없습니다" in out, out
    assert sandbox["installs"]() == []


@pytest.mark.skipif(os.name == "nt", reason="the sandbox redirects HOME/XDG, which a Windows host does not honour")
def test_the_defaults_land_under_the_fallback_home_when_HOME_is_unset(sandbox, tmp_path):
    """The fallback has to be the passwd entry, not an empty string.

    An empty HOME let `$HOME/.local/bin` collapse to `/.local/bin`, which is
    not a path anyone can write; the point of the fallback is that the install
    still lands somewhere real.
    """
    proc = sandbox["run"]("--no-p2p", pkg=1, home=None, no_overrides=True)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert "HOME: unbound variable" not in out, out
    assert _targets(sandbox) == ["."]
    assert (tmp_path / "fallback-home" / ".local" / "share" / "claunch-nogil").is_dir(), \
        sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir())[:20]
    assert (tmp_path / "fallback-home" / ".local" / "bin" / "claunch-nogil").is_file(), \
        sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))[:20]


def test_no_p2p_still_wins_when_HOME_is_unset(sandbox):
    """The flag answers the question, so no terminal is needed to install."""
    proc = sandbox["run"]("--no-p2p", pkg=1, home=None, no_overrides=True)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert "번호를 고르세요" not in out
    assert "빠른 직접 연결 기능: 포함되지 않음" in out
    assert _targets(sandbox) == ["."]


def test_the_decision_comes_before_the_zvec_build(sandbox):
    """The goal is the *order*, not just the message.

    Every case above hands in ``CLAUNCH_FT_ZVEC_WHEEL``, so they never enter
    the build path -- and a script that moved the p2p decision after the build
    (9 minutes, then ask) passed all of them. This case clears the wheel so the
    real build path runs, and asserts it was never reached.
    """
    proc = sandbox["run"]("--require-p2p", pkg=1, wheel_path=None)
    out = _out(proc)
    assert proc.returncode != 0, out
    # Stopped for the p2p reason, not because a build failed or a tool was
    # missing.
    assert "sudo apt install -y libsrtp2-dev libssl-dev pkg-config" in out
    # The build never started: no `uv build`, and therefore no 9-minute wait
    # before the question.
    calls = sandbox["calls"]()
    assert not [line for line in calls if line.startswith("build")], calls
    # And nothing was installed either.
    assert sandbox["installs"]() == []


def test_nothing_heavy_happens_before_the_decision(sandbox):
    """Weaker than the case above but pins more: no uv call at all."""
    proc = sandbox["run"]("--require-p2p", pkg=None, wheel_path=None)
    assert proc.returncode != 0, _out(proc)
    assert sandbox["calls"]() == [], sandbox["calls"]()


def test_both_flags_are_refused(sandbox):
    proc = sandbox["run"]("--no-p2p", "--require-p2p")
    assert proc.returncode == 2
    assert "contradict" in _out(proc)
    assert sandbox["installs"]() == []


def test_unknown_argument_is_refused(sandbox):
    proc = sandbox["run"]("--bogus")
    assert proc.returncode == 2
    assert "unknown argument" in _out(proc)


def test_help_lists_both_flags_and_the_non_interactive_default(sandbox):
    proc = sandbox["run"]("--help")
    out = _out(proc)
    assert proc.returncode == 0
    assert "--no-p2p" in out
    assert "--require-p2p" in out
    assert "no terminal" in out


def test_require_p2p_stops_when_the_openssl_dev_files_are_missing(sandbox):
    """libsrtp2 alone passed the old check; pylibsrtp then failed to link
    with `cannot find -lcrypto` on a machine without libssl-dev."""
    proc = sandbox["run"]("--require-p2p", pkg=0, crypto=1)
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "libssl-dev" in out.splitlines()[0], out
    assert "sudo apt install -y libsrtp2-dev libssl-dev pkg-config" in out
    assert sandbox["installs"]() == []


def test_p2p_builds_pylibsrtp_retags_it_and_overrides_it(sandbox):
    """pylibsrtp's setup.py tags every wheel cp310-abi3, which 3.14t refuses.

    The script builds it from the sdist, retags it cp314-cp314t, and points
    the install at that file through --overrides.
    """
    proc = sandbox["run"]("--require-p2p", pkg=0)
    out = _out(proc)
    assert proc.returncode == 0, out
    runs = [line for line in sandbox["calls"]() if line.startswith("tool run")]
    assert any("pip wheel" in line and "--no-binary pylibsrtp" in line and "pylibsrtp==1.0.0" in line
               for line in runs), runs
    assert any("wheel tags --python-tag cp314 --abi-tag cp314t --remove" in line for line in runs), runs
    srtp = [line for line in sandbox["overrides"]() if line.startswith("pylibsrtp @ ")]
    assert len(srtp) == 1, sandbox["overrides"]()
    assert srtp[0].endswith("/pylibsrtp-1.0.0/pylibsrtp-1.0.0-cp314-cp314t-linux_x86_64.whl"), srtp
    # The build ran before the install that uses it.
    calls = sandbox["calls"]()
    first_install = next(i for i, line in enumerate(calls) if line.startswith("tool install"))
    assert all(calls.index(line) < first_install for line in runs), calls


@pytest.mark.skipif(os.name == "nt", reason="the sandbox redirects HOME/XDG, which a Windows host does not honour")
def test_a_cached_pylibsrtp_wheel_is_reused(sandbox):
    cached = sandbox["wheels"] / "pylibsrtp-1.0.0" / "pylibsrtp-1.0.0-cp314-cp314t-linux_x86_64.whl"
    cached.parent.mkdir(parents=True)
    cached.write_text("", encoding="utf-8")
    proc = sandbox["run"]("--require-p2p", pkg=0)
    assert proc.returncode == 0, _out(proc)
    assert not [line for line in sandbox["calls"]() if line.startswith("tool run")], sandbox["calls"]()
    assert f"pylibsrtp @ file://{cached}" in sandbox["overrides"](), sandbox["overrides"]()


def test_no_p2p_does_not_build_or_override_pylibsrtp(sandbox):
    proc = sandbox["run"]("--no-p2p", pkg=None)
    assert proc.returncode == 0, _out(proc)
    assert not [line for line in sandbox["calls"]() if line.startswith("tool run")], sandbox["calls"]()
    assert not [line for line in sandbox["overrides"]() if line.startswith("pylibsrtp")], sandbox["overrides"]()


# --------------------------------------------------------------------------
# The macOS branch of the .sh script
# --------------------------------------------------------------------------


def test_macos_guide_names_homebrew_instead_of_apt(sandbox):
    proc = sandbox["run"]("--require-p2p", pkg=1, os_name="Darwin")
    out = _out(proc)
    assert proc.returncode == 1, out
    assert "brew install srtp openssl pkg-config" in out, out
    assert "apt install" not in out, out
    assert sandbox["installs"]() == []


def test_macos_summary_without_the_feature_names_homebrew(sandbox):
    proc = sandbox["run"]("--no-p2p", pkg=None, os_name="Darwin")
    out = _out(proc)
    assert proc.returncode == 0, out
    assert "이 기능을 추가하려면: brew install srtp openssl pkg-config" in out, out
    assert "apt install" not in out, out


def test_macos_p2p_install_gets_the_homebrew_paths(sandbox):
    """clang does not search /opt/homebrew, so pylibsrtp needs them passed."""
    proc = sandbox["run"](pkg=0, os_name="Darwin")
    assert proc.returncode == 0, _out(proc)
    assert _targets(sandbox) == [".[p2p]"]
    assert sandbox["install_env"]() == ["CPPFLAGS=-I/opt/homebrew/include LDFLAGS=-L/opt/homebrew/lib"]


def test_linux_p2p_install_leaves_the_compiler_flags_alone(sandbox):
    proc = sandbox["run"](pkg=0)
    assert proc.returncode == 0, _out(proc)
    assert sandbox["install_env"]() == ["CPPFLAGS= LDFLAGS="]


def test_macos_without_command_line_tools_stops_before_the_build(sandbox):
    """/usr/bin/c++ exists on a Mac without a toolchain; xcode-select decides."""
    proc = sandbox["run"]("--no-p2p", pkg=None, os_name="Darwin", wheel_path=None,
                          extra_env={"STUB_NO_XCODE": "1"})
    out = _out(proc)
    assert proc.returncode == 1, out
    assert "missing: C++ compiler (xcode-select --install)" in out, out
    assert not [line for line in sandbox["calls"]() if line.startswith("build")]


def test_macos_HOME_unset_falls_back_to_directory_services(sandbox, tmp_path):
    """macOS has no getent; the passwd entry comes from dscl."""
    proc = sandbox["run"]("--no-p2p", pkg=1, home=None, no_overrides=True, os_name="Darwin",
                          extra_env={"STUB_GETENT_EMPTY": "1"})
    out = _out(proc)
    assert proc.returncode == 0, out
    assert (tmp_path / "fallback-home" / ".local" / "bin" / "claunch-nogil").is_file(), out


# --------------------------------------------------------------------------
# The Windows counterpart, run under pwsh. claunch-fsn9f.1
# --------------------------------------------------------------------------
#
# The ps1 computed its two per-user defaults with `Join-Path $env:LOCALAPPDATA`
# and Join-Path refuses a null base, so anywhere that variable is unset --
# every host that is not Windows -- the script threw at line 66 before it
# reached the p2p decision, and the decision branches could not be run at all.
# These cases pin that the decision is reachable with LOCALAPPDATA empty, and
# that a set LOCALAPPDATA still decides where the defaults go.
#
# They need a real pwsh; where there is none they are skipped rather than
# passing on nothing.

PWSH = shutil.which("pwsh")


@pytest.fixture
def ps1_sandbox(tmp_path: Path):
    """Stub uv, stub tool python, a wheel, and a run() that runs the ps1."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(STUB_UV, encoding="utf-8")
    uv.chmod(0o755)

    # The ps1 joins 'claude-launcher\Scripts\python.exe'; on a non-Windows host
    # pwsh reads the backslash as a separator, so the stub is the nested file.
    tool_python = tmp_path / "tooldir" / "claude-launcher" / "Scripts" / "python.exe"
    tool_python.parent.mkdir(parents=True)
    tool_python.write_text(STUB_TOOL_PYTHON, encoding="utf-8")
    tool_python.chmod(0o755)

    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    wheel = wheel_dir / "zvec-0.8.0-cp314t-cp314t-linux_x86_64.whl"
    wheel.write_text("", encoding="utf-8")

    log = tmp_path / "uv.log"

    def pkg_config(exit_code: int | None) -> None:
        path = bin_dir / "pkg-config"
        if exit_code is None:
            if path.exists():
                path.unlink()
            return
        body = STUB_PKG_CONFIG.replace("{exit_code}", str(exit_code)).replace("{crypto_code}", str(exit_code))
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run(*args: str, pkg: int | None = 1, local_app_data: str | None = None,
            home: str | None = "__default__", stdin: str = ""):
        """Run the ps1. ``local_app_data=None`` unsets the variable -- the defect.

        ``pkg=None`` also strips every inherited directory that holds a
        pkg-config, for the reason ``_without_pkg_config`` gives.

        ``XDG_DATA_HOME`` is pointed inside ``tmp_path`` and ``HOME`` is too,
        because the script's own default paths resolve through both: pwsh's
        ``GetFolderPath('LocalApplicationData')`` prefers ``XDG_DATA_HOME`` over
        ``HOME``, so a sandbox that set only ``HOME`` still created
        ``$XDG_DATA_HOME/claunch-nogil/{bin,tools}`` -- outside ``tmp_path``, on
        a real directory, while the case passed (found in review, claunch-fsn9f.1).
        That preference only applies when the directory exists: measured,
        ``GetFolderPath`` returns empty for a ``XDG_DATA_HOME`` that is not
        there, and the script then falls through to ``$HOME/.local/share``. So
        the fixture creates the directory, and without the redirect the whole
        run stays inside ``tmp_path`` and the leak the review found is not
        reproduced at all.
        ``home=None`` unsets ``HOME`` instead, for the case that pins the shim's
        default against it.
        """
        pkg_config(pkg)
        env = dict(os.environ)
        for name in ("CLAUNCH_FT_ROOT", "CLAUNCH_FT_WHEELS", "CLAUNCH_FT_SHIM_DIR",
                     "CLAUNCH_FT_ZVEC_WHEEL"):
            env.pop(name, None)
        inherited = env.get("PATH", "")
        if pkg is None:
            inherited = _without_pkg_config(inherited, tmp_path / "path-shadow")
        env["PATH"] = os.pathsep.join([str(bin_dir), inherited])
        env["STUB_UV_LOG"] = str(log)
        env["STUB_TOOL_DIR"] = str(tmp_path / "tooldir")
        env["CLAUNCH_FT_ZVEC_WHEEL"] = str(wheel)
        # Created, not just named: GetFolderPath answers with this path only
        # while the directory exists (measured), and the point is to make the
        # sandbox the place the script's defaults resolve to.
        (tmp_path / "xdg-data").mkdir(exist_ok=True)
        env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
        if home is None:
            env.pop("HOME", None)
        elif home == "__default__":
            env["HOME"] = str(tmp_path / "home")
        else:
            env["HOME"] = home
        if local_app_data is None:
            env.pop("LOCALAPPDATA", None)
        else:
            env["LOCALAPPDATA"] = local_app_data
        return subprocess.run(
            [PWSH, "-NoProfile", "-File", str(PS1), *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(PS1.parents[1]),
        )

    def calls() -> list[str]:
        if not log.exists():
            return []
        return log.read_text(encoding="utf-8").splitlines()

    def targets() -> list[str]:
        out = []
        for line in calls():
            parts = line.split()
            if line.startswith("tool install") and "--editable" in parts:
                out.append(parts[parts.index("--editable") + 1])
        return out

    return {"run": run, "calls": calls, "targets": targets, "tmp": tmp_path}


needs_pwsh = pytest.mark.skipif(
    PWSH is None or os.name == "nt",
    reason="no pwsh on PATH, or a Windows host: the sandbox here models a POSIX pwsh"
)


@needs_pwsh
def test_the_ps1_reaches_the_decision_with_LOCALAPPDATA_unset(ps1_sandbox):
    """The defect: this used to die at line 66 before deciding anything.

    The interesting part is *which* failure comes back. Exit 1 is right, but
    it has to be the p2p stop with the guide -- not a null-path throw that
    never looked at p2p.
    """
    proc = ps1_sandbox["run"]("-RequireP2p", pkg=None)
    out = _out(proc)
    assert proc.returncode == 1, out
    assert "Cannot bind argument to parameter 'Path'" not in out, out
    assert "libsrtp2" in out, out
    assert "멈춥니다" in out, out
    # The decision came before any uv call, as in the .sh counterpart.
    assert ps1_sandbox["calls"]() == [], ps1_sandbox["calls"]()


@needs_pwsh
def test_the_ps1_no_p2p_branch_installs_without_the_extra(ps1_sandbox):
    proc = ps1_sandbox["run"]("-NoP2p", pkg=None)
    out = _out(proc)
    assert proc.returncode == 0, out
    # -NoP2p already answered the question, so neither is printed.
    assert "번호를 고르세요" not in out, out
    assert "고치려면" not in out, out
    assert ps1_sandbox["targets"]() == ["."], ps1_sandbox["calls"]()
    assert "빠른 직접 연결 기능: 포함되지 않음" in out, out


@needs_pwsh
def test_the_ps1_stops_without_a_terminal_when_no_flag_is_given(ps1_sandbox):
    proc = ps1_sandbox["run"](pkg=None, stdin="")
    out = _out(proc)
    assert proc.returncode == 1, out
    assert "물어볼 수 없습니다" in out, out
    # It names the flag that makes the non-interactive choice explicit.
    assert "-NoP2p" in out, out
    assert ps1_sandbox["calls"]() == [], ps1_sandbox["calls"]()


@needs_pwsh
def test_the_ps1_still_uses_LOCALAPPDATA_when_it_is_set(ps1_sandbox):
    """The fallback must not take over on Windows, where the variable is set."""
    local = ps1_sandbox["tmp"] / "localappdata"
    local.mkdir()
    proc = ps1_sandbox["run"]("-RequireP2p", pkg=None, local_app_data=str(local))
    assert proc.returncode == 1, _out(proc)
    # The tool environment defaults under it, and that is created before the
    # decision, so the directory existing is the default having been used.
    assert (local / "claunch-nogil" / "tools").is_dir(), list(local.rglob("*"))


@needs_pwsh
def test_the_ps1_reaches_the_decision_with_HOME_unset_too(ps1_sandbox):
    """The shim's default read $HOME, so it died the same way LOCALAPPDATA did.

    ``$ShimDir`` was ``Join-Path (Join-Path $HOME '.local') 'bin'`` and sat
    above the decision, so an empty ``$HOME`` stopped the script at that line
    with 'Cannot bind argument to parameter Path because it is an empty string'
    -- a spawned shell with no login state reaches it (found in review).
    """
    proc = ps1_sandbox["run"]("-RequireP2p", pkg=None, home=None)
    out = _out(proc)
    assert proc.returncode == 1, out
    assert "Cannot bind argument to parameter 'Path'" not in out, out
    assert "libsrtp2" in out, out
    assert "멈춥니다" in out, out
    assert ps1_sandbox["calls"]() == [], ps1_sandbox["calls"]()


@needs_pwsh
def test_the_ps1_defaults_land_under_the_sandbox_data_home(ps1_sandbox, tmp_path):
    """The script's own defaults must land inside the sandbox, not on the host.

    ``GetFolderPath('LocalApplicationData')`` prefers ``XDG_DATA_HOME`` over
    ``HOME``, so a sandbox that redirected only ``HOME`` still wrote
    ``$XDG_DATA_HOME/claunch-nogil/{bin,tools}`` -- outside ``tmp_path``, on a
    real directory, while every case passed (measured, found in review).

    The assertion is positive and on the sandbox: the fixture points
    ``XDG_DATA_HOME`` at an existing ``tmp_path/xdg-data``, so the tool
    environment the script creates has to appear there. Drop the redirect and
    the run resolves to the host's data home instead, and this fails wherever it
    runs -- unlike checking the host for a new directory, which could not tell a
    fresh leak from an install the machine already had (a real one from an
    earlier day sits at ~/.local/share/claunch-nogil here, which is what the
    first version of this case tripped over).
    """
    proc = ps1_sandbox["run"]("-RequireP2p", pkg=None)
    assert proc.returncode == 1, _out(proc)
    assert "멈춥니다" in _out(proc), _out(proc)
    assert (tmp_path / "xdg-data" / "claunch-nogil" / "tools").is_dir(), \
        sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "xdg-data").rglob("*"))
