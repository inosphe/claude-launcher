"""Start inputs picked from a launcher list (``options:``, claunch-iq1xc).

The declaration and the checks are the engine's (``cflow2/inputs.py``) and
run here against a stub catalog; the lists themselves are
``input_options.Catalog``'s, read from an isolated launcher home. The model
lists are held against spawn's own preset resolution for claude and codex,
the two harnesses that read a preset differently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import claude_launcher
from claude_launcher import input_options, lineage, profile, provider_spec, spawn
from claude_launcher.cflow2 import inputs
from claude_launcher.cflow2.frontend import compile_file

V2 = Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"

DECL = {
    "rp": {"type": "string", "options": {"from": "profiles"}},
    "rh": {"type": "string", "options": {"from": "harnesses", "of": "rp"}, "set_by": ["user"]},
    "dp": {"type": "string", "options": {"from": "profiles", "empty": "delegate"},
           "set_by": ["user"]},
    "dm": {"type": "string", "options": {"from": "models", "of": "dp"}, "set_by": ["user"]},
}


class Stub:
    """A catalog with fixed lists; ``work`` runs claude or codex, ``cheap``
    claude only. ``ghost`` is a profile it cannot read."""

    def __init__(self, session="work", delegate="cheap"):
        self.session, self.delegate = session, delegate

    def profiles(self):
        return ["work", "cheap", "ghost"]

    def harnesses(self, p):
        return {"work": ["claude", "codex"], "cheap": ["claude"]}.get(p)

    def models(self, p):
        return {"work": ["default", "small", "sonnet"], "cheap": ["small", "haiku"]}.get(p)

    def empty_profile(self, meaning):
        return self.delegate if meaning == "delegate" else self.session


def _schema(**extra):
    return inputs.schema({**DECL, **extra})


# --------------------------------------------------------------------------- #
# declaration
# --------------------------------------------------------------------------- #
def test_the_declaration_is_kept_with_every_key():
    sch = _schema()
    assert sch["rp"]["options"] == {"from": "profiles", "of": None, "empty": "session"}
    assert sch["rh"]["options"] == {"from": "harnesses", "of": "rp", "empty": None}
    assert sch["dp"]["options"]["empty"] == "delegate"
    assert inputs.schema({"x": {"type": "string"}})["x"]["options"] is None


@pytest.mark.parametrize("decl, why", [
    ({"x": {"type": "enum", "choices": ["a"], "options": {"from": "profiles"}}}, "string only"),
    ({"x": {"type": "string", "options": {"from": "colours"}}}, "options.from"),
    ({"x": {"type": "string", "options": {"from": "profiles", "bogus": 1}}}, "unknown options"),
    ({"x": {"type": "string", "options": {"from": "profiles", "of": "y"}}}, "applies to"),
    ({"x": {"type": "string", "options": {"from": "models", "empty": "session"}}}, "empty applies"),
    ({"x": {"type": "string", "options": {"from": "profiles", "empty": "mine"}}}, "options.empty"),
    ({"x": {"type": "string", "options": {"from": "models", "of": "nope"}}}, "not declared"),
    ({"x": {"type": "string", "options": {"from": "models", "of": "x"}}}, "itself"),
    ({"y": {"type": "string"},
      "x": {"type": "string", "options": {"from": "models", "of": "y"}}}, "not an input with"),
])
def test_a_declaration_that_does_not_hold_together_is_refused(decl, why):
    with pytest.raises(inputs.InputsError, match=why):
        inputs.schema(decl)


# --------------------------------------------------------------------------- #
# start (normalize)
# --------------------------------------------------------------------------- #
def test_without_a_catalog_the_values_are_plain_strings():
    values, errors = inputs.normalize(_schema(), {"rp": "nowhere", "rh": "x"})
    assert errors == {} and values["rp"] == "nowhere"


def test_a_profile_off_the_list_is_refused_and_its_dependent_is_not_judged():
    values, errors = inputs.normalize(_schema(), {"rp": "nowhere", "rh": "x"}, None, Stub())
    assert set(errors) == {"rp"} and "nowhere" in errors["rp"] and "work, cheap" in errors["rp"]
    assert "rp" not in values


def test_a_harness_is_checked_against_the_profile_above_it():
    sch = _schema()
    assert inputs.normalize(sch, {"rp": "work", "rh": "codex"}, None, Stub())[1] == {}
    _, errors = inputs.normalize(sch, {"rp": "cheap", "rh": "codex"}, None, Stub())
    assert "profile 'cheap' may run" in errors["rh"] and "(choices: claude)" in errors["rh"]


def test_an_empty_parent_stands_for_what_its_empty_says():
    sch = _schema()
    # rp empty -> the session's profile (work); dp empty -> the delegate setting (cheap)
    assert inputs.normalize(sch, {"rh": "codex", "dm": "haiku"}, None, Stub())[1] == {}
    _, errors = inputs.normalize(sch, {"rh": "codex"}, None, Stub(session="cheap"))
    assert "rh" in errors
    _, errors = inputs.normalize(sch, {"dm": "sonnet"}, None, Stub())
    assert "profile 'cheap' takes" in errors["dm"]


def test_a_list_the_catalog_cannot_answer_is_not_checked():
    sch = _schema()
    assert inputs.normalize(sch, {"rp": "ghost", "rh": "anything"}, None, Stub())[1] == {}
    assert inputs.normalize(sch, {"rh": "anything"}, None, Stub(session=None))[1] == {}


def test_option_lists_follow_the_values_typed_so_far():
    lists = inputs.option_lists(_schema(), {"rp": "cheap"}, Stub())
    assert lists["rp"] == {"from": "profiles", "profile": None,
                           "items": ["work", "cheap", "ghost"]}
    assert lists["rh"] == {"from": "harnesses", "profile": "cheap", "items": ["claude"]}
    assert lists["dm"] == {"from": "models", "profile": "cheap", "items": ["small", "haiku"]}
    assert inputs.option_lists(_schema(), {}, None) == {}


# --------------------------------------------------------------------------- #
# after start (settle)
# --------------------------------------------------------------------------- #
def test_a_person_writes_a_dependent_inside_its_list_only():
    sch = _schema()
    held = {"rp": "work", "rh": "", "dp": "", "dm": ""}
    assert inputs.settle(sch, held, "rh", "codex", "user", None, Stub()) == "codex"
    with pytest.raises(inputs.InputsError, match="may run"):
        inputs.settle(sch, {**held, "rp": "cheap"}, "rh", "codex", "user", None, Stub())


def test_a_parent_change_that_strands_its_dependent_is_refused():
    sch = _schema()
    held = {"rp": "", "rh": "", "dp": "work", "dm": "sonnet"}
    with pytest.raises(inputs.InputsError, match="change or clear 'dm' first"):
        inputs.settle(sch, held, "dp", "cheap", "user", None, Stub())
    # cleared first, the parent moves
    held["dm"] = inputs.settle(sch, held, "dm", "", "user", None, Stub())
    assert inputs.settle(sch, held, "dp", "cheap", "user", None, Stub()) == "cheap"
    # clearing the parent falls back to the delegate setting (cheap): sonnet is off it
    with pytest.raises(inputs.InputsError, match="change or clear 'dm' first"):
        inputs.settle(sch, {**held, "dp": "work", "dm": "sonnet"}, "dp", "", "user", None,
                      Stub())


# --------------------------------------------------------------------------- #
# the launcher's lists (input_options.Catalog)
# --------------------------------------------------------------------------- #
class _Declares:
    """A provider spec that declares models for some roles."""

    def __init__(self, models):
        self._models = models

    def model(self, role):
        value = self._models.get(role)
        if value:
            return value
        fallback = provider_spec.ROLE_FALLBACKS.get(role)
        return self.model(fallback) if fallback else None


def _codex(name="cx"):
    """A codex profile that may run codex only (``allowed_harnesses``)."""
    from claude_launcher import store

    prof = profile.create(name)
    lineage.set_harness(prof, "codex")
    store.set_profile_field(name, "allowed_harnesses", ["codex"])
    return prof


def test_the_catalog_lists_profiles_and_the_harnesses_policy_admits():
    profile.create("work")
    _codex()
    cat = input_options.Catalog()
    assert set(cat.profiles()) >= {"work", "cx"}
    assert "claude" in cat.harnesses("work")
    assert cat.harnesses("cx") == ["codex"]
    assert cat.harnesses("nowhere") is None
    assert cat.models("nowhere") is None


def test_claude_takes_every_preset_and_its_model_choices():
    profile.create("work")
    got = input_options.Catalog().models("work")
    assert got[:5] == list(provider_spec.MODEL_ROLES)
    assert {"haiku", "sonnet", "opus"} <= set(got)
    assert "luna" not in got  # a codex model choice


def test_codex_takes_only_declared_presets_and_its_model_choices(monkeypatch):
    _codex()
    got = input_options.Catalog().models("cx")
    assert not set(provider_spec.MODEL_ROLES) & set(got)
    assert "luna" in got and "sonnet" not in got
    monkeypatch.setattr(input_options.providers, "spec_for",
                        lambda prof, **kw: _Declares({"small": "gpt-mini"}))
    got = input_options.Catalog().models("cx")
    # subagent falls back to small, as the provider spec resolves it
    assert [m for m in got if m in provider_spec.MODEL_ROLES] == ["small", "subagent"]


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_model_list_is_what_spawn_resolves(monkeypatch, harness):
    """Every preset the catalog lists, spawn turns into a model; every one
    it leaves out, spawn refuses -- per harness."""
    if harness == "codex":
        prof = _codex()
        monkeypatch.setattr(input_options.providers, "spec_for",
                            lambda p, **kw: _Declares({"large": "gpt-big"}))
        monkeypatch.setattr(spawn.providers, "launch_spec",
                            lambda p, *a, **kw: _Declares({"large": "gpt-big"}))
    else:
        prof = profile.create("work")
    listed = set(input_options.Catalog().models(prof.name))
    for role in provider_spec.MODEL_ROLES:
        child = {"harness": harness, "profile": f"{prof.name}:{harness}", "model": role}
        if role in listed:
            spawn._resolve_model_preset(child)
            assert child["model"] != role or harness == "claude"
        else:
            with pytest.raises(spawn.SpawnDenied):
                spawn._resolve_model_preset(child)


def test_the_delegate_meaning_reads_the_daemon_setting():
    from claude_launcher import store

    cat = input_options.Catalog()
    assert cat.empty_profile("delegate") == store.DAEMON_DEFAULTS["delegate_profile"]
    store.set_daemon_field("delegate_profile", "cheap")
    assert input_options.Catalog().empty_profile("delegate") == "cheap"
    assert input_options.Catalog(session_profile="work:codex").empty_profile("session") == "work"


def test_the_session_profile_is_read_from_the_harness_directory():
    work = profile.create("work")
    env = {"CLAUDE_CONFIG_DIR": str(work.config_dir)}
    assert input_options.profile_from_env(env) == "work"
    cx = _codex()
    env = {"CODEX_HOME": str(cx.config_dir / "codex")}
    assert input_options.profile_from_env(env) == "cx"
    assert input_options.profile_from_env({}) is None


# --------------------------------------------------------------------------- #
# the workflow
# --------------------------------------------------------------------------- #
def test_improv_worker_v2_declares_the_four_lists():
    sch = compile_file(str(V2)).meta["inputs"]
    assert sch["reviewer_profile"]["options"]["from"] == "profiles"
    assert sch["reviewer_harness"]["options"] == {"from": "harnesses",
                                                  "of": "reviewer_profile", "empty": None}
    assert sch["delegate_profile"]["options"]["empty"] == "delegate"
    assert sch["delegate_model"]["options"] == {"from": "models", "of": "delegate_profile",
                                                "empty": None}


# --------------------------------------------------------------------------- #
# the daemon: the form's check, the run page, a person's write
# --------------------------------------------------------------------------- #
PICKER = """cflow: 2
name: picker
inputs:
  rp: {type: string, default: "", options: {from: profiles}, set_by: [user]}
  rh: {type: string, default: "", options: {from: harnesses, of: rp}, set_by: [user]}
