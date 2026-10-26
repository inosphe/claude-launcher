---
kind: decision
title: 워크플로 정본은 패키지 안 한 곳이고 프로젝트 레이어는 스크립트로만 맞춘다
tags: [cflow, workflow, 정본, drift]
refs: [AGENTS.md, tools/sync_project_layer.py, tests/test_project_layer_override.py]
date: 2026-10-26
author: s1962 (소급)
---
워크플로 문구·스텝·게이트는 `src/claude_launcher/workflows/`만 고친다. 프로젝트 레이어(`.claunch/workflows/`)는 정본에 `verify:`만 더한 사본이고 `tools/sync_project_layer.py`가 다시 쓴다.

이유: 두 파일에 같은 문구를 나란히 타이핑하면 다음 사람이 정본만 고치는 순간 같은 이름의 워크플로가 두 규칙으로 돈다. 드리프트는 테스트가 잡는다.
