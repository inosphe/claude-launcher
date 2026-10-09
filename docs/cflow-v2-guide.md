# cflow v2 작성 가이드 (PM 역할용)

대상 독자: 목표를 이슈로 나누고 자식 세션마다 절차 파일을 써서 편성하는 PM
역할 세션(`improv-pm` 런의 드라이버)과, 그 절차 파일을 검토하는 사람.

이 문서는 두 가지를 설명한다.
- v2를 만든 이유(1절)
- v2 정의를 쓰는 방법(2~8절)

정확한 문법 규격은 `docs/cflow-v2.md`가 정본이다. 이 문서가 그 규격과 다르면
규격을 따른다.

## 구현 상태

| 기능 | 상태 |
|---|---|
| IR(`cflow2/ir.py`) | 구현됨 |
| 표면 문법 컴파일러, 정적 검사, 정의 탐색 | 구현 중(단계 1) |
| 식 해석기, capability 합성과 fallback 계획 | 구현 중(단계 1) |
| 런타임, `flow_*` MCP 툴, `claunch cflow2` CLI | 단계 2 |
| 실행 주체(subagent, headless, session) | 단계 3 |
| 데몬 연동, 웹 화면, spawn에서 v2 절차 파일 받기 | 단계 4 |
| `improv-worker-v2` 정의 | 단계 5 |

아래 절에서 아직 구현되지 않은 명령이나 경로에는 `(단계 N)` 표시를 붙였다.

---

## 1. 설계 동기

### 1.1 v1에서 측정된 문제

측정 대상은 `improv-worker.yaml`이다(2026-10-06, master 933ce82d).

| 항목 | 측정값 | 의미 |
|---|---|---|
| 파일 크기 | 3260줄, 41 스텝, 간선 61 | 회차 전체가 한 평면 그래프에 있음 |
| 산문 비중 | 137,436자 중 84,594자(62%) | 의미의 대부분이 엔진이 읽지 않는 문자열에 있음 |
| 산문 안 조건 표현 | 208회 | 「~이면」「~일 때」를 에이전트가 매번 읽고 판단함 |
| 공통 beads 블록 | 165줄 × 사본 5개 | 같은 규칙을 복사하고, 테스트가 사본들이 같은지 문자열로 비교함 |
| 부분 문자열 테스트 | `tests/test_beads_protocol.py`의 `assert … in` 182개 | 규칙이 지켜지는지가 아니라 규칙 문장이 파일에 있는지를 검사함 |
| 가장 큰 스텝 | `review` 17,100자 | 한 스텝 안에 하위 절차 5개와 완료 조건 1개 |
| master 머지 금지 | description 외 산문 7곳 | 같은 불변식을 위치마다 다른 문장으로 반복함 |

### 1.2 문제의 구조

v1의 실행 단위는 스텝 하나이고, 스텝 사이의 연결은 `next`와 `select`뿐이다.
그 결과 다음 개념들을 문법으로 쓸 수 없고 산문으로 써야 한다.

| 개념 | v1에서 쓰는 방법 | v2의 문법 |
|---|---|---|
| 반복과 재시도 | `next`를 앞 스텝으로 되돌리고 산문에 횟수 규칙 | `loop`, `retry` |
| 시간 제한 | `timer` 스텝 + 산문 | `timeout` |
| 실패 시 되돌아가기 | 여러 스텝에서 `work`로 가는 간선 | `try` / `catch` |
| 반드시 실행할 정리 | 끝 쪽 스텝들(settle, end-gate) | `finally` |
| 절차 재사용 | 복사, 또는 `extends`로 정의 전체를 물려받기 | `procedures` + `call` |
| 공통 규칙 | 복사 | `fragments`, `rules` |
| 역할·상황에 따른 분기 | 산문의 조건문 | `cond`, `choose`의 `when` |
| 병렬 작업과 결과 모으기 | 서브 런 + 산문 + 체크리스트 | `par`, `group` |
| 무거운 작업을 다른 컨텍스트에 맡기기 | 리더 산문의 「실행은 subagent」 규칙 | 노드의 `executor` |
| 불변식 | 산문 반복 + 문자열 테스트 | `spec` |

### 1.3 v2의 접근

