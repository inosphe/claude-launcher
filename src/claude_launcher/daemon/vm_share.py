"""Host folders shared into a VM (vm-setup-8s6, issue spec "s2-vmshare";
evidence runs/d-009): ``tart run --dir name:path[:ro]`` mounts a host folder in
the guest at ``/Volumes/My Shared Files/<name>`` (virtiofs).

The point is that a session edits files at their host path as it would any
other, and the VM reads them. What the VM builds, it builds from a local copy
(a shared folder is about ten times slower for many small files, and swiftc
reports a source "modified during the build" right after files are written).

A share is a record ``{name, host_path, mode}`` on the VM, set by the user only.
Rules, all applied again at every start (a record in ``vms.json`` is never
trusted on its own):

* **Allowed roots.** The daemon setting ``vm_share_roots`` (default empty: nothing
  can be shared). ``host_path`` is resolved with ``realpath`` and must lie in or
  under one root, so a symbolic link out of a root is refused.
* **Never shared**, whatever the roots say: a path that is, lies under or
  *contains* ``~/.ssh``, ``~/.gnupg``, ``~/.aws``, ``~/Library`` (Keychains
  included), ``~/.claude``, ``~/.claude-launcher`` or the daemon's state
  directory. Containing is what keeps the home folder itself out.
* **Mode** ``ro`` unless the user wrote ``rw``.
* A path with ':' (tart's separator) or a control character is refused.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .vmhub import VmError, check_name

log = logging.getLogger(__name__)

GUEST_BASE = "/Volumes/My Shared Files"
SHARE_MAX = 4
MODES = ("ro", "rw")
PATH_MAX = 1024

#: Folders under the user's home that hold credentials or the daemon's own
#: state; no share may be, lie under or contain one.
HOME_FORBIDDEN = (".ssh", ".gnupg", ".aws", "Library", ".claude", ".claude-launcher")


def guest_path(name: str) -> str:
    return f"{GUEST_BASE}/{name}"


def forbidden_paths(home: Optional[Path] = None, state: Optional[Path] = None) -> List[str]:
    """The resolved paths no share may touch."""
    home = Path(home) if home is not None else Path.home()
    out = [os.path.realpath(home / sub) for sub in HOME_FORBIDDEN]
    if state is not None:
        out.append(os.path.realpath(state))
    return out


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def clean_roots(raw, forbidden: Sequence[str] = ()) -> List[str]:
    """The ``vm_share_roots`` setting as resolved, existing folders. An entry that
    is not a string, is not an absolute path to a folder, or is (or contains) a
    forbidden path is left out and logged: a bad root never widens what can be
    shared."""
    out: List[str] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if not isinstance(item, str) or not item.startswith("/"):
            log.warning("vm_share_roots: %r is not an absolute path; ignored", item)
            continue
        real = os.path.realpath(item)
        if not os.path.isdir(real) or real == "/":
            log.warning("vm_share_roots: %r is not a folder; ignored", item)
            continue
        if any(_within(real, f) or _within(f, real) for f in forbidden):
            log.warning("vm_share_roots: %r holds or is a folder that is never shared; ignored", item)
            continue
        if real not in out:
            out.append(real)
    return out


def check_share(name, host_path, mode, roots: Sequence[str], forbidden: Sequence[str]) -> Dict[str, str]:
    """A share ready to store, or a refusal. ``roots`` are :func:`clean_roots`
    output."""
    if not isinstance(name, str):
        raise VmError("vm_bad_request", "공유 이름이 필요합니다")
    check_name("공유 이름", name)
    mode = "ro" if mode in (None, "") else mode
    if mode not in MODES:
        raise VmError("vm_bad_request", f"공유 모드 {str(mode)[:20]!r}: ro 또는 rw여야 합니다")
    if (not isinstance(host_path, str) or not host_path.startswith("/") or len(host_path) > PATH_MAX
            or ":" in host_path or not host_path.isprintable()):
        raise VmError("vm_bad_request",
                      f"호스트 경로 {str(host_path)[:80]!r}: ':' 와 제어 문자 없는 절대 경로여야 합니다")
    if not roots:
        raise VmError("vm_share_no_roots",
                      "공유할 수 있는 루트가 설정되어 있지 않습니다. 데몬 설정 daemon.vm_share_roots에 "
                      "공유를 허용할 폴더를 적으십시오(비어 있으면 공유가 꺼져 있습니다)")
    real = os.path.realpath(host_path)
    if not os.path.isdir(real):
        raise VmError("vm_bad_request", f"호스트 경로 {host_path!r}는 폴더가 아니거나 없습니다")
    if ":" in real:
        raise VmError("vm_bad_request", f"호스트 경로 {real!r}에 ':' 가 있어 공유할 수 없습니다")
    if not any(_within(real, root) for root in roots):
        raise VmError("vm_share_outside_root",
                      f"{real}은(는) 허용된 루트({', '.join(roots)}) 밖입니다. 심볼릭 링크는 실제 위치로 판단합니다")
    for banned in forbidden:
        if _within(real, banned) or _within(banned, real):
            raise VmError("vm_share_forbidden",
                          f"{real}은(는) 공유할 수 없습니다: 홈 폴더 자체이거나 {banned}를 포함하거나 그 아래입니다 "
                          "(.ssh, .gnupg, .aws, Library/Keychain, .claude, .claude-launcher, 데몬 상태 폴더)")
    return {"name": name, "host_path": real, "mode": mode}


def recheck(share: dict, roots: Sequence[str], forbidden: Sequence[str]) -> Dict[str, str]:
    """A stored share against the rules as they are now. The stored path is the
    resolved one: if it now resolves elsewhere (a link was put in its place) the
    share is refused."""
    checked = check_share(share.get("name"), share.get("host_path"), share.get("mode"), roots, forbidden)
    if checked["host_path"] != share.get("host_path"):
        raise VmError("vm_share_outside_root",
                      f"{share.get('host_path')}은(는) 이제 {checked['host_path']}를 가리킵니다")
    return checked


def tart_arg(share: dict) -> str:
    """The ``--dir`` value: ``name:path`` (read-write) or ``name:path:ro``."""
    return f"{share['name']}:{share['host_path']}" + (":ro" if share.get("mode") != "rw" else "")


def public(share: dict) -> dict:
    """The record as shown, with where it appears in the guest."""
    return {"name": share["name"], "host_path": share["host_path"], "mode": share["mode"],
            "guest_path": guest_path(share["name"])}


def load_shares(raw) -> List[dict]:
    """Shares read from ``vms.json``: structurally valid ones only (the rules
    that need the roots are applied at start). Invalid entries, duplicates and
    the excess are dropped."""
    out: List[dict] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            if not isinstance(item, dict):
                raise TypeError
            name = check_name("공유 이름", str(item["name"]))
            mode = item.get("mode")
            path = item["host_path"]
            if mode not in MODES or not isinstance(path, str) or not path.startswith("/") \
                    or ":" in path or not path.isprintable() or len(path) > PATH_MAX:
                raise ValueError
        except (KeyError, TypeError, ValueError, VmError):
            log.warning("dropped %r from a VM record: not a valid share", item)
            continue
        if all(s["name"] != name for s in out):
            out.append({"name": name, "host_path": path, "mode": mode})
    return out[:SHARE_MAX]


GUIDANCE = (
    "공유 폴더: 편집은 호스트 경로에서 하고, 빌드와 git은 vm_exec로 게스트 로컬 사본에서 실행한다 "
    "(예: rsync -a --delete '{guest}/' ~/work/{name}/ 로 복사한 뒤 ~/work/{name}에서 실행). "
    "공유 폴더에서 직접 빌드하거나 git을 사용하지 않는다. 파일이 많으면 로컬 디스크보다 약 10배 느리고, "
    "방금 쓴 파일에 대해 swiftc가 'modified during the build' 오류를 내기 때문이다. "
    "ro 공유는 게스트에서 쓸 수 없다. 결과물은 rw 공유에 복사하거나 vm_get으로 회수한다."
)


def guidance(shares: Iterable[dict]) -> str:
    """The text a session is given with its shared folders."""
    shares = list(shares)
    if not shares:
        return ""
    first = shares[0]
    return GUIDANCE.format(guest=guest_path(first["name"]), name=first["name"])
