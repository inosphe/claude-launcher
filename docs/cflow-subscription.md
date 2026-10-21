# 다른 세션의 cflow 상태 구독 — 현황과 설계안

- 이슈: `claunch-ijg51`
- 작성: 세션 `s1236`, 2026-10-21
- 상태: 설계안. 구현 전이며, 사용자가 방향을 고른 뒤 별도 회차에서 구현한다.
- 근거 표기: 「사실」은 코드·문서·명령 출력으로 확인한 것이다. 「추정」은 확인하지 못한 판단이다. 원인을 확인하지 못한 것은 「원인 미상」으로 적는다.

## 0. 요약

위임한 세션의 진행을 관제 세션이 따라가는 경로는 오늘 네 가지다: 자식이 손으로 보내는 mesh fyi, v1 `RunEventClock`의 4종 이벤트, `children`의 런 요약, 그리고 웹 대시보드의 폴링이다. 이 넷은 서로 다른 질문에 답하고, 어느 것도 "내가 지켜보는 런의 상태 변화를 커서로 따라간다"에 답하지 않는다.

정리하면 세 가지다.

1. **이벤트 원천은 이미 있다.** v1·v2 모두 append-only 저널을 쓰고, v2 체크포인트에는 단조 증가하는 `revision`이 있다(`cflow2/store.py:328-355`, `cflow2/checkpoint.py`). 새 이벤트 시스템을 만들 필요가 없다.
2. **배달 경로도 이미 있다.** 데몬은 세션 터미널에 텍스트를 주입하고, 백프레셔·수신 확인·재시도를 이미 갖고 있다(`daemon/mesh_policy.py:1-60`, `daemon/session.py:2651-2710`).
3. **빠진 것은 구독 기록과 커서다.** 누가 어떤 런의 어떤 이벤트를 어디까지 읽었는지를 저장하는 자리가 없고, v2 런의 전이를 통지하는 소비자도 없다.

이 문서는 그 빈틈을 세 선택지로 나누고, 1단계로 읽기 도구, 2단계로 구독을 권한다(5절). 미확정 결정은 7절에 모았다.

## 1. 문제

관제 세션(coordinator·operator·leader·PM·thread)이 다른 세션에 일을 위임한 뒤 따라가지 못하는 경로를 코드에서 확인한 순서로 적는다.

| # | 현상 | 근거 |
|---|---|---|
| P1 | 위임 자식이 워크플로 산문이 지정한 시점에만 손으로 mesh 보고를 보낸다. 그 시점 밖의 진행은 어떤 채널로도 나가지 않는다 | `workflows/v2/improv-worker-v2.yaml` `delegate-work`(1304행 부근): "보고: 부모에게 mesh 한 통. 완료면 `DELEGATE DONE: ...`" |
| P2 | v2 런의 전이를 통지하는 소비자가 없다. v1에는 `RunEventClock`이 4종(human-gate·round-done·orphaned·session-ended)을 보내지만, v2 쪽은 `_scan_v2_ends`의 kill-on-end뿐이다 | `daemon/cflow_clock.py:2946-3050`, `:3260` |
| P3 | 수신자가 spawn 부모 또는 mesh leader로 고정되어 있다. coordinator·PM·operator·임의의 peer는 받을 대상으로 지정될 수 없다 | `daemon/cflow_clock.py` `_recipient()`: 부모 → mesh leader 순 |
| P4 | 배달은 일회성 주입이고 커서·백필이 없다. 데몬 재시작과 대화 압축 뒤에 무엇을 놓쳤는지 알 방법이 없다 | `daemon/cflow_clock.py:3632` `_deliver`; 대기분 `_debt`는 메모리에만 있다 |
| P5 | pull 채널이 반쪽이다. `flow_status`는 자기 런만 읽고(다른 세션 런은 `authorize`가 거절), `children`의 런 요약은 v1만이며, v2 런 목록은 웹 API에만 있다 | `cflow2/runtime.py:2611-2627`, `daemon/cflow_clock.py:3910-3954`, `daemon/api.py:5721-5734` |
| P6 | v2 위임 자식이 보고 없이 종료하면 부모 노드는 계속 기다린다. `delegate-work`의 `done_when`은 자식 보고 도착을 전제하고, v2에는 `orphaned`에 해당하는 통지가 없다 | `improv-worker-v2.yaml` `delegate-work`; `daemon/cflow_clock.py:_exit_events`는 v1 상태만 읽는다 |