- **조합자 문법.** 절차를 `seq`, `par`, `retry` 같은 조합자의 중첩으로 쓴다.
  제어 흐름은 전부 문법에 있고, 산문에는 「이 작업에서 무엇을 하는가」만 남는다.
- **IR 분리.** 문법은 컴파일 시점에 statechart IR로 바뀐다. 런타임, 정적 검사,
  웹 화면은 IR만 읽는다. 런은 시작 시점의 IR을 스냅샷하므로, 정의를 고쳐도
  진행 중인 런의 의미는 바뀌지 않는다.
- **위임 실행.** 작업이나 하위 트리를 하위 에이전트, 비대화형 프로세스, 자식
  세션에 맡기고, 메인 세션은 구조화된 반환값과 영수증만 받는다. 목적은 메인
  세션의 컨텍스트 절약과 실제 병렬 실행이다.
- **명세.** 불변식과 경로 성질을 선언하고 컴파일 시점과 실행 중에 기계가 검사한다.

---

## 2. 기본 단위

### 2.1 작업(task)

에이전트가 수행하는 한 단계다. `nodes:` 아래에 이름을 붙여 정의하고, `body`에서
이름으로 부른다.

```yaml
nodes:
  targeted-test:
    title: 표적 테스트
    prompt: |
      바꾼 영역의 테스트만 실행하고 수치를 보고한다.
    done_when: 실행한 테스트 모듈과 통과·실패 수가 보고에 있다
    returns:
      ok:   {passed: int, failed: int}
      fail: {reason: str}
    effects: [test.run]
```

작업에 적는 것:
- `prompt`: 그 작업에서 할 일. 다른 작업으로 언제 넘어가는지는 적지 않는다.
- `returns`: 결과의 종류와 필드. 다음 분기가 이 값을 읽는다.
- `effects`: 이 작업이 바꾸는 것. 위임의 안전장치와 명세 검사가 이 값을 쓴다.

### 2.2 조합자

| 하려는 일 | 조합자 |
|---|---|
| 순서대로 | `seq` |
| 순서대로 + 앞 결과를 다음에 넘김 | `waterfall` |
| 데이터로 기계가 분기 | `cond` |
| 에이전트나 사용자가 고름 | `choose` |
| 목록 항목마다 | `foreach` |
| 조건이 맞는 동안 반복 | `loop` (`max` 필수) |
| 동시에 실행하고 결과 모으기 | `par` |
| 실패하면 다시 시도 | `retry` (`max` 필수) |
| 시간 제한 | `timeout` |
| 실패 종류별 처리와 정리 | `try` / `catch` / `finally` |
| 다른 절차 호출 | `call` |
| 외부 상태 대기 | `wait` |
| 실행 중 발견한 일을 그룹으로 처리 | `group` |

### 2.3 제어

| 제어 | 쓰임 |
|---|---|
| `{continue: LABEL}` | 감싸는 반복의 다음 회차로 |
| `{break: LABEL}` | 반복 끝내기 |
| `{raise: OUTCOME}` | 감싸는 `try`의 `catch`로 |
| `{return: OUTCOME}` | 프로시저 끝내기 |
| `end` | 런 끝내기 |

`goto`는 없다. 「앞 단계로 돌아간다」는 반복 조합자와 `continue`로 쓴다.

---

## 3. 작성 절차

PM이 자식 하나의 절차 파일을 쓰는 순서다.

### 3.1 기반 정의를 고른다

자식의 기본 절차는 `improv-worker-v2`(단계 5)다. 그 정의는 회차의 구간을
프로시저로 내보낸다(`resolve-issue`, `item`, `land`, `settle` 등).
PM의 절차 파일은 v1의 `extends` overlay 대신 **`include` + `call`**로 쓴다.

```yaml
cflow: 2
name: gen-s90-auth-split            # 파일 머리 주석에 출처를 남긴다
# generated-by: s88 issue=claunch-abc12 at=2026-10-07T01:00:00Z
include: [improv-worker-v2]
body:
  seq:
    - id: wait-for-a
      wait:
        probe: "git merge-base --is-ancestor 1a2b3c4 HEAD"
        poll: 120
        describe: 선행 갈래 A의 tip이 기준 브랜치에 실렸다
    - call: worker-round
      with:
        verify: "uv run --no-sync pytest -q tests/test_auth.py"
```

