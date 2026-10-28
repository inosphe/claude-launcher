"""The ``round-report`` skill: the round report is the only artifact that
outlives the session.

A cflow round leaves its record in three places that never meet: the run
journal (step-by-step, machine-voiced), the board comments (scattered across
issues), and mesh fyi messages (one recipient, one send). A person who wants
to know what a round actually did has to read all three and stitch them.
So every round writes **one readable HTML page** — the only artifact that
survives the session's death.

This module owns the skill text; the worker workflow's ``wrapup`` step quotes
it by name instead of carrying the spec inline (the spec was the reason that
step's instructions blew past the reminder re-emission window — 79% of them
never reached the reminder). ``claunch install`` writes it alongside the
cflow, commit-stamp and mesh skills.
"""

from __future__ import annotations

from pathlib import Path

from . import fsplan

SKILL_MD = """\
---
name: round-report
description: >-
  Write the cflow round's final artifact — one readable HTML page the daemon
  indexes into the session's Reports block. Open this skill BEFORE writing
  the report at a wrapup (or any step whose output is a round report); it
  holds what the page must contain and how it must read.
---

# round-report — one HTML page that outlives the session

The run journal is step-by-step and machine-voiced, board comments are
scattered, a mesh fyi goes to one person once. None of them is something a
person opens later and reads. So the round writes one HTML page — the last
artifact it makes, and the only one that survives the session.

## Where to write it

Never choose the location yourself — `claunch report path --issue <이슈 id>`
stamps it (for a batch round, one page from the primary issue's id; name every
landed issue in the page's "회차의 목표와 범위" section):

    <데몬 dir>/reports/<세션>/<UTC스탬프>-<이슈>.html

The path sits outside the repository, so writing it keeps `git status` clean;
the same session + same issue gets the same path again, so rewriting
overwrites (never two half-reports). To file a page written elsewhere,
`claunch report save <파일>`.

## What the page must contain

- **한 것과 나온 것** — at the top, readable without scrolling. Three lines:
  what the round did / what that produced / what state things are in now
  (landed · request filed · frozen, with the issue id and tip). A reader who
  stops after the title still reads these three lines.
- **바뀐 뒤에 무엇이 어떻게 동작하는가** — the post-round state described in
  itself (전에는 이랬고 지금은 이렇다). What a reader wants is not "how many
  commits" but "what does this do now". So never structure the page as one
  section per commit or per issue — that is the order the author worked in,
  not the order a reader understands in. Hashes and issue ids live in the
  evidence section.
- **이 회차가 만든 것 자체** — if the round produced something people or
  agents will actually read or see, put it on the page verbatim —
  **산출물 자체를 페이지에 싣는다**: new prose,
  a config default, an API response shape, a screen, 이미 기록된 출력
  (output a command already printed into the journal, an issue comment, a
  sweep receipt or a file). The changed-files list and rationale explain it;
  they do not replace it. 다시 만들지 말고 커밋이나 기록에서 잘라 싣는다 —
  quote the bytes that already exist; do not re-run a command, a harness or a
  test to produce them. If the round's product is code only, this section may
  be empty — the diff carries it. Never invent a product to fill the section.
- **산출물과 검증은 다른 절이다** — the product is what the round made; whether
  it works is verification, and verification was done by the earlier steps of
  the round (targeted-test, review, the sweep). Writing this page does not
  verify anything again. 이 보고서는 테스트·스윕을 다시 돌리지 않고 어긋난
  값의 원인을 조사하지 않는다. When two records disagree (847 in one, 849 in
  another), write both values with both source paths; open each receipt at
  most once, to read the one line that states its condition. 기록에 없으면
  없다고 적는다 — a missing value is reported as missing, never measured now.
- 회차의 목표와 범위(제외한 것 포함), 이슈 id
- 무엇을 왜 그렇게 했는가 — changed files and the reasons for those choices
- 증거: 커밋 해시, 테스트 수치(표적 실행이면 무엇을 골랐는지), 빨갛다가
  고친 것 — never type hashes from memory; `claunch commits` already lists
  this session's stamped commits newest-first (the dashboard's Commits block
  is the same list). This page is what a person reads and that list is what a
  machine reads — when they disagree, the page is the wrong one. The deleted
  test-run values (start · duration · workers · volume) and what was kept,
  with paths and reasons, go here too.
- 착지 상태(상위 세션에 통합 요청됨 / 동결 — 브랜치 이름과 tip)
- 다음 사람에게 넘기는 것 — handoff 이슈 id, 못 한 것, 아는 구멍

An inheritance for the next person, not a self-review. A report that omits
its failures and skips passes this step and fails its purpose.

## How it must read

두괄식으로 쓴다 — lead with the conclusion; result above process. A reader of
the top three lines should be done; the reader who wants evidence scrolls.
And the page's spine is **the product, not the work history**.

Write it as a single standalone file — no external CSS, scripts or images.
The daemon serves the page under a CSP sandbox, so outside references are
blocked anyway, and a page of dead links is nothing in a few weeks.

## The file name is the index

The daemon indexes the file into the session detail view — it opens from the
dashboard session rail's Reports block. Rename it by hand and it drops out of
the index; an empty file at the path reads as no report (the step's verify
checks the format, the `<html` tag and a minimum size). Check with
`claunch report ls`.
"""


def write_skill(skills_dir: Path) -> Path:
    path = skills_dir / "round-report" / "SKILL.md"
    fsplan.write_text(path, SKILL_MD)
    return path
