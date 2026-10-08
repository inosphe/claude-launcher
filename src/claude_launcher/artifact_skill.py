"""The ``claunch-artifact`` skill: an HTML page for any purpose, filed where reports go.

The improv workflows end a round with one HTML page, the round report
(:mod:`claude_launcher.reports`). Sessions are also asked for pages *mid*-round
and outside any workflow (a design note, a comparison table, a measurement
someone wants to read in a browser), and before this skill there was no
answer to "where does that go". Written into the repository it dirties the
tree the preflight and the sweep read; written to ``/tmp`` it is gone and
shared with every other session; filed as the round report it overwrote the
real one and passed the wrapup gate in its place.

The answer is the report store itself, with a purpose name in the filename
(``claunch report path --name <purpose>``). The store does not depend on the
board: the issue in the filename is a label for the board's lookup, and a page
with no issue (no board in this workspace, or a page about no issue) is filed
as ``no-issue`` and found through the session and the Reports page. That is
what resolves "where does an artifact go when there is no beads board": it
was never stored on the board in the first place.

This module owns the skill text; ``claunch install`` writes it alongside the
other skills.
"""

from __future__ import annotations

from pathlib import Path

from . import fsplan

SKILL_NAME = "claunch-artifact"

SKILL_MD = """\
---
name: claunch-artifact
description: >-
  Make an HTML page (an artifact) for any purpose and publish it to claunch's
  report store, where the daemon indexes and serves it: a design note, a
  comparison, a measurement, a write-up someone asked for mid-round. Use
  inside a claunch session (CLAUNCH_SESSION is set) whenever you are asked to
  make, publish, file or share an artifact, an HTML page, a report or a
  write-up for a person to read, at any point, not only at a workflow's
  wrapup. Also explains where it goes when the workspace has no beads board.
---

# claunch-artifact: a page for any purpose, filed where reports go

claunch keeps one store of HTML pages per session:
`<daemon dir>/reports/<session>/`. The improv workflows write the round
report there at wrapup. This skill files any other page in the same store
under a purpose name, so it is indexed, served and found the same way, and
never mistaken for the round report.

## Steps

1. **Pick a purpose name**: lower-case words joined by single hyphens, at
   most 48 characters, e.g. `design-note`, `perf-compare`, `api-shape`. The
   name says what the page is for; it is part of the filename and of the
   index.
2. **Pick the issue the page is about** (see "Which issue" below), or none.
3. **Ask where to write it**, and write exactly there:

       claunch report path --name <name> [--issue <id>]

   The answer is `<UTC stamp>-<issue>--<name>.html` (`no-issue` in the issue
   half when you give none). Asking again with the same name and issue gives
   the same file back, so revising the page overwrites it. `--new` mints a
   second file when you want to keep the earlier version. A page you already
   wrote elsewhere is filed with
   `claunch report save <file.html> --name <name> [--issue <id>]`.
4. **Write one standalone HTML file.** No external CSS, scripts, fonts or
   images: the daemon serves the page under `Content-Security-Policy:
   sandbox`, and a page whose links die is unreadable in a few weeks. Inline
   what you need. It must contain an `<html` tag and be at least 400 bytes,
   or it is not counted as a page.
   Put the conclusion first: what the page is for and what it found, then
   the evidence. Carry the thing itself (the text, the table, the numbers,
   the command output), not a description of it.
5. **Confirm it is filed**: `claunch report check --name <name>` exits 0,
   and `claunch report ls --name <name>` shows its path and URL.
6. **Tell the requester where it is**: the file path and the dashboard URL
   (`/api/sessions/<session>/reports/<file>`, from `ls --json`). It also
   appears in the session rail's Reports block and on the Reports page.
7. **If the page is about an issue, point the board at it**:

       claunch beads comments add <id> "ARTIFACT: <name> <path>"

   The issue page already lists every page filed under its id; the comment
   is what a reader of the issue's history sees.

## Which issue

- The issue the page is about. Usually the issue this session is working
  (the `issue: <id>` line in the opening, or the run's `inputs.issue`).
- A page about a different issue carries that issue's id.
- A page about no issue carries none. Do not create an issue only to give a
  page an id: the issue is a label for lookup, not where the page is stored.

## When there is no beads board

The page is stored in the report store, never on the board, so a missing
board changes only the label and the board comment:

- Run step 3 without `--issue`. The page is filed as `...-no-issue--<name>.html`.
- Skip step 7. There is nothing to comment on, and it is not an error.
- The page is found through the session (`claunch report ls`, the session
  rail) and through the machine-wide Reports page, which lists every page on
  this machine, including those of sessions that have since ended.
- Do not move the page into the repository to make it findable: a file in
  the working tree dirties the tree the preflight and sweep compare against.

## Outside a claunch session

`claunch report` needs a session to key the store: `$CLAUNCH_SESSION`, or
`--session <name>`, or the one cflow run in this directory. With none of
them it refuses. Do not invent a session name to get past that; a page
filed under a made-up name is indexed under a session that never existed.
Write the page where the user asks (or to your scratchpad) and give the user
the path; say that it is not indexed by claunch.

## What this is not

- **Not the round report.** A workflow's wrapup asks for the round report:
  `claunch report path` *without* `--name`. An artifact does not replace it,
  and `claunch report check` without `--name` ignores artifacts, so filing
  one never satisfies the wrapup gate.
- **Not an external publication.** Publishing to a service outside this
  machine (a hosted page, a gist, a chat channel) is an outward-facing
  action: do it only when the user asks for it, in addition to filing the
  page here.
"""


def write_skill(skills_dir: Path) -> Path:
    path = skills_dir / SKILL_NAME / "SKILL.md"
    fsplan.write_text(path, SKILL_MD)
    return path