v1 overlay와 비교하면 다음과 같다.

| v1 overlay | v2 절차 파일 |
|---|---|
| `extends: improv-worker`로 전체를 물려받고 스텝 단위로 덮어씀 | 필요한 프로시저를 `include`하고 `call`로 부름 |
| 대기는 `checklist` 스텝을 `branch-setup` 앞에 끼움 | `wait` 노드를 `call` 앞에 둠 |
| 표적 검증은 스텝의 `verify`를 덮어씀 | 프로시저의 `params`(예: `verify`)로 넘김 |
| 기존 스텝의 산문을 다시 쓰면 사고 | 프로시저 본문은 include한 쪽에 있고, 호출하는 쪽은 바꿀 수 없음 |

마지막 행이 v2로 옮기는 주된 이유다. v1에서는 overlay가 기반 정의의 산문을
덮어쓸 수 있어서 `improv-pm`의 validate 스텝이 덮어쓰기를 눈으로 찾아야 했다.
v2에서는 호출하는 쪽이 프로시저의 내용을 바꿀 문법이 없다.

### 3.2 의존을 기계 검사로 쓴다

자식 B가 자식 A의 결과 위에서 시작해야 하면, B의 절차 파일 앞에 `wait`를
둔다. probe는 git이나 보드를 읽는 명령이다. 메시지로 「끝나면 알려 줄게」를
약속하지 않는다(v1 `improv-pm`과 같은 원칙).

probe의 종료 코드는 0 = 성립, 1 = 아직, 그 밖 = 판정 불가다(규격 2.3 「wait」).
판정 불가가 `undecided_max`(기본 3)번 이어지면 wait는 `undecidable`로, `seconds`
상한을 넘기면 `timeout`으로 끝난다. 둘 다 처리하는 곳이 없으면 런이 실패하므로
`on`으로 받는다. 기계가 답할 수 없는 상황이므로 보통 사용자에게 묻는다.

사용자에게 결정을 묻는 지점은 `chooser: user` 선택 대신 공용 프로시저
`ask-user`(`ask-common`, `improv-common`이 포함한다)로 쓴다(규격 2.10). 드라이버가
문제를 단순화하고 선택지별 위험·결과·가역성과 권장안을 탐색해 공통 형식으로 묻고,
답에 따라 `cond`로 갈라진다. 갈라지는 지점이면 `choices`로 선택지 이름을 고정한다.

```yaml
- id: scope-decision
  call: ask-user
  bind: scope_decision
  with: {topic: 이 회차의 범위, choices: [narrow, full], require_reason: true}
- cond:
    - {if: "$data.scope_decision.decision == 'narrow'", then: work-narrow}
    - {else: work-full}
```

`with`의 문자열에 `$`가 들어가면 식으로 읽히므로 `topic`은 `$` 없는 문장으로 쓰고,
런의 값은 `context: "$params.why"`처럼 식 하나로 넘긴다.

```yaml
- id: wait-for-a
  wait:
    probe: "git merge-base --is-ancestor 1a2b3c4 HEAD"   # 0/1, git 오류는 128
    poll: 120
    seconds: 86400
    describe: 선행 갈래 A의 tip이 기준 브랜치에 실렸다
  on:
    undecidable: {call: ask-the-user, with: {why: undecidable}}
    timeout: {call: ask-the-user, with: {why: timeout}}
```

「아직」과 「판정 불가」를 같은 종료 코드로 내는 명령(예: `&&`로 이어 붙인 명령의
마지막이 실패 시 1을 내는 경우)은 이 구분을 잃는다. 판정 불가가 있는 도구는
2로 끝나게 쓴다.

probe·`verify`에는 `${...}`를 쓸 수 없다(컴파일 오류). 런의 값은 환경 변수
`CFLOW_IN_<NAME>`(param), `CFLOW_DATA_<NAME>`(최상위 data의 스칼라)로 읽는다.

### 3.3 위임을 정한다

작업이나 하위 트리에 `executor`를 붙이면 그 부분이 다른 컨텍스트에서
실행된다. 판단 기준은 다음과 같다.

