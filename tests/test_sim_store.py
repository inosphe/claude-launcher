"""The simulation registry (claude_launcher.sim.store): ownership, quote
checks, rounds and the findings ledger, idempotent submissions."""

import pytest

from claude_launcher.sim import store as S

SPEC = "The door opens when closed.\nA closed door with a key can be locked.\n"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "spec.md").write_text(SPEC, encoding="utf-8")
    live = {"s1", "s2"}
    st = S.SimStore(tmp_path / "sims", live=lambda n: n in live)
    return st, str(root), live


def src(q="The door opens when closed."):
    return [{"ref": "spec.md", "quote": q}]


def base_patch():
    return {
        "machines": [{"id": "door", "states": ["closed", "open"], "initial": "closed"}],
        "events": [{"name": "open", "kind": "actor"}, {"name": "close", "kind": "actor"}],
        "rules": [{"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
                   "do": [{"sm": "door", "to": "open"}], "source": src()}],
    }


def test_owner_only_changes_and_anyone_reads(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    assert st.patch_model(sim["id"], "s1", base_patch())["ok"]
    with pytest.raises(S.Forbidden):
        st.patch_model(sim["id"], "s2", base_patch())
    with pytest.raises(S.Forbidden):
        st.start(sim["id"], "s2")
    assert st.get(sim["id"])["model_stats"]["rules"] == 1  # reading needs no ownership
    assert st.run_round(sim["id"], S.USER)["round"] == 1  # the user may do anything


def test_quotes_are_checked_against_the_root(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    p = base_patch()
    p["rules"][0]["source"] = src("The door flies away.")
    res = st.patch_model(sim["id"], "s1", p)
    assert not res["ok"] and "word for word" in res["rejected"][0]
    p["rules"][0]["source"] = [{"ref": "../outside.md", "quote": "x"}]
    assert not st.patch_model(sim["id"], "s1", p)["ok"]
    p["rules"][0]["source"] = [{"ref": "spec.md", "quote": "The   door opens\nwhen closed."}]
    assert st.patch_model(sim["id"], "s1", p)["ok"]  # whitespace-insensitive
    p["rules"][0].pop("source")
    p["rules"][0]["provisional"] = {"why": "guess"}
    assert st.patch_model(sim["id"], "s1", p)["ok"]


def test_patch_is_idempotent_by_key(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    first = st.patch_model(sim["id"], "s1", base_patch(), key="k1")
    again = st.patch_model(sim["id"], "s1", {"remove": {"rules": ["r.open"]}}, key="k1")
    assert again["duplicate"] and again["hash"] == first["hash"]
    assert st.get(sim["id"])["model_stats"]["rules"] == 1


def test_rounds_track_new_and_resolved_findings(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    r1 = st.run_round(sim["id"], "s1")
    assert r1["new"] >= 1
    keys = {f["key"] for f in st.list_findings(sim["id"], "open")["findings"]}
    assert "unhandled|door|open|open" in keys
    fix = {"rules": [
        {"id": "r.close", "on": {"event": "close"}, "when": {"sm": "door", "is": "open"},
         "do": [{"sm": "door", "to": "closed"}], "provisional": {"why": "symmetry"}},
        {"id": "r.open2", "on": {"event": "open"}, "when": {"sm": "door", "is": "open"},
         "do": [], "provisional": {"why": "opening an open door does nothing"}},
        {"id": "r.close2", "on": {"event": "close"}, "when": {"sm": "door", "is": "closed"},
         "do": [], "provisional": {"why": "closing a closed door does nothing"}},
    ]}
    assert st.patch_model(sim["id"], "s1", fix)["ok"]
    r2 = st.run_round(sim["id"], "s1")
    assert r2["open"] == 0 and r2["resolved"] >= 1


def test_running_campaign_ends_and_says_so(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1", settings={"max_rounds": 2})
    st.patch_model(sim["id"], "s1", base_patch())
    r1 = st.start(sim["id"], "s1")
    assert "ended" not in r1 and st.get(sim["id"])["state"] == "running"
    r2 = st.run_round(sim["id"], "s1")
    assert r2["ended"]["state"] == "stopped" and "max_rounds" in r2["ended"]["reason"]
    stopped = st.stop(sim["id"], "s1")
    assert stopped["ended"] is False


def test_stop_reports_end(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    st.start(sim["id"], "s1")
    assert st.stop(sim["id"], S.USER)["ended"] is True


def test_exit_releases_and_claim_transfer(env):
    st, root, live = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    st.start(sim["id"], "s1")
    with pytest.raises(S.Conflict):
        st.claim(sim["id"], "s2")
    live.discard("s1")
    assert st.session_exited("s1") == [sim["id"]]
    got = st.get(sim["id"])
    assert got["owner"] == "" and got["state"] == "stopped"
    assert st.claim(sim["id"], "s2")["owner"] == "s2"
    with pytest.raises(S.Forbidden):
        st.transfer(sim["id"], "s2", "s1")
    with pytest.raises(S.SimError):
        st.transfer(sim["id"], S.USER, "s1")  # not running
    live.add("s3")
    assert st.transfer(sim["id"], S.USER, "s3")["owner"] == "s3"


def test_sweep_releases_dead_owners(env):
    st, root, live = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    live.discard("s1")
    assert st.sweep() == [sim["id"]]
    assert st.get(sim["id"])["owner"] == ""


def test_resolve_ask_answer(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    st.run_round(sim["id"], "s1")
    key = "unhandled|door|open|open"
    res = st.resolve(sim["id"], "s1", [{"key": key, "action": "ask", "question": "What does open do on an open door?",
                                        "options": ["nothing", "error"]},
                                       {"key": "nope", "action": "dismiss", "reason": "x"}])
    assert res["done"] == [key] and res["errors"]
    with pytest.raises(S.Forbidden):
        st.answer(sim["id"], "s1", key, "nothing")
    ans = st.answer(sim["id"], S.USER, key, "nothing")
    assert ans["owner"] == "s1" and ans["finding"]["status"] == "answered"
    # A later round that still reaches it does not reopen the answered question.
    st.run_round(sim["id"], "s1")
    assert st.findings(sim["id"])[key]["status"] == "answered"


def test_settings_validation(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1",
                    defaults={"interpreter_profile": "ds4"})
    assert sim["settings"]["interpreter_profile"] == "ds4"
    assert sim["settings"]["interpreter_reasoning"] == "low"
    got = st.configure(sim["id"], "s1", settings={"judge_profile": "glm", "judge_reasoning": "high"})
    assert got["settings"]["judge_profile"] == "glm"
    with pytest.raises(S.SimError):
        st.configure(sim["id"], "s1", settings={"judge_reasoning": "max"})
    with pytest.raises(S.SimError):
        st.configure(sim["id"], "s1", settings={"bogus": 1})


def test_replay_and_coverage(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    st.run_round(sim["id"], "s1")
    rep = st.replay(sim["id"], "unhandled|door|open|open")
    assert rep["reproduces"] and not rep["stale"]
    cov = st.coverage(sim["id"])
    assert cov["grid"][0]["machine"] == "door"
    assert any(k.endswith("\x00open") for k in cov["gaps"])


def test_list_filters(env):
    st, root, _ = env
    a = st.create(title="a", root=root, project="p1", owner="s1", actor="s1")
    st.create(title="b", root=root, project="p2", owner="s2", actor="s2")
    assert [s["id"] for s in st.list(project="p1")] == [a["id"]]
    assert [s["id"] for s in st.list(owner="s1")] == [a["id"]]
    with pytest.raises(S.NotFound):
        st.get("sim-zzzz")
