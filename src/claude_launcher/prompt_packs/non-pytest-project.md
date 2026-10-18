---
title: pytest 밖의 테스트 — window run
description: pytest가 아닌 테스트·빌드 명령(npm, cargo, go, make, 스크립트)을 쓰는 저장소에서 그 명령을 claunch 측정 window 안에서 실행하는 규칙. 그런 저장소의 세션이나 워크스페이스에만 추가한다.
---
## pytest 밖의 테스트·빌드 명령은 `claunch window run`으로 실행한다

이 머신의 테스트 실행은 데몬의 측정 window 하나로 조율된다. window를 잡은 실행만
대기열에서 순서를 기다리고, 웹 UI의 Window 페이지에 누가 무엇을 돌리는지
표시되며, 다른 세션의 전체 스윕(sweep)과 겹쳐 돌지 않는다. window 밖에서 돈
실행은 이 조율에 들어가지 않으므로, 그 시간에 측정한 다른 세션의 테스트가
부하 때문에 실패해도 원인을 추적할 기록이 남지 않는다.

pytest는 claunch가 설치한 플러그인(`claunch_window`)이 window를 대신 잡는다.
다음 명령은 그 플러그인이 적용되지 않으므로 직접 감싼다.

- pytest가 아닌 테스트 러너: `npm test`, `pnpm test`, `cargo test`, `go test`,
  `ctest`, `dotnet test`, 엔진·에디터의 자동화 테스트 등
- 머신 자원을 오래 쓰는 빌드: `make`, `cargo build`, `npm run build`, 엔진 빌드 등
- 테스트를 여러 개 띄우는 셸 스크립트
- claunch가 설치되지 않은 venv에서 도는 pytest(플러그인이 로드되지 않는다)

### 사용법

```
claunch window run [--class targeted|sweep] [--wait 초] [--workers N] [--label 설명] -- <명령> [인자...]
```

- window가 부여될 때까지 기다렸다가 명령을 실행하고, 명령이 끝나면 window를
  해제한다. 종료 코드는 그 명령의 종료 코드다. window를 받지 못하면 1을 낸다.
- `--class targeted`(기본): 일부 테스트·단일 빌드. 상한 안에서 다른 targeted
  실행과 함께 돈다.
- `--class sweep`: 전체 테스트. 머신에서 혼자 돈다. 필요한 경우에만 쓴다.
- `--wait`: 대기열에서 기다리는 최대 시간(초, 기본 1800).
- `--workers N`: 명령이 쓰는 병렬 폭(`-j N`, `--test-threads N` 등). 부여된 폭은
  환경변수 `$CLAUNCH_WINDOW_ADVISORY_N`에 들어 있으므로 명령의 병렬 옵션에 그
  값을 넘긴다.
- `--label`: Window 페이지에 표시할 설명(기본은 명령줄).

예:

```
claunch window run -- npm test
claunch window run --workers 4 -- sh -c 'cargo test -- --test-threads "$CLAUNCH_WINDOW_ADVISORY_N"'
claunch window run --class sweep --label "full suite" -- make check
```

### 규칙

- 테스트·빌드 명령을 window 밖에서 직접 실행하지 않는다. 오래 기다려야 한다는
  이유로도 마찬가지다.
- window가 거절되거나 대기 시간이 초과되면, 거절 사유(`claunch window status`에
  표시되는 보유자와 대기열)를 보고에 옮긴다. 대기열 순서 조정(`prioritize`)과
  즉시 부여(`force`)는 운영자의 명령이므로 세션에서 실행하지 않고 사용자에게
  요청한다.
- `claunch window run` 안에서 실행된 pytest는 이미 부여된 window를 이어받는다.
  같은 실행을 위해 window를 두 번 잡지 않는다.
- 백그라운드로 돌리면 `$!`로 받은 pid로 끝을 기다린다. `claunch window run`
  프로세스가 끝나야 window가 해제된다.
