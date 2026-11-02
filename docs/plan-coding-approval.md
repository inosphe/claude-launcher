# plan 미결정과 coding-approval 게이트

이슈: `claunch-rzepk`.
이 문서는 두 신호의 관계와 순서를 정리한다. 사용자 확인은 2026-11-02에
끝났고, 구현은 다음 회차의 몫이다. 이 회차는 코드를 바꾸지 않는다.
1절의 파일·줄 인용은 `origin/master @ 2c71df0d`의 트리에서 셌다. 줄 번호는
그 트리에서만 뜻이 있으므로, master가 움직이면 같은 이름(노드 id·조건·
`otherwise: human`)의 줄을 새 트리에서 다시 센다.

## 1. 확인한 사실 (현재 코드)

| 항목 | 현재 동작 | 근거 |
|---|---|---|
| 계획과 승인 brief의 생성 | `coding-plan` 한 단계가 plan 패널 기록(`plan_set`)과 승인용 brief를 함께 만든다 | `src/claude_launcher/workflows/v2/improv-worker-v2.yaml:1649`(`coding-plan`), `:602` |
| 계획을 쓰는 자리 | `plan-panel` 조각이 `coding-plan`(그 단계가 없으면 `work`)에서 `plan_set`을 쓰라고 정한다 | `improv-worker-v2.yaml:602`, `:627` |
| 게이트의 조건 | `coding-approval-check`는 `require_coding_approval`과 `approve-always` 답만 본다. plan의 `waiting`은 보지 않는다 | `improv-worker-v2.yaml:5185`(`coding-approval-check`) |
| 게이트가 서지 않는 자동 경로 | `session_routing=true`이고 `require_coding_approval=false`면 `route-planned`로 간다 | `improv-worker-v2.yaml:5185`, `:5205`(`route-planned`) |
| approve-always 이후 | `session_routing=false`면 게이트와 `coding-plan`을 함께 건너뛰고 계획은 그때 `work`에서 처음 쓴다. `session_routing=true`면 `coding-plan`은 그대로 돌고 게이트만 서지 않는다 | `improv-worker-v2.yaml:5185`, `:5187`, `:5206` |
| plan 결정의 전달 | 사용자가 고른 결정은 `[claunch plan]` 블록으로 세션 터미널에 온다. 계획을 그 갈래에 맞게 다시 쓰라는 안내가 붙는다 | `src/claude_launcher/daemon/plan_api.py:77` |
| plan과 cflow의 연결 | plan 결정이 cflow ask를 닫는 코드 경로가 없다 | `grep -rn "plan_store\|\[claunch plan\]" src/claude_launcher/cflow2` 결과 0건 |
| plan의 대기 판정 | `summary.waiting`은 사용자가 지금 답할 수 있는 항목 수다(미결정, 미확인 verify, 미처분 followup) | `src/claude_launcher/plan/store.py:280` |
| 알림 표면 | cflow의 `urgent` ask는 대시보드 알림 카드가 된다. plan의 미결정은 레일의 ☰ plan 표시만 켠다 | `src/claude_launcher/web/static/app.js:24650`(`personGateRecords`), `:7777`(`term-planbtn`) |
| 세션의 자기 ask 답변 | `flow_approve`는 `otherwise: self`로 떨어진 ask에만 쓸 수 있다. `coding-approval`은 `otherwise: human`이다 | `src/claude_launcher/cflow2/mcp.py:261`, `improv-worker-v2.yaml:1734`(`otherwise: human`) |

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
