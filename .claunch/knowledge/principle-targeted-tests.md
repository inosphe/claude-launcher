---
kind: principle
title: 워커는 바꾼 영역의 테스트만 돌리고 전체 스윕은 리더의 배치 큐가 돌린다
tags: [pytest, sweep, window, test]
refs: [src/claude_launcher/workflows/v2/improv-worker-v2.yaml]
date: 2026-10-26
author: s1962 (소급)
---
전체 스위트는 머신의 공유 테스트 창을 오래 점유해 다른 세션의 표적 테스트를 막는다. 그래서 워커의 머지 관문은 표적 단위 테스트이고, 전체 스위트는 머지 뒤 리더의 지속 큐가 머지 5건 단위 배치로 낮은 우선순위에서 돌린다.