| 위임 | 메인에 둠 |
|---|---|
| 출력이 증거(수치, 해시, 목록)로만 쓰이고 이후 판단에 세부가 필요 없다 | 구현 작업 자체 |
| 출력이 많다: 테스트, 스윕, 검색, 보고서 생성 | 사용자와의 대화, 승인 판단 |
| 입력을 `with`로 명시할 수 있다 | 앞 단계의 세부 맥락을 계속 참조한다 |

```yaml
- id: verify-change
  executor: [headless, subagent, inline]   # 앞에서부터 가능한 것을 씀
  par:
    branches:
      tests:   targeted-test
      surface: surface-scan
    settle: allSettled
```

- `executor`는 선호 순서 목록이다. 런타임이 하네스·provider의 capability를 보고
  앞에서부터 가능한 것을 고르고, 어느 것도 안 되면 `inline`이다. `inline`은 어떤
  capability에도 기대지 않으므로 항상 마지막 경로로 존재한다.
- 하나만 고정해야 할 이유가 없으면 목록으로 쓴다.
- 위임된 하위 트리에는 `ask`(승인)도, `chooser: user`인 `choose`도 넣을 수 없다
  (`delegated-ask`, `delegated-user-gate` 오류). 위임받은 실행 주체가 사람 대신
  답하거나 게이트를 건너뛰게 되기 때문이다. 사람을 기다리는 게이트는 위임 노드
  밖에 둔다.

모든 것을 위임하면 메인의 공유 맥락이 사라지고, 같은 파일을 여러 컨텍스트가
다시 읽느라 전체 토큰 사용량은 늘어난다. 위임은 메인 컨텍스트를 줄이는
수단이고, 전체 비용을 줄이는 수단은 아니다.

### 3.4 반환값을 선언한다

위임된 노드의 결과는 `returns` 스키마로만 메인에 돌아온다. 스키마가 없으면
메인은 `ok` 하나만 받는다. 다음 분기에 필요한 값은 전부 스키마에 넣는다.

### 3.5 명세를 쓴다

```yaml
spec:
  invariants:
    no-master-merge: {never_effect: git.merge, target: master}
  paths:
    settle-before-end: {to: end, must_pass: [settle]}
```

- `invariants.never_effect`: 어느 작업도 이 효과를 선언하지 않았는지 컴파일 시점에
  검사한다. 작업의 `effects`를 정확히 적어야 의미가 있다. 이 불변식(또는 규칙의
  `effect`)이 있는 차트에서 `effects` 키가 없는 작업은 `effects-undeclared` 경고가
  난다. 효과가 없는 작업은 `effects: []`로 적는다.
- `paths.must_pass`: 시작부터 `to`까지의 모든 경로가 지정 노드를 지나는지 검사한다.
  실패하면 반례 경로를 보여 준다. 실패 결과가 조상의 `on`이나 `catch: {"*": …}`로
  가는 경로, `end`·`return`이 `finally`를 거쳐 나가는 경로도 계산에 들어간다.
  그래서 「반드시 지나야 하는 정리」는 `try … finally`로 쓰면 검사가 통과한다.
- `monitors`: 실행 중 데몬이 검사한다(단계 4).

### 3.6 검증한다

spawn 전에 다음을 돌린다.

```
claunch cflow2 validate <절차 파일 경로> --profile <자식 프로필>     (단계 2)
```

출력 내용:
- 컴파일 오류(파일:줄)
- 정적 검사 결과(`unbounded-loop`, `never-effect`, `must-pass`, `par-write-overlap`, `delegated-ask`, `delegated-user-gate`, `effects-undeclared` 등)
- 위임 노드마다 그 프로필에서 선택될 실행 주체와 fallback 경로

fallback 경로가 기대와 다르면 노드의 `executor` 목록이나 `needs`를 고친다.

### 3.7 편성한다

spawn의 `workflow`에 v2 절차 파일 경로를 준다(단계 4). v1과 같이 런 시작
시점에 스냅샷되므로, 이후 파일을 고쳐도 그 자식은 바뀌지 않는다. 재계획은
「지금 회차를 끝내고 새 파일로 새 런」이다.

