"""The folders shares may come from (vm-setup-8s6, s2-vmroots): seen and edited by the user
through the API and the CLI, instead of editing the settings file."""
from __future__ import annotations

import os

import pytest

from claude_launcher import store
from claude_launcher.daemon import actors, vm_share, vm_tart
from claude_launcher.daemon.vmhub import Registry

from test_vm_share import AGENT, Fake, Host, api_for, code_of, run  # noqa: F401

# The VM routes answer vm_disabled until the feature is switched on.
pytestmark = pytest.mark.usefixtures("vm_on")


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def stored():
    return store.daemon_config().get("vm_share_roots")


def test_a_root_is_checked_when_it_is_added(host):
    real = os.path.realpath(host.root)
    assert vm_share.check_root(str(host.root), host.forbidden) == real
    (host.root / "link").symlink_to(host.outside)
    assert vm_share.check_root(str(host.root / "link"), host.forbidden) == os.path.realpath(host.outside)
    for bad, code in ((host.home, "vm_share_forbidden"), (host.home / ".ssh", "vm_share_forbidden"),
                      (host.home / "Library", "vm_share_forbidden"), (host.state, "vm_share_forbidden"),
                      (host.home / ".claude-launcher", "vm_share_forbidden"),
                      ("/", "vm_bad_request"), (host.home / "nope", "vm_bad_request"),
                      ("relative/dir", "vm_bad_request"), (None, "vm_bad_request"), ("/a\nb", "vm_bad_request")):
        assert code_of(vm_share.check_root, str(bad) if bad is not None else None, host.forbidden) == code, bad
    (host.root / "a:b").mkdir()
    assert code_of(vm_share.check_root, str(host.root / "a:b"), host.forbidden) == "vm_bad_request"


def test_the_rows_say_why_a_stored_root_is_not_in_effect(host):
    rows = vm_share.root_rows([str(host.root), str(host.home), str(host.home / "gone"), 5, "x"], host.forbidden)
    assert [r["ok"] for r in rows] == [True, False, False, False]
    assert rows[0]["reason"] == "" and "공유할 수 없는" in rows[1]["reason"] and "폴더" in rows[2]["reason"]
    assert vm_share.root_rows(None, host.forbidden) == [] and vm_share.root_rows("x", host.forbidden) == []


def test_the_user_adds_and_removes_roots_and_a_session_cannot(tmp_path, host):
    store.set_daemon_field("vm_token_probe", "keep-me")

    async def go():
        async with api_for(tmp_path, host) as api:
            empty = await (await api.get("/api/vm-share-roots")).json()
            assert empty == {"roots": [], "enabled": False}
            body = {"action": "add", "path": str(host.root)}
            for hdr in (AGENT, {actors.HEADER: "cflow:wf"}):
                for resp in (await api.post("/api/vm-share-roots", json=body, headers=hdr),
                             await api.get("/api/vm-share-roots", headers=hdr)):
                    assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
            assert stored() == []
            ok = await api.post("/api/vm-share-roots", json=body)
            got = await ok.json()
            real = os.path.realpath(host.root)
            assert ok.status == 200 and got["enabled"] and got["roots"] == [
                {"path": real, "real": real, "ok": True, "reason": ""}]
            assert stored() == [real]
            again = await api.post("/api/vm-share-roots", json=body)
            assert (await again.json())["roots"][0]["path"] == real and stored() == [real]
            assert any(r.get("what") == "share_root_add" for r in api.reg.audit_log)
            denied = await api.post("/api/vm-share-roots", json={"action": "rm", "path": real}, headers=AGENT)
            assert denied.status == 403 and stored() == [real]
            gone = await api.post("/api/vm-share-roots", json={"action": "rm", "path": str(host.root)})
            assert gone.status == 200 and (await gone.json()) == {"roots": [], "enabled": False} and stored() == []
            assert any(r.get("what") == "share_root_rm" for r in api.reg.audit_log)
            missing = await api.post("/api/vm-share-roots", json={"action": "rm", "path": real})
            assert missing.status == 404 and (await missing.json())["code"] == "vm_root_unknown"
            odd = await api.post("/api/vm-share-roots", json={"action": "zap", "path": real})
            assert odd.status == 400

    run(go())
    assert store.daemon_config().get("vm_token_probe") == "keep-me"         # the rest of the file is untouched