nodes: {w: {prompt: w}}
body: w
"""


def _http(fn):
    import asyncio
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon import api as api_mod
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await fn(client, {"Authorization": "Bearer sekrit"})
        finally:
            await client.close()

    return asyncio.run(run())


@pytest.fixture
def picker(home, tmp_path, monkeypatch):
    from claude_launcher.cflow2 import layers
    from claude_launcher.daemon import api as api_mod, cflow2_web

    d = tmp_path / "proj"
    (d / ".claunch" / "workflows" / "v2").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "v2" / "picker.yaml").write_text(PICKER, encoding="utf-8")
    monkeypatch.setattr(layers, "package_dir", lambda: tmp_path / "no-package-v2")
    monkeypatch.setattr(cflow2_web, "_LISTS", {})
    monkeypatch.setattr(cflow2_web, "_DEFS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_LISTS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_BASES", {})
    from claude_launcher import store

    profile.create("work")
    store.set_profile_field("work", "allowed_harnesses", ["claude"])
    _codex()
    return d


def test_the_form_check_answers_the_lists_and_refuses_off_them(picker):
    cwd = str(picker)

    async def go(client, bearer):
        out = []
        for raw in ({"rp": "cx", "rh": "claude"}, {"rp": "cx", "rh": "codex"}):
            r = await client.post("/api/cflow2/inputs/check", headers=bearer, json={
                "cwd": cwd, "workflow": "picker", "workflow_version": 2,
                "cflow_inputs": raw, "profile": "work"})
            out.append((r.status, await r.json()))
        r = await client.post("/api/cflow/start", headers=bearer, json={
            "cwd": cwd, "scope": "p1", "workflow": "picker",
            "cflow_inputs": {"rp": "nowhere"}})
        out.append((r.status, await r.json()))
        return out

    (s1, bad), (s2, good), (s3, start) = _http(go)
    assert s1 == 200 and bad["ok"] is False and set(bad["errors"]) == {"rh"}
    assert bad["options"]["rh"] == {"from": "harnesses", "profile": "cx", "items": ["codex"]}
    assert "work" in bad["options"]["rp"]["items"]
    assert s2 == 200 and good["ok"] is True
    assert s3 == 400 and set(start["errors"]) == {"rp"}


def test_the_run_page_offers_the_lists_and_a_person_writes_inside_them(picker):
    from claude_launcher.cflow2 import runtime

    cwd = str(picker)
    chart = runtime.load_definition("picker", cwd)
    runtime.start(chart, cwd=cwd, scope="t", caps={}, driver="drv", inputs={"rp": "cx"})

    async def go(client, bearer):
        out = {}
        for key, value in (("off", "claude"), ("on", "codex")):
            r = await client.post("/api/cflow2/input", headers=bearer,
                                  json={"cwd": cwd, "scope": "t", "name": "rh", "value": value})
            out[key] = (r.status, await r.json())
        r = await client.post("/api/cflow2/input", headers=bearer,
                              json={"cwd": cwd, "scope": "t", "name": "rp", "value": "work"})
        out["strand"] = (r.status, await r.json())
        out["detail"] = await (await client.get(
            f"/api/cflow2/run?cwd={cwd}&scope=t", headers=bearer)).json()
        return out

    out = _http(go)
    assert out["off"][0] == 400 and "may run" in out["off"][1]["error"]
    assert out["on"][0] == 200 and out["on"][1]["inputs"]["rh"] == "codex"
    # work runs claude only: moving rp there would leave rh=codex off its list
    assert out["strand"][0] == 400 and "change or clear 'rh' first" in out["strand"][1]["error"]
    opts = out["detail"]["input_options"]
    assert opts["rh"] == {"from": "harnesses", "profile": "cx", "items": ["codex"]}
