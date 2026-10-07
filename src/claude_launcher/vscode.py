"""VS Code in the browser: one code-server per workspace and per worktree.

The daemon runs `code-server <https://github.com/coder/code-server>`_ next to
the sessions and puts it behind its own web server at ``/vscode/<key>/``
(:mod:`claude_launcher.daemon.vscode`). This module is the part that needs no
running daemon: the ``vscode:`` settings block, which directory a key opens,
where the binary and the per-server state live, and installing the pinned
release. The decisions behind the shape are recorded on claunch-9omyu.

A *target* is one directory a server opens:

- a registered workspace root -- key ``<workspace>``;
- a linked worktree under it (``<root>/.claude/worktrees/<name>``) -- key
  ``<workspace>~<name>``. A worktree gets a server of its own, not a folder
  in its workspace's: two agents in two worktrees are two editors;
- any other directory a session already stands in -- key ``adhoc-<hash>``.
  Those are reached from the session, never from a typed path, so the set of
  directories a server can open stays the registry plus the sessions the
  user created.

POSIX only in v1: code-server does not support native Windows, and the
daemon talks to it over a unix socket.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import config, store, workspaces
from . import worktree as worktree_mod

#: The code-server release claunch installs. Bumping it means replacing the
#: digests below with the ones GitHub publishes for that release's assets
#: (``digest`` in ``/repos/coder/code-server/releases``).
VERSION = "4.140.0"

#: sha256 of each release tarball, keyed by (system, arch) as code-server
#: names them. A download whose digest differs is refused, not installed.
ASSETS: Dict[Tuple[str, str], str] = {
    ("linux", "amd64"): "864c5d01c808ade57e4d12c708717be7a187219fded60428f263b9e2da9f6b48",
    ("linux", "arm64"): "ae4b07153f2037b06d24749bc8004221fcbf3ffe317038401be0452f541bf200",
    ("macos", "amd64"): "a5393b6eed4aa68b084e724c3c565f805abd996c609356043119f0323e40cf52",
    ("macos", "arm64"): "82c7144406ac31c373acfa786b6705c7c5463d895f728fde2cb94402b945b301",
}

#: Defaults for the top-level ``vscode`` block of ``~/.claunch.yaml``. Read
#: live: the daemon looks them up on every request and every idle sweep.
DEFAULTS = {
    # Whether the daemon serves /vscode/ at all.
    "enabled": True,
    # Seconds a server may go without an open connection before it is
    # stopped. The next open starts it again.
    "idle_timeout": 1800.0,
    # code-server's own port forwarding (/proxy/<port>/, /absproxy/<port>/).
    # Off: it would put every loopback port of this machine one URL away
    # through the daemon and the relay. Applies to servers started after the
    # change.
    "port_proxy": False,
    # A code-server executable to run instead of the one claunch installs.
    "command": None,
}

#: What a key may look like: the workspace-name alphabet plus the ``~`` that
#: separates a worktree from its workspace. Safe in a URL path segment.
KEY_RE = re.compile(r"^[A-Za-z0-9._-]+(~[A-Za-z0-9._-]+)?$")

#: Longest unix socket path the platforms accept, with room to spare
#: (``sun_path`` is 108 bytes on Linux and 104 on macOS).
_SOCKET_PATH_MAX = 100


class VSCodeError(Exception):
    """A setting, target or install request that cannot be honoured."""


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def settings(doc: Optional[dict] = None) -> dict:
    """The effective ``vscode`` settings, defaults merged under the file's."""
    doc = store.load() if doc is None else doc
    block = doc.get("vscode")
    merged = dict(DEFAULTS)
    if isinstance(block, dict):
        merged.update({k: v for k, v in block.items() if k in DEFAULTS})
    return merged


def _coerce(key: str, value):
    if key in ("enabled", "port_proxy"):
        if not isinstance(value, bool):
            raise VSCodeError(f"vscode.{key} must be true or false")
        return value
    if key == "idle_timeout":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise VSCodeError("vscode.idle_timeout must be a number of seconds")
        if value < 60:
            raise VSCodeError("vscode.idle_timeout must be at least 60 seconds")
        return float(value)
    if key == "command":
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise VSCodeError("vscode.command must be a path or empty")
        return value.strip()
    raise VSCodeError(f"unknown setting vscode.{key}")


def update_settings(changes: dict) -> dict:
    """Validate and write ``changes`` into the ``vscode`` block.

    Every key is checked before anything is written, so a request with one bad
    value changes nothing. Returns the effective settings afterwards.
    """
    if not isinstance(changes, dict):
        raise VSCodeError("expected an object of settings")
    clean = {key: _coerce(key, value) for key, value in changes.items()}

    def _mutate(doc: dict) -> None:
        block = doc.get("vscode")
        if not isinstance(block, dict):
            block = {}
        block.update(clean)
        doc["vscode"] = block

    return settings(store.update(_mutate))


