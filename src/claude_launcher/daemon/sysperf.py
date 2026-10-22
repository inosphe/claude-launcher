"""The machine's CPU, memory, disk I/O and disk capacity, sampled for the
dashboard (claunch-zuk0b, claunch-frgt1).

The rail header shows the latest sample; the System performance page draws
the in-memory history; and every sample is also written as one JSON line to a
size-rotated log (``<daemon dir>/logs/sysperf.log``) so a slowdown that
happened while nobody had the page open still leaves a record to read.

Capacity is a gauge, not a rate: each sample carries ``usage``, one entry per
drive the daemon cares about — the filesystem holding its own runtime root
plus every registered workspace. A path becomes its mount point, so the same
drive counts once however many workspaces sit on it (``/mnt/c`` under WSL,
``C:\\`` on Windows).

Counters come from psutil when it is importable, else from procfs (Linux,
WSL), else from the Win32 API through ctypes (CPU and memory only: Windows
has no counter for whole-disk bytes without psutil or a WMI query). Under
WSL, procfs describes the WSL VM, not the Windows host; every sample carries
its ``source`` so the page can say which machine it is looking at.

A sample is the difference between two counter readings, so the first
reading after start produces nothing; the collector reads once at start and
the first sample arrives one interval later. Reading runs in a worker thread
so a slow procfs (it happens under WSL) never stalls the event loop.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from aiohttp import web

from .. import workspaces
from . import paths

#: Seconds between samples (``daemon.sysperf_interval_s``; 0 turns the
#: collector off). The rail header asks for the latest sample at the same
#: rate, so a shorter interval would only spend reads nobody sees.
INTERVAL = 5.0
#: Samples kept in memory for the page's chart: one hour at the default
#: interval. Older samples live only in the log.
HISTORY = 720
#: The log rotates at this size and keeps this many old files: about 12 MB
#: in all, roughly a day and a half of samples at the default interval.
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 5
#: /proc/diskstats counts in 512-byte sectors whatever the device's own
#: sector size is (Documentation/admin-guide/iostats.rst).
SECTOR = 512

#: Block devices that are not disks, or whose I/O is already counted on the
#: disk beneath them (device mapper, md RAID).
_VIRTUAL = re.compile(r"^(ram|loop|zram|dm-|md|sr|fd)\d*")
#: Partition names, for when /sys/block is not there to say which names are
#: whole disks: sda1, nvme0n1p2, mmcblk0p1.
_PARTITION = re.compile(r"^(sd[a-z]+\d+|hd[a-z]+\d+|vd[a-z]+\d+|xvd[a-z]+\d+|"
                        r"nvme\d+n\d+p\d+|mmcblk\d+p\d+)$")

log = logging.getLogger("claunch.daemon.sysperf")


def log_path() -> Path:
    return paths.daemon_dir() / "logs" / "sysperf.log"


# --------------------------------------------------------------- readings
#
# A reading is a dict of raw counters at one moment:
#   t            monotonic seconds
#   cpu_busy     cumulative busy CPU time (any unit, same as cpu_total)
#   cpu_total    cumulative total CPU time
#   mem_total    bytes
#   mem_available bytes
#   swap_total, swap_free  bytes (optional)
#   disks        {name: (read_bytes, write_bytes, busy_ms)} (optional)
#   cpus         logical CPU count
#   load1        1-minute load average (optional)
#   source       "psutil" | "procfs" | "win32"


def parse_proc_stat(text: str) -> tuple:
    """(busy, total) jiffies from the aggregate ``cpu`` line. iowait counts
    as idle, as top and psutil count it."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            fields = [int(v) for v in line.split()[1:]]
            # user nice system idle iowait irq softirq steal [guest guest_nice]
            # guest time is already inside user/nice, so only the first 8 sum.
            fields = (fields + [0] * 8)[:8]
            idle = fields[3] + fields[4]
            total = sum(fields)
            return total - idle, total
    raise ValueError("no aggregate cpu line in /proc/stat")


