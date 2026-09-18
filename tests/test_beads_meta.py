"""YAML front matter on a beads issue description (``beads_meta``).

The properties under test are the ones the dashboard and the board writes
depend on: an issue written before front matter existed still reads, a block
that is not a mapping is prose rather than metadata, and a read-modify-write
cycle keeps every key it did not come for.
"""

from __future__ import annotations

from claude_launcher import beads_meta


SPEC = "## 목표\n무언가를 한다\n\n## 출처\nsession s584\n"


def test_description_without_front_matter_parses_as_body():
    meta, body = beads_meta.parse(SPEC)
    assert meta == {}
    assert body == SPEC


def test_empty_description_is_not_an_error():
    assert beads_meta.parse(None) == ({}, "")
    assert beads_meta.parse("") == ({}, "")


def test_front_matter_is_split_from_the_body():
    text = f"---\nworkspace: claude-launcher\n---\n{SPEC}"
    meta, body = beads_meta.parse(text)
    assert meta == {"workspace": "claude-launcher"}
    assert body == SPEC


def test_unterminated_fence_is_prose():
    text = f"---\nworkspace: claude-launcher\n{SPEC}"
    meta, body = beads_meta.parse(text)
    assert meta == {}
    assert body == text


def test_block_that_is_not_a_mapping_is_prose():
    # A description that opens with a horizontal rule and a list is not an
    # issue carrying metadata; truncating it to the part after the second
    # rule would lose the operator's text.
    text = "---\n- one\n- two\n---\nbody\n"
    meta, body = beads_meta.parse(text)
    assert meta == {}
    assert body == text


def test_malformed_yaml_in_the_block_is_prose():
    text = "---\nworkspace: [unclosed\n---\nbody\n"
    meta, body = beads_meta.parse(text)
    assert meta == {}
    assert body == text


def test_render_of_parse_round_trips():
    text = f"---\nworkspace: claude-launcher\n---\n{SPEC}"
    meta, body = beads_meta.parse(text)
    assert beads_meta.render(meta, body) == text


def test_render_writes_no_fence_for_an_empty_mapping():
    assert beads_meta.render({}, SPEC) == SPEC
    assert beads_meta.render({"workspace": ""}, SPEC) == SPEC
    assert beads_meta.render(None, SPEC) == SPEC


def test_set_key_adds_a_block_to_a_plain_description():
    out = beads_meta.set_key(SPEC, beads_meta.WORKSPACE, "gds5")
    assert out == f"---\nworkspace: gds5\n---\n{SPEC}"
    assert beads_meta.parse(out) == ({"workspace": "gds5"}, SPEC)


def test_set_key_keeps_unknown_keys_and_the_body():
    text = f"---\nkind: spike\nworkspace: gds5\n---\n{SPEC}"
    out = beads_meta.set_key(text, beads_meta.WORKSPACE, "claude-launcher")
    meta, body = beads_meta.parse(out)
    assert meta == {"kind": "spike", "workspace": "claude-launcher"}
    assert body == SPEC


def test_set_key_with_an_empty_value_removes_the_key():
    text = f"---\nworkspace: gds5\n---\n{SPEC}"
    assert beads_meta.set_key(text, beads_meta.WORKSPACE, "") == SPEC
    assert beads_meta.set_key(text, beads_meta.WORKSPACE, None) == SPEC


def test_set_key_removing_the_last_key_removes_the_fence_too():
    text = f"---\nworkspace: gds5\n---\n{SPEC}"
    out = beads_meta.set_key(text, beads_meta.WORKSPACE, None)
    assert not out.startswith(beads_meta.FENCE)


def test_get_returns_the_default_when_nothing_is_recorded():
    assert beads_meta.get(SPEC, beads_meta.WORKSPACE, "") == ""
    assert beads_meta.get(None, beads_meta.WORKSPACE) is None
    text = f"---\nworkspace: ''\n---\n{SPEC}"
    assert beads_meta.get(text, beads_meta.WORKSPACE, "") == ""


def test_workspace_of_takes_the_issue_row():
    row = {"id": "claunch-0xvu", "description": f"---\nworkspace: gds5\n---\n{SPEC}"}
    assert beads_meta.workspace_of(row) == "gds5"
    assert beads_meta.workspace_of({"id": "x", "description": SPEC}) == ""
    assert beads_meta.workspace_of({"id": "x"}) == ""
    assert beads_meta.workspace_of(None) == ""


def test_non_ascii_workspace_name_survives_a_round_trip():
    out = beads_meta.set_key(SPEC, beads_meta.WORKSPACE, "설계")
    assert "설계" in out                       # not escaped to \uXXXX
    assert beads_meta.get(out, beads_meta.WORKSPACE) == "설계"
