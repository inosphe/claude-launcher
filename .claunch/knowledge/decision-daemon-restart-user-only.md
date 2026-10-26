---
kind: decision
title: 데몬 재기동·정지는 사용자의 명령이다
tags: [daemon, restart, 권한]
refs: [AGENTS.md, src/claude_launcher/daemon/restart_gate.py]
date: 2026-10-26
author: s1962 (소급)
---
`claunch daemon restart`/`stop`은 붙은 모든 터미널과 관리 세션을 함께 내린다. 에이전트는 실행하지 않고 사용자에게 요청한다.

- 세션 안의 `restart`는 웹 승인 게이트로 간다. `stop`에는 게이트가 없으므로 에이전트는 어떤 경우에도 실행하지 않는다.
- 게이트를 피하는 경로(환경 변수 제거, 토큰으로 API 직접 호출)가 기술적으로 열려 있어도 금지다. 사용자 셸의 즉시 실행은 유지한다(사용자 결정).
- 워크플로에서 허용되는 경로는 스텝의 `restart:` 스크립트 하나다.
