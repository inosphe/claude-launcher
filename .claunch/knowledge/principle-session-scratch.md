---
kind: principle
title: 중간 파일은 세션별 경로($CLAUNCH_SCRATCH)에 쓰고 /tmp 고정 이름을 쓰지 않는다
tags: [scratch, tmp, test-isolation]
refs: [AGENTS.md, tools/check_tmp_paths.py, claunch-wo1zj, claunch-gziru]
date: 2026-10-26
author: s1962 (소급)
---
`/tmp`는 머신 전역 한 디렉터리이고 세션 스무 개 이상이 동시에 쓴다. 같은 이름의 파일은 말없이 덮이고, 덮인 값 위의 판정은 그럴듯하게 틀린다(2026-08-26 mesh-0824 사례).

같은 이유로 테스트·재현은 실제 런처 홈을 쓰지 않는다: pytest는 conftest의 autouse `home`, 손 재현은 `tools/isolated_home.py`로 격리한다. 실제 홈을 덮은 사고(claunch-wo1zj, claunch-gziru)가 근거다.