P6의 발생 조건은 확인했다: `session_exited` 훅은 자식 종료를 기록하지만, `_exit_events`가 `cflow_state.scopes_in`(v1)과 `cflow_engine.status`(v1)만 읽으므로 v2 자식은 통지 대상이 되지 않는다(`daemon/cflow_clock.py:3223-3290`). 부모가 그 사실을 아는 경로는 `children`을 다시 부르는 것뿐인데, 그 요약도 v1만 담는다.

## 2. 현재 있는 것

| 메커니즘 | 누가 받나 | 무엇을 | 한계 | 출처 |
|---|---|---|---|---|
| mesh `send` | 연결된 멤버 | 보내는 쪽이 쓴 텍스트 | 보내는 쪽이 기억해야 한다. 커서·백필 없음 | `daemon/mesh_policy.py`, `daemon/session.py:2662` |
| `RunEventClock` | v1 런의 spawn 부모, 없으면 mesh leader | human-gate·round-done·orphaned·session-ended | v2 없음. 수신자 고정. 일회성 | `daemon/cflow_clock.py:2946` |
| `children` | 자기 서브트리 | 자식의 v1 런 요약 | v2 런이 빠진다 | `daemon/api.py:9770` 부근, `cflow_clock.py:3910` |
| `opv2_poll` | operator 세션(자기 스코프) | Observer 이벤트, 게이트 대기, 진행 변화, 이탈 | operator 전용. 단계 전이·보고는 없다 | `daemon/operators_v2.py:1154-1230` |
| 웹 대시보드 | 사람 | v1 `status()` 페이로드, v2 `rail_entry` | 에이전트가 읽는 API가 아니다. 5초 폴링 | `daemon/api.py:4717`, `:5721`, `:5737` |
| `poll.yaml` | 런 자신 | 명령 출력 비교 | 턴을 소모한다. 마커를 손으로 관리 | `workflows/poll.yaml` |
| `flow_nudge.owed` | 사람(대시보드 카드) | 사람이 풀어야 하는 것 | 읽기 API가 없다. v1·v2를 모두 읽는다 | `daemon/flow_nudge.py:93-135` |

두 가지는 설계에 중요하다.

- **v1과 v2의 변경 토큰이 다르다.** v1은 `state.json`의 stat(`mtime_ns`, `size`)과 저널이고, v2는 체크포인트의 `revision`이 커밋마다 증가한다(`cflow2/store.py:404-419` — 상태 텍스트가 그대로이고 이벤트가 없으면 쓰기를 건너뛴다). 구독 커서를 한 형식으로 만들려면 이 차이를 흡수해야 한다.
- **배달에는 이미 상한이 있다.** mesh는 `inbox_max`·`min_gap`으로 백프레셔를 걸고, 미답변·미배달을 `owed`로 회계한다(`mesh_policy.py:1-60`). 구독 이벤트가 이 회계를 우회하면 안 된다 — 우회하면 다수 구독에서 터미널이 막힌다.

## 3. 설계 선택지

### A. 읽기 도구 (pull)

새 MCP 도구 하나로 다른 세션의 런 상태를 읽는다.

- 입력: 세션 이름 목록(또는 `children`·서브트리·프로젝트), `since`(선택).
- 출력: 슬롯마다 `{session, version(1|2), workflow, run, status, node, report(마지막 보고 요약), owed, updated}`와, `since`가 있으면 그 뒤의 저널 이벤트 요약.
- 권한: 읽기. 대상은 자기 서브트리와 같은 mesh에서 연결된 멤버, 그리고 (operator처럼) 같은 project.
- 구현 자리: v2는 `cflow2_web.rail_entry`와 `RunStore.journal_tail`, v1은 `engine.status`와 `journal.read`. `flow_nudge.owed`가 이미 두 버전을 함께 읽는 선례다.

장점: 코드가 작고, 모든 관제 세션에 즉시 효과가 있으며, 권한 모델이 단순하다. 단점: 관제 세션이 자기 루프에서 폴링해야 하고, `since` 없는 호출은 "놓쳤다"를 복구하지 못한다.

### B. 구독 (push + 커서)

데몬이 등록된 구독자의 커서를 따라 런 전이를 배달한다.

