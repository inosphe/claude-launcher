# plan 미결정과 coding-approval 게이트

이슈: `claunch-rzepk`.
이 문서는 두 신호의 관계와 순서를 정리한다. 사용자 확인은 2026-11-02에
끝났고, 구현은 다음 회차의 몫이다. 이 회차는 코드를 바꾸지 않는다.
1절의 파일·줄 인용은 `origin/master @ 3223ff4d`의 트리에서 셌다. 줄 번호는
그 트리에서만 뜻이 있으므로, master가 움직이면 같은 이름(노드 id·조건·
`otherwise: human`)의 줄을 새 트리에서 다시 센다.

## 1. 확인한 사실 (현재 코드)

| 항목 | 현재 동작 | 근거 |
|---|---|---|
| 계획과 승인 brief의 생성 | `coding-plan` 한 단계가 plan 패널 기록(`plan_set`)과 승인용 brief를 함께 만든다 | `src/claude_launcher/workflows/v2/improv-worker-v2.yaml:1750`(`coding-plan`), `:614` |
| 계획을 쓰는 자리 | `plan-panel` 조각이 `coding-plan`(그 단계가 없으면 `work`)에서 `plan_set`을 쓰라고 정한다 | `improv-worker-v2.yaml:614`, `:639` |
| 게이트의 조건 | `coding-approval-check`는 `require_coding_approval`과 `approve-always` 답만 본다. plan의 `waiting`은 보지 않는다 | `improv-worker-v2.yaml:5293`(`coding-approval-check`) |
| 게이트가 서지 않는 자동 경로 | `session_routing=true`이고 `require_coding_approval=false`면 `route-planned`로 간다 | `improv-worker-v2.yaml:5293`, `:5313`(`route-planned`) |
| approve-always 이후 | `session_routing=false`면 게이트와 `coding-plan`을 함께 건너뛰고 계획은 그때 `work`에서 처음 쓴다. `session_routing=true`면 `coding-plan`은 그대로 돌고 게이트만 서지 않는다 | `improv-worker-v2.yaml:5293`, `:5295`, `:5314` |
| plan 결정의 전달 | 사용자가 고른 결정은 `[claunch plan]` 블록으로 세션 터미널에 온다. 계획을 그 갈래에 맞게 다시 쓰라는 안내가 붙는다 | `src/claude_launcher/daemon/plan_api.py:77` |
| plan과 cflow의 연결 | plan 결정이 cflow ask를 닫는 코드 경로가 없다 | `grep -rn "plan_store\|\[claunch plan\]" src/claude_launcher/cflow2` 결과 0건 |
| plan의 대기 판정 | `summary.waiting`은 사용자가 지금 답할 수 있는 항목 수다(미결정, 미확인 verify, 미처분 followup) | `src/claude_launcher/plan/store.py:280` |
| 알림 표면 | cflow의 `urgent` ask는 대시보드 알림 카드가 된다. plan의 미결정은 레일의 ☰ plan 표시만 켠다 | `src/claude_launcher/web/static/app.js:24650`(`personGateRecords`), `:7777`(`term-planbtn`) |
| 세션의 자기 ask 답변 | `flow_approve`는 `otherwise: self`로 떨어진 ask에만 쓸 수 있다. `coding-approval`은 `otherwise: human`이다 | `src/claude_launcher/cflow2/mcp.py:261`, `improv-worker-v2.yaml:1835`(`otherwise: human`) |

## 2. 확정한 규칙

두 입력을 분리한다. 게이트의 존재와 게이트의 시점은 서로 다른 값이 정한다.

| 입력 | 정하는 것 | 출처 |
|---|---|---|
| `require_coding_approval` | 게이트를 세울지 | 런 시작 입력 |
| `plan.summary.waiting` | 언제 멈출지 | plan 패널의 미결정 수 |

### 규칙 1: plan 미결정이면 `work`를 멈춘다

`plan.summary.waiting > 0`이면 게이트 유무와 관계없이 `work` 앞에서 멈춘다.
`session_routing` 자동 경로와 `approve-always` 이후 경로에도 같은 조건을
건다. `plan_set` 실패나 데몬 거절로 `waiting`을 낼 수 없으면 멈추는 쪽으로
읽는다.

### 규칙 2: coding-approval 허가 요청은 plan 미결정과 별도다