경로로 줄 수 있는 것은 `.cflow2/generated/` 아래의 생성 파일뿐이다. 그 파일 안의
`include`·`call`은 경로가 아니라 이름이고, 레이어(프로젝트 → 전역 → 패키지)에서
찾는다. 같은 디렉터리의 다른 생성 파일을 이름으로 부를 수는 없다.

### 3.8 시작 입력을 선언한다

자식에게 줄 목표·범위·불변식·완료 기준을 오프닝 태스크 산문 대신 형식 있는
입력으로 줄 수 있다. 정의가 `inputs`를 선언하면(규격 2.6) 새 세션 폼과 spawn
모달이 칸을 그리고, spawn 도구와 CLI는 같은 이름으로 값을 받는다.

```yaml
inputs:
  goal:       {type: text, label: 목표, required: true}
  scope:      {type: list, label: 범위(경로·glob)}
  invariants: {type: invariants, label: 불변식}
  acceptance: {type: acceptance, label: 완료 기준}
nodes:
  intake:
    prompt: |
      목표: ${data.inputs.goal}
      범위: ${data.inputs.scope}
```

- 값은 런 데이터 `data.inputs`에 실리고, 프롬프트는 `${data.inputs.<이름>}`으로,
  식은 `$data.inputs.<이름>`으로 읽는다.
- `invariants`의 각 항목은 모든 작업에 전달되는 규칙이 된다. `probe`를 적은 항목은
  데몬이 300초마다 실행하는 모니터도 되며, 종료 코드가 0이 아니면 런이 멈춘다.
  예: `{text: master는 움직이지 않는다, probe: "test \"$(git rev-parse master)\" = 1a2b3c4"}`.
- `acceptance`의 각 항목은 런이 `ok`로 끝나기 직전에 확인된다. `probe`가 있으면
  엔진이 실행하고(0 = 충족), 없으면 모든 작업의 규칙 절에 실려 마지막 작업에서
  에이전트가 대조한다.
- probe는 적힌 그대로 셸에서 실행된다. 값을 끼워 넣는 문법은 없다.
- PM이 자식을 spawn할 때: MCP `spawn`의 `cflow_inputs`에
  `{"goal": "...", "invariants": [{"text": "...", "probe": "..."}]}`처럼 준다.
  CLI는 `--input goal=... --input scope=src/a --input scope=src/b` 또는
  `--inputs-file inputs.json`이다. 필수 입력이 없거나 선언에 없는 이름이 있으면
  spawn이 400으로 거절되고, 응답의 `errors`가 입력 이름별 이유를 싣는다.
- `improv-worker-v2`는 `goal`, `issue`, `scope`, `invariants`, `acceptance`,
  `max_rounds`를 선언한다. `goal`은 필수가 아니다: 데몬이 오프닝 태스크로 만든
  이슈나 리더 브리핑이 목표를 주는 경로가 있으므로, 비어 있으면 intake가 이슈·사용자·
  브리핑에서 목표를 정한다.

### 3.9 컬렉션과 trigger를 쓴다

컬렉션은 런이 여러 작업과 여러 런에 걸쳐 쌓고 고치는 기록이다(규격 2.7).
trigger는 노드에 들어가거나 떠나는 순간 데몬에게 맡기는 부수 작업이다(규격 2.8).

**컬렉션을 선언하는 경우**

| 상황 | 선언 |
|---|---|
| 자식 워커의 착지 요청을 받아 차례로 처리한다 | `queue`. 보통 `landing-queue` 형식 |
| 다른 런이 보낸 신호(예: 컷 요청, 착지 완료)를 기다린다 | `log`. 보통 `signals` 형식 |
| 한 런 안에서 처리할 항목 목록을 작업 사이에 넘긴다 | `queue` |
| 작업의 결과 값 하나를 다음 작업에 넘긴다 | 선언하지 않는다. `bind`와 `$result`를 쓴다 |

컬렉션이 없으면 같은 기록은 프롬프트 산문과 이슈 코멘트에만 있고, 엔진은 그
기록을 읽지 못한다. 엔진이 조건으로 읽어야 하는 기록(대기 해제, 분기)이면
컬렉션으로 선언한다.