def parse_meminfo(text: str) -> Dict[str, int]:
    """Bytes for MemTotal, MemAvailable, SwapTotal, SwapFree. Kernels older
    than 3.14 have no MemAvailable; then free + buffers + cached stands in."""
    kib: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            kib[key.strip()] = int(parts[0])
    available = kib.get("MemAvailable")
    if available is None:
        available = kib.get("MemFree", 0) + kib.get("Buffers", 0) + kib.get("Cached", 0)
    return {
        "mem_total": kib.get("MemTotal", 0) * 1024,
        "mem_available": available * 1024,
        "swap_total": kib.get("SwapTotal", 0) * 1024,
        "swap_free": kib.get("SwapFree", 0) * 1024,
    }


def is_whole_disk(name: str, whole: Optional[set] = None) -> bool:
    if _VIRTUAL.match(name):
        return False
    if whole is not None:
        return name in whole
    return not _PARTITION.match(name)


def parse_diskstats(text: str, whole: Optional[set] = None) -> Dict[str, tuple]:
    """{disk: (read_bytes, write_bytes, busy_ms)} for whole physical disks.
    Partitions are left out so a disk's I/O is not counted twice."""
    disks: Dict[str, tuple] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        if not is_whole_disk(name, whole):
            continue
        try:
            read_sectors = int(parts[5])
            write_sectors = int(parts[9])
            busy_ms = int(parts[12])
        except ValueError:
            continue
        disks[name] = (read_sectors * SECTOR, write_sectors * SECTOR, busy_ms)
    return disks


def _read(path: str) -> str:
    with open(path, encoding="ascii", errors="replace") as fh:
        return fh.read()


def _whole_disks() -> Optional[set]:
    try:
        return set(os.listdir("/sys/block"))
    except OSError:
        return None


def read_procfs() -> dict:
    busy, total = parse_proc_stat(_read("/proc/stat"))
    reading: Dict[str, Any] = {"cpu_busy": busy, "cpu_total": total, "source": "procfs"}
    reading.update(parse_meminfo(_read("/proc/meminfo")))
    try:
        reading["disks"] = parse_diskstats(_read("/proc/diskstats"), _whole_disks())
    except OSError:
        pass
    return reading


def read_psutil(psutil: Any) -> dict:
    times = psutil.cpu_times()
    idle = getattr(times, "idle", 0.0) + getattr(times, "iowait", 0.0)
    total = sum(times)
    for guest in ("guest", "guest_nice"):
        total -= getattr(times, guest, 0.0)
    vm = psutil.virtual_memory()
    reading: Dict[str, Any] = {
        "cpu_busy": total - idle,
        "cpu_total": total,
        "mem_total": int(vm.total),
        "mem_available": int(vm.available),
        "source": "psutil",
    }
    with contextlib.suppress(Exception):
        sm = psutil.swap_memory()
        reading["swap_total"] = int(sm.total)
        reading["swap_free"] = int(sm.free)
    with contextlib.suppress(Exception):
        per = psutil.disk_io_counters(perdisk=True) or {}
        whole = _whole_disks()
        reading["disks"] = {
            name: (int(c.read_bytes), int(c.write_bytes), int(getattr(c, "busy_time", 0)))
            for name, c in per.items()
            if sys.platform == "win32" or is_whole_disk(name, whole)
        }
    return reading


def read_win32() -> dict:  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    def ft(value: FILETIME) -> int:
        return (value.hi << 32) | value.lo

    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        raise OSError("GetSystemTimes failed")
    # Kernel time includes idle time.
    total = ft(kernel) + ft(user)
    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        raise OSError("GlobalMemoryStatusEx failed")
    return {
        "cpu_busy": total - ft(idle),
        "cpu_total": total,
        "mem_total": int(mem.ullTotalPhys),
        "mem_available": int(mem.ullAvailPhys),
        "source": "win32",
    }


def _psutil() -> Any:
    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:
        return None
    return psutil