- tool: `subscribe(session, events?, cursor?)` / `unsubscribe` / `subscriptions`. 구독 기록은 데몬 상태에 저장한다(메모리 아님).
- 이벤트 원천: v2 체크포인트 `events` 테이블 + `revision`, v1 저널 + 상태 diff. 새 스캔을 만들지 않고 기존 `cflow2_clock` 패스에 편승한다(`claunch-2cnep`의 변경 기반 깨우기와 같은 자리).
- 이벤트 집합(초안): `run_started`, `node_entered`, `report`(보고 요약 한 줄), `gate`(사람이 풀어야 하는 것), `ask`(라우팅·타임아웃), `delegation_closed`, `run_done|failed`, `session_exited`.
- 배달: 이벤트를 모아 **한 통의 fyi 블록**으로 압축한다. mesh 백프레셔 회계를 그대로 쓰고, 구독자당 상한을 둔다.
- 백필: 구독자가 `subscriptions`를 부르면 커서 이후를 요약해 돌려준다. 원본은 저널에 있으므로 데몬 재시작·대화 압축 뒤에도 복구된다.

장점: 자식 워크플로의 산문을 고치지 않아도 되고, v1·v2가 한 경로로 모이며, 놓친 것을 되짚을 수 있다. 단점: 데몬 코드가 가장 크고, 이벤트 스키마·필터·압축 정책을 정해야 하며, 잘못 만들면 터미널 폭주가 된다.

### C. 오버시어 이벤트의 v2 확장

`RunEventClock`을 v2로 넓히고 수신자를 구독자 집합으로 일반화한다.

장점: 검증된 경로의 증분이다. 단점: 지금의 4종 이벤트로는 "상태 보고"를 담지 못하고, 일회성이라는 성질도 그대로다. 이 선택지는 B의 이벤트 소스 하나로 흡수하는 편이 낫다.

### D. 운영 패턴 (코드 없음)

`poll.yaml`로 관제 세션이 다른 세션의 `claunch cflow2 status`나 보드를 주기적으로 읽는다.

장점: 오늘 바로 쓸 수 있다. 단점: 턴을 소모하고, 마커 관리가 손이고, 세션이 늘면 비용이 선형으로 는다.

### 비교

| 기준 | A 읽기 | B 구독 | C v2 확장 | D 운영 패턴 |
|---|---|---|---|---|
| 새 코드량 | 작다 | 크다 | 중간 | 없다 |
| v2 커버 | 있다 | 있다 | 있다 | 있다 |
| 놓친 것 복구 | `since`가 있을 때 | 커서·백필로 | 없다 | 없다 |
| 자식 워크플로 수정 | 불필요 | 불필요 | 불필요 | 불필요 |
| 권한 위험 | 낮다(읽기) | 중간(대상 지정) | 낮다 | 낮다 |
| 턴 소모 | 호출마다 | 배달마다 | 배달마다 | 폴링마다 |

## 4. 권고

**B를 목표로 두고, 첫 단계는 A로 연다.**

- A는 B가 쓰는 읽기 계약(슬롯·상태·마지막 보고·owed)을 그대로 정의한다. B는 그 계약에 이벤트와 커서만 얹으면 되므로, A를 먼저 내면 B의 설계가 측정 가능한 형태로 굳는다.
- C는 B의 이벤트 소스 중 한 갈래로 흡수한다. 별도 기능으로 두면 같은 일을 두 곳에서 관리하게 된다.
- D는 A·B가 서기 전의 임시 운영으로만 남긴다.

1단계(A)의 최소 계약:

1. 도구 이름과 입력: `runs(sessions?, subtree?, project?, since?)`. 세션 이름은 `members`·`children`이 이미 쓰는 식별자를 그대로 쓴다.
2. 출력 필드는 v2 `rail_entry`의 필드 이름을 재사용한다(`status`, `workflow`, `run`, `current`, `owed`, `updated`). v1은 `engine.status`에서 같은 이름으로 옮긴다.
3. 권한: 읽기만. 자기 서브트리, 같은 mesh의 연결된 멤버, 같은 project. 그 밖은 거절하고 거절 사유에 가능한 범위를 적는다.
4. `since`는 슬롯별 변경 토큰(v2 `revision`, v1 저널 위치)으로 해석하고, 응답에 다음 커서를 싣는다.

2단계(B)에서 정할 것: 이벤트 집합, 압축 간격, 구독자당 상한, 구독 생성 권한, 구독자가 종료됐을 때의 정리.

## 5. 미확정 결정