**형식 가져오기**

`improv-common`을 include하면 두 형식을 쓸 수 있다.

```yaml
include: [improv-common]
collections:
  landing: {type: landing-queue, env: {TARGET: master}}
  signals: {type: signals}
```

- `landing-queue`: key `issue`, 필드 `issue`·`branch`·`tip`·`requested_by`·`note`,
  상태 `requested → waiting/deferred → landed/rejected`. 자식(`child`)과 데몬이
  넣고, 드라이버가 상태를 옮긴다. `landed`는 데몬의 watch만 쓴다: 60초마다
  `git merge-base --is-ancestor <tip> <TARGET>`이 0이면 `landed`가 된다.
  `env.TARGET`은 필수이며, 빠뜨리면 컴파일 오류다. 런이 끝나면 `landed`·`rejected`
  항목은 버리고 `deferred`는 `requested`로 되돌려 다음 런에 넘긴다.
- `signals`: 필드 `name` 하나인 `log`. 드라이버와 같은 세션의 다른 런(`peer`)이
  넣는다. 읽는 런은 `fresh('signals', '<이름>')`으로 확인하고 `consume`으로
  처리 위치를 옮긴다.
- 쓰는 쪽의 `env`는 이름별로 형식의 `env`와 합쳐진다. 다른 키(`lifecycle` 등)는
  쓰는 쪽의 값이 형식의 값을 대체한다.

**노드 효과(`do:`)와 대기(`wait until`)**

```yaml
nodes:
  review-request:
    prompt: …
    do:
      - push: {to: signals, item: {name: "'cut'"}}
      - {set: {in: landing, key: "$data.issue", fields: {status: "'waiting'"}},
         when: "$outcome == 'ok'"}
body:
  - review-request
  - wait: {until: "count('landing', 'requested') == 0", seconds: 3600}
```

- 값은 식이다. 문자열 상수는 따옴표를 한 번 더 쓴다(`"'waiting'"`).
- `at`을 생략하면 노드를 떠날 때 실행한다. 효과가 실패하면(접근 거절, 형식 오류)
  그 노드의 결과가 `fail`이 된다.
- 노드 효과의 주체는 드라이버다. `access`가 드라이버를 막는 연산(예:
  `landing-queue`의 `set` to `landed`)은 검사 오류 `do-access`다.
- 위임된 하위 트리 안의 노드에는 `do:`를 쓸 수 없다(`effect-in-delegation`).
  효과는 위임 노드 자신에 둔다.
- `wait until`은 셸 명령 없이 엔진이 식을 판정한다. `probe`와 함께 쓸 수 없다.
  읽는 런이 없으면 결과 `absent`, `seconds`가 지나면 `timeout`이다.

**trigger**

```yaml
nodes:
  integration-request:
    triggers:
      - briefing                          # 들어갈 때
      - {do: enqueue-landing, at: leave}  # 떠날 때
```

| 동작 | 내용 |
|---|---|
| `briefing` | 드라이버 세션의 LLM 브리핑을 다시 구성한다 |
| `checks` | 드라이버 세션에 상태 점검 재보고 요청을 보낸다 |
| `enqueue-landing` | 이 세션의 in_review 이슈와 tip을 부모 런의 `landing`(v2) 또는 `landing_queue`(v1)에 넣는다 |
| `push: {to: parent:<이름>, each, item}` | 부모 런의 컬렉션에 레코드를 넣는다. 식에서 `$facts`(`head`, `branch`, `in_review`, `session`)와 `$each`를 읽는다 |

- 이름만 쓰면 들어갈 때(`at: enter`) 실행한다.
- trigger는 런을 멈추지 않는다. 실행하지 못하면 저널에 `trigger_skipped`와 사유가
  남는다. 그래서 프롬프트에 「요청이 오면 한다」고 적을 수는 있지만 「데몬이 반드시
  보낸다」고 적지 않는다.
- trigger는 최소 1회 실행된다. 데몬이 실행 도중 재기동되면 다시 실행한다. `push`는
  같은 trigger가 넣은 레코드를 다시 바꾸지 않지만, `briefing`과 `checks`는 두 번
  도착할 수 있다.
