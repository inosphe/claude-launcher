"""cflow v2 collections: declarations and pure state operations (docs/cflow-v2.md 2.7)."""

from __future__ import annotations

import copy
import json

import pytest

from claude_launcher.cflow2 import collections as C
from claude_launcher.cflow2.collections import CollectionError

LANDING = {
    "kind": "queue",
    "key": "issue",
    "item": {"issue": "str", "branch": "str", "tip": "str", "requested_by": "str", "note": "str?"},
    "status": {"values": ["requested", "waiting", "deferred", "landed", "rejected"],
               "initial": "requested", "settled": ["landed", "rejected"]},
    "access": {"push": ["child", "daemon"], "set": ["driver"], "set.landed": ["daemon"],
               "remove": ["driver"]},
    "env": {"TARGET": "master"},
    "watch": {"every": 60,
              "probe": ["git", "merge-base", "--is-ancestor", "$CFLOW_ITEM_TIP", "$CFLOW_TARGET"],
              "on": {0: {"set": {"status": "landed"}}}},
    "lifecycle": {"reset_at": ["finish"], "drop": "settled", "remap": {"deferred": "requested"},
                  "inherit": True},
}
SIGNALS = {"kind": "log", "item": {"name": "str"}, "access": {"push": ["driver", "peer"]},
           "lifecycle": {"inherit": True}}


def landing(**over):
    spec = copy.deepcopy(LANDING)
    spec.update(over)
    return C.normalize("landing", spec)


def item(issue="a", tip="t1", **kw):
    out = {"issue": issue, "branch": "b", "tip": tip, "requested_by": "w1"}
    out.update(kw)
    return out


def bad(spec, match, types=None, name="x"):
    with pytest.raises(CollectionError, match=match):
        C.normalize(name, spec, types)


# -- normalize ---------------------------------------------------------------


def test_normalize_landing_example():
    spec = landing()
    assert spec["name"] == "landing" and spec["kind"] == "queue" and spec["key"] == "issue"
    assert spec["item"]["note"] == {"type": "str", "optional": True}
    assert spec["item"]["issue"] == {"type": "str", "optional": False}
    assert spec["status"]["settled"] == ["landed", "rejected"]
    assert spec["access"]["set.landed"] == ["daemon"]
    assert spec["watch"]["on"] == {"0": {"set": {"status": "landed"}}}
    assert spec["lifecycle"] == {"reset_at": ["finish"], "drop": "settled",
                                 "remap": {"deferred": "requested"}, "inherit": True}
    # JSON round trip leaves it unchanged (watch.on keys are strings)
    assert json.loads(json.dumps(spec)) == spec


def test_normalize_defaults():
    spec = C.normalize("q", {"kind": "queue", "key": "k", "item": {"k": "int"}})
    assert spec["access"] == {"push": ["driver"], "set": ["driver"], "remove": ["driver"]}
    assert spec["status"] is None and spec["watch"] is None and spec["env"] == {}
    assert spec["lifecycle"] == {"reset_at": [], "drop": "settled", "remap": {}, "inherit": False}
    log = C.normalize("signals", SIGNALS)
    assert log["key"] is None and log["access"] == {"push": ["driver", "peer"]}


def test_status_initial_defaults_to_first_value():
    spec = C.normalize("q", {"kind": "queue", "key": "k", "item": {"k": "str"},
                             "status": {"values": ["a", "b"]}})
    assert spec["status"] == {"values": ["a", "b"], "initial": "a", "settled": []}


