"""Usage history samples and the per-key refresh clock (claunch-f5w4x.3).

What has to hold:
  one sample per real read -- a provider read that succeeds appends one line;
                              an answer from the cache appends nothing.
  restart                  -- a daemon with an empty cache starts from the
                              newest sample: no provider call inside the period.
  per-key timer            -- a key is due a period after its own last read;
                              a manual read starts that key's period over.
  clock                    -- a tick reads the due keys only, once each, and
                              reads nothing while switched off.
  disk                     -- bytes, samples and growth per day, observed once
                              the file spans a day and projected before.
"""

import asyncio
from types import SimpleNamespace

from claude_launcher import profile_usage, store, usage, usage_history
from claude_launcher.daemon import usage_clock


def _setup(monkeypatch, now):
    monkeypatch.setattr(profile_usage.time, "time", lambda: now[0])
    monkeypatch.setattr(profile_usage, "_entries", {})
    monkeypatch.setattr(profile_usage, "_locks", {})
    monkeypatch.setattr(
        profile_usage,
        "_resolve",
        lambda selector: (
            SimpleNamespace(selector=selector, name=selector),
            "p",
            f"{selector}/p/r",
            "r",
        ),
    )
    monkeypatch.setattr(profile_usage.lineage, "effective_harness", lambda _p: "h")
    calls = []

    def fetch(selected):
        calls.append((selected.selector, now[0]))
        return usage.UsageReport(
            windows=[usage.UsageWindow(name="five_hour", utilization=float(len(calls)), resets_at=None)],
            raw={},
            source="test",
        )

    monkeypatch.setattr(profile_usage.usage, "fetch", fetch)
    return calls


def test_each_provider_read_is_one_sample_and_a_cached_answer_is_none(monkeypatch):
    now = [1000.0]
    _setup(monkeypatch, now)

    profile_usage.fetch("work")
    now[0] += 60
    _, cached, _ = profile_usage.fetch("work")
    now[0] += 60
    profile_usage.fetch("work", manual=True)

    samples = usage_history.read("work/p/r")
    assert cached is True
    assert [s["at"] for s in samples] == [1000.0, 1120.0]
    assert [s["windows"][0]["utilization"] for s in samples] == [1.0, 2.0]
    assert samples[0]["key"] == "work/p/r" and samples[0]["source"] == "test"
    assert usage_history.last("work/p/r")["at"] == 1120.0


def test_a_failed_read_is_no_sample(monkeypatch):
    now = [1000.0]
    _setup(monkeypatch, now)

    def broken(_selected):
        raise usage.UsageError("no login")

    monkeypatch.setattr(profile_usage.usage, "fetch", broken)
    try:
        profile_usage.fetch("work")
    except usage.UsageError:
        pass
    assert usage_history.read("work/p/r") == []


def test_a_restarted_daemon_serves_the_newest_sample_until_the_period_ends(monkeypatch):
    now = [1000.0]
    calls = _setup(monkeypatch, now)
    profile_usage.fetch("work")
    monkeypatch.setattr(profile_usage, "_entries", {})  # the daemon restarted

    now[0] += 300
    payload, cached, updated_at = profile_usage.fetch("work")
    assert (cached, updated_at, len(calls)) == (True, 1000.0, 1)
    assert payload["windows"][0]["utilization"] == 1.0
    assert "at" not in payload

    now[0] = 1000.0 + profile_usage.PERIOD_SECONDS
    _, cached, _ = profile_usage.fetch("work")
    assert (cached, len(calls)) == (False, 2)


def test_each_key_is_due_a_period_after_its_own_last_read(monkeypatch):
    now = [1000.0]
    _setup(monkeypatch, now)
    rows = [
        {"profile": "a", "key": "a/p/r", "supported": True},
        {"profile": "a:claude", "key": "a/p/r", "supported": True},  # same account
        {"profile": "b", "key": "b/p/r", "supported": True},
        {"profile": "c", "key": "c", "supported": False},
    ]
    keys = lambda: [row["key"] for row in profile_usage.due(rows, now=now[0])]

    assert keys() == ["a/p/r", "b/p/r"]  # never read: due now
    profile_usage.fetch("a")
    now[0] += 600
    profile_usage.fetch("b")
    assert keys() == []
    assert profile_usage.schedule("a/p/r") == {
        "updated_at": 1000.0, "next_at": 1900.0, "period_s": profile_usage.PERIOD_SECONDS}

    now[0] = 1900.0
    assert keys() == ["a/p/r"]
    profile_usage.fetch("b", manual=True)  # Refresh: b's period starts over
    now[0] = 1600.0 + profile_usage.PERIOD_SECONDS
    assert keys() == ["a/p/r"]
    now[0] = 1900.0 + profile_usage.PERIOD_SECONDS
    assert keys() == ["a/p/r", "b/p/r"]


