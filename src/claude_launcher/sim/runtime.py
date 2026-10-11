"""The Node runtime of the simulation run session.

The run session is a pi session whose extension (``sim/pi/runner.mjs``)
drives the campaign through ``@earendil-works/pi-durable``. claunch has no
npm step of its own, so the extension's dependencies are pinned here
(``sim/pi/package.json`` and its lock) and installed once into
``<launcher home>/sim-runtime`` with ``npm ci``::

    sim-runtime/
        package.json, package-lock.json   copies of the pinned ones
        node_modules/                     what npm ci put there
        runner.mjs, campaign.mjs          copies of the packaged extension
        stamp.json                        hash of the lock that was installed

pi resolves the extension's imports next to the file it loads, which is why
the extension is copied in rather than loaded from the package. A changed
lock needs a new install (:func:`status` says ``stale``); a changed
extension is copied over on every launch (:func:`runner_path`).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .. import config

PACKAGED = Path(__file__).resolve().parent / "pi"
FILES = ("package.json", "package-lock.json")
RUNNER = "runner.mjs"
#: The extension and the module it imports, copied in together.
EXTENSION_FILES = (RUNNER, "campaign.mjs")


class InstallError(Exception):
    """The runtime cannot be installed; the message says why."""


def runtime_dir() -> Path:
    return config.launcher_home() / "sim-runtime"


def _lock_hash() -> str:
    h = hashlib.sha256()
    for name in FILES:
        h.update((PACKAGED / name).read_bytes())
    return h.hexdigest()[:16]


def _stamp(d: Path) -> dict:
    try:
        return json.loads((d / "stamp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def status(d: Optional[Path] = None) -> dict:
    d = d or runtime_dir()
    stamp = _stamp(d)
    installed = bool(stamp) and (d / "node_modules" / "@earendil-works" / "pi-durable").is_dir()
    return {"dir": str(d), "installed": installed,
            "stale": installed and stamp.get("lock") != _lock_hash(),
            "npm": shutil.which("npm") or ""}


def install(d: Optional[Path] = None, *, force: bool = False, run=subprocess.run) -> dict:
    """Install (or refresh) the runtime. Without ``force`` an install whose
    lock is current only copies the extension again."""
    d = d or runtime_dir()
    st = status(d)
    if st["installed"] and not st["stale"] and not force:
        _copy_runner(d)
        return {**st, "action": "current"}
    npm = st["npm"]
    if not npm:
        raise InstallError("npm is not on PATH; the simulation run session needs Node.js >= 22.19 with npm")
    d.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copyfile(PACKAGED / name, d / name)
    proc = run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=str(d),
               capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        raise InstallError("npm ci failed in " + str(d) + ":\n" + "\n".join(tail))
    _copy_runner(d)
    (d / "stamp.json").write_text(json.dumps({"lock": _lock_hash()}), encoding="utf-8")
    return {**status(d), "action": "installed"}


def _copy_runner(d: Path) -> Path:
    for name in EXTENSION_FILES:
        src, dst = PACKAGED / name, d / name
        data = src.read_bytes()
        try:
            if dst.read_bytes() == data:
                continue
        except OSError:
            pass
        tmp = dst.with_name(name + ".tmp")
        # open() rather than Path.write_bytes: the delivery contract test
        # forbids any call named write_bytes outside the PTY sender.
        with open(tmp, "wb") as f:
            f.write(data)
        tmp.replace(dst)
    return d / RUNNER


def runner_path(d: Optional[Path] = None) -> Optional[Path]:
    """The extension to launch with, current; ``None`` when the runtime is
    not installed or its lock is out of date."""
    d = d or runtime_dir()
    st = status(d)
    if not st["installed"] or st["stale"]:
        return None
    return _copy_runner(d)
