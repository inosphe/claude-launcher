"""The VM guidance pack follows a session's VM grants (vm-setup-8s6, s2-vmprompt):
sent and held when the first grant arrives, dropped with the last, never sent to a
session without a grant. The delivery is the packs pane's (session_packs)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from claude_launcher.daemon import session_packs, session_reminder, vm_pack

from test_vm_share import Host, api_for, run  # noqa: F401  (fixtures are plain helpers)
import pytest

# The VM routes answer vm_disabled until the feature is switched on.
pytestmark = pytest.mark.usefixtures("vm_on")


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


class Live:
    def __init__(self, packs=()):
        self.sdef = SimpleNamespace(prompt_packs=list(packs), name="s2")
        self.sent = []

    def queue_delivery(self, text, on_done=None, before=None):
        self.sent.append(text)
        if on_done:
            on_done(True)
        return True


class Manager:
    def __init__(self, **live):
        self.live = live

    def get(self, name):
        if name not in self.live:
            raise KeyError(name)
        return self.live[name]


def attach(api, **live):
    api.app["manager"] = Manager(**live)
    return api.app["manager"]


def test_the_first_grant_sends_the_pack_once_and_the_reminder_names_it(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            s2 = Live()
            attach(api, s2=s2)
            await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["view"]})
            assert len(s2.sent) == 1 and "claunch prompt pack" in s2.sent[0] and "vm_request" in s2.sent[0]
            assert vm_pack.PACK in session_packs.held("s2")
            ids = [e["id"] for e in session_reminder.pack_entries(s2.sdef)]
            assert ids == [vm_pack.PACK]                                  # what the reminder names each interval
            await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["files"]})
            await api.post("/api/vm/b/grant", json={"session": "s2", "scopes": ["view"]})
            assert len(s2.sent) == 1                                      # held already: not typed again
    run(go())


def test_the_pack_is_dropped_with_the_last_grant_and_only_then(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            s2 = Live()
            attach(api, s2=s2)
            await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["view"]})
            await api.post("/api/vm/b/grant", json={"session": "s2", "scopes": ["view"]})
            await api.post("/api/vm/a/revoke", json={"session": "s2"})
            assert vm_pack.PACK in session_packs.held("s2")               # still holds b
            await api.post("/api/vm/b/revoke", json={"session": "s2"})
            assert vm_pack.PACK not in session_packs.held("s2")
            assert session_reminder.pack_entries(s2.sdef) == []
            await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["view"]})
            assert len(s2.sent) == 2                                      # a new grant sends it again
    run(go())


def test_a_removed_vm_ends_the_hold_of_its_last_session(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            s2 = Live()
            attach(api, s2=s2)
            await api.post("/api/vm/b/grant", json={"session": "s2", "scopes": ["view"]})
            assert vm_pack.PACK in session_packs.held("s2")
            assert (await api.delete("/api/vm/b")).status == 200
            assert vm_pack.PACK not in session_packs.held("s2")
    run(go())


def test_a_session_without_a_grant_is_never_sent_the_pack(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            s1, s2 = Live(), Live()
            attach(api, s1=s1, s2=s2)
            await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["view"]})
            await api.post("/api/vm/a/request", json={"session": "s1", "scopes": ["view"], "shares": []},
                           headers={"X-Claunch-Actor": "agent:s1"})   # a request grants nothing
            assert s1.sent == [] and session_packs.held("s1") == {}
            await api.post("/api/vm/a/revoke", json={"session": "s1"})  # revoking nothing sends nothing
            assert s1.sent == []
    run(go())


def test_approval_sends_the_pack_too_and_an_opening_that_carries_it_is_not_doubled(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            s2, s3 = Live(), Live(packs=[{"id": vm_pack.PACK, "text": "x"}])
            attach(api, s2=s2, s3=s3)
            for who in ("s2", "s3"):
                await api.post("/api/vm/a/request", json={"session": who, "scopes": ["view"], "shares": []},
                               headers={"X-Claunch-Actor": f"agent:{who}"})
                rid = api.reg.request_get("a", who)["id"]
                assert (await api.post("/api/vm/a/approve", json={"session": who, "id": rid})).status == 200
            assert len(s2.sent) == 1 and s3.sent == []
    run(go())


def test_a_missing_manager_or_session_does_not_fail_the_grant(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            ok = await api.post("/api/vm/a/grant", json={"session": "ghost", "scopes": ["view"]})   # no manager
            assert ok.status == 200
            attach(api)
            ok = await api.post("/api/vm/b/grant", json={"session": "ghost", "scopes": ["view"]})   # unknown session
            assert ok.status == 200 and session_packs.held("ghost") == {}
    run(go())
