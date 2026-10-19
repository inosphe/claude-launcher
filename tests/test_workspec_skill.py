"""The ``work-spec`` skill (``workspec_skill``): the spec it quotes passes the
validator, and every command it tells an agent to run exists."""

import re

from claude_launcher import cli, workspec, workspec_skill


def test_the_quoted_template_is_the_valid_one():
    body = workspec_skill.SKILL_MD
    quoted = re.search(r"```yaml\n(.*?)```", body, re.S).group(1)
    assert quoted == workspec.TEMPLATE
    assert workspec.validate(workspec.load(quoted)) == []


def test_every_spec_command_it_names_exists():
    named = set(re.findall(r"claunch spec (\w+)", workspec_skill.SKILL_MD))
    assert {"check", "preview", "plan", "import"} <= named
    parser = cli.build_parser()
    for verb in named:
        args = ["spec", verb, "x.yaml"]
        assert parser.parse_args(args).func, verb


def test_write_skill(tmp_path):
    path = workspec_skill.write_skill(tmp_path)
    assert path == tmp_path / "work-spec" / "SKILL.md"
    assert path.read_text(encoding="utf-8") == workspec_skill.SKILL_MD