def read_counters() -> dict:
    """One raw reading of the machine's counters, from the best source this
    platform has."""
    psutil = _psutil()
    if psutil is not None:
        reading = read_psutil(psutil)
    elif sys.platform == "win32":
        reading = read_win32()
    else:
        reading = read_procfs()
    reading["t"] = time.monotonic()
    reading["cpus"] = os.cpu_count() or 1
    with contextlib.suppress(AttributeError, OSError):
        reading["load1"] = os.getloadavg()[0]
    return reading


# --------------------------------------------------------------- capacity
#
# A capacity entry is one drive:
#   mount    the filesystem's mount point ("/" or "/mnt/c", "C:\\")
#   label    the short name the badge prints ("C:", "/mnt/c")
#   total, used, free   bytes
#   pct      used / total in percent, None when the size is unknown

#: How long the drive list (and its numbers) is reused. Capacity moves
#: slowly and resolving it reads the config file, so one reading per minute
#: is enough for a badge that is redrawn every few seconds.
USAGE_TTL = 60.0

_WIN_DRIVE = re.compile(r"^([A-Za-z]):[\\/]?$")


def mount_point(path: str) -> Optional[str]:
    """The filesystem root ``path`` lives on, or None if it cannot be found.

    Windows drives are their own root, so a "C:\\..." path answers "C:\\"
    without walking anywhere. Elsewhere the path is walked up to an existing
    ancestor and then to the nearest mount point, which is what maps both a
    native "/" and a WSL "/mnt/c" to a drive.
    """
    try:
        current = Path(os.path.abspath(os.path.expanduser(str(path))))
    except (OSError, ValueError, TypeError):
        return None
    drive, _ = os.path.splitdrive(str(current))
    if drive:
        return drive + os.sep
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    while not os.path.ismount(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return str(current)


def drive_label(mount: str) -> str:
    """How the drive is named on screen: "C:" for a Windows drive letter,
    the mount point itself otherwise ("/", "/mnt/c")."""
    found = _WIN_DRIVE.match(mount or "")
    if found:
        return found.group(1).upper() + ":"
    return mount or "?"


def disk_usage(
    paths: Iterable[str],
    *,
    mount_of: Optional[Callable[[str], Optional[str]]] = None,
    usage_of: Optional[Callable[[str], Any]] = None,
) -> List[dict]:
    """Capacity of every distinct drive among ``paths``, mount-sorted.

    Two workspaces on one drive give one entry. A path whose drive cannot be
    resolved or read (an unplugged network share, a permission error) is
    skipped rather than raising: the badge still has to show the others.
    """
    mount_of = mount_point if mount_of is None else mount_of
    usage_of = shutil.disk_usage if usage_of is None else usage_of
    found: Dict[str, dict] = {}
    for raw in paths:
        mount = mount_of(str(raw))
        if not mount or mount in found:
            continue
        try:
            usage = usage_of(mount)
        except OSError:
            continue
        total = int(getattr(usage, "total", 0) or 0)
        used = int(getattr(usage, "used", 0) or 0)
        free = int(getattr(usage, "free", 0) or 0)
        found[mount] = {
            "mount": mount,
            "label": drive_label(mount),
            "total": total,
            "used": used,
            "free": free,
            "pct": round(100.0 * used / total, 1) if total else None,
        }
    return [found[mount] for mount in sorted(found)]


def usage_paths() -> List[str]:
    """The paths whose drives the badge reports: the daemon's own runtime
    root first, then every registered workspace."""
    targets = [str(paths.daemon_dir())]
    try:
        targets.extend(workspace.path for workspace in workspaces.list_all())
    except Exception as exc:  # a broken config must not stop the sampling
        log.warning("sysperf: cannot list workspaces: %s: %s", type(exc).__name__, exc)
    return targets


def read_usage() -> List[dict]:
    """Capacity of the daemon root's drive and every workspace's drive."""
    return disk_usage(usage_paths())


def sample(prev: dict, cur: dict, *, ts: Optional[float] = None) -> dict:
    """The rates between two readings. Counters that went backwards (a
    disk that disappeared and came back, a counter wrap) give 0, never a
    negative rate."""
    dt = max(cur["t"] - prev["t"], 1e-6)
    d_total = cur["cpu_total"] - prev["cpu_total"]
    d_busy = cur["cpu_busy"] - prev["cpu_busy"]
    cpu = 100.0 * d_busy / d_total if d_total > 0 else 0.0
    mem_total = cur.get("mem_total") or 0
    mem_used = max(mem_total - (cur.get("mem_available") or 0), 0)
    out: Dict[str, Any] = {
        "ts": round(time.time() if ts is None else ts, 3),
        "interval_s": round(dt, 3),
        "source": cur.get("source"),
        "cpus": cur.get("cpus"),
        "cpu_pct": round(min(max(cpu, 0.0), 100.0), 1),
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_pct": round(100.0 * mem_used / mem_total, 1) if mem_total else None,
        "load1": round(cur["load1"], 2) if cur.get("load1") is not None else None,
    }
    swap_total = cur.get("swap_total")
    if swap_total:
        swap_used = max(swap_total - (cur.get("swap_free") or 0), 0)
        out["swap_used"] = swap_used
        out["swap_total"] = swap_total
    cur_disks = cur.get("disks")
    prev_disks = prev.get("disks") or {}
    if cur_disks is None:
        out["disk_read_bps"] = out["disk_write_bps"] = None
        out["disk_busy_pct"] = None
        out["disks"] = {}
        return out
    per: Dict[str, dict] = {}
    for name, (rb, wb, busy) in sorted(cur_disks.items()):
        if name not in prev_disks:
            continue
        prb, pwb, pbusy = prev_disks[name]
        per[name] = {
            "read_bps": max(rb - prb, 0) / dt,
            "write_bps": max(wb - pwb, 0) / dt,
            "busy_pct": min(max(busy - pbusy, 0) / (dt * 1000.0) * 100.0, 100.0),
        }
    out["disk_read_bps"] = round(sum(d["read_bps"] for d in per.values()))
    out["disk_write_bps"] = round(sum(d["write_bps"] for d in per.values()))
    out["disk_busy_pct"] = (
        round(max(d["busy_pct"] for d in per.values()), 1) if per else None
    )
    out["disks"] = {
        name: {
            "read_bps": round(d["read_bps"]),
            "write_bps": round(d["write_bps"]),
            "busy_pct": round(d["busy_pct"], 1),
        }
        for name, d in per.items()
    }
    return out


# -------------------------------------------------------------- collector


def make_log_handler(path: Path, *, max_bytes: int = LOG_MAX_BYTES,
                     backups: int = LOG_BACKUPS) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


class SysPerf:
    def __init__(
        self,
        *,
        interval: float = INTERVAL,
        history: int = HISTORY,
        reader: Callable[[], dict] = read_counters,
        usage: Callable[[], List[dict]] = read_usage,
        usage_ttl: float = USAGE_TTL,
        log_file: Optional[Path] = None,
        log_max_bytes: int = LOG_MAX_BYTES,
        log_backups: int = LOG_BACKUPS,
    ) -> None:
        self.interval = interval
        self.history: Deque[dict] = collections.deque(maxlen=max(int(history), 1))
        self.error: Optional[str] = None
        self.log_file = log_file
        self.log_max_bytes = log_max_bytes
        self.log_backups = log_backups
        self.usage_ttl = usage_ttl
        self._reader = reader
        self._usage_reader = usage
        self._usage_cache: Optional[Tuple[float, List[dict]]] = None
        self._prev: Optional[dict] = None
        self._task: Optional[asyncio.Task] = None
        # A logger of its own per collector: never propagated into
        # daemon.log, and two collectors (tests) do not share a handler.
        # Built directly instead of through getLogger so it never enters the
        # logger registry: a registered name built from id(self) is reused
        # when a later collector lands on a freed address, and pytest's log
        # capture attaches to every registered non-propagating logger, so
        # the reused one carried that handler (claunch-0yxs1).
        self._log = logging.Logger(f"claunch.sysperf.samples.{id(self)}")
        self._log.propagate = False
        self._log.setLevel(logging.INFO)
        self._handler: Optional[logging.Handler] = None

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def _open_log(self) -> None:
        if self.log_file is None or self._handler is not None:
            return
        try:
            self._handler = make_log_handler(
                self.log_file, max_bytes=self.log_max_bytes, backups=self.log_backups
            )
        except OSError as exc:
            log.warning("sysperf: cannot open %s: %s", self.log_file, exc)
            return
        self._log.addHandler(self._handler)

    def _close_log(self) -> None:
        handler, self._handler = self._handler, None
        if handler is not None:
            self._log.removeHandler(handler)
            handler.close()

    def tick(self, reading: dict, *, usage: Optional[List[dict]] = None) -> Optional[dict]:
        """Fold one reading in; return the new sample (None for the first).

        ``usage`` is the capacity list to carry in the sample; passing it
        here rather than appending afterwards keeps the log line, which is
        written now, identical to the sample the page reads."""
        prev, self._prev = self._prev, reading
        if prev is None:
            return None
        point = sample(prev, reading)
        if usage is not None:
            point["usage"] = usage
        self.history.append(point)
        if self._handler is not None:
            self._log.info(json.dumps(point, separators=(",", ":"), sort_keys=True))
        return point

    def usage(self) -> List[dict]:
        """The drives' capacity, reused for ``usage_ttl`` seconds: the badge
        is redrawn every few seconds, capacity moves far slower than that."""
        now = time.monotonic()
        cached = self._usage_cache
        if cached is not None and now - cached[0] < self.usage_ttl:
            return cached[1]
        try:
            value = self._usage_reader()
        except Exception as exc:
            log.warning("sysperf: reading disk capacity failed: %s: %s",
                        type(exc).__name__, exc)
            return []
        self._usage_cache = (now, value)
        return value

    def poll_once(self) -> Optional[dict]:
        try:
            reading = self._reader()
        except Exception as exc:  # a missing /proc file, a ctypes failure
            message = f"{type(exc).__name__}: {exc}"
            if message != self.error:
                log.warning("sysperf: reading counters failed: %s", message)
            self.error = message
            return None
        self.error = None
        return self.tick(reading, usage=self.usage())

    def start(self) -> None:
        if not self.enabled:
            return
        if self._task is None or self._task.done():
            self._open_log()
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="sysperf"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._close_log()

    async def _run(self) -> None:
        while True:
            await asyncio.to_thread(self.poll_once)
            await asyncio.sleep(self.interval)

    def latest(self) -> Optional[dict]:
        return self.history[-1] if self.history else None

    def snapshot(self, *, history: bool = False) -> dict:
        out: Dict[str, Any] = {
            "enabled": self.enabled,
            "interval_s": self.interval,
            "error": self.error,
            "latest": self.latest(),
        }
        if history:
            out["history"] = list(self.history)
            out["history_max"] = self.history.maxlen
            out["log"] = {
                "path": str(self.log_file) if self.log_file else None,
                "max_bytes": self.log_max_bytes,
                "backups": self.log_backups,
            }
        return out


# ------------------------------------------------------------------ routes


async def h_sysperf(request: web.Request) -> web.Response:
    """``GET /api/sysperf``: the latest sample (what the rail header polls).
    ``?history=1`` adds the in-memory history and where the log is."""
    collector: SysPerf = request.app["sysperf"]
    want = str(request.query.get("history") or "").lower() in ("1", "true", "yes")
    return web.json_response(collector.snapshot(history=want))


async def _start(app: web.Application) -> None:
    app["sysperf"].start()


async def _stop(app: web.Application) -> None:
    await app["sysperf"].stop()


def install(app: web.Application, *, interval: Optional[float] = None,
            collector: Optional[SysPerf] = None) -> SysPerf:
    if collector is None:
        collector = SysPerf(
            interval=INTERVAL if interval is None else interval,
            log_file=log_path(),
        )
    app["sysperf"] = collector
    app.on_startup.append(_start)
    app.on_shutdown.append(_stop)
    app.router.add_get("/api/sysperf", h_sysperf)
    return collector
