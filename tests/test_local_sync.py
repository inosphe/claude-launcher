"""Real clones: policy, concurrency, restart and local-only receipt contracts."""
import concurrent.futures
import json

import pytest

from claude_launcher import local_sync, reposync
from test_reposync_workspaces import world, _git, _commit


def setup(world, daemon="host-a"):
    repo = str(world["a"])
    registry = local_sync.Registry(repo, daemon)
    target = {"ref": "refs/heads/master", "remote_key": local_sync.remote_key(repo, "origin"),
              "mesh": "authority-mesh", "authority": "leader", "authority_revision": "1"}
    registry.configure([target], expected_revision=0)
    sha = _git(world["a"], "rev-parse", "master")
    request = {"id": "req-a", "requester": "worker-a", "mesh": "request-mesh", "board": "a/board",
               "issue": "a-1", "remote": "origin", "ref": "refs/heads/master", "required_sha": sha,
               "policy_revision": 1, "authority_revision": "1"}
    registry.submit(request)
    return registry, request


def bind(registry):
    reservation = registry.reserve()
    return registry.bind("manager", epoch=reservation["epoch"], role="local-sync-manager", workflow="local-sync-v2")


def test_aliases_share_reservation_and_two_clones_do_not(world, tmp_path):
    registry, request = setup(world)
    wt = tmp_path / "wt"
    _git(world["a"], "worktree", "add", "-b", "feature", str(wt))
    alias = local_sync.Registry(str(wt), "host-a")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: alias.reserve(), range(8)))
    assert {r["epoch"] for r in rows} == {1}
    assert {r["key"] for r in rows} == {registry.key}
    assert local_sync.Registry(str(world["b"]), "host-b").key != registry.key
    assert local_sync.policy(str(world["b"])) == {}
    assert alias.submit(request)["id"] == request["id"]
    with pytest.raises(local_sync.PolicyError, match="idempotency"):
        alias.submit({**request, "mesh": "another"})


def test_policy_survives_manager_exit_and_blocks_all_generic_writes(world, tmp_path):
    registry, request = setup(world)
    bind(registry)
    registry.retire(epoch=1)
    _commit(world["a"], "local.txt")
    plan = reposync.plan(str(world["a"]), "origin", "master")
    before = _git(world["remote"], "rev-parse", "master")
    with pytest.raises(local_sync.PolicyError, match="PR-only"):
        reposync.execute(plan, scratch=str(tmp_path))
    assert _git(world["remote"], "rev-parse", "master") == before
    assert local_sync.Registry(str(world["a"]), "host-a").reserve()["epoch"] == 2
    assert local_sync.policy(str(world["a"]))["integration_mode"] == "pr_only"


def test_ff_receipt_is_local_and_idempotent_after_restart(world):
    registry, request = setup(world)
    manager = bind(registry)
    _git(world["a"], "switch", "-c", "work")
    new = _commit(world["b"], "remote.txt")
    _git(world["b"], "push", "origin", "master")
    registry.submit({**request, "id": "new", "required_sha": new})
    result = registry.apply("new", session="manager", epoch=manager["epoch"])
    assert result["new"] == new and result["daemon"] == "host-a"
    assert _git(world["a"], "rev-parse", "master") == new
    assert local_sync.Registry(str(world["a"]), "host-a").apply("new", session="manager", epoch=1) == result
    assert json.loads(registry.get("new")["receipt"])["required_sha"] == new


def test_stale_lease_and_policy_never_write(world):
    registry, request = setup(world)
    bind(registry)
    registry.retire(epoch=1)
    with pytest.raises(local_sync.PolicyError, match="lease"):
        registry.apply(request["id"], session="manager", epoch=1)
    with pytest.raises(local_sync.PolicyError, match="authority"):
        registry.submit({**request, "id": "bad", "authority_revision": "2"})
    with pytest.raises(local_sync.PolicyError, match="policy"):
        registry.submit({**request, "id": "bad", "policy_revision": 2})
    registry.settle(request["id"], "held")
    with pytest.raises(local_sync.PolicyError, match="held"):
        registry.apply(request["id"], session="manager", epoch=1)


