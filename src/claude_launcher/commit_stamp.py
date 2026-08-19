"""The ``commit-stamp`` skill: a commit says which session made it, and where.

A fleet of agents committing to one repository is unreadable afterwards:
``git log`` says what changed and when, but every commit is signed by the one
human whose credentials the agents share, so nothing says *which agent* did
the work or *in which checkout* it happened. Those two facts exist only at
commit time — the session name in ``CLAUNCH_SESSION``, the worktree in the
path underfoot — and are gone the moment the session is cleared or the
worktree pruned. Stamping them into the message as git trailers is the only
place they survive.

This module owns the skill text; ``claunch install`` writes it alongside the
cflow and mesh skills, so every install scope that teaches an agent to work
also teaches it to sign that work.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD = """\
---
name: commit-stamp
description: >-
  Stamp every git commit with which claunch session made it and in which
  worktree. Use whenever you are about to run `git commit` or compose a
  commit message while CLAUNCH_SESSION is set (i.e. inside a claunch-managed
  session), and keep applying it to every commit for the rest of the session.
---

# commit-stamp — a commit says which session made it, and where

Several agents commit to one repository under one human's name. The commit
message is the only place the real authorship survives the session, so end
every commit message you write with these git trailers:

    Claunch-Session: <session>
    Claunch-Worktree: <worktree>

## Where the values come from

- **Session** — the `CLAUNCH_SESSION` environment variable, set inside every
  claunch-managed session. If it is unset you are not in one: omit the
  trailer entirely. Never invent or guess a name — a wrong stamp is worse
  than none, because it reads as true.
- **Worktree** — only when the commit happens in a *linked* git worktree:
  `git rev-parse --git-dir --git-common-dir` prints two paths, and they
  differ exactly when you are in one. The name is the checkout directory's
  path below its `worktrees` parent (usually just the last path segment of
  `git rev-parse --show-toplevel`). In the main checkout, omit the trailer —
  a repository is not a worktree of itself.

An omitted fact stays omitted. Placeholders like `none` or `main` would
poison the searches below with rows that answer nothing.

## How to write them

Trailers live in the message's final paragraph — after a blank line, one per
line, alongside any `Co-Authored-By` you already add:

    fix: reconnect the relay after a dropped heartbeat

    The uplink treated one missed heartbeat as fatal, so a busy backend
    dropped every session's viewer at once.

    Claunch-Session: worker_2
    Claunch-Worktree: relay-fix
    Co-Authored-By: Claude <noreply@anthropic.com>

Spell the keys exactly `Claunch-Session` and `Claunch-Worktree`: trailers
are machine-readable only while the spelling is stable —

    git log --format='%h %(trailers:key=Claunch-Session,valueonly)'
    git log --grep='Claunch-Worktree: relay-fix'

— which is how a human later answers "what did that session actually do" and
"which commits came out of that checkout" without reading prose.

This applies to commits **you** make. Never rewrite other people's commits to
add stamps, and never amend an already-pushed commit for a missing one.
"""


def write_skill(skills_dir: Path) -> Path:
    path = skills_dir / "commit-stamp" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_MD, encoding="utf-8")
    return path
