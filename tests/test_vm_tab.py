"""The dashboard's VM tab (vm-setup-8s6): the read-only tart list the register
form uses, and the pages that name routes and elements."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from claude_launcher.daemon import vm_api
from claude_launcher.daemon.vmhub import Registry

from test_vm_frames import AGENT, Capture, _api

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