def test_divergence_preserves_local_tip_and_never_pushes(world):
    registry, request = setup(world)
    old = _commit(world["a"], "unpublished.txt")
    remote = _commit(world["b"], "remote.txt")
    _git(world["b"], "push", "origin", "master")
    bind(registry)
    result = registry.apply(request["id"], session="manager", epoch=1)
    assert result["status"] == "needs_operator"
    assert _git(world["a"], "rev-parse", result["preserved"]) == old
    assert _git(world["a"], "rev-parse", "master") == old
    assert _git(world["remote"], "rev-parse", "master") == remote


def test_checked_out_target_requires_owner_instead_of_reset(world):
    registry, request = setup(world)
    bind(registry)
    _commit(world["b"], "remote.txt")
    _git(world["b"], "push", "origin", "master")
    result = registry.apply(request["id"], session="manager", epoch=1)
    assert result["status"] == "needs_operator"
    assert _git(world["a"], "rev-parse", "master") == request["required_sha"]


def test_pr_publisher_cannot_push_protected_target(world):
    from claude_launcher import prflow
    setup(world)
    head = _git(world["a"], "rev-parse", "HEAD")
    with pytest.raises(prflow.PrError, match="PR-only"):
        prflow.push(str(world["a"]), "origin", head, "master")


def test_manager_is_not_implicitly_connected_to_all_roots():
    from claude_launcher.daemon.mesh_roles import LinkFacts, LinkPattern, LinkRule
    root_rule = LinkRule(LinkPattern(tier=0), LinkPattern(tier=0))
    manager = LinkFacts("local-sync-manager", 0, "manager")
    leader = LinkFacts("leader", 0, "leader")
    assert not root_rule.matches(manager, leader)
    explicit = LinkRule(LinkPattern(role="local-sync-manager"), LinkPattern(role="leader"))
    assert explicit.matches(manager, leader)


def test_restart_fences_old_manager_but_preserves_requests(world):
    registry, request = setup(world)
    local_sync.Registry(str(world["a"]), "host-a", boot="first")
    bind(registry)
    resumed = local_sync.Registry(str(world["a"]), "host-a", boot="second")
    assert resumed.reserve()["epoch"] == 2
    assert resumed.get(request["id"])["state"] == "pending"
    with pytest.raises(local_sync.PolicyError, match="lease"):
        resumed.apply(request["id"], session="manager", epoch=1)


def test_api_operator_boundary_and_missing_manager_resolution(world):
    from test_reposync_workspaces import _with_app, AUTH

    async def exercise(client):
        endpoint = "/api/local-sync/workspaces/proj"
        status = await (await client.get(endpoint + "/status", headers=AUTH)).json()
        target = {"ref": "refs/heads/master", "remote_key": status["remote_identities"]["origin"],
                  "mesh": "authority", "authority": "leader", "authority_revision": "1"}
        body = {"targets": [target], "revision": 0}
        denied = await client.post(endpoint + "/configure", json=body, headers=AUTH)
        assert denied.status == 403
        client.app["cookie_sessions"].add("operator-cookie")
        browser = {**AUTH, "Cookie": f"{client.app['cookie_name']}=operator-cookie"}
        enabled = await client.post(endpoint + "/configure", json=body, headers=browser)
        assert enabled.status == 200
        sha = _git(world["a"], "rev-parse", "master")
        request = {"id": "one", "requester": "worker", "mesh": "request-mesh", "board": "board",
                   "issue": "issue", "remote": "origin", "ref": "refs/heads/master", "required_sha": sha,
                   "policy_revision": 1, "authority_revision": "1"}
        submitted = await client.post(endpoint + "/request", json=request, headers=AUTH)
        doc = await submitted.json()
        assert submitted.status == 200 and doc["resolution"]["required"]
        assert doc["resolution"]["workflow"] == "local-sync-v2"
        assert doc["manager"]["session"] is None
        held = await client.post(endpoint + "/resolve", json={"request_id": "one", "state": "held"}, headers=browser)
        assert (await held.json())["state"] == "held"
    _with_app(exercise)