def test_a_forbidden_root_is_refused_with_its_reason_and_nothing_is_saved(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            for path, code in ((host.home, "vm_share_forbidden"), (host.home / ".ssh", "vm_share_forbidden"),
                               (host.home / "gone", "vm_bad_request")):
                resp = await api.post("/api/vm-share-roots", json={"action": "add", "path": str(path)})
                got = await resp.json()
                assert resp.status == 400 and got["code"] == code and "error" in got
            assert stored() == []
            for i in range(vm_share.ROOTS_MAX):
                (host.root / f"r{i}").mkdir()
                ok = await api.post("/api/vm-share-roots", json={"action": "add", "path": str(host.root / f"r{i}")})
                assert ok.status == 200
            (host.root / "extra").mkdir()
            over = await api.post("/api/vm-share-roots", json={"action": "add", "path": str(host.root / "extra")})
            assert over.status == 400 and (await over.json())["code"] == "vm_share_limit"

    run(go())


def test_a_stored_root_that_went_bad_is_listed_with_why_and_can_be_removed(tmp_path, host):
    store.set_daemon_field("vm_share_roots", [str(host.home), str(host.root), 7])

    async def go():
        async with api_for(tmp_path, host) as api:
            got = await (await api.get("/api/vm-share-roots")).json()
            assert [r["ok"] for r in got["roots"]] == [False, True] and got["enabled"]
            assert "공유할 수 없는" in got["roots"][0]["reason"]
            page = await (await api.get("/api/vm")).json()
            assert page["share_roots"] == [os.path.realpath(host.root)] and len(page["share_root_rows"]) == 2
            gone = await api.post("/api/vm-share-roots", json={"action": "rm", "path": str(host.home)})
            assert gone.status == 200 and [r["path"] for r in (await gone.json())["roots"]] == [str(host.root)]

    run(go())


def test_a_session_does_not_get_the_roots_and_the_manager_reads_them_afresh(tmp_path, host):
    store.set_daemon_field("vm_share_roots", [])
    fake = Fake()
    reg = Registry()
    reg.add("a", ssh_user="admin")
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, alive=lambda p: True,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5, locked=lambda: False,
                          share_roots=lambda: store.daemon_config().get("vm_share_roots") or [],
                          share_forbidden=lambda: host.forbidden)
    assert mgr.share_rules()[0] == []
    store.set_daemon_field("vm_share_roots", [str(host.root)])              # as the route does, no restart
    assert mgr.share_rules()[0] == [os.path.realpath(host.root)]
    vm_share.check_share("src", str(host.root / "proj"), "ro", mgr.share_rules()[0], host.forbidden)

    async def go():
        async with api_for(tmp_path, host) as api:
            store.set_daemon_field("vm_share_roots", [str(host.root)])
            mine = await (await api.get("/api/vm?session=s2", headers={actors.HEADER: "agent:s2"})).json()
            assert "share_roots" not in mine and "share_root_rows" not in mine
            assert str(host.root) not in str(mine)

    run(go())


def test_the_pending_requests_are_listed_for_the_dashboard_and_only_to_the_user(tmp_path, host):
    from test_vm_share import S2, req

    async def go():
        async with api_for(tmp_path, host) as api:
            assert await (await api.get("/api/vm-requests")).json() == {"enabled": True, "requests": []}
            body = req(share=("out", str(host.root / "other"), "rw"))
            assert (await api.post("/api/vm/a/request", json=body, headers=S2)).status == 200
            for hdr in (AGENT, S2, {actors.HEADER: "cflow:wf"}):
                denied = await api.get("/api/vm-requests", headers=hdr)
                assert denied.status == 403 and (await denied.json())["code"] == "vm_user_only"
            got = (await (await api.get("/api/vm-requests")).json())["requests"]
            assert len(got) == 1 and got[0]["vm"] == "a" and got[0]["session"] == "s2"
            assert got[0]["id"] and got[0]["scopes"] == ["view", "files"] and got[0]["shares"][0]["mode"] == "rw"
            deny = await api.post("/api/vm/a/deny", json={"session": "s2", "id": got[0]["id"]})
            assert deny.status == 200
            assert await (await api.get("/api/vm-requests")).json() == {"enabled": True, "requests": []}

    run(go())
