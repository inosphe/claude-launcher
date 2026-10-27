#!/usr/bin/env bash
# Install claunch as `claunch-nogil`: an editable uv tool on free-threaded
# CPython 3.14t, building zvec from source first (claunch-d18v8). The regular
# `claunch` tool environment is not touched.
#
# Why a script: PyPI has no cp314t wheel and no sdist for zvec 0.7/0.8, and
# pyproject.toml caps zvec below 0.8, so `uv tool install -e ".[p2p]" --python
# 3.14t` cannot resolve on its own. This script builds a zvec wheel from the
# upstream git repository once, caches it, and installs the project with
# `--overrides` pointing zvec at that wheel. pyproject.toml stays unchanged.
#
# Usage (from any directory):
#   tools/install_freethreaded.sh [--rebuild] [--no-p2p | --require-p2p]
#
# One feature is optional: the fast direct link between the browser page and
# the daemon. Without it the page still works and falls back to the relay
# server, which is slower. Building that link needs two system libraries that
# are often missing, so the script checks for them first (about a second) and
# tells you what to install, instead of finding out after the 9-minute build
# and quietly installing without it. The technical name for the feature is
# "p2p" (the `p2p` extra of this package, an extra set of dependencies).
#   --no-p2p       install without that feature, without asking
#   --require-p2p  stop with exit code 1 when the feature cannot be built
#   (neither)      ask on a terminal; with no terminal (a pipeline, CI, a
#                  spawned shell) stop instead of continuing without it
# The script installs without the feature only when --no-p2p or your answer
# said so, and the closing summary states whether it is in.
#
# Environment:
#   CLAUNCH_FT_ZVEC_REF    zvec git ref to build (default: the commit measured in README)
#   CLAUNCH_FT_ZVEC_REPO   zvec git URL (default: https://github.com/alibaba/zvec)
#   CLAUNCH_FT_WHEELS      wheel cache directory (default: ~/.cache/claunch/zvec-wheels)
#   CLAUNCH_FT_ZVEC_WHEEL  path to an existing cp314t zvec wheel; skips the build
#   CLAUNCH_FT_ROOT        tool environment and bin (default: ~/.local/share/claunch-nogil)
#   CLAUNCH_FT_SHIM_DIR    where `claunch-nogil` is written (default: ~/.local/bin)
#
# The tool environment lives in its own UV_TOOL_DIR/UV_TOOL_BIN_DIR under
# CLAUNCH_FT_ROOT, so `uv tool install` cannot replace the `claude-launcher`
# tool of the regular `claunch`. `claunch-nogil` sets PYTHON_GIL=0 and runs the
# `claunch` of that environment. Both commands share ~/.claude-launcher and port
# 8378: one daemon at a time. To go back: `claunch daemon restart`.
#
# `--reinstall` rebuilds the tool environment. `--force` is only needed when a
# `claunch` executable that uv did not create sits in the tool bin directory;
# the script does not pass it.
#
# The script never starts, stops or restarts the daemon.

set -euo pipefail

ZVEC_REPO="${CLAUNCH_FT_ZVEC_REPO:-https://github.com/alibaba/zvec}"
ZVEC_REF="${CLAUNCH_FT_ZVEC_REF:-8d4996924bce78f8b072a353803c305101bcc807}"
CACHE_ROOT="${CLAUNCH_FT_WHEELS:-${XDG_CACHE_HOME:-$HOME/.cache}/claunch/zvec-wheels}"
PYTHON_SPEC="3.14t"
FT_ROOT="${CLAUNCH_FT_ROOT:-$HOME/.local/share/claunch-nogil}"
SHIM_DIR="${CLAUNCH_FT_SHIM_DIR:-$HOME/.local/bin}"
export UV_TOOL_DIR="$FT_ROOT/tools" UV_TOOL_BIN_DIR="$FT_ROOT/bin"

