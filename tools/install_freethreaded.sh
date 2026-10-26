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
#   tools/install_freethreaded.sh [--rebuild] [--no-p2p]
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
mkdir -p "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" "$SHIM_DIR"

rebuild=0
extras=".[p2p]"
for arg in "$@"; do
    case "$arg" in
        --rebuild) rebuild=1 ;;
        --no-p2p) extras="." ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
[ -f pyproject.toml ] || { echo "pyproject.toml not found in $repo_root" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)" >&2; exit 1; }; }
need uv "https://docs.astral.sh/uv/"
need git "needed to fetch zvec and its submodules"

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
if install "$extras"; then
    :
elif [ "$extras" != "." ]; then
    # aiortc's pylibsrtp has no cp314t wheel; it compiles against libsrtp2 and
    # fails without the headers (apt install libsrtp2-dev). The daemon runs
    # unchanged without p2p, so fall back instead of aborting.
    echo "p2p extra failed to build; installing without it (apt install libsrtp2-dev, then rerun, to get it)" >&2
    install "."
fi

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

cat <<MSG

installed as claunch-nogil ($shim). The regular claunch is unchanged and the
daemon is not restarted by this script. To run the daemon on 3.14t, restart it
yourself with:

    claunch-nogil daemon restart

Only one daemon can use port 8378. Going back: claunch daemon restart
MSG
