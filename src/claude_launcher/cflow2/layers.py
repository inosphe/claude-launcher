"""Where v2 definitions live (``docs/cflow-v2.md`` section 1).

Three layers, nearest first: the project's ``.claunch/workflows/v2/``, the
global ``<launcher home>/workflows/v2/``, and the package's
``workflows/v2/``. A name is a file stem; the nearest layer wins and the
files it hides are reported as ``shadows``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from .. import config
from . import ir

LAYER_PROJECT = "project"
LAYER_GLOBAL = "global"
LAYER_PACKAGE = "package"
LAYER_FILE = "file"

PROJECT_V2 = Path(".claunch") / "workflows" / "v2"


@dataclass
class Located2:
    name: str
    path: Path
    layer: str
    shadows: Tuple[Path, ...] = ()
    #: why the file could not be compiled (``None`` when it compiles)
    error: Optional[str] = None
    kind: Optional[str] = None
    description: str = ""


def package_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "workflows" / "v2"


def global_dir() -> Path:
    return config.launcher_home() / "workflows" / "v2"


def project_dir(cwd: Optional[str] = None) -> Path:
    return Path(cwd or os.getcwd()) / PROJECT_V2


def search_dirs(cwd: Optional[str] = None) -> List[Tuple[str, Path]]:
    """``(layer, dir)`` in priority order: project, global, package."""
    return [
        (LAYER_PROJECT, project_dir(cwd)),
        (LAYER_GLOBAL, global_dir()),
        (LAYER_PACKAGE, package_dir()),
    ]


def _candidates(cwd: Optional[str] = None) -> Dict[str, List[Tuple[str, Path]]]:
    hits: Dict[str, List[Tuple[str, Path]]] = {}
    seen = set()
    for layer, base in search_dirs(cwd):
        if not base.is_dir():
            continue
        try:
            resolved = base.resolve()
        except OSError:
            continue
        if resolved in seen:  # e.g. cwd is this checkout: project == package
            continue
        seen.add(resolved)
        for path in sorted(base.glob("*.yaml")):
            hits.setdefault(path.stem, []).append((layer, path))
    return hits


def find(name: str, cwd: Optional[str] = None) -> Optional[Path]:
    """The file a name (or an explicit ``.yaml`` path) resolves to."""
    if name.endswith((".yaml", ".yml")):
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = Path(cwd or os.getcwd()) / path
        return path if path.is_file() else None
    hits = _candidates(cwd).get(name)
    return hits[0][1] if hits else None


def resolver(cwd: Optional[str] = None):
    """A ``resolve_include`` for :mod:`frontend` bound to ``cwd``."""
    return lambda name: find(name, cwd)


def list_definitions(cwd: Optional[str] = None) -> List[Located2]:
    """Every v2 definition visible from ``cwd``; a file that does not
    compile is listed with ``error`` set rather than dropped."""
    from . import frontend

    out: List[Located2] = []
    for name, hits in sorted(_candidates(cwd).items()):
        layer, path = hits[0]
        item = Located2(name, path, layer, tuple(p for _, p in hits[1:]))
        try:
            chart = frontend.compile_file(path, resolve_include=resolver(cwd))
        except Exception as exc:  # noqa: BLE001 - a listing must survive any file
            item.error = str(exc) or type(exc).__name__
        else:
            item.kind = chart.kind
            item.description = chart.description
        out.append(item)
    return out


def load(name_or_path: Union[str, Path], cwd: Optional[str] = None) -> ir.Chart:
    from . import frontend

    path = find(str(name_or_path), cwd)
    if path is None:
        names = ", ".join(sorted(_candidates(cwd))) or "(none)"
        raise frontend.FrontendError(
            f"no v2 definition named {name_or_path!r} (available: {names}; searched "
            f"{', '.join(str(d) for _, d in search_dirs(cwd))})"
        )
    return frontend.compile_file(path, resolve_include=resolver(cwd))
