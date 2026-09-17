# cflow의 스텝 그래프를 오토마톤으로 읽기

작성 계기: `claunch-693w`에서 사용자가 `await-landing`에 반복(10분 간격 재촉)과
실패 전이를 넣어 달라고 요청하면서 「이거 regex 구조 같다」고 관찰했고, 그
관찰을 확장해 볼 수 있는지 물었다. 이 문서는 그 관찰이 어디까지 정확한지, 어디서
성립하지 않는지, 그리고 그 대응 관계로 무엇을 할 수 있는지를 적는다.

이 문서는 설계 기록이고 구현 계획이 아니다. 3절의 항목 가운데 3.1은 이미
엔진에 구현되어 있고(확인 결과를 그 절에 적었다), 3.2~3.4는 구현되어 있지
않다.

## 1. 관찰은 정확하다 — 대응 관계

정규 표현식은 유한 오토마톤(finite automaton)의 표기법이다. cflow의 워크플로도
유한한 상태 집합과 그 사이의 전이로 정의되므로, 둘은 같은 것을 다른 문법으로
적는다. 대응은 다음과 같다.

| 정규 표현식 | cflow | 근거 |
| --- | --- | --- |
| 상태 | 스텝(step) | `model.Step`, `state["current"]` |
| 입력 기호 | 스텝을 떠나게 만든 사건 | `next` 호출, select의 옵션, 게이트 승인, 체크리스트 통과, 타이머 발화 |
| 전이 함수 | `next:` / `select.options[*].next` / `ask.on_decline` / `checklist.then` / `timer.then`·`after` | `model.Step.successors()` |
| 시작 상태 | `start:` | `model.Workflow.start` |
| 수용 상태 | `END` (`next: end`) | 런이 `done`이 된다 |
| 연접 (`AB`) | `A.next: B` | |
| 분기 (`A\|B`) | `select`의 옵션들 | |
| 반복 (`A*`) | 자기 자신이나 앞 스텝으로 되돌아가는 `next` | 이번에 추가한 `await-landing -> landing-nudge -> await-landing`가 그것이다 |

이번 변경을 이 표기로 적으면 다음과 같다. 착지 대기 구간은

```
integration-request · (nudge · await)* · (landed | rebase | remeasure | declined)
```

이고, 거절 갈래는

```
declined · settle · gate · (work · … · await)   —   되감기(정규 표현식의 backreference와 무관한 단순 루프)
```

이다. 즉 사용자의 요청은 「`await-landing`에 Kleene star를 붙이고, 대체 분기에
항을 하나 더 늘려 달라」로 읽을 수 있고, 실제 구현도 그 두 가지였다.

## 2. 대응이 성립하지 않는 지점

관찰을 확장하기 전에 경계를 먼저 적는다. 아래 넷 때문에 cflow는 정규 언어보다
넓고, 「워크플로를 regex로 적자」는 그대로는 성립하지 않는다.

### 2.1 방문 횟수를 세므로 유한 오토마톤이 아니다

`max_visits`(기본 25)는 스텝별 방문 횟수를 세고, 초과하면 런을 사람 앞에
세운다(`engine` 2010행 부근, `waiting_approval` / `reason: loop_limit`).
`loop_extensions`는 그 한도를 연장한다. 카운터가 붙은 오토마톤은 유한 상태가
아니다. 정규 표현식의 표기로 옮기면 `A{0,25}`에 해당하고, 그 25는 상태 집합
바깥의 변수(`state["visits"]`)에 들어 있다.

이번 nudge 루프도 그 카운터에 얹혀 있다. 10분 간격으로 약 25회면 대략 4시간이고,
그 뒤에는 루프 가드가 사람에게 「한 번 더 재촉하면 달라지는가」를 묻는다.

### 2.2 전이가 시간과 외부 상태에 달려 있다

