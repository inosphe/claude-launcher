# 월드 모델 시뮬레이션

claunch-efnbt. gds6 시뮬레이션 v2(`~/works/gds6`, `notes/designframe-sim-v2-alignment-s134.md`)의 결정적 걸음
엔진을 게임 기획 전용 부분을 빼고 일반화한 기능이다. 세션이 명세·코드·시나리오에서 형식 모델(상태 기계, 변수,
사건, ECA 규칙, 불변식)을 세우면, 데몬이 그 모델을 걸어서 다음을 찾는다.

| 종류 | 뜻 |
|---|---|
| `unhandled` | 걸음이 도달한 (기계, 상태, 사건) 칸에 받는 규칙이 없음. 그 기계에 그 사건을 받는 규칙이 다른 상태에는 있음 |
| `conflict` | 같은 우선순위의 규칙 둘이 같은 자리(기계·변수·타이머)를 다르게 바꿈. 둘 다 적용하지 않음 |
| `invariant` | 걸음 뒤 불변식이 거짓 |
| `stuck` | 일어날 수 있는 사건도, 걸린 타이머도 없음 |
| `loop` | 사건 연쇄(`emit`)가 16을 넘음 |
| `eval-error` | 식 계산 실패(모르는 이름, 형 불일치) |

모든 발견에는 witness(체크포인트, 씨앗, 사건열, 모름 가드에 대한 가정)가 붙고, 코드가 재생해 같은 자리를
다시 낸다(`sim_replay`).

## 구성

| 층 | 위치 | 하는 일 |
|---|---|---|
| 엔진 | `src/claude_launcher/sim/{expr,model,engine,rng}.py` | 식 언어(3값), 모델 검사, step·explore(Go-Explore 셀 보관소)·replay |
| 저장소 | `src/claude_launcher/sim/store.py` | 레코드·소유권·모델·라운드·발견 원장. 데몬이 유일한 기록자 |
| API | `src/claude_launcher/daemon/sim_api.py` | `/api/sims...`, 소유 세션에 알림 입력, 세션 종료 시 소유권 해제 |
| MCP | `src/claude_launcher/sim_mcp.py` | `sim_*` 도구. 바꾸는 호출은 `$CLAUNCH_SESSION`을 호출자로 보냄 |
| 화면 | `src/claude_launcher/web/static/sim-page.js` | `#/sims`(목록·새 시뮬레이션), `#/sims/<id>/<탭>` |

저장 위치는 `<런처 홈>/daemon/simulations/<id>/`(`sim.json`, `model.json`, `findings.json`, `rounds.jsonl`,
`journal.jsonl`, `submits.json`)이다. 세션이 어느 워크트리에 있든 같은 레코드를 읽는다.

## 소유권

- 소유 세션은 하나다(`owner`). 만든 세션이 소유자가 되고, 웹에서 만들면 고른 세션이 소유자가 된다.
- 소유 세션만 모델 변경·설정·실행·중지·라운드·발견 처리를 할 수 있다. 다른 세션은 읽기만 한다.
- 사용자(웹 UI, 바디에 `session`이 없는 호출)는 모든 일을 할 수 있고, 소유권 이관·해제·삭제·질문 답변도 한다.
- 소유 세션이 종료되면 데몬의 종료 훅이 소유권을 풀고, 실행 중이던 캠페인은 `stopped`가 된다. 데몬 시작 때도
  살아 있지 않은 소유자를 같은 방식으로 정리한다.
- 소유자가 없는 시뮬레이션은 세션이 `sim_claim`으로 가져갈 수 있다. 소유자가 살아 있으면 409로 거절하고,
  이관은 사용자가 한다.
- 호출자 구분은 데몬의 다른 게이트와 같은 신뢰 모델이다. 데몬은 토큰 하나를 쓰므로, 규칙은 MCP 도구를 거치는
  에이전트에게 강제된다.

## 캠페인과 알림