@pytest.mark.parametrize("spec,match", [
    ({"kind": "stack", "item": {"a": "str"}}, "kind must be"),
    ({"kind": "queue", "item": {"a": "str"}}, "needs key"),
    ({"kind": "queue", "key": "b", "item": {"a": "str"}}, "not an item field"),
    ({"kind": "queue", "key": "a", "item": {"a": "str?"}}, "cannot be optional"),
    ({"kind": "queue", "key": "a", "item": {"a": "list"}}, "must be str or int"),
    ({"kind": "log", "key": "a", "item": {"a": "str"}}, "only for kind: queue"),
    ({"kind": "log", "item": {}}, "non-empty"),
    ({"kind": "log", "item": {"a": "float"}}, "unknown type"),
    ({"kind": "log", "item": {"seq": "int"}}, "reserved"),
    ({"kind": "log", "item": {"a": "str"}, "bogus": 1}, "unknown key"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "status": {"values": ["x"], "initial": "y"}}, "initial"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "status": {"values": ["x"], "settled": ["z"]}}, "subset"),
    ({"kind": "log", "item": {"a": "str"}, "status": {"values": ["x"]}}, "only for kind: queue"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "access": {"push": ["robot"]}},
     "unknown principal"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "access": {"pop": ["driver"]}},
     "must be push, set"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "access": {"set.done": ["driver"]}},
     "declares no status"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "status": {"values": ["x"]},
      "access": {"set.done": ["driver"]}}, "not a declared status"),
    ({"kind": "log", "item": {"a": "str"}, "access": {"set": ["driver"]}}, "only supports push"),
    ({"kind": "log", "item": {"a": "str"}, "env": {"T": 3}}, "must be a string"),
    ({"kind": "log", "item": {"a": "str"}, "env": {"bad-name": "x"}}, "env name"),
    ({"kind": "log", "item": {"a": "str"}, "watch": {"probe": "true", "on": {0: {"remove": True}}}},
     "only for kind: queue"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "watch": {"every": 5, "probe": "true", "on": {0: {"remove": True}}}}, ">= 15"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "watch": {"probe": "test ${data.x}", "on": {0: {"remove": True}}}}, r"\$\{\.\.\.\}"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "watch": {"probe": ["test", "${x}"], "on": {0: {"remove": True}}}}, "not allowed"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "watch": {"probe": 3, "on": {0: {"remove": True}}}}, "command string"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "watch": {"probe": "true", "on": {"x": {"remove": True}}}}, "exit code"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "watch": {"probe": "true", "on": {0: {"set": {"nope": "1"}}}}}, "unknown field"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "status": {"values": ["x"]},
      "watch": {"probe": "true", "on": {0: {"set": {"status": "y"}}}}}, "not one of"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "watch": {"probe": "true", "on": {0: {"remove": False}}}}, "must be true"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"},
      "watch": {"probe": "true", "on": {0: {"set": {"a": "x"}}}}}, "key field"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "lifecycle": {"drop": "some"}},
     "drop must be"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "lifecycle": {"remap": {"x": "y"}}},
     "needs a status"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "status": {"values": ["x"]},
      "lifecycle": {"remap": {"x": "y"}}}, "not a declared status"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "lifecycle": {"inherit": "yes"}},
     "true or false"),
    ({"kind": "queue", "key": "a", "item": {"a": "str"}, "lifecycle": {"when": 1}}, "unknown key"),
])
def test_normalize_rejects(spec, match):
    bad(spec, match)


def test_errors_are_value_errors_with_the_name():
    with pytest.raises(ValueError, match="collection 'x': kind"):
        C.normalize("x", {"kind": "nope", "item": {"a": "str"}})
    with pytest.raises(CollectionError, match="name"):
        C.normalize("bad name", SIGNALS)


def test_watch_on_true_key_from_yaml11_reads_as_on():
    spec = copy.deepcopy(LANDING)
    spec["watch"] = {"probe": "true", True: {0: {"set": {"status": "landed"}}}}
    assert C.normalize("landing", spec)["watch"]["on"] == {"0": {"set": {"status": "landed"}}}


# -- type: -------------------------------------------------------------------


TYPES = {"landing-queue": dict(copy.deepcopy(LANDING), env={"TARGET": None}),
         "signals": SIGNALS}


def test_type_expands_and_env_merges_by_name():
    spec = C.normalize("landing", {"type": "landing-queue", "env": {"TARGET": "main"}}, TYPES)
    assert spec["env"] == {"TARGET": "main"} and spec["kind"] == "queue"
    assert spec["status"]["values"][0] == "requested"
    log = C.normalize("sig", {"type": "signals"}, TYPES)
    assert log["kind"] == "log" and log["name"] == "sig"


def test_type_required_env_must_be_set():
    bad({"type": "landing-queue"}, "env.TARGET is required", TYPES, name="landing")
    bad({"type": "landing-queue", "env": {"OTHER": "x"}}, "env.TARGET is required", TYPES)
    # the type itself validates with the hole left open
    C.normalize("landing-queue", TYPES["landing-queue"], allow_unset_env=True)


