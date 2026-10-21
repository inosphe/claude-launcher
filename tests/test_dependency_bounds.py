"""A release that a fresh resolve may pick has to be installable on Linux.

``uv.lock`` is ignored (``.gitignore``), so every new worktree resolves
``pyproject.toml`` from scratch. zvec 0.8.0 publishes ``macosx_11_0_arm64``
wheels and no sdist, and an unpinned ``zvec>=0.7,<1`` let a fresh resolve pick
it, so ``uv sync --extra test`` exited 2 on Linux -- "Distribution
``zvec==0.8.0`` ... doesn't have a source distribution or wheel for the current
platform" -- and the delayed sweep could not build a venv (claunch-im642).
0.7.0 ships cp314 manylinux/musllinux/win wheels, so it is the version every
platform can install.

The bound below is the fix, and this test is what keeps it: it fails the
moment the upper bound admits 0.8.0 again, which is when somebody has to check
the newer release's wheels instead of trusting the range.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _dependency(name: str) -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for spec in project["project"]["dependencies"]:
        if re.split(r"[\s<>=!~;]", spec, maxsplit=1)[0] == name:
            return spec
    raise AssertionError(
        f"{name} is not a runtime dependency: {project['project']['dependencies']}"
    )


def _exclusive_upper_bound(spec: str) -> tuple[int, ...]:
    # '<=' is not an exclusive bound: '<=0.8.0' admits the release this guards.
    match = re.search(r"<(?!=)\s*(\d+(?:\.\d+)*)", spec)
    assert match, f"no exclusive upper bound in {spec!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def test_zvec_upper_bound_excludes_the_release_without_linux_wheels():
    spec = _dependency("zvec")
    bound = _exclusive_upper_bound(spec)
    bound += (0,) * max(0, 3 - len(bound))
    assert bound <= (0, 8, 0), (
        f"{spec!r} admits zvec 0.8.0, whose only wheels are macosx_11_0_arm64 "
        "and which has no sdist -- a fresh `uv sync` then cannot install it on "
        "Linux (claunch-im642)"
    )
