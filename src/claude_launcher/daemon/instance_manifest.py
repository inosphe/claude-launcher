"""Per-instance manifests: what a named daemon instance shares and what it owns.

A named instance (``claunch -L NAME`` / ``CLAUNCH_DAEMON=NAME``) always owns its
runtime state directory (:func:`paths.daemon_dir`), but by default it shares
everything else with the default daemon: the launcher home (profiles, global
workflows, the sync merge base) and the config file ``~/.claunch.yaml``.
Isolating those used to mean exporting ``CLAUDE_LAUNCHER_HOME`` and
``CLAUDE_LAUNCHER_SYNC_FILE`` by hand in every shell that talks to the
instance — miss one and the command silently reads the shared set.

The manifest writes that choice down once, next to the instance's state::

    <base home>/daemons/<name>/instance.yaml

    home: ~/.claude-launcher-team     # omitted = share the base home
    config: ~/.claunch-team.yaml      # omitted = share ~/.claunch.yaml
    port: 8390                        # omitted = ephemeral port

:func:`apply` turns it into the environment variables the rest of the launcher
already resolves every path through, so ``config.launcher_home()`` and
``config.sync_file()`` need no knowledge of instances. It runs at the two
entry points (the CLI after ``-L``, the daemon after ``--name``) and, because
spawned daemons and sessions inherit the environment, once per process tree.

The *base home* is the home in effect before any manifest applied — the one
the manifest is found in. The instance's state directory stays under it (see
:func:`paths.daemon_dir`), so ``daemon restart --all`` and ``daemon instance
ls`` still see every instance from a plain shell.

The manifest's values win over the variables already in the environment: the
base home itself is read from ``CLAUDE_LAUNCHER_HOME``, so an "environment
wins" rule would make the manifest dead for anyone who runs with a custom base
home. To change an instance's home, edit its manifest.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .. import atomic, config
from . import paths

MANIFEST_NAME = "instance.yaml"

#: Name of the instance whose manifest this process tree applied.
APPLIED_ENV = "CLAUNCH_INSTANCE_APPLIED"
#: The launcher home before the manifest replaced it (where manifests live).
BASE_ENV = paths.INSTANCE_BASE_ENV
#: JSON ``{var: previous value or null}`` so a switch to another instance
#: (``restart --all``, ``claunch -L other`` from inside an instance's session)
#: can put the environment back before applying the next manifest.
PREV_ENV = "CLAUNCH_INSTANCE_PREV"

#: Manifest key -> the environment variable it sets.
_KEY_ENV = {
    "home": config.LAUNCHER_HOME_ENV,
    "config": config.LAUNCHER_SYNC_ENV,
    "port": "CLAUNCH_DAEMON_PORT",
}

#: Every variable :func:`apply` may set -- what a caller that switches
#: instances in-process saves and restores.
ENV_VARS = (APPLIED_ENV, BASE_ENV, PREV_ENV, *_KEY_ENV.values())


class InstanceManifestError(Exception):
    """A manifest that cannot be read or would break isolation."""


@dataclass
class Manifest:
    name: str
    path: Path
    home: Optional[Path] = None
    config: Optional[Path] = None
    port: Optional[int] = None

    def env(self) -> Dict[str, str]:
        out = {}
        if self.home is not None:
            out[_KEY_ENV["home"]] = str(self.home)
        if self.config is not None:
            out[_KEY_ENV["config"]] = str(self.config)
        if self.port is not None:
            out[_KEY_ENV["port"]] = str(self.port)
        return out

    def to_doc(self) -> dict:
        doc = {}
        if self.home is not None:
            doc["home"] = str(self.home)
        if self.config is not None:
            doc["config"] = str(self.config)
        if self.port is not None:
            doc["port"] = self.port
        return doc


def manifest_path(name: str) -> Path:
    return paths.base_home() / "daemons" / paths.validate_instance(name) / MANIFEST_NAME


def _as_path(value, key: str, where: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise InstanceManifestError(f"{where}: '{key}' must be a non-empty path")
    p = Path(value.strip()).expanduser()
    if not p.is_absolute():
        raise InstanceManifestError(
            f"{where}: '{key}' must be an absolute path (or start with ~), got {value!r}"
        )
    return p


def _as_port(value, where: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise InstanceManifestError(f"{where}: 'port' must be an integer 1-65535, got {value!r}")
    return value


def parse(name: str, path: Path, doc) -> Manifest:
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise InstanceManifestError(f"{path}: expected a mapping")
    unknown = sorted(set(doc) - set(_KEY_ENV))
    if unknown:
        raise InstanceManifestError(
            f"{path}: unknown key(s) {', '.join(unknown)} (allowed: home, config, port)"
        )
    m = Manifest(name=name, path=path)
    if doc.get("home") is not None:
        m.home = _as_path(doc["home"], "home", path)
    if doc.get("config") is not None:
        m.config = _as_path(doc["config"], "config", path)
    if doc.get("port") is not None:
        m.port = _as_port(doc["port"], path)
    return m


def load(name: str) -> Optional[Manifest]:
    """The manifest of instance ``name``, or None when it has none."""
    if not name:
        return None
    path = manifest_path(name)
    if not path.is_file():
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InstanceManifestError(f"{path}: cannot read manifest: {exc}") from exc
    return parse(name, path, doc)


def all_manifests() -> List[Manifest]:
    out = []
    root = paths.base_home() / "daemons"
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not (d / MANIFEST_NAME).is_file():
            continue
        try:
            paths.validate_instance(d.name)
        except ValueError:
            continue
        m = load(d.name)
        if m is not None:
            out.append(m)
    return out


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def check_conflicts(m: Manifest) -> None:
    """Refuse a manifest whose home is another instance's home.

    Two instances on one home would share profiles and the sync merge base —
    exactly what declaring a home was meant to prevent — without either
    manifest saying so.
    """
    if m.home is None:
        return
    for other in all_manifests():
        if other.name == m.name or other.home is None:
            continue
        if _same(other.home, m.home):
            raise InstanceManifestError(
                f"instance {m.name!r}: home {m.home} is already the home of "
                f"instance {other.name!r} ({other.path})"
            )


def _undo() -> None:
    """Restore the variables a previously applied manifest replaced."""
    raw = os.environ.pop(PREV_ENV, None)
    os.environ.pop(APPLIED_ENV, None)
    os.environ.pop(BASE_ENV, None)
    if not raw:
        return
    try:
        prev = json.loads(raw)
    except ValueError:
        return
    for var, value in prev.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


def apply() -> Optional[Manifest]:
    """Apply the active instance's manifest to ``os.environ`` (idempotent).

    Returns the manifest applied, or None. A process tree that already applied
    this instance's manifest (a daemon started by the CLI, a session started by
    that daemon) is left as it is; one that applied another instance's first
    has it undone. Raises :class:`InstanceManifestError` on a bad manifest.
    """
    name = paths.instance()
    applied = os.environ.get(APPLIED_ENV)
    if applied is not None:
        if applied == name:
            return None
        _undo()
    m = load(name)
    if m is None:
        return None
    check_conflicts(m)
    base = str(config.launcher_home())
    prev = {}
    for var, value in m.env().items():
        prev[var] = os.environ.get(var)
        os.environ[var] = value
    os.environ[PREV_ENV] = json.dumps(prev)
    os.environ[BASE_ENV] = base
    os.environ[APPLIED_ENV] = name
    return m


def write(m: Manifest) -> None:
    m.path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(m.to_doc(), sort_keys=False, allow_unicode=True) if m.to_doc() else "{}\n"
    with atomic.scratch(m.path) as tmp:
        tmp.write_text(text, encoding="utf-8")
        atomic.replace(tmp, m.path)