def test_type_other_keys_replace():
    spec = C.normalize("l", {"type": "landing-queue", "env": {"TARGET": "m"},
                             "lifecycle": {"inherit": False}}, TYPES)
    assert spec["lifecycle"]["inherit"] is False and spec["lifecycle"]["drop"] == "settled"


def test_unknown_type():
    bad({"type": "nope"}, "unknown collection type 'nope'", TYPES)
    bad({"type": "nope"}, "known: none", None)


# -- push --------------------------------------------------------------------


def test_queue_push_assigns_seq_at_by_and_initial_status():
    spec, st = landing(), None
    st = C.empty(spec)
    ev = C.apply_push(st, spec, item(), principal="child", by="w1", now=100)
    assert ev["event"] == "collection_pushed" and ev["collection"] == "landing"
    assert ev["key"] == "a" and ev["replaced"] is False and ev["seq"] == 1
    rec = st["items"][0]
    assert rec["status"] == "requested" and rec["seq"] == 1 and rec["at"] == 100 and rec["by"] == "w1"
    assert "note" not in rec


def test_queue_push_same_key_replaces_in_place_and_resets_status():
    spec = landing()
    st = C.empty(spec)
    C.apply_push(st, spec, item("a"), principal="child", by="w1", now=1)
    C.apply_push(st, spec, item("b"), principal="child", by="w2", now=2)
    C.apply_set(st, spec, "a", {"status": "waiting"}, principal="driver", by="lead", now=3)
    ev = C.apply_push(st, spec, item("a", tip="t2"), principal="child", by="w1", now=4)
    assert ev["replaced"] is True and ev["previous"]["status"] == "waiting"
    assert [r["issue"] for r in st["items"]] == ["a", "b"]
    assert st["items"][0]["tip"] == "t2" and st["items"][0]["status"] == "requested"
    assert st["seq"] == 3


def test_status_change_stamps_since_and_other_fields_do_not():
    spec = landing()
    st = C.empty(spec)
    C.apply_push(st, spec, item("a"), principal="child", by="w1", now=1)
    assert st["items"][0]["since"] == 1
    C.apply_set(st, spec, "a", {"note": "n"}, principal="driver", by="lead", now=5)
    assert st["items"][0]["since"] == 1
    C.apply_set(st, spec, "a", {"status": "waiting"}, principal="driver", by="lead", now=6)
    assert st["items"][0]["since"] == 6 and st["items"][0]["at"] == 1
    C.apply_set(st, spec, "a", {"status": "waiting"}, principal="driver", by="lead", now=9)
    assert st["items"][0]["since"] == 6
    with pytest.raises(CollectionError, match="set by the engine"):
        C.apply_set(st, spec, "a", {"since": 3}, principal="driver", by="lead", now=10)


def test_reset_remap_restamps_since_when_given_a_time():
    spec = landing()
    st = C.empty(spec)
    C.apply_push(st, spec, item("a"), principal="child", by="w1", now=1)
    C.apply_set(st, spec, "a", {"status": "deferred"}, principal="driver", by="lead", now=2)
    C.reset(st, spec, 50)
    assert st["items"][0]["status"] == "requested" and st["items"][0]["since"] == 50
    assert st["items"][0]["at"] == 1


def test_log_push_appends():
    spec = C.normalize("signals", SIGNALS)
    st = C.empty(spec)
    C.apply_push(st, spec, {"name": "cut"}, principal="driver", by="s", now=1)
    ev = C.apply_push(st, spec, {"name": "cut"}, principal="peer", by="s+stack", now=2)
    assert ev["seq"] == 2 and "key" not in ev
    assert [r["seq"] for r in st["items"]] == [1, 2]


