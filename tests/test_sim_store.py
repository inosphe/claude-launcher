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
    # What the user reads is Korean unless the setting says otherwise.
    assert sim["settings"]["language"] == "한국어"
    assert st.configure(sim["id"], "s1", settings={"language": " English "})["settings"]["language"] == "English"
    for bad in ("", "  ", 3, "x" * 41):
        with pytest.raises(S.SimError):
            st.configure(sim["id"], "s1", settings={"language": bad})


def test_replay_and_coverage(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    st.run_round(sim["id"], "s1")
    rep = st.replay(sim["id"], "unhandled|door|open|open")
    assert rep["reproduces"] and not rep["stale"]
    # The trace: start state, then per step the event and what it changed;
    # the last step raises the finding.
    assert rep["start"]["sm"] == {"door": "closed"}
    assert rep["steps"][-1]["event"] == "open" and rep["steps"][-1]["sm"] == {"door": "open"}
    opened = next(x for x in rep["steps"] if x["changed"])
    assert opened["changed"] == {"door": ["closed", "open"]} and opened["fired"] == ["r.open"]
    assert rep["raised_at"] == len(rep["steps"])
    # A witness whose rule left the model no longer reproduces it.
    st.patch_model(sim["id"], "s1", {"remove": {"rules": ["r.open"]}})
    assert st.replay(sim["id"], "unhandled|door|open|open")["reproduces"] is False
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


# -- carry (S3b) ---------------------------------------------------------------

def test_carry_marks_rules_whose_quote_is_gone(env, tmp_path):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1", sources=["spec.md"])
    st.patch_model(sim["id"], "s1", base_patch())
    (tmp_path / "repo" / "spec.md").write_text("The door swings open when it is closed.\n", encoding="utf-8")
    res = st.start(sim["id"], "s1", round=False)
    assert res["carry"]["stale_sources"] == [{"section": "rules", "id": "r.open"}]
    assert [x["id"] for x in st.work(sim["id"])["stale"]] == ["r.open"]
    # A stale rule does not block other changes.
    other = {"rules": [{"id": "r.close", "on": {"event": "close"}, "when": {"sm": "door", "is": "open"},
                        "do": [{"sm": "door", "to": "closed"}], "provisional": {"why": "x"}}]}
    assert st.patch_model(sim["id"], "s1", other)["ok"]
    # Re-quoting it clears it.
    fix = base_patch()["rules"][0]
    fix["source"] = src("The door swings open when it is closed.")
    assert st.patch_model(sim["id"], "s1", {"rules": [fix]})["ok"]
    assert st.work(sim["id"])["stale"] == []


def test_carry_reopens_a_question_when_its_sources_changed(env, tmp_path):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1", sources=["*.md"])
    st.patch_model(sim["id"], "s1", base_patch())
    st.run_round(sim["id"], "s1")
    key = "unhandled|door|open|open"
    st.resolve(sim["id"], "s1", [{"key": key, "action": "ask", "question": "and then?"}])
    assert st.findings(sim["id"])[key]["question"]["docs"] == {"spec.md": S.source_digests(root, ["spec.md"])["spec.md"]}
    # Unchanged sources: the question stays.
    assert st.start(sim["id"], "s1", round=False)["carry"]["reopened"] == []
    st.stop(sim["id"], "s1")
    with open(tmp_path / "repo" / "spec.md", "a", encoding="utf-8") as f:
        f.write("Opening an open door does nothing.\n")
    assert st.start(sim["id"], "s1", round=False)["carry"]["reopened"] == [key]
    f = st.findings(sim["id"])[key]
    assert f["status"] == "open" and f["prev_question"]["text"] == "and then?" and "question" not in f


def test_carry_resolves_a_question_the_model_no_longer_reaches(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    st.run_round(sim["id"], "s1")
    key = "unhandled|door|open|open"
    st.resolve(sim["id"], "s1", [{"key": key, "action": "ask", "question": "and then?"}])
    st.patch_model(sim["id"], "s1", {"rules": [{"id": "r.open2", "on": {"event": "open"},
                                               "when": {"sm": "door", "is": "open"}, "do": [],
                                               "provisional": {"why": "no-op"}}]})
    assert st.start(sim["id"], "s1", round=False)["carry"]["resolved"] == [key]
    assert st.findings(sim["id"])[key]["resolution"]["by"] == "carry"


def test_create_from_another_simulation(env):
    st, root, _ = env
    a = st.create(title="a", root=root, owner="s1", actor="s1", sources=["spec.md"])
    st.patch_model(a["id"], "s1", base_patch())
    st.run_round(a["id"], "s1")
    key = "unhandled|door|open|open"
    st.resolve(a["id"], "s1", [{"key": key, "action": "ask", "question": "q"}])
    st.answer(a["id"], S.USER, key, "nothing happens")
    st.judge(a["id"], "s1", [{"id": "r.open", "verdict": "confirmed", "why": "ok"}])
    b = st.create(title="b", root=root, owner="s2", actor="s2", from_sim=a["id"],
                  settings={"max_rounds": 5})
    assert b["from"] == a["id"] and b["sources"] == ["spec.md"] and b["settings"]["max_rounds"] == 5
    assert b["model_stats"]["rules"] == 1
    assert list(st.findings(b["id"])) == [key] and st.findings(b["id"])[key]["status"] == "answered"
    assert st.unjudged(b["id"]) == []
    with pytest.raises(S.NotFound):
        st.create(title="c", root=root, owner="s1", actor="s1", from_sim="sim-zzzzzz")


def test_triage_sorts_open_findings(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    st.run_round(sim["id"], "s1")
    key = "unhandled|door|open|open"
    with pytest.raises(S.Forbidden):
        st.triage(sim["id"], "s2", [{"key": key, "verdict": "spec", "why": "x"}])
    res = st.triage(sim["id"], "s1", [{"key": key, "verdict": "intended", "why": "already open"},
                                      {"key": key, "verdict": "bogus", "why": "x"},
                                      {"key": "nope", "verdict": "spec", "why": "x"}], key="t1")
    assert res["done"] == [key] and len(res["errors"]) == 2
    f = st.findings(sim["id"])[key]
    assert f["status"] == "dismissed" and f["triage"]["verdict"] == "intended" and f["triage"]["round"] == 1
    assert st.triage(sim["id"], "s1", [], key="t1")["duplicate"]
    assert st.configure(sim["id"], "s1", settings={"triage": False})["settings"]["triage"] is False
    with pytest.raises(S.SimError):
        st.configure(sim["id"], "s1", settings={"triage": 1})


# -- edit proposals (S3d) ------------------------------------------------------

def test_edit_proposals_are_checked_and_never_applied(env, tmp_path):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    st.run_round(sim["id"], "s1")
    key = "unhandled|door|open|open"
    st.resolve(sim["id"], "s1", [{"key": key, "action": "ask", "question": "and then?"}])
    assert [f["key"] for f in st.edit_targets(sim["id"])] == [key]
    good = {"file": "spec.md", "old": "", "new": "Open decision: what pressing open on an open door does.",
            "kind": "decide", "reason": "the spec is silent", "findings": [key]}
    breaking = {"file": "spec.md", "old": "The door opens when closed.", "new": "The door slides open when closed.",
                "kind": "clarify", "reason": "wording", "findings": [key]}
    res = st.propose(sim["id"], "s1", [
        good, breaking,
        {**good, "old": "not in the file"},
        {**good, "file": "../outside.md"},
        {**good, "findings": []},
        {**good, "kind": "rewrite"},
    ], key="p1")
    assert len(res["done"]) == 2 and len(res["errors"]) == 4
    props = st.proposals(sim["id"])
    by_kind = {p["kind"]: p for p in props.values()}
    assert by_kind["decide"]["check"]["breaks"] == []
    assert by_kind["clarify"]["check"]["breaks"] == ["rule r.open"]  # its quote would be gone
    assert (tmp_path / "repo" / "spec.md").read_text(encoding="utf-8") == SPEC  # nothing applied
    assert st.edit_targets(sim["id"]) == []  # covered by a proposal
    assert st.get(sim["id"])["proposals"]["proposed"] == 2
    pid = by_kind["decide"]["id"]
    with pytest.raises(S.Forbidden):
        st.decide_proposal(sim["id"], "s1", pid, True)
    got = st.decide_proposal(sim["id"], S.USER, pid, True, "ok")
    assert got["proposal"]["status"] == "accepted" and got["owner"] == "s1"
    st.decide_proposal(sim["id"], S.USER, by_kind["clarify"]["id"], False)
    assert st.get(sim["id"])["proposals"] == {"proposed": 0, "accepted": 1, "rejected": 1}
    assert st.configure(sim["id"], "s1", settings={"max_edits": 0})["settings"]["max_edits"] == 0
    with pytest.raises(S.SimError):
        st.configure(sim["id"], "s1", settings={"max_edits": -1})


def test_coverage_separates_static_text_from_the_last_rounds_observation(env):
    st, root, _ = env
    sim = st.create(title="door", root=root, owner="s1", actor="s1")
    st.patch_model(sim["id"], "s1", base_patch())
    # Before any round: no observation file, which is not "unseen".
    cov = st.coverage(sim["id"])
    assert cov["observed_from"] is None
    assert all(c["observed"] is None for c in cov["cells"].values())
    st.run_round(sim["id"], "s1")
    cov = st.coverage(sim["id"])
    k = "door\x00closed\x00open"
    assert cov["observed_from"]["current"] and cov["observed_from"]["round"] == 1
    assert cov["cells"][k]["rule"] == "yes" and cov["cells"][k]["observed"]["handled"] > 0
    gap = "door\x00open\x00open"
    assert cov["cells"][gap]["observed"]["unhandled"] > 0 and cov["cells"][gap]["finding"] == "open"
    assert cov["counts"]["cells"] == len(cov["cells"]) and cov["counts"]["candidates"] == len(cov["cells"])
    assert 0 < cov["counts"]["observed"] <= cov["counts"]["cells"]
    # The old keys stay for the existing reader.
    assert k in cov["handled"] and cov["gaps"][gap] == "open"
    # After a model change the last round no longer counts as evidence.
    p = base_patch()
    p["rules"].append({"id": "r.close", "on": {"event": "close"}, "when": {"sm": "door", "is": "open"},
                       "do": [{"sm": "door", "to": "closed"}], "provisional": {"why": "symmetry"}})
    st.patch_model(sim["id"], "s1", p)
    cov = st.coverage(sim["id"])
    assert cov["observed_from"]["current"] is False
    assert all(c["observed"] is None for c in cov["cells"].values())