rebuild=0
p2p_choice=""
saw_no_p2p=0
saw_require_p2p=0
for arg in "$@"; do
    case "$arg" in
        --rebuild) rebuild=1 ;;
        --no-p2p) saw_no_p2p=1 ;;
        --require-p2p) saw_require_p2p=1 ;;
        -h|--help) sed -n '2,/^$/p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done
if [ "$saw_no_p2p" = 1 ] && [ "$saw_require_p2p" = 1 ]; then
    echo "--no-p2p and --require-p2p contradict each other; pass one" >&2
    exit 2
fi
if [ "$saw_no_p2p" = 1 ]; then p2p_choice="no"; fi
if [ "$saw_require_p2p" = 1 ]; then p2p_choice="require"; fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
[ -f pyproject.toml ] || { echo "pyproject.toml not found in $repo_root" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)" >&2; exit 1; }; }
need uv "https://docs.astral.sh/uv/"
need git "needed to fetch zvec and its submodules"

# One optional feature needs two system libraries: the fast direct link that
# lets the browser page talk to the daemon without going through the relay
# server. Without it everything still works; the page takes the relay path,
# which is slower. The libraries are often already installed and sometimes
# are not, so the check happens here — before the nine-minute zvec build —
# and a missing library asks you what to do instead of quietly installing
# without the feature (claunch-fsn9f).
p2p_missing() {
    if ! command -v pkg-config >/dev/null 2>&1; then
        echo "이 컴퓨터에 필요한 패키지 정보를 찾는 도구(pkg-config)가 없어서, 브라우저와 데몬을 직접 연결하는 빠른 통신 기능을 켤 수 없습니다."
        return 0
    fi
    if ! pkg-config --exists libsrtp2 2>/dev/null; then
        echo "이 컴퓨터에 libsrtp2(빠른 통신 기능이 쓰는 암호화 라이브러리)가 없어서, 브라우저와 데몬을 직접 연결하는 기능을 켤 수 없습니다."
        return 0
    fi
    return 1
}

extras=".[p2p]"
p2p_installed=1
p2p_reason=""
if [ "$p2p_choice" != "no" ]; then
    p2p_reason="$(p2p_missing || true)"
fi
if [ -n "$p2p_reason" ]; then
    cat >&2 <<MSG
$p2p_reason

무엇이 달라지나요:
  이 설치로 claunch는 그대로 동작합니다. 다만 웹 화면을 열어 둘 때, 브라우저와
  데몬이 서버를 거치지 않고 직접 연결되는 빠른 경로는 쓰지 못하고 중계 서버를
  거치는 느린 경로로 동작합니다. (이 기능의 이름은 p2p입니다.)

고치려면 아래 두 줄을 그대로 실행한 뒤 이 스크립트를 다시 실행하세요:
    sudo apt install -y libsrtp2-dev pkg-config
    tools/install_freethreaded.sh --rebuild

지금 어떻게 할까요?
  1) 이 기능 없이 설치를 마친다  (나중에 위 두 줄로 추가할 수 있습니다)
  2) 여기서 멈춘다  (위 두 줄을 실행한 뒤 다시 실행하세요)
MSG
    if [ "$p2p_choice" = "require" ]; then
        echo "멈춥니다 — --require-p2p를 주셨습니다." >&2
        exit 1
    fi
    if [ ! -t 0 ]; then
        echo "멈춥니다: 여기서는 물어볼 수 없습니다(터미널이 아닌 곳에서 실행 중). 이 기능 없이 설치하려면 --no-p2p를 붙여 다시 실행하세요." >&2
        exit 1
    fi
    while :; do
        printf '번호를 고르세요 [1/2] (그냥 Enter: 2, 멈춤): ' >&2
        answer=""
        IFS= read -r answer || true
        case "$answer" in
            1) extras="."; p2p_installed=0; break ;;
            2|"") echo "멈춥니다." >&2; exit 1 ;;
            *) echo "1 또는 2를 입력하세요." >&2 ;;
        esac
    done
elif [ "$p2p_choice" = "no" ]; then
    extras="."
    p2p_installed=0
fi

mkdir -p "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" "$SHIM_DIR"
uv python install "$PYTHON_SPEC"

