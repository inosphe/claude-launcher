"""Where v2 definitions live (``docs/cflow-v2.md`` section 1).

Three layers, nearest first: the project's ``.claunch/workflows/v2/``, the
global ``<launcher home>/workflows/v2/``, and the package's
``workflows/v2/``. A name is a file stem; the nearest layer wins and the
files it hides are reported as ``shadows``.

Definitions are found by *name* only (doc section 1): :func:`find` refuses
a path, so ``include``/``call``, the definition API and a start by name
read nothing outside these three directories. The one path-based load is
starting a generated file (``.cflow2/generated/*.yaml``), whose containment
the daemon checks (``daemon/cflow2_web.choose``) before :func:`load_file`.

A long-lived process (the daemon) pins the package layer at its start
(:func:`pin_package`): its compiler is the code it imported then, and a
checkout that moves on underneath an editable install would otherwise hand
that compiler definitions written for newer code (claunch-qjgfr,
claunch-nzrdb). :func:`package_drift` says what the package directory on
disk has changed since.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from collections import OrderedDict
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

#: compiled charts by (path as given, its stat, :func:`layer_stamp`) -- an
#: ``include``/``call`` resolves by name from the layer files, so their stamp
#: covers everything a chart was built from (claunch-cb65c.1: a session start
#: compiled the same files three times on the daemon loop). The charts are
#: shared: a caller must not change one (``inputs.apply`` copies first).
_COMPILED: "OrderedDict[tuple, ir.Chart]" = OrderedDict()
_COMPILED_MAX = 256
_COMPILED_LOCK = threading.Lock()

#: the definition names each compiled file resolved (its includes and
#: calls, transitively) by path -- what :func:`drift_of` checks
_DEPS: Dict[str, frozenset] = {}

#: the package layer as :func:`pin_package` copied it: ``(dir, {file name:
#: sha256}, pinned at)``; ``None`` reads the package directory itself
_PINNED: Optional[Tuple[Path, Dict[str, str], float]] = None

#: :func:`_digests` of the live package directory, by its stat stamp
_LIVE: Optional[Tuple[tuple, Dict[str, str]]] = None


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


def live_package_dir() -> Path:
    """The package's ``workflows/v2`` on disk, pinned or not."""
    return Path(__file__).resolve().parent.parent / "workflows" / "v2"


def package_dir() -> Path:
    """The package layer this process reads: the pinned copy once
    :func:`pin_package` ran, the package directory before."""
    return _PINNED[0] if _PINNED is not None else live_package_dir()


