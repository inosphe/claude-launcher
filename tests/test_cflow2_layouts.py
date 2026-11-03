"""cflow v2 layout metadata (claunch-yklgu.1).

A layout is a *separate file* beside the workflow; the workflow carries only
a reference (``layout: <id>@<version>``). The compiler resolves each reference
once and snapshots the result into the chart, so a run's ``chart.json``
carries the layout it started with. Serving a run is a view model over that
snapshot -- the gds6-df2s contract B payload.

Two behaviours this file pins, because they are the ones a reader relies on:

- a block whose type the layout does not allow, and a layout that does not
  resolve, come back as placeholder blocks (the rest of the page still reads);
- neither is a compile error: a layout problem never stops a run.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher.cflow2 import frontend, layers, layouts, runtime
from claude_launcher.daemon import api as api_mod, cflow2_web
from claude_launcher.daemon.manager import SessionManager

REVIEW_LAYOUT = """\
layout: df2s.review
version: 1
api: 1
title: 검토 화면
components: [declaration_table, batch_review]
blocks:
  - type: declaration_table
    title: 선언
  - type: batch_review
    title: 검토
"""

SEED_LAYOUT = """\
layout: df2s.seed
version: 1
title: 씨앗
blocks:
  - type: declaration_table
    title: 씨앗 칸
"""

WORKFLOW = """\
cflow: 2
name: layout-demo
layout: df2s.review@1
nodes:
  seed: {title: Seed, prompt: do seed, layout: df2s.seed@1}
  review: {title: Review, prompt: do review}
body: {seq: [seed, review]}
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    v2 = d / ".claunch" / "workflows" / "v2"
    (v2 / "layouts").mkdir(parents=True)
    (v2 / "layout-demo.yaml").write_text(WORKFLOW, encoding="utf-8")
    (v2 / "layouts" / "df2s.review.yaml").write_text(REVIEW_LAYOUT, encoding="utf-8")
    (v2 / "layouts" / "df2s.seed.yaml").write_text(SEED_LAYOUT, encoding="utf-8")
    # Only this test's layers: the package's definitions are another session's
    # work in progress, and their compile state is not this test's.
    monkeypatch.setattr(layers, "package_dir", lambda: tmp_path / "no-package-v2")
    monkeypatch.setattr(layers, "_PINNED", None)
    layers._COMPILED.clear()
    monkeypatch.setattr(cflow2_web, "_LISTS", {})
    monkeypatch.setattr(cflow2_web, "_DEFS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_LISTS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_BASES", {})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.chdir(d)
    return d


def _http(fn):
    """Run ``fn(client, bearer)`` against a real app with no sessions."""
    from aiohttp.test_utils import TestClient, TestServer

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


# --------------------------------------------------------------------------- #
# references and the registry
# --------------------------------------------------------------------------- #


def test_a_reference_is_an_id_and_a_version():
    assert layouts.parse_reference("df2s.review@1") == ("df2s.review", 1)
    assert layouts.is_reference("a.b-c_1@2")
    for bad in ("df2s.review", "df2s.review@", "@1", "df2s/review@1",
                "df2s.review@x", "df2s.review@0", "../x@1", ""):
        assert not layouts.is_reference(bad), bad
        with pytest.raises(ValueError):
            layouts.parse_reference(bad)


def test_the_registry_names_each_layouts_api_and_components(proj):
    entries = {e["id"]: e for e in layouts.registry(str(proj))}
    assert set(entries) == {"df2s.review", "df2s.seed"}
    review = entries["df2s.review"]
    assert review["ref"] == "df2s.review@1"
    assert review["api"] == layouts.SUPPORTED_API
    assert review["components"] == ["declaration_table", "batch_review"]
    # no components list: the layout allows the block types it declares
    assert entries["df2s.seed"]["components"] == ["declaration_table"]
    assert sorted(layouts.refs(str(proj))) == ["df2s.review@1", "df2s.seed@1"]


