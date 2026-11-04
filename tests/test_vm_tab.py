"""The dashboard's VM tab (vm-setup-8s6): the read-only tart list the register
form uses, and the pages that name routes and elements."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from claude_launcher.daemon import vm_api
from claude_launcher.daemon.vmhub import Registry

from test_vm_frames import AGENT, Capture, _api
import pytest

# The VM routes answer vm_disabled until the feature is switched on.
pytestmark = pytest.mark.usefixtures("vm_on")

STATIC = Path(vm_api.__file__).resolve().parent.parent / "web" / "static"


def run(coro):
    return asyncio.run(coro)


def test_the_tart_list_marks_registered_and_unusable_names(tmp_path):
    async def go():
        async with _api(tmp_path, Capture()) as api:
            api.reg.add("mine", "alpha")
            api.mgr_fake.tart.update({"alpha": "running", "beta": "stopped", "bad name": "stopped"})
            resp = await api.get("/api/vm-tart")
            assert resp.status == 200
            rows = {r["name"]: r for r in (await resp.json())["tart"]}
            assert rows["alpha"]["registered"] and rows["alpha"]["id"] == "mine" and rows["alpha"]["running"]
            assert not rows["beta"]["registered"] and rows["beta"]["usable"]
            assert not rows["bad name"]["usable"]
    run(go())


def test_a_session_cannot_read_the_host_inventory(tmp_path):
    async def go():
        async with _api(tmp_path, Capture()) as api:
            resp = await api.get("/api/vm-tart", headers=AGENT)
            assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
    run(go())


def test_the_tab_is_wired_into_the_dashboard():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    page = (STATIC / "vm-page.js").read_text(encoding="utf-8")
    assert '<a href="#/vm" data-page="vm">' in index
    assert 'id="vm-view"' in index and 'static/vm-page.js' in index
    assert 'vm: "vm-view"' in app and 'parts[0] === "vm"' in app
    assert 'VmPage.open()' in app and 'VmPage?.stop()' in app
    assert "innerHTML" not in page


def test_the_panel_calls_the_tart_route_and_no_ssh_text_is_left():
    js = (STATIC / "vm.js").read_text(encoding="utf-8")
    html = (STATIC / "vm.html").read_text(encoding="utf-8")
    assert "api/vm-tart" in js
    assert "--ssh-key" not in html and "--ssh-key" not in js
    for ident in re.findall(r'\$\("([a-z-]+)"\)', js):
        assert f'id="{ident}"' in html, ident


def test_the_panel_has_the_preset_controls_and_the_dns_notice():
    js = (STATIC / "vm.js").read_text(encoding="utf-8")
    html = (STATIC / "vm.html").read_text(encoding="utf-8")
    assert "api/vm-presets" in js
    for ident in ("vm-presets", "vm-preset-save-go", "vm-preset-newid", "vm-preset-list"):
        assert f'id="{ident}"' in html
    assert "networksetup -setdnsservers" in html and "admin 비밀번호" in html
    assert "confirm(" in js[js.index("function deletePreset"):]
    assert "innerHTML" not in js


def test_the_panel_has_the_network_section_and_its_warning():
    js = (STATIC / "vm.js").read_text(encoding="utf-8")
    html = (STATIC / "vm.html").read_text(encoding="utf-8")
    for ident in ("vm-expose-go", "vm-allow-go", "vm-net-warning", "vm-net-pending", "vm-expose", "vm-allow"):
        assert f'id="{ident}"' in html
    assert "같은 LAN의 모든 기기" in html and "호스트 방화벽은 바꾸지 않습니다" in html
    assert "api/vm/${encodeURIComponent(selected)}/expose" in js and "/allow" in js
    assert "192.168.1.213" in html and "/32" in html   # the placeholder shows the shape of the value

def test_the_panel_has_the_lock_controls():
    js = (STATIC / "vm.js").read_text(encoding="utf-8")
    html = (STATIC / "vm.html").read_text(encoding="utf-8")
    for ident in ("vm-guard-toggle", "vm-guard-chip", "vm-guard-note"):
        assert f'id="{ident}"' in html
    assert "/${op}" in js and "vm.locked" in js
    assert "호스트 화면 잠금과는 별개" in html     # the two locks are told apart
    assert "innerHTML" not in js


def test_the_panel_has_the_files_section_and_its_scope():
    js = (STATIC / "vm.js").read_text(encoding="utf-8")
    html = (STATIC / "vm.html").read_text(encoding="utf-8")
    for ident in ("vm-files-host", "vm-files-dir", "vm-files-save", "vm-files-default", "vm-files-log",
                  "vm-grant-files", "vm-files-note"):
        assert f'id="{ident}"' in html
    assert "받은 파일은 실행하지 않습니다" in html
    assert 'const SCOPES = ["view", "control", "exec", "files"]' in js
    assert "/files_dir" in js and "api/vm/${encodeURIComponent(vm.id)}/files`" in js
    assert 'scope === "exec" &&' in js          # only exec asks first; files has no special dialog
    assert "innerHTML" not in js


def test_the_panel_keeps_unregistering_and_deleting_apart():
    js = (STATIC / "vm.js").read_text(encoding="utf-8")
    html = (STATIC / "vm.html").read_text(encoding="utf-8")
    for ident in ("vm-unregister", "vm-danger", "vm-destroy", "vm-destroy-confirm", "vm-destroy-blockers"):
        assert f'id="{ident}"' in html
    assert "tart VM은 남음" in html and "되돌릴 수 없" in html
    assert 'method: "DELETE"' in js and "/destroy" in js
    assert "confirm: id" in js                    # the delete sends the typed id, as the preset delete does
    assert "innerHTML" not in js


def test_the_session_vm_pane_is_wired_into_the_dashboard_header():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    pane = (STATIC / "vm-session-pane.js").read_text(encoding="utf-8")
    assert 'id="term-vmbtn"' in index and "static/vm-session-pane.js" in index
    assert 'btn: "term-vmbtn"' in app and '"packs", "cli", "vm"' in app
    assert 'kind === "vm"' in app and 'splitKind === "vm"' in app and 'toggleSplitPane("vm")' in app
    # the pane is the VM page in session mode: no route of its own, nothing written with innerHTML
    assert "vm?embed=1&session=" in pane and "innerHTML" not in pane and "fetch(" not in pane


def test_session_mode_calls_only_the_existing_user_only_routes():
    js = (STATIC / "vm.js").read_text(encoding="utf-8")
    html = (STATIC / "vm.html").read_text(encoding="utf-8")
    for ident in ("vm-sess", "vm-sess-list", "vm-sess-new", "vm-sess-new-pick", "vm-sess-pick", "vm-sess-connect"):
        assert f'id="{ident}"' in html
    routes = set(re.findall(r"post\(`?\"?(api/[a-z/${}A-Z().,-]+)", js))
    assert "/revoke`, { session: SESSION }" in js and "/grant`, { session: SESSION, scopes }" in js
    assert "/create`, { id }" in js
    assert "연결을 해제합니다. 이 세션의 권한만 회수하며, VM은 정지되거나 삭제되지 않습니다" in js
    assert "innerHTML" not in js