def test_exact_owner_approval_allows_checked_out_ff_and_stale_approval_does_not(world):
    registry, request = setup(world)
    bind(registry)
    new = _commit(world["b"], "remote.txt")
    _git(world["b"], "push", "origin", "master")
    registry.apply(request["id"], session="manager", epoch=1)
    registry.approve_checkout(request["id"], epoch=1, old=request["required_sha"], new=new, identity=local_sync.checkout_identity(str(world["a"])))
    newer = _commit(world["b"], "later.txt")
    _git(world["b"], "push", "origin", "master")
    assert registry.apply(request["id"], session="manager", epoch=1)["status"] == "needs_operator"
    assert _git(world["a"], "rev-parse", "master") == request["required_sha"]
    registry.approve_checkout(request["id"], epoch=1, old=request["required_sha"], new=newer, identity=local_sync.checkout_identity(str(world["a"])))
    assert registry.apply(request["id"], session="manager", epoch=1)["new"] == newer


def test_local_landing_and_receipt_verifiers_fail_closed(world):
    local_sync.allow_local(str(world["a"]))
    registry, request = setup(world)
    with pytest.raises(local_sync.PolicyError, match="local landing"):
        local_sync.allow_local(str(world["a"]))
    with pytest.raises(local_sync.PolicyError, match="no terminal"):
        local_sync.verify_receipt(str(world["a"]), request["id"], 1)
    bind(registry)
    registry.apply(request["id"], session="manager", epoch=1)
    local_sync.verify_receipt(str(world["a"]), request["id"], 1)
    with pytest.raises(local_sync.PolicyError, match="another manager"):
        local_sync.verify_receipt(str(world["a"]), request["id"], 2)


@pytest.mark.parametrize("queued", [False, True])
def test_manager_notification_uses_mesh_handle_and_reports_pending_delivery(world, monkeypatch, queued):
    import asyncio
    from types import SimpleNamespace
    from claude_launcher.daemon import local_sync_api
    registry, request = setup(world)
    bind(registry)
    agent = SimpleNamespace(exited=False, sdef=SimpleNamespace(cwd=str(world["a"])))
    delivered = []

    async def send(mesh, sender, to, text, **kwargs):
        # Mesh recipients are handles; session IDs are not accepted here.
        assert to == "local-sync-alias"
        delivered.append((mesh, sender, to))
        return {"queued": queued, "notice": "relay disconnected" if queued else ""}

    mesh = SimpleNamespace(send=send, meshes_for_session=lambda session: [
        {"mesh": request["mesh"], "role": "local-sync-manager", "handle": "local-sync-alias"}])
    run = SimpleNamespace(exists=lambda: True,
                          load_chart=lambda: SimpleNamespace(name="local-sync-v2"),
                          load_state=lambda: {"status": "running"})
    monkeypatch.setattr(local_sync_api, "RunStore", lambda *args: run)
    api_request = SimpleNamespace(app={"manager": SimpleNamespace(get=lambda session: agent), "mesh": mesh})
    error = asyncio.run(local_sync_api._notify(api_request, registry, request["id"]))
    assert delivered == [(request["mesh"], request["requester"], "local-sync-alias")]
    assert error == ("delivery pending: relay disconnected" if queued else "")


