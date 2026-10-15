"""The MCP server instructions stay short and point at the mcp-* prompt
packs that carry the guidance (claunch-8do04).

Claude Code keeps only the first 4096 characters of a server's
instructions; at 5128 the threads section was cut mid-sentence and its
guidance never reached the agent."""

import pytest

from claude_launcher import mcp_server, prompt_packs, prompt_packs_mcp

#: What a client is measured to keep (Claude Code: 4096); the budget leaves
#: room under it so one more pointer does not tip the tail off again.
BUDGET = 3000

MCP_PACKS = ["mcp-threads", "mcp-qa", "mcp-sim"]


def test_the_merged_instructions_fit_the_client_window():
    assert len(mcp_server.SERVER.instructions) <= BUDGET


@pytest.mark.parametrize("pack_id", MCP_PACKS)
def test_each_tool_family_points_at_its_shipped_default_pack(pack_id):
    assert f"'{pack_id}'" in mcp_server.SERVER.instructions
    assert pack_id in prompt_packs.DEFAULT_PACKS
    pack = prompt_packs.shipped(pack_id)
    assert pack.title.startswith("MCP")


def test_the_threads_pack_says_how_to_alert_the_user():
    body = prompt_packs.shipped("mcp-threads").body
    # an existing thread first, a new one with a title the agent chooses
    assert "thread_list" in body and "thread_reply" in body and "thread_open" in body
    assert "notice card" in body
    assert "qa_ask thread=<id>" in body


def test_prompt_pack_reads_a_pack_and_lists_them(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(prompt_packs_mcp, "_workspace", lambda: None)
    names = {t["name"] for t in mcp_server.TOOLS}
    assert "prompt_pack" in names
    doc = mcp_server.SERVER.dispatch("prompt_pack", {"id": "mcp-qa"})
    assert doc["id"] == "mcp-qa" and doc["source"] == prompt_packs.PACKAGE
    assert "qa_ask" in doc["body"]
    listed = {p["id"] for p in mcp_server.SERVER.dispatch("prompt_pack", {})["packs"]}
    assert set(MCP_PACKS) <= listed
    with pytest.raises(prompt_packs_mcp.PromptPackMcpError):
        mcp_server.SERVER.dispatch("prompt_pack", {"id": "no-such-pack"})
