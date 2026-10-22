"""The machine sampler behind the rail header's CPU / MEM / IO / DISK badge
and the System performance page (daemon/sysperf.py, claunch-zuk0b,
claunch-frgt1)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store, workspaces
from claude_launcher.daemon import sysperf

STATIC = Path(__file__).resolve().parents[1] / "src" / "claude_launcher" / "web" / "static"

PROC_STAT = """\
cpu  100 0 50 800 50 0 0 0 0 0
cpu0 50 0 25 400 25 0 0 0 0 0
intr 1 2 3
"""

MEMINFO = """\
MemTotal:       16000000 kB
MemFree:         2000000 kB
MemAvailable:    6000000 kB
Buffers:          100000 kB
Cached:          3000000 kB
SwapTotal:       4000000 kB
SwapFree:        3000000 kB
"""

DISKSTATS = """\
   1       0 ram0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
   7       0 loop0 10 0 80 0 0 0 0 0 0 0 0 0 0 0 0 0 0
   8       0 sda 787 497 2000 834 10 0 1000 0 0 556 834 0 0 0 0 0 0
   8       1 sda1 700 400 1900 800 10 0 1000 0 0 500 800 0 0 0 0 0 0
 259       0 nvme0n1 10 0 4000 1 20 0 3000 0 0 100 1 0 0 0 0 0 0
 259       1 nvme0n1p1 10 0 4000 1 20 0 3000 0 0 100 1 0 0 0 0 0 0
 253       0 dm-0 10 0 4000 1 20 0 3000 0 0 100 1 0 0 0 0 0 0
