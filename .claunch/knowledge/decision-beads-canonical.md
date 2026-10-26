---
kind: decision
title: 일감의 정본은 beads 보드이고 issues.jsonl은 손으로 고치지 않는다
tags: [beads, board, 정본]
refs: [AGENTS.md, claunch-hjwvb]
date: 2026-10-26
author: s1962 (소급)
---
세션이 한 회차를 쓸 일, 다른 세션으로 넘어갈 일은 beads 이슈로 존재한다. 호출은 `claunch beads <br 인자>`.

이유: 보드의 실체는 `.beads/beads.db`이고 `issues.jsonl`은 내보낸 산출물이다. 손으로 쓴 줄은 다음 `sync --flush-only`에서 경고 없이 사라지고, DB에 없는 줄은 stale 가드로 flush 전체를 멈춘다. 그래서 jsonl은 git에서도 추적하지 않는다(claunch-hjwvb).

실행 기록(저널), 승인·선택(cflow), 순간의 사건(mesh 메시지)은 beads에 두지 않는다.
