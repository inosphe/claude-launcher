"""The ``work-spec`` skill: how an agent writes a work spec that imports.

A work spec (``docs/work-spec.md``, :mod:`claude_launcher.workspec`) is the
one YAML document that sets up a whole job -- mesh, agent tree, issues,
contracts. An agent asked to draft one has the format only if someone points
it at the doc; what it does not have is the loop that makes a draft land:
which rules the validator enforces, the commands that check and preview it
without writing anything, and the fact that the import is the person's to
run. This module owns that text; ``claunch install`` writes it beside the
other skills. Its template is :data:`workspec.TEMPLATE`, the same one the
``#/spec`` page inserts, so the skill never quotes a spec that does not pass.
"""

from __future__ import annotations

from pathlib import Path

from . import fsplan, workspec

SKILL_NAME = "work-spec"

SKILL_MD = """\
---
name: work-spec
description: >-
  Write or revise a claunch work spec -- the one YAML document (spec: 1) that
  sets up a whole job: mesh, leader choice, agent tree (sub-leads, workers,
  reviewers, subroles), beads issues with dependencies, contracts and open
  decisions. Use when asked to draft a work spec, turn a plan or an issue-gen
  output into one, or fix a spec that `claunch spec check` or the dashboard's
  Spec page (#/spec) rejects. NOT for running the import: that is the
  person's (`claunch spec import` or the page's import button).
---

# work-spec -- write a spec that checks, previews and imports

The format is `docs/work-spec.md` in the claunch repository; the worked
example is `docs/examples/work-spec.example.yaml`. This skill is the loop
and the rules the validator enforces.

## The loop

1. Start from the template below (the Spec page's 예시 템플릿 button inserts
   the same text) or from the example. Save it as a `.yaml` file in your
   scratch directory (`$CLAUNCH_SCRATCH`), not in the work tree.
2. `claunch spec check <file>` -- prints `ok`, or one line per problem as
   `<file>:<line>: <path>: <message>`. Fix every line, rerun until `ok`.
   The validator reports shape errors (a mapping or a list where a scalar
   belongs) first and alone: fixing those can reveal more.
3. `claunch spec preview <file>` -- the sessions the import would start
   (the agent tree under the leader, each with its issue queue) and the
   issues in their start order (`wave`: 0 = nothing blocks it). Read it
   against what was asked: is every issue owned, does every worker have
   work, does the order match the dependencies you meant?
4. `claunch spec plan <file> --leader own|shared:<session>` -- the exact
   actions, in order. Nothing is written.
5. Hand the file over. The import creates issues and sessions; it is the
   person's to run, with the leader choice that is theirs to make. Do not run
   `claunch spec import` unless they told you to.

The Spec page (#/spec) does the same: it checks while you type, every error
has a 줄 N button that selects its line, and a valid spec shows the session
and work preview before any leader is chosen.

## Rules the validator enforces

- `spec: 1`; `name` is a lowercase slug (`[a-z0-9-]`, max 48) -- it names
  the mesh (`spec-<name>`) and every issue's external ref
  (`spec:<name>:<key>`). Unknown keys are rejected at the top level, in
  `agents`, in `issues` and in `issues[].contract`.
- **Leader**: never listed in `agents`; refer to it as `leader` (reserved).
  `leader.mode` is `ask` (default -- the importer decides), `own` or
  `shared`. `mesh.mode: attach` needs `mesh.name` and makes the mesh's
  current leader the leader, so `own` is refused there.
- **Agents**: `id` slug, `kind` is `sub-lead` / `worker` (default) /
  `reviewer`. `parent` is `leader` (default) or a `sub-lead` -- workers and
  reviewers hold no children. Depth below the leader is at most 3. `role`
  may not be an exclusive role (a second command line is `kind: sub-lead`).
  `subroles` must be defined under the top-level `subroles:` with a
  `prompt`, and a subrole may not reuse a mesh role's name. `connect` names
  other agent ids.
- **Queues live on issues**: give `issues[].assignee` an agent id (or
  `human`); never list work under an agent. The first issue by priority is
  the spawn's issue, the rest are assigned to it.
- **Issues**: `key` unique (`[A-Za-z0-9._:-]`), `title` required, `type`
  one of epic/task/bug/doc/chore/feature, `priority` 0-4 (default 2).
  `parent` must be an epic in this spec. An epic is assigned only to a
  sub-lead (its stack collects the children); a doc is not assigned.
  `review` names a `reviewer` agent. `blocked_by` names keys in this spec and
  may not form a cycle. `goal` / `scope` / `done_when` become the issue body.
- **References** `kind` file/url/text/issue with the value under the key of
  that name. **Contracts** need `kind` (seam/event/store/interface/policy)
  and `text`; `parties` are agent ids. Issues cite them by id in `refs` /
  `contracts`; a contract for one issue only goes in `issues[].contract`.
- **Decisions** need `key` and `question`; `gates` lists the issue keys
  that wait for the answer.

## Writing it well

- One issue is one landing: what one worker finishes and lands. Split by
  what can be reviewed and merged on its own, and say the order with
  `blocked_by` rather than in prose -- the preview's waves and the board's
  dependency graph read only `blocked_by`.
- Use a sub-lead when three or more workers touch one area: give it the epic
  of that area and put the workers under it, so the leader integrates one
  branch.
- Every agent should own at least one issue; an agent with an empty queue
  spawns idle. A reviewer owns none and is named by `review`.
- Put a rule shared by several issues in `contracts` (it becomes a doc
  issue every party reads) instead of copying it into each `goal`.
- An open question that blocks work is a `decision` with `gates`, not a
  sentence in a goal: the import marks the gated issues `BLOCKED`.

## Template

```yaml
""" + workspec.TEMPLATE + """```
"""


def write_skill(skills_dir: Path) -> Path:
    path = skills_dir / SKILL_NAME / "SKILL.md"
    fsplan.write_text(path, SKILL_MD)
    return path