| # | 결정 | 선택지 | 권고 |
|---|---|---|---|
| D1 | 구독·읽기 권한 범위 | (a) 자기 서브트리만 (b) 서브트리 + 연결된 멤버 (c) 같은 project 전체 | (b). 지금 `RunEventClock`의 수신자 규칙(부모 → leader)을 넓히는 최소 폭이고, operator의 project 스코프와 충돌하지 않는다 |
| D2 | 이벤트 해상도 | (a) 런 전이(노드 진입 포함) 전부 (b) 보고·게이트·종료만 (c) 보고 요약만 | (b). 노드 진입까지 배달하면 긴 런에서 터미널이 막힌다. 전이는 pull(A)로 본다 |
| D3 | 배달 형태 | (a) 즉시 주입 (b) 주기 다이제스트 (c) 즉시 + 커서 백필 | (c). 즉시성은 게이트·종료에만 필요하고, 나머지는 다음 호출에서 따라잡으면 된다 |
| D4 | 이번 작업의 범위 | (a) 설계만 (b) A 구현까지 (c) A+B 구현 | (b). A는 작고 즉시 효과가 있으며, B의 계약을 측정 가능하게 만든다 |

## 6. 이 문서가 다루지 않은 것

- v1 cflow 클록의 폴링→변경 기반 전환 자체는 `claunch-2cnep`의 몫이다. 이 문서는 그 전환이 만드는 변경 통지를 소비하는 쪽만 다룬다.
- coordinator의 위계 권한(`orphans`·`adopt`)은 `claunch-fnnrk`의 몫이다. 구독은 그 권한을 넓히지 않는다.
- 위임 ask가 아무에게도 닿지 않는 라우팅 결함은 `claunch-b0cd`의 몫이다. 구독은 라우팅을 고치지 않는다.

## 7. 근거

- `src/claude_launcher/daemon/cflow_clock.py:2946-3050` — `RunEventClock`의 목적과 이벤트 4종
- `src/claude_launcher/daemon/cflow_clock.py:3223-3290` — `session_exited`와 `_exit_events`(v1 상태만 읽는다)
- `src/claude_launcher/daemon/cflow_clock.py:3291-3330` — `_scan_v2_ends`(v2는 kill-on-end만)
- `src/claude_launcher/daemon/cflow_clock.py:3632-3690` — `_deliver`와 수신자 결정
- `src/claude_launcher/daemon/cflow_clock.py:3825-3900` — `event_block`의 문구
- `src/claude_launcher/daemon/cflow_clock.py:3910-3954` — `run_summary`(v1 전용)
- `src/claude_launcher/cflow2/runtime.py:2611-2627` — `authorize`: 다른 세션의 런 거절
- `src/claude_launcher/cflow2/store.py:328-355` — 저널 읽기, `journal_tail`
- `src/claude_launcher/cflow2/store.py:404-419` — 상태가 그대로면 쓰기를 건너뛴다
- `src/claude_launcher/cflow2/checkpoint.py` — `events` 테이블과 `revision`
- `src/claude_launcher/daemon/cflow2_web.py:873-970` — `rail_entry`의 필드
- `src/claude_launcher/daemon/cflow2_web.py:1111-1140` — `rail_entries`(세션별)
- `src/claude_launcher/daemon/api.py:5721-5750` — `/api/cflow2/runs`, `/api/cflow2/rail`
- `src/claude_launcher/daemon/api.py:9770-9830` — `children` 응답의 런 요약
- `src/claude_launcher/daemon/operators_v2.py:1154-1230` — `poll`의 커서·attention·progress
- `src/claude_launcher/daemon/flow_nudge.py:93-135` — `owed`: v1·v2를 함께 읽는다
- `src/claude_launcher/daemon/mesh_policy.py:1-60` — 백프레셔·owed·ack_timeout
- `src/claude_launcher/daemon/session.py:2651-2710` — `queue_delivery`
- `src/claude_launcher/daemon/session_events.py:1-60` — 세션 제어 이벤트 저장(200건)
- `src/claude_launcher/workflows/v2/improv-worker-v2.yaml:1304-1352` — `delegate-work`의 보고 규약
- `src/claude_launcher/workflows/v2/improv-coordinator-v2.yaml:92-113` — `coordinate`의 자식 보고 처리
- `src/claude_launcher/workflows/poll.yaml:1-40` — 폴링 런