`interval`(paced option), `timer:`, `checklist:`, `awaits:`의 전이 조건은 입력
기호가 아니라 **시계와 외부 세계의 종료 코드**다. 이번에 쓴 `interval: 600`이
정확히 그것이다: 같은 `nudge` 선택이 시점에 따라 전이가 되기도 하고 보류
(`waiting_window`)가 되기도 한다. 그래서 전이 함수의 정의역이 `(상태, 기호)`에서
`(상태, 기호, 시각, 외부 상태)`로 넓어진다.

### 2.3 상태가 스텝 하나로 끝나지 않는다

런은 스텝 id 외에 보고(`report`), 보류(`window`), 열린 질문(`ask`), 방문 카운터,
서브 런 슬롯을 함께 들고 있다. 예를 들어 체크리스트 스텝은 「항목이 전부 참」과
「이 스텝의 report가 제출됨」이 **둘 다** 성립해야 떠난다. 상태 하나에 두 개의
독립된 조건이 걸리는 것은 오토마톤의 상태 분할로 표현할 수는 있으나, 표기가
읽히지 않게 된다.

### 2.4 사람의 `goto`는 선언되지 않은 전이다

`claunch cflow goto <step>`은 그래프에 없는 간선을 그 자리에서 만든다. 오토마톤의
비유로는 「밖에서 상태 레지스터에 직접 쓰기」이고, 이것은 언어를 벗어나는
장치다. 다만 저널에 남으므로 사후에 읽을 수는 있다.

이번 변경의 동기 절반이 여기에 있었다. 거절 경로가 선언되어 있지 않아 `goto`가
유일한 출구였고, 즉 자주 쓰이는 전이가 그래프 밖에 있었다.

## 3. 확장 가능성

이 대응 관계에서 값이 나오는 방향은 **정규 언어 도구가 제공하는 검사를 스텝
그래프에 적용하는 것**이다(「워크플로를 regex 문법으로 적기」는 3.5절의 이유로
제외한다). 문법을 바꾸지 않고 얻을 수 있는 것들을 값이 큰 순서로 적는다.

### 3.1 도달 가능성 검사 — 이미 구현되어 있다

확인 결과 이 검사는 이미 엔진에 있다. `model._graph_warnings`가 세 가지를
계산한다: 시작 상태에서 도달 불가능한 스텝(`_reachable`), 들어가면 종료에 닿을 수
없는 스텝(`_can_finish`), 그리고 사이클(`_cycle_nodes`). 「종료에 닿을 수 있는
스텝이 하나도 없다」는 경고가 아니라 파싱 오류다(`model.py` 2541행 부근).
간선은 `Step.successors()`가 답하고, 여기에는 `ask.on_decline`,
`checklist.then`·`otherwise.then`, `timer.after`가 모두 포함된다.

이번 변경을 그 검사로 확인한 결과는 다음과 같다.

| 스텝 | 시작에서 도달 가능 | 종료에 도달 가능 |
| --- | --- | --- |
| `landing-nudge` | 예 | 예 |
| `landing-declined` | 예 | 예 |
| `rework-gate` | 예 | 예 |
| `landing-declined-hold` | 예 | 예 |

`improv-worker`와 `improv-worker-remote` 양쪽에서 같은 결과가 나왔고, 도달
불가능 스텝과 막다른 스텝 목록은 둘 다 비어 있다. 사이클 경고는 이 변경 이전에도
있었다(`intake` ↔ `queue-recheck` 루프).

그래서 이 항목에서 남는 것은 검사 자체가 아니라 **경고가 어디까지 보이는가**다.
지금 `workflow.warnings`는 `claunch cflow lint`의 출력(`cli.py` 815행)과 `start`
응답의 `workflow_warnings` 필드에 실린다. 워크플로 정본을 고치는 작업에서 이
경고를 보려면 그 둘 중 하나를 의도적으로 불러야 하고, `tools/sync_project_layer.py`
나 테스트 스위트는 부르지 않는다. 사이클 경고가 29개 스텝을 한 줄에 나열하는
현재 형태도 읽히지 않는 쪽에 가깝다: 이번 변경으로 늘어난 네 스텝이 그 줄에
섞여 들어갔고, 줄 자체는 변경 전과 구분되지 않는다.

