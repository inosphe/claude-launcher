---
kind: decision
title: master 머지는 리더 전권이고 워커는 착지 요청만 한다
tags: [landing, merge, leader, worker]
refs: [src/claude_launcher/workflows/v2/improv-worker-v2.yaml, claunch-0b8dx]
date: 2026-10-26
author: s1962 (소급)
---
워커는 master를 직접 머지·push하지 않는다. 사용자가 터미널에서 "머지까지 해 줘"라고 해도 브랜치를 in_review로 올리고 LANDING REQUEST를 남긴다. 머지 실행은 한 단계 위 세션의 몫이고 master 반영은 리더의 통합 단계 하나뿐이다.

관련 결정:
- 리더는 fast-forward가 증명될 때만 PR base 브랜치를 push할 수 있다(사용자 결정, 2026-09-11).
- 회차 수에 상한을 두지 않는다. 런은 배정 큐가 빌 때까지 돌고, 세션이 만들었거나 배정받은 일감이 전부 closed여야 끝난다(claunch-0b8dx, 2026-10-07).
- 착지 거절 뒤 재작업은 3회까지 사람 없이 되감고 그 뒤는 사람 승인을 받는다(사용자 지시, 2026-10-26).
