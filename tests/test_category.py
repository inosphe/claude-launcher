from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from claude_launcher import category as c, glossary, workspaces


@pytest.fixture
def ws(home, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return workspaces.add(str(root), "alpha")


def S(name):
    return {"kind": "session", "ref": name}


def I(ident):
    return {"kind": "issue", "ref": ident}


def node(ws, label, parent=None, **kw):
    return c.create_node(ws, {"label": label, "parent_id": parent, **kw}, "user")


def place(ws, item):
    return c.where(ws, item["kind"], item["ref"])["node"]


def test_workspace_isolation_and_separate_from_glossary(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    other = workspaces.add(str(other), "beta")
    node(ws, "web ui")
    assert c.tree(other)["nodes"] == []
    assert c.db_path(ws) != glossary.db_path(ws)
    assert c.db_path(ws).parent.name == "categories"
    assert c.workspace(cwd=str(tmp_path / "repo" / ".claude" / "worktrees" / "x")) == ws
    with pytest.raises(c.CategoryError) as exc:
        c.workspace(cwd=str(tmp_path / "nowhere"))
    assert exc.value.status == 404


def test_vocabulary_is_unique_across_the_tree(ws):
    web = node(ws, "Web UI", aliases=["dashboard"])
    with pytest.raises(c.CategoryError) as exc:
        node(ws, "web-ui", parent=web["id"])
    assert exc.value.status == 409
    assert exc.value.extra["candidates"][0]["id"] == web["id"]
    with pytest.raises(c.CategoryError):
        node(ws, "Dashboard")
    tabs = node(ws, "tabs", parent=web["id"])
    assert {tuple(n["path"]) for n in c.tree(ws)["nodes"]} == {("Web UI",), ("Web UI", "tabs")}
    renamed = c.update_node(ws, tabs["id"], {"label": "Tab routing", "aliases": ["tabs"]}, "user")
    assert renamed["label"] == "Tab routing" and renamed["aliases"] == ["tabs"]


def test_move_refuses_cycles_and_delete_moves_items_up(ws):
    a = node(ws, "daemon")
    b = node(ws, "restart", parent=a["id"])
    with pytest.raises(c.CategoryError):
        c.update_node(ws, a["id"], {"parent_id": b["id"]})
    with pytest.raises(c.CategoryError):
        c.update_node(ws, a["id"], {"parent_id": a["id"]})
    c.assign(ws, [S("s1")], b["id"], "user")
    with pytest.raises(c.CategoryError) as exc:
        c.delete_node(ws, a["id"])
    assert exc.value.status == 409
    c.delete_node(ws, b["id"], "user")
    assert place(ws, S("s1"))["id"] == a["id"]
    c.delete_node(ws, a["id"], "user")
    assert place(ws, S("s1")) is None


def test_an_item_sits_in_one_node(ws):
    a, b = node(ws, "beads"), node(ws, "mesh")
    c.assign(ws, [S("s1"), I("claunch-1")], a["id"], "user")
    c.assign(ws, [S("s1")], b["id"], "user", "moved")
    items = c.tree(ws)["items"]
    assert sorted((i["ref"], i["node_id"]) for i in items) == sorted(
        [("s1", b["id"]), ("claunch-1", a["id"])])
    assert [i["ref"] for i in c.items_of(ws, a["id"])] == ["claunch-1"]
    c.unassign(ws, [S("s1")])
    assert place(ws, S("s1")) is None


def test_items_of_includes_descendants(ws):
    a = node(ws, "web")
    b = node(ws, "category tab", parent=a["id"])
    c.assign(ws, [S("s1")], a["id"])
    c.assign(ws, [S("s2")], b["id"])
    assert {i["ref"] for i in c.items_of(ws, a["id"])} == {"s1", "s2"}
    assert [i["ref"] for i in c.items_of(ws, a["id"], descendants=False)] == ["s1"]
    assert c.items_of(ws, b["id"])[0]["path"] == ["web", "category tab"]


def test_extract_moves_selected_items_into_a_new_child(ws):
    a = node(ws, "web")
    inner = node(ws, "beads page", parent=a["id"])
    outside = node(ws, "cflow")
    c.assign(ws, [S("s1"), S("s2"), S("s3")], a["id"])
    c.assign(ws, [S("s4")], inner["id"])
    c.assign(ws, [S("s5")], outside["id"])
    with pytest.raises(c.CategoryError):
        c.extract(ws, a["id"], [S("s1"), S("s5")], {"label": "routing"})
    out = c.extract(ws, a["id"], [S("s1"), S("s4")], {"label": "routing"}, "user")
    assert out["node"]["parent_id"] == a["id"]
    assert out["node"]["path"] == ["web", "routing"]
    assert place(ws, S("s1"))["id"] == out["node"]["id"]
    assert place(ws, S("s4"))["id"] == out["node"]["id"]
    assert place(ws, S("s2"))["id"] == a["id"]


def test_merge_items_goes_under_the_lowest_common_ancestor(ws):
    root = node(ws, "web")
    left = node(ws, "beads page", parent=root["id"])
    right = node(ws, "sims page", parent=root["id"])
    deep = node(ws, "sim pane", parent=right["id"])
    c.assign(ws, [S("a")], left["id"])
    c.assign(ws, [S("b")], deep["id"])
    out = c.merge(ws, {"items": [S("a"), S("b")], "label": "split panes"}, "user")
    assert out["node"]["parent_id"] == root["id"]
    assert place(ws, S("a"))["id"] == out["node"]["id"]
    # An unassigned item counts as the root.
    out2 = c.merge(ws, {"items": [S("a"), S("z")], "label": "loose"}, "user")
    assert out2["node"]["parent_id"] is None


def test_merge_folds_nodes_and_keeps_their_vocabulary(ws):
    web = node(ws, "web")
    one = node(ws, "glossary page", parent=web["id"], aliases=["용어집"])
    two_parent = node(ws, "cli")
    two = node(ws, "glossary cli", parent=two_parent["id"])
    child = node(ws, "composer", parent=one["id"])
    c.assign(ws, [S("s1")], one["id"])
    c.assign(ws, [S("s2")], two["id"])
    out = c.merge(ws, {"nodes": [one["id"], two["id"]], "label": "glossary"}, "user")
    merged = out["node"]
    assert merged["parent_id"] is None  # lca(web, cli) is the root
    assert set(merged["aliases"]) == {"glossary page", "용어집", "glossary cli"}
    ids = {n["id"] for n in c.tree(ws)["nodes"]}
    assert one["id"] not in ids and two["id"] not in ids
    assert place(ws, S("s1"))["id"] == merged["id"]
    assert place(ws, S("s2"))["id"] == merged["id"]
    assert [n for n in c.tree(ws)["nodes"] if n["id"] == child["id"]][0]["parent_id"] == merged["id"]
    assert c.search(ws, "용어집")[0]["id"] == merged["id"]


def test_merge_refuses_nested_selection_and_too_little(ws):
    a = node(ws, "a1")
    b = node(ws, "b1", parent=a["id"])
    with pytest.raises(c.CategoryError):
        c.merge(ws, {"nodes": [a["id"], b["id"]], "label": "x"})
    with pytest.raises(c.CategoryError):
        c.merge(ws, {"items": [S("only")], "label": "x"})


def test_search_normalized_alias_substring_and_fuzzy(ws):
    a = node(ws, "Quick-Fork", aliases=["퀵포크"])
    node(ws, "beads board")
    assert c.search(ws, "quick fork")[0]["id"] == a["id"]
    assert c.search(ws, "퀵포크")[0]["match"] == "exact"
    assert c.search(ws, "fork")[0]["id"] == a["id"]
    assert c.search(ws, "quik fork")[0]["id"] == a["id"]
    assert c.search(ws, "") and c.search(ws, "!!!") == []


def test_create_now_makes_the_node_and_places_the_items(ws):
    web = node(ws, "web")
    made = c.create_now(ws, {"label": "category tab", "parent_id": web["id"],
                             "aliases": ["category pane"], "description": "the page"},
                        [S("s1"), I("claunch-9")], "agent s1", "why")
    assert made["path"] == ["web", "category tab"]
    assert made["aliases"] == ["category pane"]
    assert place(ws, S("s1"))["id"] == made["id"]
    assert place(ws, I("claunch-9"))["id"] == made["id"]
    # Nothing waits for a person: no proposal exists for these items.
    assert c.proposals(ws) == []
    assert c.where(ws, "session", "s1")["proposals"] == []
    ops = [e["op"] for e in c.events(ws)]
    assert ops[:2] == ["assign", "create"]   # newest first; "web" made the third
    assert c.events(ws)[0]["detail"]["reason"] == "why"


def test_create_now_refuses_a_spelling_that_names_a_node(ws):
    web = node(ws, "Web UI", aliases=["dashboard"])
    with pytest.raises(c.CategoryError) as exc:
        c.create_now(ws, {"label": "web-ui"}, [S("s1")], "agent")
    assert exc.value.status == 409
    assert exc.value.extra["candidates"][0]["id"] == web["id"]
    # Refused whole: no node, no placement.
    assert [n["label"] for n in c.tree(ws)["nodes"]] == ["Web UI"]
    assert place(ws, S("s1")) is None


def test_a_job_keeps_the_proposal_choice_it_was_started_with(ws):
    job = c.job_start(ws, "a", "inplace", proposal=True)
    assert job["proposal"] is True
    c.job_update(ws, "a", c.DONE)
    assert c.job_start(ws, "a", "inplace")["proposal"] is False
    assert c.job(ws, "a")["proposal"] is False


def test_a_jobs_table_from_before_the_column_gains_it(ws):
    import sqlite3

    path = c.db_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.executescript(
        "CREATE TABLE jobs (session TEXT PRIMARY KEY, mode TEXT NOT NULL, fork TEXT, "
        "state TEXT NOT NULL, note TEXT NOT NULL, result TEXT, "
        "started_at TEXT NOT NULL, finished_at TEXT)")
    db.execute("INSERT INTO jobs VALUES ('old','inplace',NULL,'done','',NULL,'2026-01-01',NULL)")
    db.commit()
    db.close()
    assert c.job(ws, "old")["proposal"] is False
    assert c.job_start(ws, "new", "inplace", proposal=True)["proposal"] is True


def test_proposal_parks_items_and_accept_moves_them(ws):
    web = node(ws, "web")
    p = c.propose(ws, {"label": "category tab", "parent_id": web["id"], "reason": "new UI",
                       "items": [S("s1"), I("claunch-9")]}, "s1-cat1")
    assert p["status"] == "pending"
    # Not a node yet; items wait in the parent.
    assert [n["label"] for n in c.tree(ws)["nodes"]] == ["web"]
    assert place(ws, S("s1"))["id"] == web["id"]
    assert c.where(ws, "session", "s1")["proposals"][0]["id"] == p["id"]
    out = c.review(ws, p["id"], "accept", "user")
    assert out["node"]["path"] == ["web", "category tab"]
    assert place(ws, S("s1"))["id"] == out["node"]["id"]
    assert place(ws, I("claunch-9"))["id"] == out["node"]["id"]
    with pytest.raises(c.CategoryError) as exc:
        c.review(ws, p["id"], "reject")
    assert exc.value.status == 409


def test_accept_leaves_items_a_person_moved(ws):
    web, other = node(ws, "web"), node(ws, "daemon")
    p = c.propose(ws, {"label": "tabs", "parent_id": web["id"], "reason": "r",
                       "items": [S("s1"), S("s2")]}, "agent")
    c.assign(ws, [S("s2")], other["id"], "user")
    out = c.review(ws, p["id"], "accept", "user")
    assert place(ws, S("s1"))["id"] == out["node"]["id"]
    assert place(ws, S("s2"))["id"] == other["id"]


def test_reject_leaves_items_in_the_parent(ws):
    web = node(ws, "web")
    p = c.propose(ws, {"label": "tabs", "parent_id": web["id"], "reason": "r",
                       "items": [S("s1")]}, "agent")
    out = c.review(ws, p["id"], "reject", "user")
    assert out["node"] is None and out["proposal"]["status"] == "rejected"
    assert place(ws, S("s1"))["id"] == web["id"]
    assert c.proposals(ws) == []


def test_top_level_proposal_keeps_items_where_they_were(ws):
    old = node(ws, "misc")
    c.assign(ws, [S("s1")], old["id"])
    p = c.propose(ws, {"label": "observability", "reason": "r", "items": [S("s1"), S("s2")]}, "agent")
    assert place(ws, S("s1"))["id"] == old["id"]
    assert place(ws, S("s2")) is None
    out = c.review(ws, p["id"], "accept", "user")
    assert out["node"]["parent_id"] is None
    assert place(ws, S("s1"))["id"] == out["node"]["id"]
    assert place(ws, S("s2"))["id"] == out["node"]["id"]


def test_proposal_clash_refused_duplicate_absorbed_similar_reported(ws):
    web = node(ws, "Web UI")
    with pytest.raises(c.CategoryError) as exc:
        c.propose(ws, {"label": "web-ui", "reason": "r"}, "agent")
    assert exc.value.status == 409
    with pytest.raises(c.CategoryError):
        c.propose(ws, {"label": "tabs"}, "agent")  # reason required
    one = c.propose(ws, {"label": "tabs", "parent_id": web["id"], "reason": "r",
                         "items": [S("a")]}, "x")
    two = c.propose(ws, {"label": "Tabs", "parent_id": web["id"], "reason": "r2",
                         "items": [S("b")]}, "y")
    assert one["id"] == two["id"]
    assert {i["ref"] for i in c.proposals(ws)[0]["items"]} == {"a", "b"}
    near = c.propose(ws, {"label": "Web UIs", "reason": "r"}, "z")
    assert [n["id"] for n in near["similar"]] == [web["id"]]


def test_accept_after_parent_deleted_follows_the_items(ws):
    a = node(ws, "web")
    b = node(ws, "pages", parent=a["id"])
    p = c.propose(ws, {"label": "tabs", "parent_id": b["id"], "reason": "r",
                       "items": [S("s1")]}, "agent")
    c.delete_node(ws, b["id"], "user")
    assert c.proposals(ws)[0]["parent_id"] == a["id"]
    out = c.review(ws, p["id"], "accept", "user")
    assert out["node"]["parent_id"] == a["id"]
    assert place(ws, S("s1"))["id"] == out["node"]["id"]


def test_concurrent_reviews_create_one_node(ws):
    p = c.propose(ws, {"label": "tabs", "reason": "r"}, "agent")
    barrier = Barrier(4)

    def go(_):
        barrier.wait()
        try:
            return c.review(ws, p["id"], "accept", "user")["node"]["id"]
        except c.CategoryError as exc:
            return exc.status

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(go, range(4)))
    assert sorted(map(str, results)).count("409") == 3
    assert len(c.tree(ws)["nodes"]) == 1


def test_events_record_each_change(ws):
    a = node(ws, "web")
    c.assign(ws, [S("s1")], a["id"], "user", "why")
    ops = [e["op"] for e in c.events(ws)]
    assert ops[:2] == ["assign", "create"]
    assert c.events(ws)[0]["detail"]["reason"] == "why"


def test_only_an_unfinished_in_place_job_is_replaced(ws):
    c.job_start(ws, "a", "inplace")
    with pytest.raises(c.CategoryError) as exc:
        c.job_start(ws, "a", "inplace")
    assert exc.value.status == 409
    assert c.job_start(ws, "a", "inplace", replace_inplace=True)["state"] == c.RUNNING
    # a running fork is not the session's own to abandon
    c.job_update(ws, "a", c.DONE)
    c.job_start(ws, "a", "fork", "a-cat1")
    with pytest.raises(c.CategoryError):
        c.job_start(ws, "a", "inplace", replace_inplace=True)


def test_split_makes_several_children_and_places_the_items_among_them(ws):
    web = node(ws, "web")
    c.assign(ws, [S("s1"), S("s2"), S("s3")], web["id"])
    out = c.split(ws, web["id"], [
        {"label": "beads page", "items": [S("s1")]},
        {"label": "sims page", "aliases": ["시뮬"], "items": [S("s2"), S("s3")]},
    ], "user", "two subjects under one page")
    assert out["parent_id"] == web["id"]
    assert [ch["node"]["label"] for ch in out["children"]] == ["beads page", "sims page"]
    assert out["children"][1]["node"]["path"] == ["web", "sims page"]
    assert place(ws, S("s1"))["label"] == "beads page"
    assert place(ws, S("s2"))["label"] == "sims page"
    # The item that was not named stays where it was.
    assert place(ws, S("s3"))["label"] == "sims page"


def test_split_refuses_an_item_outside_the_parent_or_in_two_parts(ws):
    web = node(ws, "web")
    other = node(ws, "cflow")
    c.assign(ws, [S("s1")], web["id"])
    c.assign(ws, [S("s2")], other["id"])
    with pytest.raises(c.CategoryError):
        c.split(ws, web["id"], [{"label": "x", "items": [S("s2")]}], "user")
    with pytest.raises(c.CategoryError):
        c.split(ws, web["id"], [{"label": "x", "items": [S("s1")]},
                                {"label": "y", "items": [S("s1")]}], "user")
    with pytest.raises(c.CategoryError):
        c.split(ws, web["id"], [], "user")


def test_split_at_the_top_level_and_with_no_items(ws):
    web = node(ws, "web")
    c.assign(ws, [S("s1")], web["id"])
    out = c.split(ws, None, [{"label": "ui"}, {"label": "daemon"}], "user")
    assert out["parent_id"] is None
    assert [ch["node"]["path"] for ch in out["children"]] == [["ui"], ["daemon"]]
    assert place(ws, S("s1"))["id"] == web["id"]


def test_merge_takes_an_explicit_parent_for_registration(ws):
    ui = node(ws, "web")
    a = node(ws, "beads page")
    b = node(ws, "sims page")
    c.assign(ws, [S("x")], a["id"])
    c.assign(ws, [S("y")], b["id"])
    # Without parent_id the two flat nodes meet at the root.
    assert c.merge(ws, {"nodes": [a["id"], b["id"]], "label": "pages"},
                   "user")["node"]["parent_id"] is None
    a2 = node(ws, "chart page")
    c.assign(ws, [S("p")], a2["id"])
    out = c.merge(ws, {"nodes": [a2["id"]], "items": [S("y")], "label": "charts",
                       "parent_id": ui["id"]}, "user")
    assert out["node"]["parent_id"] == ui["id"]
    assert place(ws, S("p"))["id"] == out["node"]["id"]


def test_merge_into_a_node_inside_a_folded_one_is_refused(ws):
    outer = node(ws, "web")
    inner = node(ws, "beads page", parent=outer["id"])
    c.assign(ws, [S("a")], inner["id"])
    with pytest.raises(c.CategoryError):
        c.merge(ws, {"nodes": [outer["id"]], "items": [S("b")],
                     "label": "squash", "parent_id": inner["id"]}, "user")