def _digests(base: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        paths = sorted(base.glob("*.yaml"))
    except OSError:
        return out
    for path in paths:
        try:
            out[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def pin_package(dest: Union[str, Path]) -> Path:
    """Copy the package layer's ``*.yaml`` into ``dest`` (replacing what is
    there) and read the package layer from that copy for the rest of this
    process. Meant for the daemon's start, so the definitions it serves are
    the ones its compiler and runtime were written for."""
    global _PINNED
    dest = Path(dest)
    src = live_package_dir()
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for path in sorted(src.glob("*.yaml")):
        shutil.copy2(path, tmp / path.name)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    _PINNED = (dest, _digests(dest), time.time())
    return dest


def _live_digests() -> Dict[str, str]:
    global _LIVE
    base = live_package_dir()
    stamp = []
    try:
        for path in sorted(base.glob("*.yaml")):
            st = os.stat(path)
            stamp.append((path.name, st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    key = tuple(stamp)
    if _LIVE is None or _LIVE[0] != key:
        _LIVE = (key, _digests(base))
    return _LIVE[1]


def package_drift() -> Optional[dict]:
    """What the package directory on disk changed since :func:`pin_package`:
    ``{"pinned_at", "changed", "added", "removed"}`` (file names), ``None``
    when nothing is pinned."""
    if _PINNED is None:
        return None
    pinned, live = _PINNED[1], _live_digests()
    return {
        "pinned_at": _PINNED[2],
        "changed": sorted(n for n in pinned if n in live and live[n] != pinned[n]),
        "added": sorted(n for n in live if n not in pinned),
        "removed": sorted(n for n in pinned if n not in live),
    }


def drift_of(name: str, cwd: Optional[str] = None) -> List[str]:
    """The package files behind definition ``name`` -- its own file and every
    definition it includes or calls, as far as they come from the package
    layer -- whose copy on disk differs from the pinned one. Empty when
    nothing is pinned or nothing behind ``name`` moved."""
    drift = package_drift()
    if not drift:
        return []
    moved = set(drift["changed"]) | set(drift["removed"])
    if not moved:
        return []
    path = find(name, cwd)
    if path is None:
        return []
    if str(path) not in _DEPS:
        try:
            _compile(path, cwd)
        except Exception:  # noqa: BLE001 - a file that does not compile has its own error
            pass
    names = {name} | set(_DEPS.get(str(path), ()))
    out = []
    for dep in sorted(names):
        hit = find(dep, cwd)
        if hit is not None and hit.parent == package_dir() and hit.name in moved:
            out.append(hit.name)
    return out


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
        # The same directory reached twice is listed once, under the nearer
        # layer: e.g. a launcher home whose workflows/v2 is a symlink to the
        # package's, or a project layer pointing at the global one. (The
        # project layer of this checkout is .claunch/workflows/v2, which is
        # not the package directory src/claude_launcher/workflows/v2.)
        if resolved in seen:
            continue
        seen.add(resolved)
        for path in sorted(base.glob("*.yaml")):
            hits.setdefault(path.stem, []).append((layer, path))
    return hits


def is_name(name: str) -> bool:
    """Whether ``name`` is a bare definition name (no path separator, no
    ``.yaml`` suffix, not ``.``/``..``)."""
    name = str(name or "")
    return bool(name) and "/" not in name and "\\" not in name \
        and not name.endswith((".yaml", ".yml")) and name not in (".", "..") \
        and not name.startswith("~")


def find(name: str, cwd: Optional[str] = None) -> Optional[Path]:
    """The layer file a definition name resolves to; ``None`` for an
    unknown name and for anything that is not a bare name (a path)."""
    if not is_name(name):
        return None
    hits = _candidates(cwd).get(name)
    return hits[0][1] if hits else None


def resolver(cwd: Optional[str] = None):
    """A ``resolve_include`` for :mod:`frontend` bound to ``cwd``."""
    return lambda name: find(name, cwd)


def layer_stamp(cwd: Optional[str] = None) -> tuple:
    """``(path, mtime_ns, size)`` of every ``*.yaml`` in the layers visible
    from ``cwd``. A missing directory adds nothing, so two directories with
    no project layer share one stamp."""
    out = []
    for _layer, base in search_dirs(cwd):
        try:
            names = sorted(n for n in os.listdir(base) if n.endswith(".yaml"))
        except OSError:
            continue
        for name in names:
            path = os.path.join(str(base), name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append((path, st.st_mtime_ns, st.st_size))
    return tuple(out)


def _recording(cwd: Optional[str], seen: set):
    base = resolver(cwd)

    def resolve(name: str):
        seen.add(name)
        return base(name)
    return resolve


def _compile(path: Path, cwd: Optional[str]) -> ir.Chart:
    from . import frontend

    seen: set = set()
    try:
        st = os.stat(path)
    except OSError:
        chart = frontend.compile_file(path, resolve_include=_recording(cwd, seen))
        _DEPS[str(path)] = frozenset(seen)
        return chart
    key = (str(path), st.st_mtime_ns, st.st_size, layer_stamp(cwd))
    with _COMPILED_LOCK:
        hit = _COMPILED.get(key)
        if hit is not None:
            _COMPILED.move_to_end(key)
            return hit
    chart = frontend.compile_file(path, resolve_include=_recording(cwd, seen))
    with _COMPILED_LOCK:
        _DEPS[str(path)] = frozenset(seen)
        _COMPILED[key] = chart
        while len(_COMPILED) > _COMPILED_MAX:
            _COMPILED.popitem(last=False)
    return chart


def list_definitions(cwd: Optional[str] = None) -> List[Located2]:
    """Every v2 definition visible from ``cwd``; a file that does not
    compile is listed with ``error`` set rather than dropped."""
    out: List[Located2] = []
    for name, hits in sorted(_candidates(cwd).items()):
        layer, path = hits[0]
        item = Located2(name, path, layer, tuple(p for _, p in hits[1:]))
        try:
            chart = _compile(path, cwd)
        except Exception as exc:  # noqa: BLE001 - a listing must survive any file
            item.error = str(exc) or type(exc).__name__
        else:
            item.kind = chart.kind
            item.description = chart.description
        out.append(item)
    return out


def load(name: str, cwd: Optional[str] = None) -> ir.Chart:
    """Compile the definition ``name`` from the layers (a path is refused)."""
    from . import frontend

    if not is_name(str(name)):
        raise frontend.FrontendError(
            f"{name!r} is a path; v2 definitions are loaded by name from the layers "
            f"(a generated file is started through its .cflow2/generated path)"
        )
    path = find(str(name), cwd)
    if path is None:
        names = ", ".join(sorted(_candidates(cwd))) or "(none)"
        raise frontend.FrontendError(
            f"no v2 definition named {name!r} (available: {names}; searched "
            f"{', '.join(str(d) for _, d in search_dirs(cwd))})"
        )
    return _compile(path, cwd)


def load_file(path: Union[str, Path], cwd: Optional[str] = None) -> ir.Chart:
    """Compile a file the caller already located: a layer file from
    :func:`list_definitions`, or a generated file whose containment the
    caller checked. Its includes and calls resolve by name in ``cwd``'s
    layers."""
    return _compile(Path(path), cwd)
