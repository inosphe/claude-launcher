"""File transfer between a host folder and a VM's guest work folder
(vm-setup-8s6; docs/vm-hub.md "파일 전송").

A session with a ``files`` grant has four operations, all limited to two
folders:

* the **host folder** ``<daemon>/vm/<id>/files/`` (0700). A session names a path
  relative to it; absolute paths, ``..`` and symbolic links are refused.
* the VM's **guest work folder** (``Vm.work_dir``, default
  ``/Users/<account>/work``, set by the user only). The same relative-path
  rule applies, and the guest side checks it again.

Transport is ``tar`` streamed through ``tart exec -i`` (the only guest channel
there is), with absolute guest paths. Nothing here is executed on either side:
files are written, never run, and setuid/setgid/sticky bits are dropped.

Why this method for the guest check: the guest script resolves the work
folder with ``cd -P ... && pwd -P`` and then walks the requested path one
component at a time, refusing a symbolic link at any step. It uses only the
shell's builtins and ``find``/``tar``/``mv``/``rm``, so it does not depend on a
``realpath`` binary, and the path never reaches the guest unquoted (it is
embedded with :func:`shlex.quote`). Because the requested path has no ``..``
and no link on the way, the result lies inside the real work folder. A guest
process that swaps a folder for a link during a transfer is outside the model:
the guest's own user can already do anything in it.

Host side, the tar that comes back is never handed to ``tar -x``. It is read
member by member (:func:`extract_tar`): only regular files and directories
whose names are plain relative paths under the expected top name are written,
into a fresh staging folder, with ``O_EXCL|O_NOFOLLOW`` and owner-only modes.
The staging folder is renamed to its place only after the whole stream was
read and the guest said it succeeded, so a failed transfer leaves nothing.
"""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import stat
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import vm_tart, vmhub
from .vmhub import Registry, VmError

MAX_BYTES = 512 * 1024 * 1024
MAX_ITEMS = 10_000
TIMEOUT = 600.0
LS_MAX = 1000

SEGMENT_MAX = 255
PATH_MAX = 512
DEPTH_MAX = 16
#: Temporary names this module makes next to a destination; a session cannot
#: name them, and ``ls`` hides them.
RESERVED = ".claunch-"
STALE_AFTER = 24 * 3600


# ---- paths a session names ------------------------------------------------ #
def check_rel(what: str, value, *, empty_ok: bool = False) -> List[str]:
    """A session's path as its segments. Relative to the folder it is for,
    plain printable text, no '..' or '.', no backslash, bounded."""
    if value is None or value == "":
        if empty_ok:
            return []
        raise VmError("vm_bad_request", f"{what}: 경로가 필요합니다")
    if not isinstance(value, str):
        raise VmError("vm_bad_request", f"{what}: 문자열이어야 합니다")
    text = value[:-1] if value.endswith("/") and len(value) > 1 else value
    bad = (not text or text.startswith("/") or "\\" in text or len(text) > PATH_MAX
           or not text.isprintable())
    parts = text.split("/")
    if bad or any(p in ("", ".", "..") or len(p) > SEGMENT_MAX or p.startswith(RESERVED) for p in parts) \
            or len(parts) > DEPTH_MAX:
        raise VmError("vm_files_unsafe",
                      f"{what} {value[:80]!r}: 작업 폴더 기준 상대 경로여야 합니다 "
                      f"(절대 경로, '..', '.', 빈 구성요소, 역슬래시, 제어 문자, 깊이 {DEPTH_MAX}·길이 {PATH_MAX} 초과, "
                      f"'{RESERVED}' 접두어는 쓸 수 없습니다)")
    return parts


def _shown(parts: List[str]) -> str:
    return "/".join(parts)