# --------------------------------------------------------------------------- #
# platform and paths
# --------------------------------------------------------------------------- #
def unsupported_reason() -> Optional[str]:
    """Why this machine cannot run the feature, or ``None`` when it can."""
    if sys.platform == "win32":
        return "VS Code servers are not supported on a Windows daemon (code-server needs Linux, macOS or WSL)"
    return None


def asset_key() -> Optional[Tuple[str, str]]:
    """This machine's (system, arch) in code-server's release names."""
    system = {"linux": "linux", "darwin": "macos"}.get(sys.platform)
    arch = {
        "x86_64": "amd64", "amd64": "amd64",
        "aarch64": "arm64", "arm64": "arm64",
    }.get(platform.machine().lower())
    if system is None or arch is None:
        return None
    return system, arch


def root_dir() -> Path:
    """Everything the feature keeps: installs, shared settings, server state."""
    return config.launcher_home() / "vscode"


def install_dir() -> Path:
    return root_dir() / "code-server" / VERSION


def installed_binary() -> Optional[Path]:
    path = install_dir() / "bin" / "code-server"
    return path if path.is_file() else None


def binary(cfg: Optional[dict] = None) -> Optional[str]:
    """The code-server to run: the configured command, else the install."""
    cfg = settings() if cfg is None else cfg
    command = cfg.get("command")
    if command:
        found = shutil.which(command) or (command if Path(command).is_file() else None)
        return found
    path = installed_binary()
    return str(path) if path else None


def shared_dir() -> Path:
    """Extensions and User settings every server shares."""
    return root_dir() / "shared"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def server_dir(key: str) -> Path:
    """One server's own user-data-dir (state, unsaved-buffer backups, log).

    Named by a digest of the key so the path stays short: code-server puts an
    IPC socket inside it, and a socket path has a hard length limit.
    """
    return root_dir() / "servers" / _digest(key)


def socket_path(key: str) -> Path:
    """The unix socket a server listens on (mode 0600, owner only).

    Under the launcher home when the path fits the socket length limit, else
    under the system temp directory in a per-user, per-launcher-home
    directory, so two daemons on one machine never share a name.
    """
    name = f"{_digest(key)}.sock"
    preferred = root_dir() / "run"
    if len(str(preferred / name)) <= _SOCKET_PATH_MAX:
        return preferred / name
    uid = os.getuid() if hasattr(os, "getuid") else 0
    home = _digest(str(config.launcher_home()))
    return Path(tempfile.gettempdir()) / f"claunch-vscode-{uid}-{home}" / name


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Target:
    key: str
    folder: str
    kind: str  # "workspace" | "worktree" | "adhoc"
    workspace: str = ""
    worktree: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "folder": self.folder,
            "kind": self.kind,
            "workspace": self.workspace,
            "worktree": self.worktree,
        }


def _worktrees(ws: workspaces.Workspace) -> List[Path]:
    base = worktree_mod.worktrees_dir(Path(ws.path))
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    # A linked worktree's checkout carries a ``.git`` *file*; anything else in
    # that directory is not a checkout this feature should open.
    return [p for p in entries if (p / ".git").is_file()]


def workspace_target(name: str, worktree: Optional[str] = None,
                     doc: Optional[dict] = None) -> Target:
    """The target for a registered workspace, or one of its worktrees."""
    ws = workspaces.get(name, doc)
    if ws is None:
        raise VSCodeError(f"no workspace named {name!r}")
    if not worktree:
        return Target(key=ws.name, folder=ws.path, kind="workspace", workspace=ws.name)
    for path in _worktrees(ws):
        if path.name == worktree:
            return Target(
                key=f"{ws.name}~{path.name}", folder=str(path), kind="worktree",
                workspace=ws.name, worktree=path.name,
            )
    raise VSCodeError(f"workspace {name!r} has no worktree named {worktree!r}")


def path_target(path: str, doc: Optional[dict] = None) -> Target:
    """The target a directory belongs to.

    Inside a registered workspace: its worktree when the path is in one, the
    workspace root otherwise. Anywhere else: an ad-hoc target for the
    directory itself. Callers decide whether an ad-hoc target is acceptable;
    the API takes one only from an existing session's directory.
    """
    try:
        resolved = workspaces.normalize_path(path)
    except workspaces.WorkspaceError as exc:
        raise VSCodeError(str(exc)) from exc
    ws = workspaces.owning(resolved, doc)
    if ws is not None:
        here = Path(resolved)
        for candidate in _worktrees(ws):
            try:
                here.relative_to(candidate)
            except ValueError:
                continue
            return workspace_target(ws.name, candidate.name, doc)
        return workspace_target(ws.name, None, doc)
    return Target(key=f"adhoc-{_digest(resolved)}", folder=resolved, kind="adhoc")