def test_a_tick_reads_the_due_keys_once_and_nothing_when_switched_off(monkeypatch):
    now = [1000.0]
    calls = _setup(monkeypatch, now)
    rows = [
        {"profile": "a", "key": "a/p/r", "supported": True},
        {"profile": "b", "key": "b/p/r", "supported": True},
    ]
    monkeypatch.setattr(profile_usage, "targets", lambda: rows)
    clock = usage_clock.UsageClock()

    assert sorted(asyncio.run(clock.tick())) == ["a/p/r", "b/p/r"]
    assert sorted(c[0] for c in calls) == ["a", "b"]
    assert asyncio.run(clock.tick()) == []

    now[0] += profile_usage.PERIOD_SECONDS
    clock.reading.add("b/p/r")  # a slow read still in flight
    assert asyncio.run(clock.tick()) == ["a/p/r"]

    store.set_daemon_field("usage_refresh", False)
    now[0] += profile_usage.PERIOD_SECONDS
    assert asyncio.run(clock.tick()) == []
    assert len(calls) == 3


def test_disk_stats_project_before_a_day_and_observe_after():
    key = "work/default/anthropic"
    usage_history.append(key, {"windows": []}, 0.0)
    usage_history.append(key, {"windows": []}, 900.0)
    size = usage_history.path_for(key).stat().st_size

    early = usage_history.stats(key, period=900)
    assert (early["bytes"], early["samples"], early["basis"]) == (size, 2, "projected")
    assert early["per_day_bytes"] == size / 2 * 96

    usage_history.append(key, {"windows": []}, 2 * 86400.0)
    late = usage_history.stats(key, period=900)
    size = usage_history.path_for(key).stat().st_size
    assert (late["samples"], late["basis"]) == (3, "observed")
    assert late["per_day_bytes"] == size / 2
    assert (late["first_at"], late["last_at"]) == (0.0, 2 * 86400.0)

    total = usage_history.total()
    assert (total["bytes"], total["files"]) == (size, 1)
    # The key is quoted whole: one file in the history directory, no subdirectory.
    assert usage_history.path_for(key).parent == usage_history.history_dir()


def test_a_line_cut_short_is_skipped():
    key = "k"
    usage_history.append(key, {"windows": []}, 1.0)
    with open(usage_history.path_for(key), "a", encoding="utf-8") as fh:
        fh.write('{"at": 2.0, "windo')
    assert [s["at"] for s in usage_history.read(key)] == [1.0]
    assert usage_history.last(key)["at"] == 1.0
    assert usage_history.stats(key, period=900)["samples"] == 1


def test_a_field_the_writer_adds_rides_through_unchanged():
    """A payload may carry a field of the writer's own (claunch-23r7d).

    `profile_usage` samples the digest of the launcher token a reading
    describes, so a daemon that restarts inside a key's period can tell
    whether the newest sample is still about the key the profile holds. The
    store has to hand that field back as it came — the module docstring says
    so, and a reader that dropped it would silently seed the cache with the
    departed key's numbers (claunch-vz8nj).
    """
    key = "work/default/anthropic"
    usage_history.append(key, {"windows": [], "credential": "abc123def456"}, 1.0)

    assert usage_history.last(key) == {
        "windows": [], "credential": "abc123def456", "at": 1.0}
    assert usage_history.read(key) == [usage_history.last(key)]


def test_a_full_file_rotates_and_reads_span_every_generation(monkeypatch):
    key = "fw/fireworks/fireworks-billing"
    monkeypatch.setattr(usage_history, "MAX_BYTES", 1)  # every append rotates
    monkeypatch.setattr(usage_history, "KEEP", 2)
    for at in (1.0, 2.0, 3.0, 4.0):
        usage_history.append(key, {"windows": []}, at)

    # Current + two generations; the first sample went with the third.
    names = [p.name for p in usage_history.paths(key)]
    quoted = usage_history.path_for(key).name[: -len(usage_history.SUFFIX)]
    assert names == [f"{quoted}.2.jsonl", f"{quoted}.1.jsonl", f"{quoted}.jsonl"]
    assert [s["at"] for s in usage_history.read(key)] == [2.0, 3.0, 4.0]
    assert [s["at"] for s in usage_history.read(key, since=3.0)] == [3.0, 4.0]
    assert usage_history.last(key)["at"] == 4.0

    st = usage_history.stats(key, period=900)
    assert (st["samples"], st["files"], st["first_at"]) == (3, 3, 2.0)
    assert st["bytes"] == sum(p.stat().st_size for p in usage_history.paths(key))
    assert usage_history.total()["files"] == 3


def test_a_file_under_the_limit_is_not_rotated():
    key = "k2"
    usage_history.append(key, {"windows": []}, 1.0)
    usage_history.append(key, {"windows": []}, 2.0)
    assert usage_history.paths(key) == [usage_history.path_for(key)]
