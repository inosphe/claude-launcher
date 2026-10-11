"""The world-model walk (claude_launcher.sim): expressions, model checks,
step semantics, exploration findings, witness replay."""

from claude_launcher.sim import engine, expr, model as M

SRC = [{"ref": "spec.md", "quote": "x"}]


def door(**extra):
    m = {
        "machines": [{"id": "door", "states": ["closed", "open", "locked"], "initial": "closed"}],
        "vars": [{"name": "keys", "init": 0, "domain": [0, 1]}],
        "events": [{"name": n, "kind": "actor"} for n in ("open", "close", "lock", "unlock")],
        "rules": [
            {"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
             "do": [{"sm": "door", "to": "open"}], "source": SRC},
            {"id": "r.close", "on": {"event": "close"}, "when": {"sm": "door", "is": "open"},
             "do": [{"sm": "door", "to": "closed"}], "source": SRC},
            {"id": "r.lock", "on": {"event": "lock"},
             "when": {"all": [{"sm": "door", "is": "closed"}, {"var": "keys", "gt": 0}]},
             "do": [{"sm": "door", "to": "locked"}], "source": SRC},
            {"id": "r.unlock", "on": {"event": "unlock"}, "when": {"sm": "door", "is": "locked"},
             "do": [{"sm": "door", "to": "closed"}], "provisional": {"why": "the spec is silent"}},
        ],
        "invariants": [], "checkpoints": {},
    }
    for k, v in extra.items():
        m[k] = v
    return m


def test_three_valued_conditions():
    env = expr.Env({"n": 3}, {"m": "a"})
    assert expr.eval_cond({"all": [{"var": "n", "gt": 1}, {"prose": "x"}]}, env) == expr.UNKNOWN
    assert expr.eval_cond({"all": [{"var": "n", "gt": 5}, {"prose": "x"}]}, env) is False
    assert expr.eval_cond({"any": [{"sm": "m", "is": ["a", "b"]}, {"prose": "x"}]}, env) is True
    assert expr.eval_cond({"not": {"prose": "x"}}, env) == expr.UNKNOWN
    assert expr.eval_value({"op": "+", "args": [{"var": "n"}, 2]}, env) == 5


def test_check_model_wants_sources_or_provisional():
    m = door()
    assert M.check_model(m) == []
    m["rules"][0].pop("source")
    problems = M.check_model(m)
    assert any("needs a source" in p for p in problems)


def test_check_model_catches_unknown_names():
    m = door()
    m["rules"][0]["do"] = [{"sm": "door", "to": "ajar"}]
    m["rules"][1]["on"] = {"event": "slam"}
    problems = M.check_model(m)
    assert any("no state ajar" in p for p in problems)
    assert any("slam is not a declared event" in p for p in problems)


def test_step_applies_matching_rule_and_reports_cell_gap():
    walk = engine.prepare(door())
    s = M.initial_state(walk)
    r = engine.step(walk, s, "open")
    assert r["fired"] == ["r.open"] and r["state"]["sm"]["door"] == "open"
    r2 = engine.step(walk, r["state"], "open")
    assert [f["key"] for f in r2["findings"]] == ["unhandled|door|open|open"]


def test_conflict_applies_neither():
    m = door()
    m["rules"].append({"id": "r.open2", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
                       "do": [{"sm": "door", "to": "locked"}], "source": SRC})
    walk = engine.prepare(m)
    r = engine.step(walk, M.initial_state(walk), "open")
    assert r["fired"] == []
    assert r["findings"][0]["kind"] == "conflict"
    assert r["state"]["sm"]["door"] == "closed"


def test_priority_breaks_the_tie():
    m = door()
    m["rules"].append({"id": "r.open2", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
                       "do": [{"sm": "door", "to": "locked"}], "priority": 1, "source": SRC})
    walk = engine.prepare(m)
    r = engine.step(walk, M.initial_state(walk), "open")
    assert r["fired"] == ["r.open2"] and not r["findings"]


def test_invariant_and_emit_chain():
    m = door(vars=[{"name": "opens", "init": 0}],
             events=[{"name": "open", "kind": "actor"}, {"name": "close", "kind": "actor"},
                     {"name": "count", "kind": "system"}],
             invariants=[{"id": "at-most-one", "holds": {"var": "opens", "le": 1}, "source": SRC}])
    m["rules"] = [
        {"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
         "do": [{"sm": "door", "to": "open"}, {"emit": "count"}], "source": SRC},
        {"id": "r.count", "on": {"event": "count"}, "do": [{"add": "opens", "value": 1}], "source": SRC},
        {"id": "r.close", "on": {"event": "close"}, "when": {"sm": "door", "is": "open"},
         "do": [{"sm": "door", "to": "closed"}], "source": SRC},
    ]
    walk = engine.prepare(m)
    s = engine.step(walk, M.initial_state(walk), "open")["state"]
    assert s["vars"]["opens"] == 1
    s = engine.step(walk, s, "close")["state"]
    r = engine.step(walk, s, "open")
    assert any(f["key"] == "invariant|at-most-one" for f in r["findings"])