def test_a_layout_is_found_by_name_in_the_nearest_layer(proj):
    global_layouts = layers.global_dir() / layouts.LAYOUT_DIR
    global_layouts.mkdir(parents=True)
    (global_layouts / "df2s.review.yaml").write_text(
        REVIEW_LAYOUT.replace("검토 화면", "전역 화면"), encoding="utf-8")
    assert layouts.find("df2s.review", str(proj)) == (
        proj / ".claunch" / "workflows" / "v2" / "layouts" / "df2s.review.yaml")
    # a path is not a name, the same rule the v2 layers apply to definitions
    assert layouts.find("../df2s.review", str(proj)) is None


def test_a_layout_file_names_its_own_id_and_version(proj, tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text(REVIEW_LAYOUT.replace("version: 1", "version: 0"), encoding="utf-8")
    with pytest.raises(layouts.LayoutError):
        layouts.load(path)
    path.write_text(REVIEW_LAYOUT.replace("components: [declaration_table, batch_review]",
                                          "components: [declaration_table]\nextra: 1"),
                    encoding="utf-8")
    with pytest.raises(layouts.LayoutError):
        layouts.load(path)


# --------------------------------------------------------------------------- #
# the compile snapshot
# --------------------------------------------------------------------------- #


def test_the_compiled_chart_snapshots_the_layout(proj):
    chart = layers.load("layout-demo", str(proj))
    assert chart.meta["layout"] == "df2s.review@1"
    assert chart.meta["node_layouts"] == {"seed": "df2s.seed@1"}
    entry = chart.meta["layouts"]["df2s.review@1"]
    assert entry["api"] == 1
    assert entry["blocks"][0] == {"type": "declaration_table", "title": "선언"}
    assert entry["title"] == "검토 화면"


def test_the_snapshot_survives_the_chart_round_trip(proj):
    chart = layers.load("layout-demo", str(proj))
    again = type(chart).from_json(chart.to_json())
    assert again.meta["layouts"] == chart.meta["layouts"]
    assert again.meta["node_layouts"] == chart.meta["node_layouts"]


def test_a_layout_edit_is_seen_by_the_next_compile(proj):
    chart = layers.load("layout-demo", str(proj))
    assert chart.meta["layouts"]["df2s.review@1"]["title"] == "검토 화면"
    path = proj / ".claunch" / "workflows" / "v2" / "layouts" / "df2s.review.yaml"
    path.write_text(REVIEW_LAYOUT.replace("검토 화면", "검토 화면 2"), encoding="utf-8")
    again = layers.load("layout-demo", str(proj))
    assert again.meta["layouts"]["df2s.review@1"]["title"] == "검토 화면 2"


def test_a_layout_reference_must_be_well_formed(proj):
    with pytest.raises(frontend.FrontendError) as exc:
        frontend.compile_text("cflow: 2\nname: x\nlayout: df2s.review\nbody: end\n",
                              resolve_layout=layouts.resolver(str(proj)))
    assert "layout" in str(exc.value)


# --------------------------------------------------------------------------- #
# the view model
# --------------------------------------------------------------------------- #


def test_the_view_model_is_the_contract_payload(proj):
    chart = layers.load("layout-demo", str(proj))
    assert layouts.view_model(chart) == {
        "layout": "df2s.review@1",
        "api": 1,
        "title": "검토 화면",
        "blocks": [
            {"type": "declaration_table", "title": "선언"},
            {"type": "batch_review", "title": "검토"},
        ],
    }
    # the node's own reference wins for that node
    assert layouts.view_model(chart, "seed") == {
        "layout": "df2s.seed@1",
        "api": 1,
        "title": "씨앗",
        "blocks": [{"type": "declaration_table", "title": "씨앗 칸"}],
    }


def test_a_block_the_layout_does_not_allow_becomes_a_placeholder(proj):
    path = proj / ".claunch" / "workflows" / "v2" / "layouts" / "df2s.review.yaml"
    path.write_text(
        REVIEW_LAYOUT.replace("  - type: batch_review\n    title: 검토\n",
                              "  - type: batch_review\n    title: 검토\n"
                              "  - type: diff\n    title: 차이\n"),
        encoding="utf-8")
    chart = layers.load("layout-demo", str(proj))
    view = layouts.view_model(chart)
    assert [b["type"] for b in view["blocks"]] == [
        "declaration_table", "batch_review", "unknown"]
    assert view["blocks"][2] == {
        "type": "unknown",
        "layout": "df2s.review@1",
        "unknown_type": "diff",
        "note": layouts.NOTE_UNKNOWN,
    }


def test_an_api_mismatch_is_a_placeholder_not_a_refusal(proj):
    path = proj / ".claunch" / "workflows" / "v2" / "layouts" / "df2s.review.yaml"
    path.write_text(REVIEW_LAYOUT.replace("api: 1", "api: 2"), encoding="utf-8")
    chart = layers.load("layout-demo", str(proj))  # still compiles
    assert chart.meta["layouts"]["df2s.review@1"]["api"] == 2
    view = layouts.view_model(chart)
    # the payload is what this build speaks; the mismatch is visible in it
    assert view["api"] == layouts.SUPPORTED_API
    assert view["blocks"] == [{
        "type": "unknown",
        "layout": "df2s.review@1",
        "unknown_type": "api:2",
        "note": layouts.NOTE_API.format(have=layouts.SUPPORTED_API, want=2),
    }]


def test_a_missing_layout_file_warns_and_serves_a_placeholder(proj):
    (proj / ".claunch" / "workflows" / "v2" / "layouts" / "df2s.review.yaml").unlink()
    chart = layers.load("layout-demo", str(proj))
    assert any("df2s.review@1" in w for w in chart.warnings)
    view = layouts.view_model(chart)
    assert view["layout"] == "df2s.review@1"
    assert view["blocks"][0]["type"] == "unknown"
    assert view["blocks"][0]["unknown_type"] == "not_found"


def test_a_step_layout_follows_the_deepest_live_node(proj):
    chart = layers.load("layout-demo", str(proj))
    state = {"inst": {"i1": {"node": "body"}, "i2": {"node": "seed"}}}
    assert layouts.active_node(chart, state) == "seed"
    assert layouts.view_model(
        chart, layouts.active_node(chart, state))["layout"] == "df2s.seed@1"
    assert layouts.active_node(chart, {"inst": {}}) is None
    # a chart with no layout at all serves an empty, well-formed payload
    plain = frontend.compile_text("cflow: 2\nname: plain\nbody: end\n")
    assert layouts.view_model(plain) == {
        "layout": None, "api": layouts.SUPPORTED_API, "title": "plain", "blocks": []}


# --------------------------------------------------------------------------- #
# the daemon's routes
# --------------------------------------------------------------------------- #


def test_the_registry_route_lists_layouts(proj):
    async def go(client, bearer):
        assert (await client.get(f"/api/cflow2/layouts?cwd={proj}")).status == 401
        data = await (await client.get(f"/api/cflow2/layouts?cwd={proj}",
                                       headers=bearer)).json()
        assert data["api"] == layouts.SUPPORTED_API
        assert {e["id"] for e in data["layouts"]} == {"df2s.review", "df2s.seed"}
        assert sorted(data["refs"]) == ["df2s.review@1", "df2s.seed@1"]

    _http(go)


def test_a_run_serves_its_layout_view_model(proj):
    cwd, scope = str(proj), "stack"
    chart = layers.load("layout-demo", cwd)
    runtime.start(chart, cwd=cwd, scope=scope, caps={})

    async def go(client, bearer):
        assert (await client.get(f"/api/cflow2/layout?cwd={cwd}&scope={scope}")).status == 401
        body = await (await client.get(
            f"/api/cflow2/layout?cwd={cwd}&scope={scope}", headers=bearer)).json()
        # the first task is live, so its own layout is the one served
        assert body["layout"] == "df2s.seed@1"
        assert body["api"] == layouts.SUPPORTED_API
        assert body["title"] == "씨앗"
        assert [b["type"] for b in body["blocks"]] == ["declaration_table"]
        missing = await client.get(
            f"/api/cflow2/layout?cwd={cwd}&scope=nope", headers=bearer)
        assert missing.status == 404

    _http(go)