- 상태는 `idle`, `running`, `stopped`이고 `state_reason`이 이유를 적는다.
- `running` 동안에는 받아들여진 모델 변경마다 라운드를 다시 돈다.
- 라운드 뒤 열린 발견이 없으면 `idle`로, `max_rounds`에 닿으면 `stopped`로 끝난다.
- 캠페인이 끝나면(위 두 경우와 중지) 데몬이 소유 세션 터미널에 `[claunch simulation] ... finished` 한 줄을
  입력한다. 소유 세션은 `sim_show`/`sim_findings`로 결과를 읽고 사용자와 논의를 이어 간다.
- 사람 결정이 필요한 빈칸은 소유 세션이 `sim_resolve`의 `ask`로 올린다. 사용자가 Sims 탭에서 답하면 그 답이
  소유 세션에 입력되고, 세션은 출처 `user:<발견 키>`로 모델에 반영한다.
- 웹의 「새 시뮬레이션」은 레코드를 만들고 고른 세션에 생성 요청을 입력한다. 「소유 세션에 요청」은 임의의 말이나
  계속 진행 요청을 입력한다.

## 모델 규칙

- 규칙과 불변식에는 출처가 있어야 한다: `source: [{ref, quote}]`. `ref`는 시뮬레이션 루트(소유 세션의
  체크아웃) 기준 파일 경로이고, 데몬이 `quote`가 그 파일에 글자 그대로(공백 차이 무시) 있는지 확인한다.
  `issue:`, `user:`, `url:` 참조는 확인하지 않는다.
- 문서가 말하지 않는 규칙은 `provisional: {why}`로 표시한다. 걸음은 그 규칙도 쓰고, 그 규칙을 지나간 뒤의
  발견에는 `provisional` 목록이 붙는다.
- 아직 식으로 옮기지 못한 조건은 `{prose}`로 둔다. 걸음은 모름 가드를 정책 난수로 골라 witness에 가정으로 남긴다.
- 상태는 Markov여야 한다. 다음 걸음이 과거 서술에 기대면 변수가 하나 모자란 것이다.
- 모델 변경(`sim_patch`)은 검사를 통과하지 못하면 통째로 거절되고, 이유 목록을 돌려준다. `key`를 주면 같은
  키의 재제출은 한 번만 반영된다.
- 식·효과 형식 전체는 `sim_guide` 도구가 돌려준다.

## 설정

| 키 | 기본값 | 뜻 |
|---|---|---|
| `interpreter_profile` | 데몬 `simulation_interpreter_profile` | 해석(B)에 쓸 pi 프로필 |
| `judge_profile` | 데몬 `simulation_judge_profile` | 판정(C)에 쓸 pi 프로필(B와 다른 모델 권장) |
| `interpreter_reasoning`, `judge_reasoning` | `low` | `none`·`low`·`medium`·`high` |
| `seed`, `budget`, `horizon` | 1, 2000, 12 | 걸음 씨앗, 라운드당 걸음 수, 셀 하나에서 이어 가는 걸음 수 |
| `max_rounds` | 20 | 한 번 실행의 라운드 상한 |

화면의 프로필 선택지는 pi 하네스로 실행할 수 있는 프로필만 보여 준다(`GET /api/sims/profiles`). 설정은 언제든 바꿀
수 있고 다음 라운드·실행부터 적용된다.

## 단계

- S1(claunch-efnbt.1, 이 문서의 범위): 엔진, 저장소, 소유권, API, MCP, Sims 탭, 알림. 해석은 소유 세션이 직접 한다.
- S2(claunch-efnbt.2): 데몬이 실행마다 띄우는 pi-durable 실행 세션이 원천 형식화 → 빈칸 묶음 해석(B) → 판정(C, 다른
  모델)을 물음마다 새 durable 대화로 돌린다. 위 프로필·reasoning 설정을 쓴다. 제출 API는 S1의 키 멱등 제출을 쓴다.
- S3(claunch-efnbt.3): witness 재생 화면, carry, triage, 편집 제안.