"""


def test_proc_stat_counts_iowait_as_idle():
    busy, total = sysperf.parse_proc_stat(PROC_STAT)
    assert total == 1000
    assert busy == 150  # user 100 + system 50; idle 800 and iowait 50 are idle


def test_proc_stat_without_aggregate_line_is_an_error():
    with pytest.raises(ValueError):
        sysperf.parse_proc_stat("cpu0 1 2 3 4\n")


def test_meminfo_reads_bytes_and_swap():
    mem = sysperf.parse_meminfo(MEMINFO)
    assert mem["mem_total"] == 16000000 * 1024
    assert mem["mem_available"] == 6000000 * 1024
    assert mem["swap_total"] == 4000000 * 1024
    assert mem["swap_free"] == 3000000 * 1024


def test_meminfo_without_memavailable_falls_back_to_free_buffers_cached():
    text = "\n".join(l for l in MEMINFO.splitlines() if not l.startswith("MemAvailable"))
    mem = sysperf.parse_meminfo(text)
    assert mem["mem_available"] == (2000000 + 100000 + 3000000) * 1024


def test_diskstats_keeps_whole_physical_disks_only():
    disks = sysperf.parse_diskstats(DISKSTATS)
    assert sorted(disks) == ["nvme0n1", "sda"]
    read_b, write_b, busy = disks["sda"]
    assert (read_b, write_b, busy) == (2000 * 512, 1000 * 512, 556)


def test_diskstats_trusts_sys_block_when_given():
    # /sys/block names whole disks; a name absent from it is a partition.
    disks = sysperf.parse_diskstats(DISKSTATS, whole={"sda", "loop0", "dm-0"})
    assert sorted(disks) == ["sda"]


def _reading(t, busy, total, *, disks=None, avail=6 * 2**30):
    out = {
        "t": t, "cpu_busy": busy, "cpu_total": total,
        "mem_total": 16 * 2**30, "mem_available": avail,
        "cpus": 8, "load1": 1.234, "source": "procfs",
    }
    if disks is not None:
        out["disks"] = disks
    return out


def test_sample_rates_between_two_readings():
    prev = _reading(0.0, 100, 1000, disks={"sda": (0, 0, 0)})
    cur = _reading(5.0, 350, 2000, disks={"sda": (5 * 1024 * 1024, 1024 * 1024, 2500)})
    s = sysperf.sample(prev, cur, ts=123.0)
    assert s["ts"] == 123.0
    assert s["cpu_pct"] == 25.0
    assert s["mem_pct"] == 62.5
    assert s["mem_used"] == 10 * 2**30
    assert s["disk_read_bps"] == 1024 * 1024
    assert s["disk_write_bps"] == round(1024 * 1024 / 5)
    assert s["disk_busy_pct"] == 50.0
    assert s["disks"]["sda"]["busy_pct"] == 50.0
    assert s["load1"] == 1.23
    assert s["source"] == "procfs"


def test_sample_never_reports_a_negative_rate():
    prev = _reading(0.0, 100, 1000, disks={"sda": (1000, 1000, 100)})
    cur = _reading(5.0, 100, 1000, disks={"sda": (0, 0, 0), "sdb": (9, 9, 9)})
    s = sysperf.sample(prev, cur)
    assert s["cpu_pct"] == 0.0
    assert s["disk_read_bps"] == 0 and s["disk_write_bps"] == 0
    # A disk that appeared between the readings has no rate yet.
    assert list(s["disks"]) == ["sda"]


def test_sample_without_disk_counters_says_so():
    s = sysperf.sample(_reading(0.0, 0, 10), _reading(1.0, 5, 20))
    assert s["disk_read_bps"] is None and s["disk_write_bps"] is None
    assert s["disks"] == {}


def test_history_is_bounded_and_first_reading_makes_no_sample(tmp_path):
    clock = iter(range(100))

    def reader():
        t = next(clock)
        return _reading(float(t), t * 10, t * 100)

    sp = sysperf.SysPerf(history=3, reader=reader)
    assert sp.poll_once() is None
    for _ in range(5):
        assert sp.poll_once() is not None
    assert len(sp.history) == 3
    assert sp.snapshot()["latest"] is sp.history[-1]


def test_reader_failure_is_reported_not_raised():
    def reader():
        raise FileNotFoundError("/proc/stat")

    sp = sysperf.SysPerf(reader=reader)
    assert sp.poll_once() is None
    assert "FileNotFoundError" in sp.snapshot()["error"]


def test_log_writes_one_json_line_per_sample_and_rotates(tmp_path):
    clock = iter(range(1000))

    def reader():
        t = next(clock)
        return _reading(float(t), t * 10, t * 100)

    log_file = tmp_path / "logs" / "sysperf.log"
    sp = sysperf.SysPerf(reader=reader, log_file=log_file, log_max_bytes=600, log_backups=2)
    sp._open_log()
    try:
        sp.poll_once()
        first = sp.poll_once()
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[-1]) == first
        for _ in range(40):
            sp.poll_once()
    finally:
        sp._close_log()
    names = sorted(p.name for p in log_file.parent.iterdir())
    assert names == ["sysperf.log", "sysperf.log.1", "sysperf.log.2"]


def test_log_does_not_reach_the_daemon_log(tmp_path, caplog):
    sp = sysperf.SysPerf(reader=lambda: _reading(0.0, 0, 1), log_file=tmp_path / "s.log")
    sp._open_log()
    try:
        with caplog.at_level("INFO"):
            sp.tick(_reading(0.0, 0, 1))
            sp.tick(_reading(1.0, 1, 2))
    finally:
        sp._close_log()
    assert not [r for r in caplog.records if r.name.startswith("claunch.sysperf")]


def test_log_is_not_reached_through_the_logger_registry(tmp_path):
    # claunch-0yxs1: pytest's log capture attaches to every registered
    # non-propagating logger, and a name built from id(self) comes back when a
    # collector lands on a freed address. A handler put on the registry's
    # logger of the same name must not see the collector's samples.
    sp = sysperf.SysPerf(reader=lambda: _reading(0.0, 0, 1), log_file=tmp_path / "s.log")
    seen = []

    class Grab(logging.Handler):
        def emit(self, record):
            seen.append(record)

    grab = Grab()
    registered = logging.getLogger(sp._log.name)
    registered.addHandler(grab)
    sp._open_log()
    try:
        sp.tick(_reading(0.0, 0, 1))
        sp.tick(_reading(1.0, 1, 2))
    finally:
        sp._close_log()
        registered.removeHandler(grab)
        logging.root.manager.loggerDict.pop(sp._log.name, None)
    assert seen == []
    assert (tmp_path / "s.log").read_text(encoding="utf-8").strip()


def test_read_counters_on_this_machine():
    reading = sysperf.read_counters()
    assert reading["cpu_total"] > 0
    assert reading["mem_total"] > 0
    assert reading["source"] in ("psutil", "procfs", "win32")


# ---------------------------------------------------------------- capacity


def _disk(total, used):
    return SimpleNamespace(total=total, used=used, free=total - used)


def test_mount_point_of_a_directory_is_its_filesystem_root(tmp_path):
    mount = sysperf.mount_point(str(tmp_path))
    assert mount and os.path.ismount(mount)


def test_mount_point_walks_a_missing_path_to_an_existing_ancestor(tmp_path):
    assert (sysperf.mount_point(str(tmp_path / "missing" / "deeper"))
            == sysperf.mount_point(str(tmp_path)))


def test_drive_label_names_windows_drives_and_posix_mounts():
    assert sysperf.drive_label("F:\\") == "F:"
    assert sysperf.drive_label("c:") == "C:"
    assert sysperf.drive_label("/mnt/c") == "/mnt/c"
    assert sysperf.drive_label("/") == "/"


def test_disk_usage_lists_each_mount_once():
    mounts = {"/ws/a": "/", "/ws/b": "/", "/ws/c": "/mnt/d"}
    sizes = {"/": _disk(1000, 250), "/mnt/d": _disk(2000, 500)}
    rows = sysperf.disk_usage(
        ["/ws/a", "/ws/b", "/ws/c"],
        mount_of=mounts.get,
        usage_of=lambda mount: sizes[mount],
    )
    assert [row["mount"] for row in rows] == ["/", "/mnt/d"]
    assert rows[0] == {
        "mount": "/", "label": "/", "total": 1000, "used": 250, "free": 750, "pct": 25.0,
    }
    assert rows[1]["pct"] == 25.0


def test_disk_usage_skips_a_drive_it_cannot_read():
    def usage(mount):
        if mount == "/mnt/e":
            raise OSError("the drive is not connected")
        return _disk(100, 90)

    rows = sysperf.disk_usage(
        ["/a", "/b"], mount_of={"/a": "/mnt/e", "/b": "/"}.get, usage_of=usage
    )
    assert [row["mount"] for row in rows] == ["/"]
    assert rows[0]["pct"] == 90.0


def test_usage_paths_are_the_daemon_root_and_every_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(sysperf.paths, "daemon_dir", lambda: tmp_path / "daemon")
    monkeypatch.setattr(sysperf.workspaces, "list_all", lambda: [
        workspaces.Workspace("a", "/mnt/c/works/a"),
        workspaces.Workspace("b", "/mnt/d/works/b"),
    ])
    assert sysperf.usage_paths() == [
        str(tmp_path / "daemon"), "/mnt/c/works/a", "/mnt/d/works/b",
    ]


def test_usage_paths_survive_a_boardless_or_broken_config(tmp_path, monkeypatch):
    monkeypatch.setattr(sysperf.paths, "daemon_dir", lambda: tmp_path / "daemon")

    def broken():
        raise store.StoreError("cannot read config file")

    monkeypatch.setattr(sysperf.workspaces, "list_all", broken)
    assert sysperf.usage_paths() == [str(tmp_path / "daemon")]


def test_samples_carry_disk_capacity_and_reuse_it():
    clock = iter(range(100))
    calls = []

    def usage():
        calls.append(1)
        return [{"mount": "/", "label": "/", "total": 100, "used": 50, "free": 50, "pct": 50.0}]

    sp = sysperf.SysPerf(reader=lambda: _reading(float(next(clock)), 0, 1), usage=usage)
    assert sp.poll_once() is None
    point = sp.poll_once()
    assert point["usage"][0]["pct"] == 50.0
    assert sp.snapshot()["latest"]["usage"] == point["usage"]
    for _ in range(3):
        sp.poll_once()
    assert len(calls) == 1, "capacity is reused inside USAGE_TTL"


def test_usage_cache_expires_after_its_ttl():
    clock = iter(range(100))
    calls = []

    def usage():
        calls.append(1)
        return []

    sp = sysperf.SysPerf(
        reader=lambda: _reading(float(next(clock)), 0, 1), usage=usage, usage_ttl=-1
    )
    for _ in range(3):
        sp.poll_once()
    assert len(calls) == 3


def test_usage_reader_failure_leaves_the_sample_without_capacity():
    clock = iter(range(100))

    def usage():
        raise OSError("statvfs failed")

    sp = sysperf.SysPerf(reader=lambda: _reading(float(next(clock)), 0, 1), usage=usage)
    sp.poll_once()
    point = sp.poll_once()
    assert point["usage"] == []
    assert sp.snapshot()["error"] is None, "capacity is not the counter reading"


def test_api_latest_and_history(tmp_path):
    clock = iter(range(100))

    def reader():
        t = next(clock)
        return _reading(float(t), t * 10, t * 100, disks={"sda": (t * 512, 0, 0)})

    usage = [{"mount": "/", "label": "/", "total": 1000, "used": 250, "free": 750, "pct": 25.0}]
    collector = sysperf.SysPerf(
        interval=0, reader=reader, usage=lambda: usage, log_file=tmp_path / "sysperf.log"
    )
    for _ in range(3):
        collector.poll_once()

    async def run():
        app = web.Application()
        sysperf.install(app, collector=collector)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/sysperf")
            assert resp.status == 200
            latest = await resp.json()
            resp = await client.get("/api/sysperf?history=1")
            return latest, await resp.json()
        finally:
            await client.close()

    latest, full = asyncio.run(run())
    assert latest["latest"]["cpu_pct"] == 10.0
    assert latest["latest"]["usage"] == usage
    assert "history" not in latest
    assert latest["enabled"] is False  # interval 0: the collector is off
    assert len(full["history"]) == 2
    assert full["history_max"] == sysperf.HISTORY
    assert full["log"]["path"] == str(tmp_path / "sysperf.log")


def test_collector_task_samples_on_its_own(tmp_path):
    clock = iter(range(1000))

    def reader():
        t = next(clock)
        return _reading(float(t), t * 10, t * 100)

    sp = sysperf.SysPerf(interval=0.01, reader=reader, log_file=tmp_path / "s.log")

    async def run():
        sp.start()
        try:
            for _ in range(500):
                if len(sp.history) >= 2:
                    break
                await asyncio.sleep(0.01)
        finally:
            await sp.stop()

    asyncio.run(run())
    assert len(sp.history) >= 2
    assert (tmp_path / "s.log").read_text(encoding="utf-8").count("\n") >= 2


def test_daemon_default_interval():
    assert store.DAEMON_DEFAULTS["sysperf_interval_s"] == sysperf.INTERVAL == 5.0


def test_dashboard_wires_badge_page_and_route():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    page_js = (STATIC / "sysperf-page.js").read_text(encoding="utf-8")
    header = html[html.index('<aside id="sidebar">'):html.index("</header>")]
    assert re.search(r'<a id="sysperf-badge"[^>]*href="#/sysperf"', header)
    assert '<div id="sysperf-view" class="hidden"></div>' in html
    assert html.index("static/sysperf-page.js") < html.index("static/app.js")
    assert 'sysperf: "sysperf-view"' in app_js
    assert 'if (parts[0] === "sysperf") return { page: "sysperf" };' in app_js
    assert "globalThis.SysPerfPage.open()" in app_js
    assert 'if (r.page !== "sysperf") globalThis.SysPerfPage?.stop();' in app_js
    assert "/api/sysperf" in page_js
    assert "globalThis.SysPerfPage = { open, stop, startBadge, stopBadge }" in page_js
    # Disk capacity (claunch-frgt1): the badge names the fullest drive and
    # the page tabulates them.
    assert "DISK ${disk.label}" in page_js
    assert 'node("h3", "", "Disk capacity")' in page_js
    assert ".sysperf-usage" in (STATIC / "style.css").read_text(encoding="utf-8")