- trigger가 하는 일을 프롬프트에서 다시 지시하지 않는다(예: 착지 큐에 직접
  등록하라는 산문). 같은 요청이 두 번 들어간다.
- `push`와 `enqueue-landing`은 위임된 하위 트리 안에서 쓸 수 없다
  (`trigger-in-delegation`). `briefing`과 `checks`는 쓸 수 있다.
- `parent:` 주소는 trigger에서만 쓴다. `main:`·`side:` 주소(같은 세션의 다른 런,
  규격 2.9)는 읽기와 `consume`에만 쓴다. 다른 런에 신호를 보내려면 자기 log에
  쓰고, 받는 런이 `fresh('main:signals', NAME)`으로 읽게 한다(`peer-write`).
- 한 log에 이름이 다른 신호가 섞이면 `consume: {from: ADDR, name: NAME}`으로 그
  이름만 소비한다. 이름 없는 `consume`은 log 전체를 소비해 처리하지 않은 다른 신호도
  지운다.

---

## 4. capability와 fallback 읽기

위임 노드가 실제로 어떻게 실행되는지는 자식 세션의 하네스와 provider에 달려
있다. capability는 `~/.claunch.yaml`에서 provider와 profile 단위로 기술한다
(규격 6.2절).

```
claunch cflow2 caps --profile <프로필>       (단계 2)
```

이 명령은 13개 capability의 값과 각 값이 정해진 층(하네스 기본값, provider,
profile)을 보여 준다.

자주 보게 될 fallback:

| 상황 | 일어나는 일 |
|---|---|
| 하네스에 비대화형 실행이 없음 | `headless`를 건너뛰고 목록의 다음 실행 주체 |
| 구조화 출력 플래그가 없음 | 출력의 마지막 ```json 블록을 추출해 스키마 검증. 실패하면 1회 재실행 후 `fail` |
| 읽기 전용 위임인데 샌드박스·도구 제한이 없음 | 일회용 worktree에서 실행하고 변경을 버림 |
| 병렬 하위 에이전트가 없음 | 직렬 실행. `race`는 선언 순서대로 시도 |
| 비용 상한 플래그가 없음 | 상한을 강제하지 않고 기록만 남김. `strict_budget: true`면 그 실행 주체를 건너뜀 |

모든 fallback은 런 저널에 `fallback` 이벤트(원인 capability와 선택된 경로)로
남는다.

---

## 5. v1 패턴 대응표

| v1에서 이렇게 썼다면 | v2에서는 |
|---|---|
| `select` 옵션 하나가 앞 스텝으로 되돌아감 | `loop` 또는 `retry` + `{continue: …}` |
| `timer` 스텝으로 대기 후 다른 스텝으로 | `timeout: {do, seconds, else}` |
| 재촉 스텝과 대기 스텝이 서로를 가리킴 | `loop: {while: "true", max: 25, do: {choose: {options: {nudge: {interval: 600, then: [재촉 작업]}, …}}}}` — 간격은 재촉 옵션에 건다. 루프의 `every`는 간격 동안 선택 전체를 닫아, 그사이 온 답(착지 등)을 고를 수 없다(claunch-jbk5w) |
| 여러 스텝에서 `work`로 가는 간선 | `try: … catch: {rework: …}` |
| `skip: true` + `editable` + 에이전트의 `set_state` | `cond: [{if: "$data.stack_open", then: …}]` |
| 공통 블록 복사 | `fragments` + `${frag:이름}` |
| 같은 금지 문장을 여러 스텝에 | `rules` + `deliver` |
| 서브 런 시작을 산문으로 지시 | `group` 또는 `par` |
| 「subagent를 띄워서 돌리고 영수증을 확인」 산문 | `executor` + `returns` + `spec.ensures` |
| `extends` overlay | `include` + `call` |
| `landing_queue:` | `collections: {landing: {type: landing-queue, env: {TARGET: master}}}` |
| `publishes`/`awaits` | `signals` 형식의 `log` + `do: [{push: …}]` / `wait: {until: "fresh(…)", consume: […]}` |
| `triggers:` | 같은 이름의 노드 키 `triggers:`(형태 동일) |

---

## 6. 점검 목록

spawn 전에 확인한다.

- [ ] 파일 첫 키가 `cflow: 2`이고, 파일이 `v2/` 디렉터리나 `.cflow2/generated/` 아래에 있다
- [ ] 모든 `loop`·`retry`에 `max`가 있다
- [ ] 위임 노드마다 `returns`가 있고, 다음 분기가 쓰는 값이 스키마에 있다
- [ ] 위임 노드 안에 `ask`도 `chooser: user`인 `choose`도 없다
- [ ] 모든 작업에 `effects`가 적혀 있다(읽기만 하면 `fs.read`, `test.run`, 효과가 없으면 `[]`)
- [ ] probe가 있는 `wait`는 `seconds` 상한이 있고 `undecidable`·`timeout`을 `on`으로 받는다
- [ ] `verify`·probe에 `${...}`가 없다(값은 `CFLOW_IN_*`·`CFLOW_DATA_*` 환경 변수)
- [ ] 병렬 분기 중 쓰기를 하는 것은 `paths`가 서로 겹치지 않는다
- [ ] 의존은 `wait`의 probe로 적었고, 메시지 약속에 기대지 않는다
- [ ] 컬렉션을 읽는 식·`do:`·`wait until`이 선언된 컬렉션만 가리키고, `landing-queue`에 `env.TARGET`이 있다
- [ ] 위임 노드 안쪽에 `do:`, `push`·`enqueue-landing` trigger가 없다
- [ ] `claunch cflow2 validate --profile <자식 프로필>` 출력이 보고에 있고, 경고마다 받아들이는 이유가 있다

---

## 7. 자주 하는 실수

| 실수 | 결과 | 대처 |
|---|---|---|
| prompt에 「끝나면 X 단계로 간다」를 적음 | 문법의 흐름과 산문이 어긋남 | 흐름은 조합자로만 쓴다 |
| `returns` 없이 위임 | 메인이 결과 내용을 받지 못함 | 필요한 필드를 스키마에 넣는다 |
| `executor: headless` 하나만 지정 | 비대화형 실행이 없는 하네스에서 바로 `inline`으로 내려감 | 선호 순서 목록으로 쓴다 |
| `effects`를 비워 둠 | 실행기는 쓰기 가능으로 보고(읽기 전용 경로를 쓰지 않음), 정적 검사는 효과 없음으로 봄(`never-effect`가 놓침, `effects-undeclared` 경고) | 모든 작업에 적는다. 효과가 없으면 `effects: []` |
| `verify`에 `${params.x}`를 씀 | 컴파일 오류(값이 셸 명령이 되어 verify를 우회할 수 있음) | `"$CFLOW_IN_X"`처럼 환경 변수로 읽는다 |
| probe가 있는 `wait`에 상한이 없음 | 조건을 판정할 수 없는 상태에서 무기한 대기 | `seconds`를 주고 `undecidable`·`timeout`을 `on`으로 받는다 |
| 위임 노드 안에 `choose`의 `chooser: user` | 비대화형 프로세스 안에서 사용자 선택을 받을 수 없음 | 사용자 선택은 위임 노드 밖에 둔다 |
| `do:`의 문자열 상수를 따옴표 없이 씀(`status: waiting`) | 식에서 `waiting`은 알 수 없는 이름이라 컴파일 오류 | `"'waiting'"`처럼 식 안에서 따옴표로 감싼다 |
| `log`에 `set`·`remove`, `queue`에 `consume` | 검사 오류 `collection-op-kind` | `log`는 추가만 한다. 처리 위치는 `consume`으로 옮긴다 |
| 프로시저가 자기 자신을 호출 | 컴파일 오류(같은 런 안의 재귀는 금지) | 재귀가 필요하면 자식 세션 spawn으로 |

---

## 8. 참고

| 문서 | 내용 |
|---|---|
| `docs/cflow-v2.md` | v2 정본 규격 |
| `docs/cflow-as-automaton.md` | v1 스텝 그래프를 오토마톤으로 읽은 기록 |
| `docs/cflow-delegation.md` | v1의 승인 위임(`ask.from`) 규격 |
| 보드 `claunch-yq80p` | 설계 논의 기록(ANALYSIS-1~7) |