wheel="${CLAUNCH_FT_ZVEC_WHEEL:-}"
if [ -z "$wheel" ]; then
    # zvec builds with scikit-build-core; cmake and ninja come from the build
    # requirements. Only a C++ compiler has to exist on the machine.
    command -v c++ >/dev/null 2>&1 || command -v g++ >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1 \
        || { echo "missing: C++ compiler (apt install build-essential)" >&2; exit 1; }
    outdir="$CACHE_ROOT/${ZVEC_REF:0:12}"
    if [ "$rebuild" = 1 ]; then rm -rf "$outdir"; fi
    wheel="$(ls "$outdir"/zvec-*-cp314t-*.whl 2>/dev/null | head -n 1 || true)"
    if [ -z "$wheel" ]; then
        mkdir -p "$outdir"
        src="$(mktemp -d "${TMPDIR:-/tmp}/zvec-src.XXXXXX")"
        trap 'rm -rf "$src"' EXIT
        echo "building zvec @ $ZVEC_REF for $PYTHON_SPEC (about 9 minutes on 4+ cores)"
        git clone --quiet --recursive "$ZVEC_REPO" "$src/zvec"
        git -C "$src/zvec" checkout --quiet "$ZVEC_REF"
        git -C "$src/zvec" submodule update --quiet --init --recursive
        uv build --wheel --python "$PYTHON_SPEC" --out-dir "$outdir" "$src/zvec"
        wheel="$(ls "$outdir"/zvec-*-cp314t-*.whl | head -n 1)"
    fi
fi
[ -f "$wheel" ] || { echo "zvec wheel not found: $wheel" >&2; exit 1; }
wheel="$(cd "$(dirname "$wheel")" && pwd)/$(basename "$wheel")"
echo "zvec wheel: $wheel"

overrides="$(mktemp "${TMPDIR:-/tmp}/claunch-ft-overrides.XXXXXX")"
trap 'rm -f "$overrides"' EXIT
printf 'zvec @ file://%s\n' "$wheel" > "$overrides"

install() {
    uv tool install --python "$PYTHON_SPEC" --editable "$1" --reinstall --overrides "$overrides"
}
# No fallback here. p2p was already decided above, so a failure now means this
# machine differs from what pkg-config reported; downgrading the install on the
# way out would hide that (claunch-fsn9f). Retry with --no-p2p to choose it.
install "$extras"

mkdir -p "$SHIM_DIR"
shim="$SHIM_DIR/claunch-nogil"
printf '#!/usr/bin/env bash\nPYTHON_GIL=0 exec "%s/claunch" "$@"\n' "$UV_TOOL_BIN_DIR" > "$shim"
chmod +x "$shim"
echo "wrote $shim"

tool_python="$(uv tool dir)/claude-launcher/bin/python"
PYTHON_GIL=0 "$tool_python" - <<'PY'
import sys, sysconfig
import zvec
print("python:", sys.version.split()[0], "| Py_GIL_DISABLED =", sysconfig.get_config_var("Py_GIL_DISABLED"))
print("zvec:", getattr(zvec, "__version__", "?"), "| GIL enabled with PYTHON_GIL=0:", sys._is_gil_enabled())
PY

if [ "$p2p_installed" = 1 ]; then
    p2p_line="빠른 직접 연결 기능: 포함됨 (브라우저와 데몬이 중계 서버를 거치지 않고 연결될 수 있습니다)"
else
    p2p_line="빠른 직접 연결 기능: 포함되지 않음. 이 설치는 중계 서버를 거치는 느린 경로로 동작합니다.
  이 기능을 추가하려면: sudo apt install -y libsrtp2-dev pkg-config && tools/install_freethreaded.sh --rebuild"
fi

cat <<MSG

installed as claunch-nogil ($shim). The regular claunch is unchanged and the
daemon is not restarted by this script. To run the daemon on 3.14t, restart it
yourself with:

    claunch-nogil daemon restart

$p2p_line

Only one daemon can use port 8378. Going back: claunch daemon restart
MSG
