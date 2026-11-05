"""The test tree and the gate's run modes (claunch-ce872).

``src/claude_launcher/test_tree.py`` decides which mode ``tools/changed_tests.py``
runs in and, in ``sampling``, which tests the first pass runs and what the
second pass expands to. The cases pin the parts a reader cannot check by
running the gate once:

* the mode order: command line, then ``CLAUNCH_TEST_MODE``, then ``full``
  under ``CI``, else ``sampling`` -- and a misspelt mode is an error;
* a module no declaration names still has a node, so the tree covers the
  suite;
* the sample is a function of the seed and the node ids, every module runs at
  least one test, and importance moves the share;
* a failure expands to its node, the nodes below it and the nodes depending
  on it;
* a sampled receipt never answers for a partial run, and a partial key is the
  key every receipt on disk was filed under.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from claude_launcher import test_tree

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "changed_tests.py"


def _load():
    spec = importlib.util.spec_from_file_location("changed_tests_tt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


changed_tests = _load()


# -- mode -----------------------------------------------------------------


@pytest.mark.parametrize(
    "cli, env, want",
    [
        ("partial", {"CLAUNCH_TEST_MODE": "full", "CI": "true"}, ("partial", "cli")),
        (None, {"CLAUNCH_TEST_MODE": "Partial", "CI": "true"}, ("partial", "env")),
        (None, {"CI": "true"}, ("full", "ci")),
        (None, {"CI": "1"}, ("full", "ci")),
        (None, {"CI": "false"}, ("sampling", "default")),
        (None, {"CI": ""}, ("sampling", "default")),
        (None, {}, ("sampling", "default")),
    ],
)
def test_mode_order(cli, env, want):
    assert test_tree.resolve_mode(cli, env) == want


def test_a_misspelt_mode_is_an_error_not_a_fallback():
    with pytest.raises(ValueError, match="names no mode"):
        test_tree.resolve_mode(None, {"CLAUNCH_TEST_MODE": "ful", "CI": "true"})


def test_gate_flags_map_to_modes():
    """``--sampling``/``--no-sampling``/``--full`` are the three modes, and exclusive."""
    for flag, mode in (("--sampling", "sampling"), ("--no-sampling", "partial"), ("--full", "full")):
        assert flag in SCRIPT.read_text(encoding="utf-8")
        assert test_tree.resolve_mode(mode, {})[0] == mode
    with pytest.raises(SystemExit):
        changed_tests.main(["--full", "--no-sampling", "--list"])


def test_gate_refuses_a_misspelt_env_mode(monkeypatch, capsys):
    monkeypatch.setenv("CLAUNCH_TEST_MODE", "sample")
    assert changed_tests.main(["--list"]) == changed_tests.CANNOT_TELL
    assert "names no mode" in capsys.readouterr().out


# -- tree -----------------------------------------------------------------


DECL = {
    "nodes": [
        {
            "name": "cflow",
            "modules": ["tests/test_cflow_*.py"],
            "importance": "high",
            "children": [
                {"name": "cflow-rail", "modules": ["tests/test_cflow_rail*.py"]},
            ],
        },
        {"name": "web", "modules": ["tests/test_web_*.py"], "depends": ["cflow-rail"]},
        {"name": "gates", "modules": ["tests/test_gate.py"], "importance": "critical",
         "smoke": ["tests/test_gate.py::test_smoke*"]},
    ]
}


def test_deepest_declared_node_wins_and_undeclared_modules_are_derived():
    tree = test_tree.parse_tree(DECL)
    assert tree.node_for("tests/test_cflow_rail_x.py").name == "cflow-rail"
    assert tree.node_for("tests/test_cflow_store.py").name == "cflow"
    derived = tree.node_for("tests\\test_mesh_wire.py")
    assert (derived.name, derived.derived, derived.importance) == ("mesh", True, "normal")


def test_declaration_errors_are_named():
    with pytest.raises(ValueError, match="unknown node 'nope'"):
        test_tree.parse_tree({"nodes": [{"name": "a", "depends": ["nope"]}]})
    with pytest.raises(ValueError, match="importance"):
        test_tree.parse_tree({"nodes": [{"name": "a", "importance": "urgent"}]})
    with pytest.raises(ValueError, match="declared twice"):
        test_tree.parse_tree({"nodes": [{"name": "a"}, {"name": "a"}]})


def test_the_repository_declaration_parses_and_names_real_modules():
    tree = test_tree.load_tree(ROOT)
    on_disk = set(test_tree.repo_modules(ROOT))
    for node in tree.nodes.values():
        for pattern in node.modules:
            assert any(
                __import__("fnmatch").fnmatchcase(m, pattern) for m in on_disk
            ), f"{node.name}: {pattern} matches no test module"


def test_without_a_declaration_every_module_is_derived(tmp_path):
    assert test_tree.load_tree(tmp_path).nodes == {}


# -- sample ---------------------------------------------------------------


def _ids(module, n):
    return [f"{module}::test_{i:03d}" for i in range(n)]


def test_sample_is_deterministic_per_seed_and_moves_with_it():
    tree = test_tree.parse_tree(DECL)
    ids = _ids("tests/test_mesh_a.py", 40)
    a = test_tree.sample(ids, tree, rate=0.25, seed="s1").picked
    b = test_tree.sample(ids, tree, rate=0.25, seed="s1").picked
    c = test_tree.sample(ids, tree, rate=0.25, seed="s2").picked
    assert a == b
    assert a != c
    # first test is the representative; ceil(39 * 0.25) = 10 more
    assert ids[0] in a and len(a) == 11
    assert a == [n for n in ids if n in set(a)]   # collection order kept


def test_importance_sets_the_share():
    tree = test_tree.parse_tree(DECL)
    high = test_tree.sample(_ids("tests/test_cflow_store.py", 41), tree, rate=0.25, seed="x")
    normal = test_tree.sample(_ids("tests/test_mesh_a.py", 41), tree, rate=0.25, seed="x")
    critical = test_tree.sample(_ids("tests/test_gate.py", 41), tree, rate=0.25, seed="x")
    assert len(high.picked) == 1 + 20       # rate doubled
    assert len(normal.picked) == 1 + 10
    assert critical.complete


def test_declared_smoke_tests_are_the_representatives():
    tree = test_tree.parse_tree({"nodes": [
        {"name": "g", "modules": ["tests/test_g.py"], "smoke": ["tests/test_g.py::test_smoke*"],
         "importance": "low"},
    ]})
    ids = _ids("tests/test_g.py", 10) + ["tests/test_g.py::test_smoke_a", "tests/test_g.py::test_smoke_b"]
    picked = test_tree.sample(ids, tree, rate=0.0, seed="x").picked
    assert picked == ["tests/test_g.py::test_smoke_a", "tests/test_g.py::test_smoke_b"]


def test_every_module_runs_at_least_one_test():
    tree = test_tree.Tree()
    ids = _ids("tests/test_a.py", 3) + _ids("tests/test_b.py", 1)
    smp = test_tree.sample(ids, tree, rate=0.0, seed="x")
    assert {test_tree.module_of(n) for n in smp.picked} == {"tests/test_a.py", "tests/test_b.py"}
    assert smp.modules["tests/test_a.py"] == ("a", 1, 3)


# -- expansion ------------------------------------------------------------


def test_failure_expands_to_children_and_dependents():
    tree = test_tree.parse_tree(DECL)
    modules = [
        "tests/test_cflow_store.py", "tests/test_cflow_rail_x.py",
        "tests/test_web_rail.py", "tests/test_mesh_a.py", "tests/test_gate.py",
    ]
    nodes, mods = test_tree.expansion(
        ["tests/test_cflow_store.py::test_x"], tree, modules
    )
    # cflow failed: its child cflow-rail, and web which depends on cflow-rail
    assert nodes == ["cflow", "cflow-rail", "web"]
    assert mods == ["tests/test_cflow_rail_x.py", "tests/test_cflow_store.py", "tests/test_web_rail.py"]


def test_a_leaf_failure_does_not_widen_to_its_parent():
    tree = test_tree.parse_tree(DECL)
    nodes, mods = test_tree.expansion(
        ["tests/test_web_rail.py::test_y"], tree,
        ["tests/test_web_rail.py", "tests/test_cflow_store.py"],
    )
    assert nodes == ["web"] and mods == ["tests/test_web_rail.py"]


# -- receipts -------------------------------------------------------------


def test_partial_key_is_unchanged_and_sampling_key_differs():
    files = ["tests/test_a.py", "tests/test_b.py"]
    legacy = changed_tests.selection_key(files)
    assert changed_tests.selection_key(files, (), "") == legacy
    sampled = changed_tests.selection_key(files, (), changed_tests.sampling_variant(0.25, None))
    full = changed_tests.selection_key(files, (), "full")
    assert len({legacy, sampled, full}) == 3
    assert changed_tests.sampling_variant(0.25, None) != changed_tests.sampling_variant(0.5, None)


def test_a_sampled_receipt_does_not_answer_for_a_partial_run(tmp_path):
    import json

    files = ["tests/test_a.py"]
    variant = changed_tests.sampling_variant(0.25, None)
    path = changed_tests.receipt_path(ROOT, "t" * 40, files, tmp_path, (), variant)
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"tree": "t" * 40, "selection": files, "deselected": [],
               "exit_code": 0, "variant": variant}
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert changed_tests.find_receipt(ROOT, "t" * 40, files, tmp_path, (), variant)
    assert changed_tests.find_receipt(ROOT, "t" * 40, files, tmp_path, ()) is None
    # even under the partial key's file name, a body that says sampling is refused
    legacy = changed_tests.receipt_path(ROOT, "t" * 40, files, tmp_path, ())
    legacy.write_text(json.dumps(receipt), encoding="utf-8")
    assert changed_tests.find_receipt(ROOT, "t" * 40, files, tmp_path, ()) is None


def test_sampled_pass_hands_pytest_an_argsfile():
    cmd = changed_tests.build_command(
        ["tests/test_a.py", "tests/test_b.py"], basetemp="bt", args=["@x.args"], units=2,
    )
    assert "@x.args" in cmd and "tests/test_a.py" not in cmd
    assert cmd[cmd.index("-n") + 1] == "2"
    full = changed_tests.build_command(
        ["tests"], basetemp="bt", with_tail=True, units=changed_tests.MAX_WORKERS,
    )
    assert "-m" not in full
    assert full[full.index("-n") + 1] == str(changed_tests.MAX_WORKERS)
