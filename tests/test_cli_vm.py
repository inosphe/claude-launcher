"""``claunch vm``: the user's commands over /api/vm, against a fake client."""
from __future__ import annotations

import argparse
import json

import pytest

from claude_launcher import cli_vm, daemon_client


class Client:
    def __init__(self):
        self.calls = []
        self.answer = {"id": "a", "state": "running", "grants": ["view", "control"],
                       "changed": [], "inflight": 0}
        self.error = None

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if self.error:
            raise self.error
        return self.answer

    def get(self, path, **kw):
        return self.request("GET", path)


@pytest.fixture
def client(monkeypatch):
    c = Client()
    monkeypatch.setattr(cli_vm, "_client", lambda: c)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    return c


def run(*argv):
    parser = argparse.ArgumentParser()
    cli_vm.register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["vm", *argv])
    return args.func(args)


def test_add_registers_with_an_absolute_key_path(client, capsys):
    assert run("add", "a", "--ssh-key", "~/k") == 0
    method, path, body = client.calls[0]
    assert (method, path) == ("POST", "/api/vm")
    assert body["id"] == "a" and body["tart_name"] == "a" and body["ssh_key"].startswith("/")
    assert "~" not in body["ssh_key"]


def test_start_and_stop_post_to_the_vm_route(client, capsys):
    assert run("start", "a") == 0
    assert run("force-stop", "a") == 0
    assert [c[1] for c in client.calls] == ["/api/vm/a/start", "/api/vm/a/force_stop"]
    assert "a: running" in capsys.readouterr().out


def test_grant_names_the_scopes_and_exec_is_only_there_when_asked(client, capsys):
    run("grant", "a", "s1", "control")
    assert client.calls[-1][2] == {"session": "s1", "scopes": ["control"]}
    with pytest.raises(SystemExit):
        run("grant", "a", "s1", "root")


def test_a_refusal_is_printed_and_exits_1(client, capsys):
    client.error = daemon_client.DaemonClientError("POST /api/vm/a/start: vm_slots_full: 2 of 2",
                                                   status=409)
    assert run("start", "a") == 1
    assert "vm_slots_full" in capsys.readouterr().err


def test_a_managed_sessions_shell_is_refused_before_asking(client, monkeypatch, capsys):
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    assert run("grant", "a", "s1", "control") == 1
    assert client.calls == []
    assert "the user's to do" in capsys.readouterr().err


def test_listing_is_open_to_a_session(client, monkeypatch, capsys):
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    client.answer = {"vms": [{"id": "a", "state": "running", "channel": "up",
                              "ip": "1.2.3.4", "controller": "s1", "human": "idle",
                              "grants": {"s1": ["view", "control"]}}], "slot_limit": 2}
    assert run("list") == 0
    out = capsys.readouterr().out
    assert "a" in out and "up" in out and "s1" in out and "view+control" in out


def test_takeover_and_return(client, capsys):
    run("takeover", "a")
    run("return", "a")
    assert [c[2]["action"] for c in client.calls] == ["driving", "idle"]


def test_json_output_is_the_daemons_answer(client, capsys):
    run("start", "a", "--json")
    assert json.loads(capsys.readouterr().out)["state"] == "running"


def test_preset_commands_post_to_the_preset_routes(client, capsys):
    client.answer = {"name": "gold", "source": "a", "presets": [
        {"name": "gold", "source": "a", "description": "d", "present": False}],
        "vm": {"id": "n", "state": "running"}, "started": True}
    assert run("preset", "save", "a", "gold", "-d", "clean") == 0
    assert run("preset", "list") == 0
    assert run("preset", "new", "gold", "n") == 0
    assert run("preset", "describe", "gold", "text") == 0
    assert [(c[0], c[1]) for c in client.calls] == [
        ("POST", "/api/vm-presets"), ("GET", "/api/vm-presets"),
        ("POST", "/api/vm-presets/gold/create"), ("POST", "/api/vm-presets/gold/describe")]
    assert client.calls[0][2] == {"vm": "a", "name": "gold", "description": "clean"}
    out = capsys.readouterr().out
    assert "tart copy missing" in out and "n: running" in out


def test_preset_rm_needs_yes_and_then_names_the_preset(client, capsys):
    assert run("preset", "rm", "gold") == cli_vm.EXIT_REFUSED
    assert client.calls == []
    assert "--yes" in capsys.readouterr().err
    assert run("preset", "rm", "gold", "--yes") == 0
    assert client.calls == [("POST", "/api/vm-presets/gold/delete", {"confirm": "gold"})]


def test_a_session_shell_cannot_use_preset_commands(client, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    assert run("preset", "list") == cli_vm.EXIT_REFUSED and client.calls == []


def test_expose_and_allow_commands(client, capsys):
    client.answer = {"id": "a", "expose": ["2222:22"], "allow": ["192.168.1.213/32"], "net_pending": True}
    assert run("expose", "a", "add", "2222:22") == 0
    assert run("expose", "a", "rm", "2222") == 0
    assert run("allow", "a", "add", "192.168.1.213") == 0
    assert run("allow", "a", "rm", "192.168.1.213/32") == 0
    assert run("expose", "a", "list") == 0
    assert client.calls == [
        ("POST", "/api/vm/a/expose", {"action": "add", "external": "2222", "internal": "22"}),
        ("POST", "/api/vm/a/expose", {"action": "rm", "external": "2222"}),
        ("POST", "/api/vm/a/allow", {"action": "add", "cidr": "192.168.1.213"}),
        ("POST", "/api/vm/a/allow", {"action": "rm", "cidr": "192.168.1.213/32"}),
        ("GET", "/api/vm/a", None)]
    out = capsys.readouterr().out
    assert "expose: 2222:22" in out and "allow:  192.168.1.213/32" in out and "restart the VM" in out


def test_expose_needs_a_value_and_the_pair_shape(client, capsys):
    assert run("expose", "a", "add") == cli_vm.EXIT_REFUSED
    assert run("expose", "a", "add", "2222") == cli_vm.EXIT_REFUSED
    assert client.calls == []


def test_a_session_shell_cannot_change_the_network_settings(client, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    assert run("allow", "a", "add", "1.2.3.4") == cli_vm.EXIT_REFUSED and client.calls == []