게이트의 존재는 `require_coding_approval`이 정한다. 게이트의 시점만 plan
미결정에 의존한다. 미결정이 남아 있는 동안에는 게이트를 세우지 않고,
미결정이 모두 해소된 뒤에 요청이 켜져 있으면 게이트를 세운다.

### 규칙 3: 순서는 plan 결정 → coding-approval → `work`

1. `coding-plan`이 plan과 brief를 쓴다.
2. `plan.waiting > 0`이면 plan 결정을 기다린다. 이 동안 게이트는 서지 않는다.
3. `plan.waiting == 0`이 되면:
   - `require_coding_approval=true`이고 결정이 `approve-always`가 아니면
     게이트에서 멈춘다.
   - `require_coding_approval=false`이거나 결정이 `approve-always`면 `work`로
     간다.
4. `approve-always`는 이후 일감의 게이트만 끄고, plan 미결정 멈춤은 끄지
   않는다.
5. 게이트가 없는 경로(`session_routing`, `approve-always` 이후)도 plan
   미결정이면 `work` 앞에서 멈춘다.

## 3. 반영 지점 (다음 회차)

1. `coding-plan`이 `plan_waiting`을 반환값에 싣는다. `plan_set`의 응답에 있는
   `summary.waiting`을 그대로 쓴다.
2. `coding-plan` 뒤, `coding-approval-check` 앞에 plan 결정 대기 노드를 둔다.
   이 노드는 `plan_waiting > 0`인 동안 끝나지 않는다. `[claunch plan]` 블록이
   올 때마다 `plan_get`으로 plan을 다시 읽어 `waiting`을 갱신하고 보고한다.
3. `coding-approval-check`의 조건은 지금과 같게 둔다
   (`require_coding_approval && decision != 'approve-always'`). 대기 노드가
   앞에 있으므로 게이트는 미결정이 해소된 뒤에만 선다.
4. `coding-plan`을 건너뛰는 경로(`approve-always` 이후, `session_routing`에서
   `require_coding_approval=false`)는 `work`가 계획을 처음 쓸 때 같은 대기를
   한 번 둔다. 계획을 쓴 직후, 코드를 바꾸기 전이다.
5. `plan_set` 실패, plan 없음, 데몬 거절은 모두 멈춤으로 읽는다. 모르는 값을
   "결정 없음"으로 읽지 않는다.
6. plan의 미결정을 알림 카드로 올린다. 지금은 레일의 ☰ plan 표시만 켜지므로,
   대기 노드가 게이트 대신 서는 동안 사용자가 그 사실을 알 길이 없다.

## 4. 검토한 대안과 버린 이유

### 대안 A: plan 결정이 cflow ask를 닫는다

plan 패널의 답이 곧 게이트의 답이 되게 하는 안이다. 두 가지가 막는다.

- 세션은 자기 `otherwise: human` ask를 답할 수 없다
  (`src/claude_launcher/cflow2/mcp.py:261`). 게이트를 닫는 주체가 사람도
  세션도 아니게 되므로, 데몬이 plan 결정과 ask를 잇는 새 경로가 필요하다.
- plan API는 사용자의 계획 쓰기와 세션의 결정 쓰기를 모두 거부한다
  (`src/claude_launcher/daemon/plan_api.py:20`). 게이트의 답을 plan에 옮겨
  적을 주체가 없다.

현재 확정안은 이 배선을 필요로 하지 않는다. 게이트는 미결정 뒤에 서고,
사용자가 두 자리에서 각각 답한다.

### 대안 B: 게이트가 plan 미결정을 프롬프트에 실어 보여 준다

게이트를 없애지 않고 프롬프트에 미결정 목록을 싣는 안이다. 사용자가 같은
계획을 두 표면에서 읽는 일이 남는다. 순서 문제를 풀지 못하므로 최종안으로
채택하지 않았다. 대안 A의 배선이 서기 전의 임시 조치로는 쓸 수 있다.

## 5. 리스크

- plan에 결정이 남아 있으면 자동 경로도 멈춘다. 오래된 결정 하나가 런을
  세울 수 있다. 결정을 더 이상 필요 없게 만든 세션은 `plan_set`으로 그
  결정을 step으로 바꾸거나 제거해야 한다. 결정을 답하면
  `summary.waiting`은 저절로 0이 된다.
