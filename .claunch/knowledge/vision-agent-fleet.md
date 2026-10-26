---
kind: vision
title: claunch는 여러 에이전트 세션이 사람의 판단 아래 협업하는 작업장이다
tags: [vision, mesh, cflow, beads]
refs: [AGENTS.md, docs/cflow-v2.md]
date: 2026-10-26
author: s1962 (소급)
---
claunch는 Claude Code·codex 같은 에이전트 세션 수십 개를 한 데몬 아래 띄우고, 세션끼리 메시(mesh)로 말하고, 일감을 beads 보드로 나누고, 작업 절차를 cflow 워크플로로 고정하는 도구다.

방향:
- 세션이 할 수 있는 일은 세션이 하고, 되돌리기 어려운 결정(머지 정책, 데몬 재기동, 공유 브랜치, 외부 게시)은 사람이 대시보드에서 내린다.
- 사람이 터미널을 지켜보지 않아도 되도록, 사람이 답해야 할 것은 알림(thread, QA, flow_nudge)으로 올라온다.
- 대화에만 남은 결정은 다음 세션이 읽지 못한다. 결정·이유·니즈는 보드와 기록(이 Knowledge)에 남긴다.
