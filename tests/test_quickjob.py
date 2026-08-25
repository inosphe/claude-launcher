"""The quick-job defaults: one YAML block, read by a form, written by a form.

The dashboard's leader panel spawns a preconfigured worker, and everything
but the task comes from the ``quick_job`` block of ``~/.claunch.yaml``. Two
things must hold or the form starts lying: :mod:`quickjob` reads the block
the way every other launcher setting is read (malformed values fall back
field by field, never raise), and what ``PUT /api/quickjob`` stores is
exactly what the next ``GET`` — and the next hand-edit of the YAML — sees.
"""

from __future__ import annotations

import asyncio
import time

import yaml

from claude_launcher import quickjob, store
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}


# --------------------------------------------------------------------------- #
# the module: reading and writing the block
# --------------------------------------------------------------------------- #
def test_defaults_stand_without_a_block(home):
    """A fresh install has no quick_job block: the form gets the worker
    convention, not an empty form."""
    assert quickjob.load() == quickjob.DEFAULTS
    assert quickjob.load()["role"] == "worker"


def test_the_default_names_no_workflow_so_the_parents_pair_decides(home):
    """The one field the worker convention does NOT fill in. Which run a
    child drives is the parent's workflow's to declare
    (``default_child_cflow``); a name hard-coded here would hand the same run
    to the children of a session driving something else entirely, which is
    the bug the pair closes. Empty means the spawn inherits it."""
    assert quickjob.load()["workflow"] == ""
    # naming one is still allowed, and then it overrides the pair
    quickjob.save({"workflow": "improv-worker"})
    assert quickjob.load()["workflow"] == "improv-worker"


def test_block_overrides_field_by_field(home):
    store.update(lambda doc: doc.update(
        {"quick_job": {"role": "coder", "worktree": False}}
    ))
    block = quickjob.load()
    assert block["role"] == "coder"
    assert block["worktree"] is False
    # untouched keys keep their defaults
    assert block["workflow"] == quickjob.DEFAULTS["workflow"]
    assert block["name_prefix"] == quickjob.DEFAULTS["name_prefix"]


def test_malformed_values_fall_back_alone(home):
    """One bad field must not take the other defaults down with it — the
    block is read on a dashboard poll, and a YAML typo is a wrong default,
    not a broken dashboard."""
    store.update(lambda doc: doc.update(
        {"quick_job": {
            "role": 7,                # wrong type -> default
            "workflow": "  review ",  # stripped
            "worktree": "yes",        # not a bool -> default
            "surprise": "ignored",    # unknown -> dropped
        }}
    ))
    block = quickjob.load()
    assert block["role"] == quickjob.DEFAULTS["role"]
    assert block["workflow"] == "review"
    assert block["worktree"] is quickjob.DEFAULTS["worktree"]
    assert "surprise" not in block

    # a block that is not a mapping at all reads as the defaults
    store.update(lambda doc: doc.update({"quick_job": "worker"}))
    assert quickjob.load() == quickjob.DEFAULTS


def test_save_is_partial_and_round_trips(home, config_file):
    """Save merges over what is stored, normalises like load, and lands in
    the YAML file itself — the block a user would edit by hand."""
    quickjob.save({"role": " reviewer ", "worktree": False})
    assert quickjob.load()["role"] == "reviewer"
    assert quickjob.load()["worktree"] is False

    # a later partial save keeps the earlier field
    quickjob.save({"workflow": "review"})
    block = quickjob.load()
    assert block["role"] == "reviewer"
    assert block["workflow"] == "review"

    on_disk = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert on_disk["quick_job"]["role"] == "reviewer"
    assert on_disk["quick_job"]["workflow"] == "review"


def test_save_refuses_what_load_would_never_read(home):
    """An unknown key or a wrong type is refused, not stored: a value that
    would silently never be read again is worse than an error."""
    import pytest

    with pytest.raises(ValueError, match="unknown quick_job key"):
        quickjob.save({"roll": "worker"})
    with pytest.raises(ValueError, match="must be a string"):
        quickjob.save({"role": 7})
    with pytest.raises(ValueError, match="true or false"):
        quickjob.save({"worktree": "yes"})
    # nothing above may have half-written
    assert quickjob.load() == quickjob.DEFAULTS


# --------------------------------------------------------------------------- #
# the API: what the dashboard actually calls
# --------------------------------------------------------------------------- #
async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_api_get_and_put_agree_with_the_yaml(home, tmp_path, config_file):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            resp = await client.get("/api/quickjob", headers=BEARER)
            assert resp.status == 200
            assert (await resp.json())["quick_job"] == quickjob.DEFAULTS

            resp = await client.put(
                "/api/quickjob", headers=BEARER,
                json={"role": "reviewer", "worktree": False},
            )
            assert resp.status == 200
            block = (await resp.json())["quick_job"]
            assert block["role"] == "reviewer"
            assert block["worktree"] is False
            # what PUT reports is what GET reads next, and what the YAML holds
            resp = await client.get("/api/quickjob", headers=BEARER)
            assert (await resp.json())["quick_job"] == block
            on_disk = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            assert on_disk["quick_job"]["role"] == "reviewer"

            # refusals are 400 with the module's own message, and store nothing
            resp = await client.put(
                "/api/quickjob", headers=BEARER, json={"roll": "worker"}
            )
            assert resp.status == 400
            assert "unknown quick_job key" in (await resp.json())["error"]
            resp = await client.get("/api/quickjob", headers=BEARER)
            assert (await resp.json())["quick_job"]["role"] == "reviewer"
        finally:
            await client.close()

    asyncio.run(run())