def test_a_trigger_push_with_the_same_tid_is_a_replay():
    spec = landing()
    st = C.empty(spec)
    C.apply_push(st, spec, item("a"), principal="child", by="w1", now=1, tid="r/t1:0")
    assert st["items"][0]["_tid"] == "r/t1:0"
    C.apply_set(st, spec, "a", {"status": "deferred"}, principal="driver", by="lead", now=2)
    before = [dict(r) for r in st["items"]]
    ev = C.apply_push(st, spec, item("a", tip="t2"), principal="child", by="w1", now=3,
                      tid="r/t1:0")
    assert ev["replay"] is True and st["items"] == before and st["seq"] == 1
    # another trigger (or no trigger) pushing the same key is a new request
    C.apply_push(st, spec, item("a", tip="t2"), principal="child", by="w1", now=4, tid="r/t2:0")
    assert st["items"][0]["status"] == "requested" and st["items"][0]["_tid"] == "r/t2:0"

    log = C.normalize("signals", SIGNALS)
    ls = C.empty(log)
    C.apply_push(ls, log, {"name": "cut"}, principal="driver", by="s", now=1, tid="r/t1:0")
    ev = C.apply_push(ls, log, {"name": "cut"}, principal="driver", by="s", now=2, tid="r/t1:0")
    assert ev["replay"] is True and len(ls["items"]) == 1
    C.apply_push(ls, log, {"name": "cut"}, principal="driver", by="s", now=3, tid="r/t1:1")
    assert len(ls["items"]) == 2


@pytest.mark.parametrize("it,principal,match", [
    (item(), "driver", "access denied"),
    (item(), "alien", "unknown principal"),
    ({"issue": "a"}, "child", "missing required"),
    (dict(item(), extra=1), "child", "unknown field"),
    (dict(item(), tip=3), "child", "must be str"),
    (dict(item(), status="landed"), "child", "status is set by the engine"),
    (dict(item(), seq=4), "child", "set by the engine"),
    ("x", "child", "must be a mapping"),
])
def test_push_rejects_without_change(it, principal, match):
    spec = landing()
    st = C.empty(spec)
    before = copy.deepcopy(st)
    with pytest.raises(CollectionError, match=match):
        C.apply_push(st, spec, it, principal=principal, by="w", now=1)
    assert st == before


def test_field_types():
    spec = C.normalize("q", {"kind": "queue", "key": "k", "item": {
        "k": "int", "b": "bool?", "l": "list?", "m": "map?", "a": "any?"}})
    st = C.empty(spec)
    C.apply_push(st, spec, {"k": 1, "b": True, "l": [1], "m": {"x": 1}, "a": None},
                 principal="driver", by="d", now=0)
    with pytest.raises(CollectionError, match="must be int"):
        C.apply_push(st, spec, {"k": True}, principal="driver", by="d", now=0)
    with pytest.raises(CollectionError, match="must be bool"):
        C.apply_push(st, spec, {"k": 2, "b": 1}, principal="driver", by="d", now=0)
    with pytest.raises(CollectionError, match="must be map"):
        C.apply_push(st, spec, {"k": 2, "m": []}, principal="driver", by="d", now=0)


# -- set / remove ------------------------------------------------------------


def _filled():
    spec = landing()
    st = C.empty(spec)
    C.apply_push(st, spec, item("a"), principal="child", by="w1", now=1)
    return spec, st


def test_set_status_uses_set_dot_value_over_set():
    spec, st = _filled()
    with pytest.raises(CollectionError, match=r"access denied.*set\.landed"):
        C.apply_set(st, spec, "a", {"status": "landed"}, principal="driver", by="d", now=2)
    ev = C.apply_set(st, spec, "a", {"status": "landed"}, principal="daemon", by="clock", now=2)
    assert ev["event"] == "collection_set" and ev["before"] == {"status": "requested"}
    assert st["items"][0]["status"] == "landed"
    # set.landed does not open other values to the daemon
    with pytest.raises(CollectionError, match="access denied"):
        C.apply_set(st, spec, "a", {"status": "waiting"}, principal="daemon", by="c", now=3)


def test_set_fields_and_clear_optional():
    spec, st = _filled()
    C.apply_set(st, spec, "a", {"note": "hi", "status": "deferred"}, principal="driver", by="d", now=2)
    assert st["items"][0]["note"] == "hi" and st["items"][0]["status"] == "deferred"
    C.apply_set(st, spec, "a", {"note": None}, principal="driver", by="d", now=3)
    assert "note" not in st["items"][0]


@pytest.mark.parametrize("key,fields,match", [
    ("a", {"status": "gone"}, "not one of"),
    ("a", {"issue": "b"}, "key field"),
    ("a", {"tip": None}, "required"),
    ("a", {"seq": 3}, "set by the engine"),
    ("a", {}, "non-empty"),
    ("zz", {"status": "waiting"}, "no record"),
])
def test_set_rejects_without_change(key, fields, match):
    spec, st = _filled()
    before = copy.deepcopy(st)
    with pytest.raises(CollectionError, match=match):
        C.apply_set(st, spec, key, fields, principal="driver", by="d", now=2)
    assert st == before