# ---- the host folder ------------------------------------------------------- #
class HostFolder:
    """``<root>/files``: owner-only, and everything under it is reached by a
    walk that refuses symbolic links."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure(self) -> Path:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        st = os.lstat(self.root)
        if not stat.S_ISDIR(st.st_mode):
            raise VmError("vm_files_unsafe", f"{self.root}은(는) 폴더가 아니거나 심볼릭 링크입니다")
        if st.st_mode & 0o077:
            os.chmod(self.root, 0o700)
        return self.root

    def resolve(self, parts: List[str], *, create_parents: bool = False) -> Path:
        """``root/parts``, with no link on the way (the last part included),
        and contained in the real root. Missing parents are made when asked."""
        base = self.ensure()
        real = Path(os.path.realpath(base))
        cur = base
        for i, name in enumerate(parts):
            cur = cur / name
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                if create_parents and i < len(parts) - 1:
                    os.mkdir(cur, 0o700)
                    continue
                break
            if stat.S_ISLNK(st.st_mode):
                raise VmError("vm_files_unsafe", f"호스트 폴더 안의 {_shown(parts[:i + 1])}은(는) 심볼릭 링크입니다")
            if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
                raise VmError("vm_files_unsafe", f"호스트 폴더 안의 {_shown(parts[:i + 1])}은(는) 폴더가 아닙니다")
        out = base.joinpath(*parts)
        inside = os.path.realpath(out)
        if inside != str(real) and not inside.startswith(str(real) + os.sep):
            raise VmError("vm_files_unsafe", "경로가 호스트 폴더 밖을 가리킵니다")
        return out

    def sweep(self, now: Optional[float] = None) -> None:
        """Remove staging folders a crashed transfer left."""
        now = time.time() if now is None else now
        try:
            for entry in os.scandir(self.root):
                if entry.name.startswith(RESERVED) and entry.is_dir(follow_symlinks=False) \
                        and now - entry.stat(follow_symlinks=False).st_mtime > STALE_AFTER:
                    shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            pass


# ---- what goes to the guest ------------------------------------------------ #
def scan_source(path: Path, *, max_bytes: int, max_items: int) -> Tuple[int, int, List[Tuple[Path, str]]]:
    """Walk what a put will send: ``(items, bytes, [(path, arcname-suffix)])``.
    Anything but regular files and folders (a symbolic link, a socket, a
    device) refuses the whole put; so do the limits."""
    items, total = 0, 0
    order: List[Tuple[Path, str]] = []
    stack: List[Tuple[Path, str]] = [(path, "")]
    while stack:
        cur, rel = stack.pop()
        st = os.lstat(cur)
        items += 1
        if items > max_items:
            raise VmError("vm_files_limit", f"항목이 {max_items}개를 넘습니다")
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
            if total > max_bytes:
                raise VmError("vm_files_limit", f"전송 크기가 {max_bytes // (1024 * 1024)} MiB를 넘습니다")
        elif not stat.S_ISDIR(st.st_mode):
            raise VmError("vm_files_unsafe",
                          f"{cur.name}: 심볼릭 링크나 특수 파일은 보낼 수 없습니다")
        order.append((cur, rel))
        if stat.S_ISDIR(st.st_mode):
            for child in sorted(os.listdir(cur), reverse=True):
                stack.append((cur / child, f"{rel}/{child}"))
    return items, total, order


def write_tar(stdin, order: List[Tuple[Path, str]], top: str, deadline: float) -> None:
    """The tar of a scanned source to ``stdin``: regular files and folders only,
    owner and group zeroed, setuid/setgid/sticky and group/other bits gone."""
    with tarfile.open(fileobj=stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for path, rel in order:
            if time.monotonic() > deadline:
                raise VmError("vm_timeout", "전송 제한 시간을 넘었습니다")
            info = tar.gettarinfo(str(path), arcname=top + rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o700 if info.isdir() else (info.mode & 0o700) | 0o600
            if info.isdir():
                tar.addfile(info)
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise VmError("vm_files_unsafe", f"{path.name}: 일반 파일이 아닙니다")
                tar.addfile(info, fh)


# ---- what comes back ------------------------------------------------------- #
def member_parts(name: str) -> List[str]:
    """A tar member's name as segments, or a refusal: absolute, '..', '.',
    empty, control characters, too long or too deep."""
    text = name[:-1] if name.endswith("/") else name
    parts = text.split("/")
    if (not text or name.startswith("/") or "\\" in text or "\x00" in text or not text.isprintable()
            or len(parts) > DEPTH_MAX * 2 or len(text) > PATH_MAX * 4
            or any(p in ("", ".", "..") or len(p) > SEGMENT_MAX for p in parts)):
        raise VmError("vm_files_unsafe", f"받은 tar의 항목 이름이 안전하지 않습니다: {name[:80]!r}")
    return parts


def extract_tar(stream, staging: Path, top: str, *, max_bytes: int, max_items: int,
                deadline: float) -> Tuple[int, int]:
    """Read a tar from ``stream`` into the existing folder ``staging``,
    returning ``(items, bytes)``. Every member is checked before anything of it
    is written: only regular files and folders, names under ``top``, no
    duplicates, limits. Files are created ``O_EXCL|O_NOFOLLOW`` with owner-only
    modes; folders are made one real level at a time. AppleDouble companions
    (``._name``) that macOS tar adds for extended attributes are dropped."""
    items = nbytes = 0
    made_dirs = set()
    try:
        tar = tarfile.open(fileobj=stream, mode="r|")
    except tarfile.TarError as exc:
        raise VmError("vm_files_failed", f"게스트가 보낸 데이터를 tar로 읽을 수 없습니다: {exc}") from None
    with tar:
        try:
            for member in tar:
                if time.monotonic() > deadline:
                    raise VmError("vm_timeout", "전송 제한 시간을 넘었습니다")
                parts = member_parts(member.name)
                if parts[-1].startswith("._"):
                    continue
                if parts[0] != top:
                    raise VmError("vm_files_unsafe", f"받은 tar에 요청하지 않은 항목이 있습니다: {member.name[:80]!r}")
                if not (member.isdir() or member.isreg()) or member.type in (tarfile.GNUTYPE_SPARSE,):
                    raise VmError("vm_files_unsafe",
                                  f"받은 tar에 일반 파일·폴더가 아닌 항목이 있습니다: {member.name[:80]!r}")
                items += 1
                if items > max_items:
                    raise VmError("vm_files_limit", f"항목이 {max_items}개를 넘습니다")
                if member.isreg():
                    nbytes += member.size
                    if nbytes > max_bytes:
                        raise VmError("vm_files_limit", f"전송 크기가 {max_bytes // (1024 * 1024)} MiB를 넘습니다")
                cur = staging
                for name in (parts[:-1] if member.isreg() else parts):
                    cur = cur / name
                    if cur in made_dirs:
                        continue
                    try:
                        os.mkdir(cur, 0o700)
                    except FileExistsError:
                        if not stat.S_ISDIR(os.lstat(cur).st_mode):
                            raise VmError("vm_files_unsafe", f"받은 tar의 항목이 겹칩니다: {member.name[:80]!r}") from None
                    made_dirs.add(cur)
                if member.isdir():
                    os.chmod(cur, 0o700)
                    continue
                target = cur / parts[-1]
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    src = tar.extractfile(member)
                    left = member.size
                    while left > 0:
                        if time.monotonic() > deadline:
                            raise VmError("vm_timeout", "전송 제한 시간을 넘었습니다")
                        chunk = src.read(min(left, 1 << 20))
                        if not chunk:
                            raise VmError("vm_files_failed", "받은 tar가 중간에 끊겼습니다")
                        os.write(fd, chunk)
                        left -= len(chunk)
                    # owner bits only: setuid/setgid/sticky and group/other are gone
                    os.fchmod(fd, (member.mode & 0o700) | 0o600)
                finally:
                    os.close(fd)
        except tarfile.TarError as exc:
            raise VmError("vm_files_failed", f"받은 tar가 올바르지 않습니다: {exc}") from None
        except FileExistsError:
            raise VmError("vm_files_unsafe", "받은 tar에 같은 이름의 항목이 둘 있습니다") from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise VmError("vm_files_unsafe", "받은 tar가 심볼릭 링크를 통해 쓰려 했습니다") from None
            raise VmError("vm_files_failed", f"호스트에 쓰지 못했습니다: {exc.strerror or exc}") from None
    return items, nbytes


# ---- guest scripts ---------------------------------------------------------- #
#: Exit codes of the scripts below; the message is on stderr after
#: ``claunch-files:``.
RC_USAGE, RC_MISSING, RC_LINK, RC_SPECIAL, RC_EXISTS, RC_INTEGRITY = 64, 66, 67, 68, 73, 74

_HEAD = r'''set -eu
set -f
fail() { echo "claunch-files: $2" >&2; exit "$1"; }
W=%(work)s
walk() {
  P=$W; _rest=$1
  while [ -n "$_rest" ]; do
    case $_rest in */*) _c=${_rest%%%%/*}; _rest=${_rest#*/};; *) _c=$_rest; _rest=;; esac
    P=$P/$_c
    if [ -L "$P" ]; then
      if [ -z "$_rest" ] && [ "${2:-}" = last ]; then :; else fail 67 "symbolic link in path: $_c"; fi
    fi
  done
}
'''

_OPEN = r'''[ -d "$W" ] || fail 66 "work folder does not exist"
[ -L "$W" ] && fail 67 "work folder is a symbolic link"
W=$(cd -P -- "$W" && pwd -P)
'''


def _script(work: str, body: str) -> str:
    return (_HEAD % {"work": shlex.quote(work)}) + body


def put_script(work: str, parent: str, name: str, items: int, overwrite: bool) -> str:
    return _script(work, r'''mkdir -p -- "$W"
[ -L "$W" ] && fail 67 "work folder is a symbolic link"
W=$(cd -P -- "$W" && pwd -P)
find "$W" -maxdepth 1 -name '.claunch-put.*' -mmin +120 -exec rm -rf -- {} + 2>/dev/null || true
P=$W; _rest=%(parent)s
while [ -n "$_rest" ]; do
  case $_rest in */*) _c=${_rest%%%%/*}; _rest=${_rest#*/};; *) _c=$_rest; _rest=;; esac
  P=$P/$_c
  if [ -L "$P" ]; then fail 67 "symbolic link in path: $_c"; fi
  if [ -e "$P" ] && [ ! -d "$P" ]; then fail 73 "not a folder: $_c"; fi
  [ -d "$P" ] || mkdir -- "$P"
done
N=%(name)s
T=$P/$N
if [ -L "$T" ]; then fail 67 "symbolic link at the destination"; fi
if [ -e "$T" ] && [ %(overwrite)d != 1 ]; then fail 73 "destination exists"; fi
STAGE=$(mktemp -d "$W/.claunch-put.XXXXXX")
trap 'rm -rf -- "$STAGE"' EXIT
/usr/bin/tar -xf - -C "$STAGE" --no-same-owner || fail 74 "tar could not unpack the stream"
[ "$(ls -A -- "$STAGE" | wc -l | tr -d ' ')" = 1 ] && [ -e "$STAGE/$N" ] && [ ! -L "$STAGE/$N" ] \
  || fail 74 "unexpected contents in the stream"
[ -z "$(find "$STAGE" \( -type l -o \( ! -type f ! -type d \) \) -print -quit)" ] \
  || fail 68 "the stream held a link or a special file"
[ "$(find "$STAGE/$N" | wc -l | tr -d ' ')" = %(items)d ] || fail 74 "the transfer is incomplete"
find "$STAGE" \( -perm -4000 -o -perm -2000 \) -exec chmod ug-s {} +
if [ -e "$T" ]; then rm -rf -- "$T"; fi
mv -- "$STAGE/$N" "$T"
''' % {"parent": shlex.quote(parent), "name": shlex.quote(name), "items": items,
       "overwrite": 1 if overwrite else 0})


def get_script(work: str, rel: str) -> str:
    return _script(work, _OPEN + r'''walk %(rel)s
[ -e "$P" ] || fail 66 "not found"
[ -z "$(find "$P" \( -type l -o \( ! -type f ! -type d \) \) -print -quit)" ] \
  || fail 68 "it holds a link or a special file"
echo "claunch-files: items $(find "$P" | wc -l | tr -d ' ')" >&2
cd -- "$(dirname -- "$P")"
COPYFILE_DISABLE=1 exec /usr/bin/tar -cf - -- "$(basename -- "$P")"
''' % {"rel": shlex.quote(rel)})


def ls_script(work: str, rel: str) -> str:
    return _script(work, _OPEN + r'''walk %(rel)s
[ -d "$P" ] || fail 66 "not a folder, or not found"
echo "ROOT|$P"
find "$P" -mindepth 1 -maxdepth 1 ! -name '.claunch-*' -exec /usr/bin/stat -f '%%HT|%%z|%%N' {} + | head -n %(max)d
''' % {"rel": shlex.quote(rel), "max": LS_MAX + 1})


def rm_script(work: str, rel: str) -> str:
    return _script(work, _OPEN + r'''[ -n %(rel)s ] || fail 64 "the work folder itself is not removed"
walk %(rel)s last
[ -e "$P" ] || [ -L "$P" ] || fail 66 "not found"
if [ -L "$P" ]; then rm -f -- "$P"; else rm -rf -- "$P"; fi
''' % {"rel": shlex.quote(rel)})


_KIND = {"Regular File": "file", "Directory": "dir", "Symbolic Link": "symlink"}

_RC_CODE = {RC_USAGE: "vm_bad_request", RC_MISSING: "vm_files_missing", RC_LINK: "vm_files_unsafe",
            RC_SPECIAL: "vm_files_unsafe", RC_EXISTS: "vm_files_exists", RC_INTEGRITY: "vm_files_failed"}


def guest_failure(out: vm_tart.StreamResult, what: str) -> VmError:
    """The guest script's refusal as an error: its ``claunch-files:`` line
    carries the reason."""
    line = next((ln for ln in out.stderr.splitlines() if ln.startswith("claunch-files:")), "")
    reason = line.partition(":")[2].strip() or out.stderr.strip()[:200] or f"exit {out.rc}"
    if out.timed_out:
        return VmError("vm_timeout", f"{what}: 제한 시간 안에 끝나지 않아 중단했습니다")
    return VmError(_RC_CODE.get(out.rc, "vm_files_failed"), f"{what}: {reason}")


class Files:
    """The four operations, behind the registry's grant/lock/ready gate (the
    caller made ``begin_call``). Every call is recorded with path and byte
    count (``Registry.note_files``), never contents."""

    def __init__(self, registry: Registry, manager: vm_tart.Manager, root: Path, *,
                 max_bytes: int = MAX_BYTES, max_items: int = MAX_ITEMS,
                 timeout: float = TIMEOUT) -> None:
        self.registry = registry
        self.manager = manager
        self.root = root
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.timeout = timeout
        self._busy: set = set()
        self._busy_lock = threading.Lock()

    # ---- places ---------------------------------------------------------- #
    def host_dir(self, vm_id: str) -> Path:
        return self.root / vmhub.check_name("id", vm_id) / "files"

    def _host(self, vm_id: str) -> HostFolder:
        return HostFolder(self.host_dir(vm_id))

    def describe(self, vm_id: str) -> dict:
        vm = self.registry.get(vm_id)
        return {"host_dir": str(self.host_dir(vm_id)), "guest_dir": vm.work_dir,
                "guest_dir_default": vmhub.FILES_DIR_DEFAULT.format(user=vm.ssh_user),
                "limits": {"max_bytes": self.max_bytes, "max_items": self.max_items,
                           "timeout": self.timeout},
                "recent": [dict(r) for r in self.registry.files_log.get(vm_id, ())]}

    # ---- plumbing ---------------------------------------------------------- #
    def _vm(self, vm_id: str) -> vmhub.Vm:
        vm = self.registry.get(vm_id)
        if not vm.files_ready:
            raise vmhub.not_ready(vm)
        return vm

    class _Slot:
        def __init__(self, owner: "Files", vm_id: str) -> None:
            self.owner, self.vm_id = owner, vm_id

        def __enter__(self):
            with self.owner._busy_lock:
                if self.vm_id in self.owner._busy:
                    raise VmError("vm_files_busy", f"{self.vm_id}: 다른 파일 전송이 진행 중입니다", retry_after=5)
                self.owner._busy.add(self.vm_id)

        def __exit__(self, *exc):
            with self.owner._busy_lock:
                self.owner._busy.discard(self.vm_id)

    def _recorded(self, vm_id: str, session: str, op: str, path, fn: Callable[[], dict]) -> dict:
        shown = "".join(c for c in str(path) if c.isprintable())[:200]
        try:
            with self._Slot(self, vm_id):
                out = fn()
        except VmError as exc:
            self.registry.note_files(vm_id, session, op=op, path=shown, ok=False,
                                     code=exc.code, why=str(exc)[:200])
            raise
        self.registry.note_files(vm_id, session, op=op, path=shown, ok=True,
                                 bytes=out.get("bytes", 0), items=out.get("items", 0))
        return out

    # ---- operations -------------------------------------------------------- #
    def put(self, vm_id: str, session: str, path, dest=None, overwrite: bool = False) -> dict:
        def run():
            src = check_rel("path", path)
            target = check_rel("dest", dest, empty_ok=True) or [src[-1]]
            return self._put(vm_id, src, target, bool(overwrite))
        return self._recorded(vm_id, session, "put", path, run)

    def _put(self, vm_id, src, target, overwrite) -> dict:
        vm = self._vm(vm_id)
        folder = self._host(vm_id)
        source = folder.resolve(src)
        if not os.path.lexists(source):
            raise VmError("vm_files_missing", f"호스트 폴더에 {_shown(src)}이(가) 없습니다")
        items, total, order = scan_source(source, max_bytes=self.max_bytes, max_items=self.max_items)
        script = put_script(vm.work_dir, "/".join(target[:-1]), target[-1], items, overwrite)
        deadline = time.monotonic() + self.timeout
        out = self.manager.guest_stream(
            vm, script, timeout=self.timeout,
            feed=lambda stdin: write_tar(stdin, order, target[-1], deadline))
        for err in (out.feed_error, out.drain_error):
            if isinstance(err, VmError):
                raise err
            if err is not None:
                raise VmError("vm_files_failed", f"보내는 중 오류: {err}")
        if out.rc != 0:
            raise guest_failure(out, f"put {_shown(src)}")
        return {"ok": True, "path": _shown(target), "guest_dir": vm.work_dir,
                "bytes": total, "items": items}

    def get(self, vm_id: str, session: str, path, dest=None, overwrite: bool = False) -> dict:
        def run():
            src = check_rel("path", path)
            target = check_rel("dest", dest, empty_ok=True) or [src[-1]]
            return self._get(vm_id, src, target, bool(overwrite))
        return self._recorded(vm_id, session, "get", path, run)

    def _get(self, vm_id, src, target, overwrite) -> dict:
        vm = self._vm(vm_id)
        folder = self._host(vm_id)
        if os.path.lexists(folder.resolve(target)) and not overwrite:
            raise VmError("vm_files_exists", f"호스트 폴더에 {_shown(target)}이(가) 이미 있습니다")
        folder.sweep()
        staging = Path(tempfile.mkdtemp(prefix=RESERVED + "get.", dir=folder.ensure()))
        result: Dict[str, int] = {}
        deadline = time.monotonic() + self.timeout

        def drain(stdout) -> None:
            result["items"], result["bytes"] = extract_tar(
                stdout, staging, src[-1], max_bytes=self.max_bytes, max_items=self.max_items,
                deadline=deadline)

        try:
            out = self.manager.guest_stream(vm, get_script(vm.work_dir, _shown(src)),
                                            drain=drain, timeout=self.timeout)
            err = out.drain_error
            if isinstance(err, VmError) and err.code != "vm_files_failed":
                raise err           # unsafe member, limit, time: the reason is ours
            if out.rc != 0:
                raise guest_failure(out, f"get {_shown(src)}")
            if err is not None:
                raise err if isinstance(err, VmError) else VmError("vm_files_failed", f"받는 중 오류: {err}")
            expect = next((int(ln.rsplit(" ", 1)[1]) for ln in out.stderr.splitlines()
                           if ln.startswith("claunch-files: items ")), None)
            if expect is None or expect != result.get("items") or not (staging / src[-1]).exists():
                raise VmError("vm_files_failed", f"get {_shown(src)}: 받은 항목 수가 게스트가 알린 수와 다릅니다 "
                                                 f"({result.get('items')} / {expect})")
            final = folder.resolve(target, create_parents=True)
            old = staging / "replaced"
            if os.path.lexists(final):
                if not overwrite:
                    raise VmError("vm_files_exists", f"호스트 폴더에 {_shown(target)}이(가) 이미 있습니다")
                os.rename(final, old)
            try:
                os.rename(staging / src[-1], final)
            except OSError as exc:
                if old.exists():
                    os.rename(old, final)
                raise VmError("vm_files_failed", f"호스트에 옮기지 못했습니다: {exc.strerror or exc}") from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {"ok": True, "path": _shown(target), "host_path": str(final),
                "bytes": result["bytes"], "items": result["items"]}

    def ls(self, vm_id: str, session: str, path=None) -> dict:
        def run():
            return self._ls(vm_id, check_rel("path", path, empty_ok=True))
        return self._recorded(vm_id, session, "ls", path or ".", run)

    def _ls(self, vm_id, rel) -> dict:
        vm = self._vm(vm_id)
        rc, out, err = self.manager.guest_run(vm, ls_script(vm.work_dir, _shown(rel)), 60)
        if rc != 0:
            raise guest_failure(vm_tart.StreamResult(rc, err, timed_out=rc == 124), f"ls {_shown(rel) or '.'}")
        lines = out.splitlines()
        root = lines[0].partition("|")[2] if lines and lines[0].startswith("ROOT|") else ""
        entries = []
        for line in lines[1:]:
            kind, _, rest = line.partition("|")
            size, _, name = rest.partition("|")
            if not root or not name.startswith(root + "/") or not size.isdigit():
                continue    # a name with a newline in it: not listed
            kind = _KIND.get(kind, "other")
            entries.append({"name": name[len(root) + 1:], "kind": kind,
                            "size": int(size) if kind == "file" else None})
        entries.sort(key=lambda e: e["name"])
        return {"ok": True, "path": _shown(rel), "guest_dir": vm.work_dir,
                "entries": entries[:LS_MAX], "truncated": len(entries) > LS_MAX, "items": len(entries)}

    def rm(self, vm_id: str, session: str, path, confirm=None) -> dict:
        def run():
            rel = check_rel("path", path)
            if confirm != _shown(rel):
                raise VmError("vm_confirm", f"삭제하려면 confirm에 경로 {_shown(rel)!r}를 그대로 적어야 합니다")
            return self._rm(vm_id, rel)
        return self._recorded(vm_id, session, "rm", path, run)

    def _rm(self, vm_id, rel) -> dict:
        vm = self._vm(vm_id)
        rc, _, err = self.manager.guest_run(vm, rm_script(vm.work_dir, _shown(rel)), 60)
        if rc != 0:
            raise guest_failure(vm_tart.StreamResult(rc, err, timed_out=rc == 124), f"rm {_shown(rel)}")
        return {"ok": True, "path": _shown(rel)}
