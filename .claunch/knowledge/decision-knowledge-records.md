---
kind: decision
title: 암묵지는 Knowledge 기록으로 명시하고 회차 앞뒤에서 참조·기록한다
tags: [knowledge, knowledge-base, improv-worker-v2, A/B]
refs: [claunch-269q1, src/claude_launcher/knowledge.py]
date: 2026-10-26
author: s1962
---
사용자 요구(claunch-269q1): 의사결정 과정, 목표의 비전, 니즈 정렬 기록을 이슈·세션 기록에서 간접 참조하던 것을 명시적인 기록으로 남기고 색인해서 에이전트가 작업 목표의 맥락을 갖게 한다.

초안의 결정:
- 저장소 두 개를 한 색인으로 읽는다: git이 추적하는 `.claunch/knowledge/*.md`(리뷰를 거치는 기록)와 워크스페이스별 SQLite(세션·사람이 바로 쓰는 기록).
- 검색은 어휘 기반(부분 문자열)이다. 임베딩(RAG)·Jev 연동은 후속이다.
- improv-worker-v2: category-tag 뒤 `knowledge-recall`(시작 입력 `knowledge_recall`, 기본 false — 참조 효과를 A/B로 비교하기 위해), category-refresh 뒤 `knowledge-record`(`knowledge_record`, 기본 true). 둘 다 `set_by: [user]`라 런 도중에 바꿀 수 있다.
- 웹 대시보드의 Knowledge 탭은 Hive 옆에 둔다.