def test_set_on_queue_without_status():
    spec = C.normalize("q", {"kind": "queue", "key": "k", "item": {"k": "str"}})
    st = C.empty(spec)
    C.apply_push(st, spec, {"k": "x"}, principal="driver", by="d", now=0)
    with pytest.raises(CollectionError, match="declares no status"):
        C.apply_set(st, spec, "x", {"status": "a"}, principal="driver", by="d", now=0)


def test_remove():
    spec, st = _filled()
    with pytest.raises(CollectionError, match="access denied"):
        C.apply_remove(st, spec, "a", principal="child", by="w", now=2)
    ev = C.apply_remove(st, spec, "a", principal="driver", by="d", now=2)
    assert ev["event"] == "collection_removed" and ev["item"]["issue"] == "a"
    assert st["items"] == []
    with pytest.raises(CollectionError, match="no record"):
        C.apply_remove(st, spec, "a", principal="driver", by="d", now=3)


def test_set_and_remove_refuse_a_log():
    spec = C.normalize("signals", SIGNALS)
    st = C.empty(spec)
    C.apply_push(st, spec, {"name": "x"}, principal="driver", by="d", now=0)
    with pytest.raises(CollectionError, match="only for a queue"):
        C.apply_set(st, spec, 1, {"name": "y"}, principal="driver", by="d", now=0)
    with pytest.raises(CollectionError, match="only for a queue"):
        C.apply_remove(st, spec, 1, principal="driver", by="d", now=0)


# -- access_ok ---------------------------------------------------------------


def test_access_ok():
    spec = landing()
    assert C.access_ok(spec, "push", "child") and not C.access_ok(spec, "push", "driver")
    assert C.access_ok(spec, "set", "driver") and C.access_ok(spec, "set", "driver", "waiting")
    assert not C.access_ok(spec, "set", "driver", "landed")
    assert C.access_ok(spec, "set", "daemon", "landed")
    log = C.normalize("signals", SIGNALS)
    assert not C.access_ok(log, "set", "driver") and C.access_ok(log, "push", "peer")


# -- reset / inherit ---------------------------------------------------------


def _mixed():
    spec = landing()
    st = C.empty(spec)
    for k in "abcd":
        C.apply_push(st, spec, item(k), principal="child", by="w", now=0)
    C.apply_set(st, spec, "a", {"status": "landed"}, principal="daemon", by="c", now=1)
    C.apply_set(st, spec, "b", {"status": "deferred"}, principal="driver", by="d", now=1)
    C.apply_set(st, spec, "c", {"status": "rejected"}, principal="driver", by="d", now=1)
    return spec, st


def test_reset_drops_settled_and_remaps():
    spec, st = _mixed()
    summary = C.reset(st, spec)
    assert summary["event"] == "collection_reset"
    assert summary["dropped"] == ["a", "c"] and summary["kept"] == ["b", "d"]
    assert summary["remapped"] == {"b": ["deferred", "requested"]}
    assert [(r["issue"], r["status"]) for r in st["items"]] == [("b", "requested"), ("d", "requested")]
    assert st["seq"] == 4


@pytest.mark.parametrize("drop,kept", [("all", []), ("none", ["a", "b", "c", "d"])])
def test_reset_drop_modes(drop, kept):
    spec, st = _mixed()
    spec["lifecycle"]["drop"] = drop
    C.reset(st, spec)
    assert [r["issue"] for r in st["items"]] == kept


def test_reset_log_keeps_seq_for_cursors():
    spec = C.normalize("signals", dict(SIGNALS, lifecycle={"drop": "all"}))
    st = C.empty(spec)
    C.apply_push(st, spec, {"name": "x"}, principal="driver", by="d", now=0)
    summary = C.reset(st, spec)
    assert summary["dropped"] == [1] and st["items"] == [] and st["seq"] == 1
    C.apply_push(st, spec, {"name": "y"}, principal="driver", by="d", now=0)
    assert st["items"][0]["seq"] == 2


def test_inherit_cleans_up_and_does_not_touch_the_old_state():
    spec, st = _mixed()
    old = copy.deepcopy(st)
    new = C.inherit(st, spec)
    assert st == old
    assert [r["issue"] for r in new["items"]] == ["b", "d"] and new["seq"] == 4
    new2, summary = C.inherit_report(st, spec)
    assert new2 == new and summary["event"] == "collection_inherited" and summary["inherited"]