### 3.2 그래프를 그림으로 내기

같은 간선 집합을 DOT나 Mermaid로 출력하면 대시보드와 문서가 같은 그림을 쓸 수
있다. 지금은 워크플로의 모양을 아는 방법이 yaml을 처음부터 읽는 것뿐이고, 이
파일은 improv-worker 기준 2,400행이 넘는다. 사람이 「거절하면 어디로 가는가」를
묻는 자리가 없다는 것이 이번 작업에서 실제로 느린 부분이었다.

### 3.3 저널을 그래프에 대조하기

저널(`.cflow/runs/*/journal.jsonl`)은 실제로 지나간 상태 열이다. 그 열이 선언된
간선만으로 설명되는지 확인하면, `goto`로 만들어진 전이가 어디에 몇 번 있었는지
세어진다. 같은 (출발, 도착) 쌍의 `goto`가 반복해서 나타나면 그것은 **선언되었어야
할 간선**이다.

이번 변경이 정확히 그 사례다. 「거절 → wrapup」이 `goto`로 반복되고 있었고,
그것이 워크플로에 빠진 간선이라는 신호였다. 다만 이번에는 사용자가 알아차렸고
기계는 세고 있지 않았다.

### 3.4 스텝 그래프의 동등성 검사

`improv-worker`와 `improv-worker-remote`처럼 한쪽이 다른 쪽을 `extends`할 때,
두 그래프가 어느 간선에서 갈리는지를 기계가 답할 수 있다. 지금은
`tests/test_improv_worker_remote.py`가 그 질문을 손으로 적은 단언 목록으로
대신하고 있고, 그래서 이번처럼 기반 워크플로에 간선을 늘리면 그 목록이 조용히
낡을 수 있다(이번에는 낡지 않았지만, 낡지 않았다는 것을 확인한 것도 사람이다).

### 3.5 하지 않는 편이 나은 것

- **워크플로를 정규 표현식 문자열로 적기.** 2절의 네 가지가 표기에 들어오지 못한다.
  들어오게 만들면 그것은 이미 정규 표현식이 아니고, 읽기는 yaml보다 나빠진다.
- **`A*`, `A{n,m}` 같은 반복 연산자를 yaml 문법에 추가하기.** 반복의 상한은 이미
  `max_visits`에 있고, 두 자리에 적으면 갈린다. 이번 nudge 루프도 새 문법 없이
  기존 `interval` + `next` 되돌리기로 적혔다.

## 4. 이번 변경이 그래프에 더한 것

| 항목 | 내용 |
| --- | --- |
| 새 상태 | `landing-nudge`, `landing-declined`, `rework-gate`, `landing-declined-hold` |
| 새 간선 | `await-landing -> landing-nudge` (paced, 600초), `landing-nudge -> await-landing`, `await-landing -> landing-declined`, `landing-declined -> rework-gate`, `rework-gate -> work`, `rework-gate -(on_decline)-> landing-declined-hold`, `landing-declined-hold -> wrapup` |
| 선언되지 않은 전이에서 회수한 것 | 「상위가 거절 → 착지 없이 마감」. 이전에는 사람의 `claunch cflow goto wrapup`뿐이었다 |
| 새로 생긴 것 | 「상위가 거절 → 고쳐서 다시」. 이전에는 어떤 경로로도 표현되지 않았다 |
| 사이클 | 둘. `await-landing` 되돌기(재촉)와 `work` 되감기(재작업). 둘 다 `max_visits`가 상한이고, 후자는 그 앞에 사람 게이트가 하나 더 있다 |