- `plan_set`이 실패하면 `work`가 시작되지 않는다. 데몬이 plan API를 받지
  못하는 동안 회차가 멈춘다. 이 동작은 안전한 쪽을 고른 결과이고, 멈춘
  사실을 보고에 남겨야 한다.
- plan 미결정 알림이 없으면 사용자는 대기 중인 런을 알아채지 못한다. 3절
  6번이 이 규칙의 필수 조각이다.
- `approve-always`의 뜻이 넓어진다. 지금은 이후 일감의 게이트를 끄고,
  확정안에서는 plan 미결정 멈춤을 끄지 않는다. 이 차이를 `approve-always`
  선택지 설명에 적어야 한다.

## 6. 구현 (2회차, `claunch-rzepk.2`)

3절의 지점을 다음 파일이 맡는다. 2번의 「`[claunch plan]` 블록이 올 때마다
`plan_get`으로 다시 읽는다」는 에이전트 작업 대신 `wait` probe로 옮겼다 —
대기 중인 회차에는 턴이 없으므로, 엔진이 `poll` 초마다 게이트를 직접
실행하는 편이 결정이 풀린 순간을 놓치지 않는다.

| 지점 | 파일 | 내용 |
|---|---|---|
| 1 | `src/claude_launcher/workflows/v2/improv-worker-v2.yaml` (`coding-plan`) | `returns.ok.plan_waiting`(`int?`)에 `summary.waiting`을 싣는다. 읽지 못하면 -1과 사유 |
| 2 | 같은 파일 (`plan-wait-before-gate`) | `coding-plan` 뒤·게이트 앞의 `wait`. probe는 `claunch gate plan-waiting`(0 통과, 1 계속 대기, 그 밖은 판정 불가), `poll: 30`, `undecided_max: 3`, `seconds` 없음(상한 없음) |
| 3 | 같은 파일 (`coding-approval-check`) | 조건은 그대로. 대기가 앞에 서므로 게이트는 미결정이 풀린 뒤에만 선다 |
| 4 | 같은 파일 (`plan-seed`, `plan-wait-after-seed`) | `coding-plan`을 건너뛰는 갈래(`approve-always` 이후, 자동 경로)에서 계획을 먼저 확보하고 같은 대기를 `work` 앞에 둔다 |
| 5 | `src/claude_launcher/gates/plan_waiting.py` | plan 저장소(`daemon_dir()/plan.sqlite3`)를 읽기 전용으로 열어 `waiting`을 판정한다. plan 없음·읽기 실패·값 없음은 모두 2 |
| 6 | `src/claude_launcher/daemon/cflow2_web.py`, `src/claude_launcher/web/static/app.js` | rail의 `answers`에 `plan-wait`를 싣고(probe가 `plan-waiting` 게이트인 `wait`), 알림 카드는 그 세션의 plan 패널을 연다. 카드에는 누를 버튼이 없다 |

게이트는 `claunch gate plan-waiting`으로 어떤 저장소에서도 실행되며
(`src/claude_launcher/cli_gate.py`의 `GATES`), 판정 불가가 세 번 쌓이면
`plan-wait-unclear`가 계속 기다릴지 진행할지 사람에게 묻는다(규칙 1의
「모르는 값은 멈춤」).

## 7. 작업 시작 대기와 closure의 분리 (`claunch-jw1v2`)

2회차 구현에서 두 작업 시작 대기(`plan-wait-before-gate`, `plan-wait-after-seed`)는
`summary.waiting` 전체를 읽었다. 이 값에는 앞 회차가 남긴 closure verify·followup이
들어 있어, 이미 착지한 작업의 확인 항목이 다음 회차의 `work`를 막았다. 규칙 1의
「plan 미결정」은 지금 계획의 고르지 않은 decision을 뜻하도록 좁혔다.