def test_inherit_keeps_records_a_trigger_push_marked_with_tid():
    spec = landing(lifecycle={"inherit": True, "drop": "none"})
    st = C.empty(spec)
    C.apply_push(st, spec, item("a"), principal="child", by="w1", now=1, tid="r/t1:0")
    C.apply_push(st, spec, item("b"), principal="child", by="w1", now=2)
    new, summary = C.inherit_report(st, spec)
    assert [r["issue"] for r in new["items"]] == ["a", "b"] and summary["invalid"] == []
    assert new["items"][0]["_tid"] == "r/t1:0"
    # the replay guard still sees the inherited marker
    ev = C.apply_push(new, spec, item("a"), principal="child", by="w1", now=3, tid="r/t1:0")
    assert ev.get("replay")


def test_inherit_off_or_kind_change_starts_empty():
    spec, st = _mixed()
    spec["lifecycle"]["inherit"] = False
    assert C.inherit(st, spec) == C.empty(spec)
    log = C.normalize("landing", SIGNALS)
    assert C.inherit(st, log) == C.empty(log)
    assert C.inherit(None, landing()) == C.empty(landing())


def test_inherit_drops_records_the_new_declaration_rejects():
    spec, st = _mixed()
    new_spec = landing(item={"issue": "str", "branch": "str", "tip": "str",
                             "requested_by": "str", "extra": "str"},
                       status={"values": ["requested", "deferred", "landed"],
                               "initial": "requested", "settled": ["landed"]},
                       access={"push": ["child"]},
                       lifecycle={"inherit": True, "drop": "none"})
    new, summary = C.inherit_report(st, new_spec)
    assert new["items"] == [] and sorted(summary["invalid"]) == ["a", "b", "c", "d"]
    new_spec2 = landing(status={"values": ["requested", "deferred", "landed"],
                                "initial": "requested", "settled": ["landed"]},
                        access={"push": ["child"]},
                        lifecycle={"inherit": True, "drop": "none"})
    new = C.inherit(st, new_spec2)
    # 'rejected' is no longer a status: back to initial
    assert {r["issue"]: r["status"] for r in new["items"]}["c"] == "requested"


# -- reads -------------------------------------------------------------------


def test_items_count_fresh():
    spec, st = _mixed()
    assert [r["issue"] for r in C.items(st)] == ["a", "b", "c", "d"]
    assert [r["issue"] for r in C.items(st, "deferred")] == ["b"]
    assert C.count(st) == 4 and C.count(st, "landed") == 1 and C.count(None) == 0
    got = C.items(st)
    got[0]["issue"] = "mutated"
    assert st["items"][0]["issue"] == "a"
    assert [r["issue"] for r in C.unsettled(st, spec)] == ["b", "d"]

    log = C.normalize("signals", SIGNALS)
    ls = C.empty(log)
    assert not C.fresh(ls, None)
    C.apply_push(ls, log, {"name": "cut"}, principal="driver", by="d", now=0)
    C.apply_push(ls, log, {"name": "landed"}, principal="driver", by="d", now=0)
    assert C.fresh(ls, 0) and C.fresh(ls, 1) and not C.fresh(ls, 2)
    assert C.fresh(ls, 0, "cut") and not C.fresh(ls, 1, "cut") and C.fresh(ls, 1, "landed")
    assert C.end_cursor(ls) == 2


def test_watch_action():
    spec = landing()
    assert C.watch_action(spec, 0) == {"set": {"status": "landed"}}
    assert C.watch_action(spec, 1) is None


# -- addresses ---------------------------------------------------------------


@pytest.mark.parametrize("addr,want", [
    ("landing", ("self", "landing")),
    ("self:landing", ("self", "landing")),
    ("parent:landing", ("parent", "landing")),
    ("main:signals", ("main", "signals")),
    ("side:stack:signals", ("side:stack", "signals")),
])
def test_parse_address(addr, want):
    assert C.parse_address(addr) == want


@pytest.mark.parametrize("addr", ["", "a:b", "side::x", "x:y:z", "parent:", 3, "parent:bad name"])
def test_parse_address_rejects(addr):
    with pytest.raises(CollectionError):
        C.parse_address(addr)
