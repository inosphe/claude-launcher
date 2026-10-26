---
kind: decision
title: 에이전트의 카테고리 배치는 제안이 아니라 즉시 등록이다
tags: [category, category-tree, improv-worker-v2]
refs: [claunch-06fwj, src/claude_launcher/workflows/v2/improv-worker-v2.yaml]
date: 2026-10-23
author: s1962 (소급)
---
improv-worker-v2는 intake 뒤(category-tag)와 wrapup 뒤(category-refresh)에 세션과 이슈를 워크스페이스 카테고리 트리에 배치한다. 새 카테고리는 즉시 만들고, 제안 큐는 사람이 체크박스로 고를 때만 쓴다(사용자 결정, 2026-10-23). 겹치는 라벨은 409로 거절되어 한 키워드가 두 노드로 갈라지지 않는다.