def key_target(key: str, doc: Optional[dict] = None) -> Optional[Target]:
    """The registered target a key names, or ``None`` (ad-hoc keys included:
    those are only known to the daemon that created them)."""
    if not KEY_RE.match(key or "") or key.startswith("adhoc-"):
        return None
    name, _, wt = key.partition("~")
    try:
        return workspace_target(name, wt or None, doc)
    except VSCodeError:
        return None


def list_targets(doc: Optional[dict] = None) -> List[Target]:
    """Every registered root and each of its worktrees, in picker order."""
    out: List[Target] = []
    for ws in workspaces.list_all(doc):
        out.append(Target(key=ws.name, folder=ws.path, kind="workspace", workspace=ws.name))
        for path in _worktrees(ws):
            out.append(Target(
                key=f"{ws.name}~{path.name}", folder=str(path), kind="worktree",
                workspace=ws.name, worktree=path.name,
            ))
    return out


# --------------------------------------------------------------------------- #
# installing
# --------------------------------------------------------------------------- #
def release_url(key: Tuple[str, str]) -> str:
    system, arch = key
    name = f"code-server-{VERSION}-{system}-{arch}.tar.gz"
    return f"https://github.com/coder/code-server/releases/download/v{VERSION}/{name}"


def _safe_extract(archive: Path, dest: Path) -> None:
    """Unpack ``archive`` into ``dest``, refusing anything that would land
    outside it (absolute names, ``..``, links pointing out)."""
    root = dest.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            target = (root / member.name).resolve()
            if target != root and root not in target.parents:
                raise VSCodeError(f"refusing archive entry outside the install: {member.name}")
            if member.issym() or member.islnk():
                link = Path(member.linkname)
                base = target.parent if member.issym() else root
                pointed = (base / link).resolve()
                if link.is_absolute() or (pointed != root and root not in pointed.parents):
                    raise VSCodeError(f"refusing link out of the install: {member.name}")
            if member.isdev():
                raise VSCodeError(f"refusing device entry: {member.name}")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(root, members=members, filter="fully_trusted")
        else:  # pragma: no cover -- Python without extraction filters
            tar.extractall(root, members=members)


def install(progress: Optional[Callable[[int, int], None]] = None,
            fetch: Optional[Callable[[str, Path, Callable[[int, int], None]], None]] = None) -> Path:
    """Download, verify and unpack the pinned release; returns the binary.

    Blocking (a ~220 MB download): the daemon runs it in a thread. The
    archive is checked against :data:`ASSETS` before a single file is
    unpacked, and the release is moved into place only once it is complete,
    so an interrupted install leaves nothing that looks installed.
    ``fetch`` replaces the download (tests).
    """
    reason = unsupported_reason()
    if reason:
        raise VSCodeError(reason)
    key = asset_key()
    if key is None or key not in ASSETS:
        raise VSCodeError(f"no code-server release for {sys.platform}/{platform.machine()}")
    existing = installed_binary()
    if existing is not None:
        return existing
    report = progress or (lambda done, total: None)
    work = root_dir() / "downloads"
    work.mkdir(parents=True, exist_ok=True)
    archive = work / f"code-server-{VERSION}.tar.gz.part"
    (fetch or _download)(release_url(key), archive, report)
    digest = hashlib.sha256()
    with archive.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != ASSETS[key]:
        archive.unlink(missing_ok=True)
        raise VSCodeError("the downloaded code-server does not match its published sha256")
    staging = Path(tempfile.mkdtemp(prefix="unpack-", dir=work))
    try:
        _safe_extract(archive, staging)
        unpacked = [p for p in staging.iterdir() if p.is_dir()]
        if len(unpacked) != 1 or not (unpacked[0] / "bin" / "code-server").is_file():
            raise VSCodeError("the code-server archive does not have the expected layout")
        final = install_dir()
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            shutil.rmtree(final)
        unpacked[0].rename(final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)
    found = installed_binary()
    if found is None:  # pragma: no cover -- guarded by the layout check above
        raise VSCodeError("code-server was unpacked but its binary is missing")
    return found


def _download(url: str, dest: Path, progress: Callable[[int, int], None]) -> None:
    with urllib.request.urlopen(url, timeout=60) as resp, dest.open("wb") as out:  # noqa: S310 -- fixed https URL
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            progress(done, total)