| 항목 | 내용 | 위치 |
|---|---|---|
| 작업 시작용 집계 | `summary.waiting_decisions`는 고르지 않은 decision 수다. `summary.waiting`은 지금처럼 decision·미확인 verify·미처분 followup을 합한다 | `src/claude_launcher/plan/store.py` (`summarize`) |
| probe의 범위 | `claunch gate plan-waiting --scope start`는 `waiting_decisions`를 읽는다. 값이 없는 데몬이면 `blockers`의 decision 줄을 센다. 기본 `--scope settle`은 `waiting`을 읽는다 | `src/claude_launcher/gates/plan_waiting.py` |
| 워크플로 | 작업 시작 대기 두 곳은 `--scope start`, settle의 `closure-wait`·`closure-wait-timed`는 기본 범위다. 네 곳 모두 `--notify`를 준다 | `src/claude_launcher/workflows/v2/improv-worker-v2.yaml` |
| 멈춤 알림 | `--notify`는 멈춘 항목 집합마다 세션 스레드 「plan 게이트 대기」에 한 번 쓴다. 같은 집합이면 다시 쓰지 않고, 통과하면 기록을 지운다. 기록은 세션 scratch의 `plan-gate-notice.json`이다 | `plan_waiting.notify_stall` |
| 보류 판정 | verify의 `deferred`(보류)는 waiting·closable을 막지 않는다. 세션은 그 확인을 보드 이슈로 옮기고 detail에 이슈 id를 적는다 | `store.VERDICTS`, `plan_api.decided_text` |
| 결과 유지 | passed·deferred verify와 처리된 followup은 title·detail만 바뀌면 결과와 rev를 유지하고 `set` 이벤트의 `reworded_kept`에 남는다. 다시 물으려면 `plan_node`에 `reopen=<이유>`를 준다. failed verify는 문구가 바뀌면 다시 묻는다 | `store._kept`, `store.patch` |
| 세션이 처리한 followup | 세션이 `status=done`으로 쓴 followup은 사용자 결정 없이 waiting에서 빠진다 | `store.summarize` |
| 참고(note) | closure 종류 `note`는 결정을 받지 않고 waiting·closable을 막지 않는다. 패널의 「참고」 절과 end-decision 브리프에 목록으로 나온다 | `store.CLOSURE_KINDS`, `plan-pane.js` |

## 8. 계층 결정, 자동 선택, 배제 선택지, 변경 전달 (`claunch-vmcg7`)

| 항목 | 내용 | 위치 |
|---|---|---|
| 계층 | 단계·결정의 children은 최대 5단계다. 결정의 자식 가운데 한 선택지에만 의존하는 항목은 `under: <선택지 label>`을 가진다. `under`가 없는 자식은 결정 전체에 속한다. 부모가 결정이 아니거나, 없는 label이거나, 배제된 선택지를 가리키는 `under`는 거절된다 | `store._Builder.node` |
| 활성 경로 | `walk_active`는 고르지 않은 선택지 아래 항목을 건너뛴다. `summary.waiting`·`waiting_decisions`·`blockers`는 활성 경로만 세고, 건너뛴 id는 `summary.inactive`에 남는다. 활성 경로 밖의 결정에 `decide`를 보내면 거절된다 | `store.walk_active`, `store.summarize`, `store.decide` |
| 자동 선택 | 선택지의 `default: true`는 권장안을 겸한다(결정마다 최대 하나). 쓰는 시점에 결과가 `{choice, by: "agent"}`로 채워져 사용자를 기다리지 않고, `summary.auto`에 그 수가 남는다. 패널은 「자동 선택됨」으로 표시하고 다른 선택지의 「이 안 선택」 버튼을 유지한다 | `store._options`, `store._Builder.node`, `plan-pane.js` |
| 배제 선택지 | 선택지의 `rejected: <이유>`는 고를 수 없는 선택지다. 권장·default를 겸할 수 없고, 고를 수 있는 선택지가 2개(default가 있으면 1개) 이상이어야 한다. 패널은 취소선과 「배제 이유」를 표시한다 | `store._options`, `store.decide`, `plan-pane.js`, `plan-page.js` |
| 변경 전달 | 이미 답한 결정(자동 선택 포함)을 다른 선택지로 바꾸면 `[claunch plan]` 블록이 `CHANGED`로 시작하고 변경 전·후 선택지와 각 효과·리스크, 이전 안으로 한 작업을 다시 보라는 지시를 싣는다. 같은 선택지를 다시 고르면 확인 문장만 싣는다 | `plan_api.decided_text` |
| 선택지 삭제 | `plan_node`로 options를 바꿀 때 `under` 자식이 가리키는 선택지를 지우거나 배제하면 거절된다 | `store.patch` |
| 작업 시작 대기 | 자동 선택된 결정과 비활성 항목은 `--scope start`의 대기 수에 들어가지 않는다 | `gates/plan_waiting.py` (변경 없음, `waiting_decisions`를 읽음) |