@pytest.mark.parametrize("approve_before_move", [False, True])
def test_checkout_owner_consent_cannot_follow_branch_to_another_worktree(world, tmp_path, approve_before_move):
    registry, request = setup(world)
    bind(registry)
    new = _commit(world["b"], "remote.txt")
    _git(world["b"], "push", "origin", "master")
    plan = registry.apply(request["id"], session="manager", epoch=1)
    assert plan["checkout"] == str(world["a"])
    if approve_before_move:
        registry.approve_checkout(request["id"], epoch=1, old=request["required_sha"], new=new, identity=local_sync.checkout_identity(str(world["a"])))
    _git(world["a"], "switch", "-c", "away")
    other = tmp_path / "other-owner"
    _git(world["a"], "worktree", "add", str(other), "master")
    before = (_git(other, "rev-parse", "HEAD"), _git(other, "write-tree"),
              {str(p.relative_to(other)): p.read_bytes() for p in other.rglob("*") if p.is_file()})
    if not approve_before_move:
        with pytest.raises(local_sync.PolicyError, match="checkout identity changed"):
            registry.approve_checkout(request["id"], epoch=1, old=request["required_sha"], new=new, identity=local_sync.checkout_identity(str(world["a"])))
    result = registry.apply(request["id"], session="manager", epoch=1)
    assert result["status"] == "needs_operator" and result["checkout"] == str(other)
    with pytest.raises(local_sync.PolicyError, match="checkout identity changed"):
        registry.approve_checkout(request["id"], epoch=1, old=request["required_sha"], new=new,
                                  identity=plan["checkout_identity"])
    after = (_git(other, "rev-parse", "HEAD"), _git(other, "write-tree"),
             {str(p.relative_to(other)): p.read_bytes() for p in other.rglob("*") if p.is_file()})
    assert before == after


def test_deleted_remote_target_cannot_complete_from_stale_tracking_ref(world):
    registry, request = setup(world)
    bind(registry)
    _git(world["a"], "switch", "-c", "work")
    new = _commit(world["b"], "remote.txt")
    _git(world["b"], "push", "origin", "master")
    _git(world["a"], "fetch", "origin")
    _git(world["remote"], "update-ref", "-d", "refs/heads/master")
    assert _git(world["a"], "rev-parse", "origin/master") == new
    with pytest.raises(reposync.SyncError, match="couldn't find remote ref"):
        registry.apply(request["id"], session="manager", epoch=1)
    assert _git(world["a"], "rev-parse", "master") == request["required_sha"]
    assert registry.get(request["id"])["state"] == "pending"
    assert registry.get(request["id"])["receipt"] is None


def test_exact_fetch_ignores_tracking_refspec_that_excludes_target(world):
    registry, request = setup(world)
    bind(registry)
    _git(world["a"], "switch", "-c", "work")
    _git(world["b"], "push", "origin", "master:unrelated")
    _git(world["a"], "config", "remote.origin.fetch", "+refs/heads/unrelated:refs/remotes/origin/unrelated")
    new = _commit(world["b"], "remote.txt")
    _git(world["b"], "push", "origin", "master")
    assert _git(world["a"], "rev-parse", "origin/master") != new
    result = registry.apply(request["id"], session="manager", epoch=1)
    assert result["new"] == new
    assert _git(world["a"], "rev-parse", "master") == new
    assert _git(world["a"], "for-each-ref", "--format=%(refname)", "refs/claunch/local-sync-fetch/") == ""


def test_claunch_reposync_verify_allow_local(world, monkeypatch, capsys):
    # The workflow verify line runs through claunch's own interpreter
    # (claunch-pr46o): a bare python3 on PATH need not have the package.
    from claude_launcher import cli
    monkeypatch.chdir(world["a"])
    assert cli.main(["reposync", "verify", "allow-local"]) == 0
    setup(world)
    assert cli.main(["reposync", "verify", "allow-local"]) == 1
    assert "PR-only repository" in capsys.readouterr().err


def test_packaged_verify_lines_do_not_name_a_bare_interpreter():
    from pathlib import Path
    import claude_launcher
    root = Path(claude_launcher.__file__).parent / "workflows"
    bad = [f"{p.relative_to(root)}:{n}" for p in sorted(root.rglob("*.yaml"))
           for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
           if "verify:" in line and "claude_launcher." in line]
    assert bad == [], "use 'claunch reposync verify <check>': " + ", ".join(bad)