def test_loop_is_cut():
    m = door(events=[{"name": "ping", "kind": "actor"}])
    m["rules"] = [{"id": "r.ping", "on": {"event": "ping"}, "do": [{"emit": "ping"}], "source": SRC}]
    walk = engine.prepare(m)
    r = engine.step(walk, M.initial_state(walk), "ping")
    assert any(f["kind"] == "loop" for f in r["findings"])


def test_explore_is_deterministic_and_witnesses_replay():
    walk = engine.prepare(door())
    a = engine.explore(walk, seed=7, budget=300)
    b = engine.explore(engine.prepare(door()), seed=7, budget=300)
    assert a["fired"] == b["fired"]
    assert sorted(p["finding"]["key"] for p in a["problems"]) == sorted(p["finding"]["key"] for p in b["problems"])
    assert a["problems"]
    for p in a["problems"]:
        assert engine.reproduces(walk, p["witness"], p["finding"]["key"])


def test_input_domain_reaches_the_guarded_branch():
    walk = engine.prepare(door())
    r = engine.explore(walk, seed=3, budget=400)
    assert r["fired"].get("r.lock", 0) > 0  # keys=1 start combination
    # A finding reached after the guessed unlock rule fired says it leans on it.
    assert any("r.unlock" in p["provisional"] for p in r["problems"])


def test_unknown_guard_is_assumed_and_recorded():
    m = door()
    m["rules"][0]["when"] = {"all": [{"sm": "door", "is": "closed"}, {"prose": "not raining"}]}
    walk = engine.prepare(m)
    r = engine.explore(walk, seed=5, budget=200)
    assert r["assumptions"] > 0
    with_assume = [p for p in r["problems"] if any("assume" in s for s in p["witness"]["steps"])]
    assert with_assume
    for p in with_assume:
        assert engine.reproduces(walk, p["witness"], p["finding"]["key"])


def test_stuck_when_nothing_can_happen():
    m = door(events=[{"name": "open", "kind": "actor",
                      "available": {"sm": "door", "is": "closed"}}])
    m["rules"] = [{"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
                   "do": [{"sm": "door", "to": "open"}], "source": SRC}]
    walk = engine.prepare(m)
    r = engine.explore(walk, seed=1, budget=50)
    assert any(p["finding"]["kind"] == "stuck" for p in r["problems"])


def test_availability_rule_folds_into_event():
    m = door()
    m["rules"].append({"id": "a.unlock", "kind": "available", "on": {"event": "unlock"},
                       "when": {"sm": "door", "is": "locked"}, "do": [], "source": SRC})
    walk = engine.prepare(m)
    s = M.initial_state(walk)
    assert "unlock" not in engine.available_events(walk, s)


def test_timers_and_wait():
    m = door(events=[{"name": "open", "kind": "actor"}, {"name": "autoclose", "kind": "system"}])
    m["rules"] = [
        {"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
         "do": [{"sm": "door", "to": "open"}, {"schedule": "autoclose", "in": 5}], "source": SRC},
        {"id": "r.auto", "on": {"event": "autoclose"}, "when": {"sm": "door", "is": "open"},
         "do": [{"sm": "door", "to": "closed"}], "source": SRC},
    ]
    walk = engine.prepare(m)
    s = engine.step(walk, M.initial_state(walk), "open")["state"]
    assert engine.WAIT in engine.available_events(walk, s)
    r = engine.step(walk, s, engine.WAIT)
    assert r["state"]["sm"]["door"] == "closed" and r["state"]["clock"] == 5


def test_apply_patch_upserts_and_removes():
    m = M.apply_patch(door(), {"rules": [{"id": "r.open", "on": {"event": "open"}, "do": [], "source": SRC}],
                               "remove": {"rules": ["r.close"]}, "checkpoints": {"locked": {"sm": {"door": "locked"}}}})
    ids = [r["id"] for r in m["rules"]]
    assert "r.close" not in ids and m["rules"][ids.index("r.open")]["do"] == []
    assert m["checkpoints"]["locked"]["sm"]["door"] == "locked"
