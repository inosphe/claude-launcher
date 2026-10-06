# cflow v2 — 조합자 문법, statechart IR, 위임 실행, 명세

이슈: `claunch-yq80p`. 설계 논의 기록은 그 이슈의 ANALYSIS-1~7 코멘트에 있다.
이 문서는 v2의 정본 규격이다. 구현은 `src/claude_launcher/cflow2/`에 있고,
v1(`src/claude_launcher/cflow/`)과 병행한다.

## 0. 결정 사항

| 항목 | 결정 | 출처 |
|---|---|---|
| 런타임 | v1과 별도 런타임. v1 엔진·상태 디렉터리를 공유하지 않는다 | 사용자 선택 |
| IR | 표면 문법과 실행 사이에 IR을 둔다. IR은 계층적 statechart(SCXML 계열)다 | ANALYSIS-6 |
| 재귀 | 같은 런 안의 재귀 호출은 금지한다. `call`은 컴파일 시점에 펼친다 | 사용자 결정 |
| 실행 주체 | `inline`, `subagent`, `headless`, `session` 네 가지. 첫 단계에서 넷 다 지원한다 | 사용자 결정 |
| capability | 실행 주체가 기대는 하네스·provider 기능을 원자 단위로 정의하고 `~/.claunch.yaml`의 provider에서 기술한다. 기능에 따라 갈라지는 모든 경로에 fallback을 정의한다 | 사용자 결정, 6절 |
| 정의 | 첫 v2 정의는 `improv-worker-v2`. v1 정의는 deprecated 표시 후 유지한다 | 사용자 선택 |

## 1. 파일과 레이어

- v2 정의는 각 레이어의 `v2/` 하위 디렉터리에 둔다.
  - 패키지: `src/claude_launcher/workflows/v2/*.yaml`
  - 전역: `~/.claude-launcher/workflows/v2/*.yaml`
  - 프로젝트: `<repo>/.claunch/workflows/v2/*.yaml`
- 우선순위는 v1과 같다: 프로젝트 → 전역 → 패키지.
- 생성 파일(PM이 자식마다 쓰는 절차 파일): `<세션 디렉터리>/.cflow2/generated/*.yaml`.
  레이어 탐색 대상이 아니며 spawn의 `workflow`에 절대 경로로 넘긴다(v1의
  `.cflow/generated/`와 같은 규칙). `include`는 레이어에서 이름으로 찾는다.
- `include`는 대상 파일의 `kind`와 관계없이 그 파일의 `fragments`·`rules`·
  `procedures`를 가져온다. 그래서 `improv-worker-v2`는 회차의 구간을
  `procedures`로 정의하고 `body`에서 호출하는 형태로 작성한다
  (작성 가이드: `docs/cflow-v2-guide.md`).
- v1의 탐색(`state._candidates`, `bundled_workflows`)과 저장소 도구
  (`tools/sync_project_layer.py`, `tools/check_tmp_paths.py`)는 디렉터리를
  재귀하지 않으므로 v2 파일은 v1 경로에 들어가지 않는다.
- 모든 v2 파일의 첫 키는 `cflow: 2`다. v2 로더는 이 키가 없는 파일을 거절한다.
  v1 파서는 모르는 최상위 키를 거절하지 않으므로(`model.parse_doc`), 형식 키와
  디렉터리 분리를 함께 쓴다.
- 이름 충돌: 병행 기간 동안 v2 정의는 v1과 다른 이름을 쓴다(`improv-worker-v2`).
  목록 API는 두 버전을 한 목록에 싣고 `version` 필드로 구분한다.

## 2. 표면 문법

### 2.1 최상위 키

| 키 | 의미 |
|---|---|
| `cflow` | 형식 버전. 값은 `2` |
| `name`, `description` | 정의 이름과 설명 |
| `kind` | `workflow`(기본) 또는 `procedure`(다른 정의가 `call`로 부르는 정의) |
| `default_role`, `filter_roles`, `default_child_cflow`, `priority` | v1과 같은 의미 |
| `params` | 입력 선언. `{name: {type, required, default}}` |
| `returns` | 결과 선언. `{outcome: {field: type}}` |
| `data` | 런 데이터 모델의 초기값 |
| `include` | 다른 v2 파일의 `fragments`·`rules`·`procedures`를 가져온다 |
| `fragments` | 이름 붙은 산문 조각 |
| `rules` | 전역 규칙(불변식의 산문 표현과 근거) |
| `procedures` | 이 파일 안에서 정의하는 프로시저 |
| `nodes` | 이름 붙은 작업(task) 정의 |
| `body` | 조합자 트리. 런의 시작점 |
| `spec` | 명세(8절) |

### 2.2 작업(task)

`nodes:` 아래의 항목 하나가 작업 하나다. 작업은 에이전트가 수행하는 단위다.

```yaml
nodes:
  work:
    title: 작업 실행
    prompt: |
      피처 브랜치에서 목표를 구현한다.
      ${frag:no-premature-rebase}
    done_when: 범위의 구현이 커밋으로 존재한다
    verify: "uv run --no-sync pytest -q tests/test_x.py"
    executor: inline                  # 또는 선호 순서 목록 [headless, subagent, inline]
    needs: [tools.restrict]           # 실행 주체에 추가로 요구하는 capability
    returns:
      ok:   {files: list, summary: str}
      fail: {reason: str}
    effects: [fs.write, git.commit]
    budget: {seconds: 1800}
    rules: [no-master-merge]
    ask: {prompt: 진행할까요?, from: [{role: leader}], otherwise: human}
```

- `prompt`는 템플릿이다. `${frag:NAME}`은 컴파일 시점에 전개한다. `${NAME.path}`
  형태의 값 참조는 렌더 시점에 런 데이터로 채운다. text id는 전개 후의 템플릿에서
  계산하므로 값 참조가 text id를 바꾸지 않는다.
- `returns`가 없으면 결과는 `{outcome: ok}` 하나다.

#### verify

- 표기: 명령 문자열, argv 목록, 또는 `{run, timeout}`(`run` 대신 v1 키 `command`도
  받는다). 컴파일러는 `{run, timeout}`으로 정규화해 IR에 싣는다. `timeout`을
  생략하면 런타임이 v1 기본값 900초를 쓴다. 문자열 명령의 `${...}` 값 참조는 실행
  직전에 렌더한다.
- 실행 시점: 작업이 `ok`로 보고될 때. 다른 결과(`fail` 등)는 verify를 거치지 않는다.
- 실행 위치: 런의 cwd. 환경은 프로세스 환경에 `CFLOW2_RUN`, `CFLOW2_NODE`,
  `CFLOW2_INSTANCE`, `CFLOW2_SCOPE`와 param마다 `CFLOW_IN_<NAME>`(런의 params 위에
  작업이 속한 프로시저의 params를 덮은 값)을 더한 것이다.
- 런 잠금 밖에서 실행한다. 잠금 없이 읽은 스냅샷에서 인스턴스를 찾고, 명령을
  실행하고, 잠금을 잡은 뒤 같은 인스턴스가 아직 활성 작업일 때만 결과를 적용한다.
  그사이 인스턴스가 바뀌었으면 아무것도 적용하지 않고 보고를 거절한다.
- 종료 코드 0: `verify_passed`를 남기고 보고를 진행한다. 0이 아니거나 시간 초과
  (프로세스 그룹 종료, 코드 124): `verify_failed`(출력의 마지막 40줄 포함)를 남기고
  보고를 거절한다. 오류 메시지에 출력의 끝부분이 실린다. 작업은 활성 상태로 남는다.
- 위임 실행 단위(`flow_delegate`)로 들어온 결과에는 verify를 적용하지 않는다.

#### ask

`ask`는 작업에 들어가기 위한 승인이며 방문마다 다시 요구된다(v1과 같은 의미).
`from`·`otherwise`·`timeout`은 v1 파서(`cflow.model._parse_delegate`)로 검증하므로
후보 표기(`role`, `scope`, `connect`)와 오류 문구가 v1과 같다.

```yaml
ask:
  prompt: 진행할까요?
  from: [{role: reviewer, scope: sibling}, {role: leader, scope: ancestor}]
  otherwise: human        # human(기본) | self | self:<option>(options가 있을 때)
  timeout: 900            # 그룹마다의 응답 기한(초). 없으면 기한 없음
  on_decline: declined    # 거절 시 작업의 결과 이름(기본 declined)
  # options: {pass: {outcome: ok, description: …}, changes: {description: …}}
```

- 형태 두 가지:

  | 형태 | 답 | 결과 |
  |---|---|---|
  | 승인(`options` 없음) | `approve`, `decline` | `approve`면 작업이 활성화된다. `decline`이면 작업이 `on_decline` 결과(기본 `declined`)로 끝난다 |
  | 결정(`options` 있음) | 옵션 이름 | 작업이 그 옵션의 `outcome`(기본은 옵션 이름)으로 끝난다. 값은 `{decision, reason, by, ask}` |

- `on_decline`의 대응: v2에는 goto가 없으므로 `on_decline`은 노드 라벨 대신 결과
  이름을 받는다. 작업 이름은 조합자 매핑이 아니어서 `on`을 붙일 수 없으므로, 거절된
  작업은 그 결과로 끝나고 감싸는 `try`의 `catch`(또는 감싸는 조합자의 `on`)가 그
  결과를 처리한다. 처리하는 곳이 없으면 비-ok 결과의 일반 규칙대로 런이 `failed`로
  끝난다(차트의 `returns`가 그 결과를 선언하면 `done`).
- 작업에 들어가면 인스턴스가 `await: approval`이 되고 ask 레코드
  (`state.json`의 `asks`, id `ask2-<hex>`)가 열린다. 드라이버는 그 작업을 보고할 수
  없다. `flow_status`의 `asks` 항목이 질문, 작업 프롬프트(`task_prompt`), 답 목록,
  현재 해소 단계(`resolution`), 다음 행동(`how`)을 싣는다.
- 해소 단계:

  | resolution | 의미 | 답하는 쪽 |
  |---|---|---|
  | `routing` | 다음 `from` 그룹을 메시 명단에 대해 해석해야 함 | 데몬 클럭 또는 드라이버의 다음 MCP 호출이 라우팅 |
  | `asked` | 응답자 세션에게 질문이 나감(`deadline`까지) | 그 응답자(`flow_answer`) |
  | `human` | 모든 그룹이 소진되고 `otherwise: human` | 사람(`claunch cflow2 approve`, 웹 UI). 런은 `waiting_approval`(reason `ask`) |
  | `self` | 모든 그룹이 소진되고 `otherwise: self` | 드라이버(`flow_approve`) |

  `otherwise: self:<option>`이면 그룹 소진 시점에 그 옵션을 응답자 없이 택한다
  (`ask_answered`의 `by`가 `unanswered`). 사람의 답은 해소 단계와 관계없이 적용되고,
  질문을 받고 있던 응답자에게는 질문이 닫혔다는 `fyi` 메시지가 간다.
- 라우팅(`cflow2/asks.py`): 런 잠금 밖에서 v1의 `responders.pool`로 명단을 한 번
  읽고 `Pool.match`로 그룹을 앞에서부터 해석한다. 후보 규칙(질문한 세션과 그
  하위 세션 제외, `scope`, `connect: true`의 연결 생성, 원격·종료 세션 제외)은 v1과
  같다. 명단을 읽지 못한 경우(데몬 무응답)는 그룹을 소비하지 않고 30초 뒤 다시
  시도하며 최대 4회까지 미룬다(`ask_deferred`). 질문은 v1과 같은 경로
  (`POST /api/mesh/<mesh>/messages`, type `decide`)로 전달되고, ask id → 런 위치
  색인이 `<런처 홈>/cflow2/asks/`에 기록된다. 응답자는 id만으로 답한다.
- 응답 권한: `flow_answer`의 세션 신원은 MCP 서버 환경의 `CLAUNCH_SESSION`에서
  읽는다. 라우팅 시점에 기록된 그룹의 세션만 답할 수 있고, 런 자신의 세션은 답할
  수 없다. `abstain`은 다음 그룹으로 넘긴다.
- 기한: 데몬 클럭이 매 틱 `asks.tick`을 호출한다. `asked` 그룹이 `deadline`을
  넘기면 `ask_timeout`을 남기고 다음 그룹으로 간다. 그룹이 남지 않으면 `otherwise`로
  간다. `human`·`self`로 넘어간 ask는 런의 세션에 한 번 통지된다.
- CLI 보호: `claunch cflow2 approve`는 사람의 경로다. v1의 `claunch cflow approve`와
  같이 CLI는 사람과 에이전트의 셸 호출을 구별하지 못하므로, 하네스 거부 규칙
  (`install.GATE_DENY_RULES`)에 `claunch cflow2 approve|select|extend|resume`을
  넣어 에이전트의 셸 호출을 막는다. 드라이버가 직접 답할 수 있는 경로는
  `self` 해소 단계의 `flow_approve` 하나다.
- 위임 실행 단위 아래의 작업에 `ask`가 있으면 정적 검사 오류(`delegated-ask`)다.

#### 프로시저 params 기본값

`call`이 펼친 `scope` 노드는 프로시저의 `params` 선언을 IR에 싣는다. 런타임은
scope에 들어갈 때 `with`에 없는 param에 `default`를 채우고, 기본값도 없으면
`null`을 넣는다. 호출 지점의 필수 param 누락은 컴파일 오류다. `group` 멤버는
`flow_fork`의 `with`가 param이 되며, 선언에 없는 키나 기본값 없는 필수 param이
빠진 fork는 거절된다.

### 2.3 조합자

조합자 트리의 항목은 셋 중 하나다: 작업 이름(문자열), 조합자 매핑, 제어 매핑.

| 조합자 | 형태 | 의미 |
|---|---|---|
| `seq` | `seq: [a, b, c]` | 순서대로 실행 |
| `waterfall` | `waterfall: [a, b]` | `seq`와 같고, 각 항목의 결과 값을 다음 항목의 `$prev`로 넘긴다 |
| `cond` | `cond: [{if: EXPR, then: T}, …, {else: T}]` | 식으로 기계가 고르는 분기 |
| `choose` | `choose: {prompt, chooser, options: {name: {description, then: T}}}` | 에이전트나 사용자가 고르는 분기. 옵션에 `when: EXPR`을 붙이면 거짓일 때 고를 수 없다 |
| `foreach` | `foreach: {in: EXPR, as: item, do: T, par: false}` | 목록의 항목마다 실행 |
| `loop` | `loop: {while: EXPR \| until: EXPR, max: N, do: T}` | 조건 루프. `max` 필수 |
| `par` | `par: {branches: {a: T, b: T}, settle: all}` | 병렬 실행. `settle`은 `all`, `allSettled`, `race`, `any`, `quorum:K` |
| `retry` | `retry: {do: T, max: N, every: SECONDS, until: EXPR, between: T}` | 재시도. `between`은 시도 사이에 실행하는 트리 |
| `timeout` | `timeout: {do: T, seconds: N, else: T}` | 기한 초과 시 `else`로 감 |
| `try` | `try: T, catch: {OUTCOME: T}, finally: T` | 결과 종류로 예외 경로를 처리하고, 마지막에 정리 트리를 실행 |
| `call` | `call: PROC, with: {…}, bind: VAR` | 프로시저 호출. 컴파일 시 펼친다 |
| `wait` | `wait: {probe: CMD, poll: S, describe: …}` 또는 `wait: {seconds: N}` | 외부 신호 대기 |
| `group` | `group: {name, run: PROC, limit, settle, do: T}` | 동적 분기. `do` 실행 중 에이전트가 `fork`로 멤버를 추가하고, `do`를 떠나기 전에 합류 |

모든 조합자 매핑에 붙일 수 있는 공통 키:

- `id`: 노드 id. 생략하면 부모 경로에서 만든다.
- `executor`, `needs`, `budget`: 하위 트리 전체를 그 실행 주체에 위임한다(7절).
- `bind: VAR`: 결과 값을 데이터 모델에 저장한다.
- `on: {OUTCOME: CONTROL}`: 결과 종류별 처리.

제어 매핑:

| 제어 | 의미 |
|---|---|
| `{continue: LABEL}` | 감싸는 `loop`·`foreach`·`retry`의 다음 회차로 |
| `{break: LABEL}` | 감싸는 반복을 끝냄 |
| `{raise: OUTCOME}` | 감싸는 `try`의 `catch`로 |
| `{return: OUTCOME, value: {…}}` | 프로시저(또는 런)를 그 결과로 끝냄 |
| `end` | 런 종료 |

`goto`는 없다. 앞 단계로 되돌아가는 경로는 반복 조합자와 `continue`로 쓴다.

### 2.4 식(expression)

`cond.if`, `loop.while`, `choose.options.*.when`, `foreach.in`, `with` 값에 쓰는
식이다. 실행은 엔진 안의 해석기가 하고, 파이썬 `eval`을 쓰지 않는다.

- 값: 문자열, 정수, 실수, `true`/`false`/`null`, 목록 `[…]`
- 참조: `$params.x`, `$data.x.y`, `$prev`, `$item`, `$result.<node>`,
  `$visits.<node>`, `$caps.<capability>`
- 연산: `== != < <= > >=`, `in`, `not in`, `and`, `or`, `not`, 괄호
- 함수: `len(x)`, `empty(x)`

### 2.5 fragments, rules, include

```yaml
include: [improv-common]             # v2/improv-common.yaml 의 fragments·rules·procedures
fragments:
  beads-common: |
    ── beads(br) 규칙 — 공통 ──
    …
rules:
  no-master-merge:
    text: 워커는 master를 직접 머지하지 않는다.
    why: 2026-09-20 claunch-97knp …
    deliver: [branch-setup, commit, integration-request]   # 또는 all
    effect: {deny: git.merge, target: master}
```

- fragment 안에서 다른 fragment를 참조할 수 있다. 순환은 컴파일 오류다.
- 규칙의 `text`는 `deliver`에 적힌 작업의 프롬프트 끝에 붙는다. `why`는 기본
  페이로드에 싣지 않고 `rule` 조회로 읽는다.
- 규칙의 `effect`는 정적 명세 검사에 들어간다(8절).

## 3. IR

IR은 JSON으로 직렬화되는 statechart다. 버전은 `ir_version`(현재 1)으로 표시한다.

```json
{
  "ir_version": 1,
  "name": "improv-worker-v2", "kind": "workflow", "description": "…",
  "meta": {"default_role": "worker", "filter_roles": null, "default_child_cflow": null, "priority": 0},
  "params": {…}, "returns": {…}, "data": {…},
  "rules": {"no-master-merge": {"text": "…", "why": "…", "effect": {…}}},
  "spec": {…},
  "root": "n0",
  "nodes": {
    "n0": {"type": "seq", "children": ["n1", "n2"], "src": "improv-worker-v2.yaml:40"},
    "n1": {"type": "task", "label": "intake", "title": "…", "prompt": "…", "text_id": "…",
           "executor": ["inline"], "needs": [], "returns": {…}, "effects": [], "rules": ["…"],
           "verify": null, "ask": null, "done_when": "…", "budget": null}
  }
}
```

| 노드 type | statechart 대응 | 필드 |
|---|---|---|
| `task` | 원자 상태 | 2.2절 필드 + `label` |
| `seq` | 복합 상태(순차) | `children`, `pass_prev` |
| `cond` | 가드 전이를 가진 복합 상태 | `branches: [{guard, child}]` |
| `choose` | 외부 선택 전이 | `prompt`, `chooser`, `options: [{name, description, guard, child}]` |
| `loop` | 복합 상태 + 자기 전이 | `mode`(while/until/foreach/retry), `guard`, `max`, `child`, `between`, `every`, `items`, `as`, `parallel` |
| `par` | 병렬 영역 | `branches: {name: child}`, `settle` |
| `timeout` | 시간 전이를 가진 복합 상태 | `child`, `seconds`, `else` |
| `try` | 복합 상태 + 결과 전이 | `child`, `catch: {outcome: child}`, `finally` |
| `wait` | 외부 이벤트 대기 원자 상태 | `probe`, `poll`, `seconds`, `describe` |
| `group` | 동적 병렬 영역 | `child`, `member`(펼친 프로시저의 루트), `limit`, `settle`, `name` |
| `control` | 전이 | `op`(continue/break/raise/return/end), `target`, `outcome`, `value` |
| `scope` | `call`을 펼친 복합 상태 | `proc`, `child`, `with`, `bind` |

모든 노드에 공통: `id`, `type`, `parent`, `src`(원본 파일:줄), `executor`(위임
지점이면), `bind`, `on`.

노드 id는 작성자가 준 `id`·작업 이름에서 만들고, 없으면 부모 id와 위치에서
만든다. 같은 정의를 다시 컴파일하면 같은 id가 나온다.

## 4. 런타임

### 4.1 상태 저장

`<repo>/.cflow2/runs/<scope>/` (v1의 `.cflow/`와 분리):

| 파일 | 내용 |
|---|---|
| `chart.json` | 시작 시점의 IR 스냅샷. 런은 이 파일만 읽는다 |
| `state.json` | 활성 구성(active configuration), 데이터 모델, 노드별 방문 수, 위임 실행 단위 |
| `journal.jsonl` | 추가만 하는 이벤트 로그 |
| `receipts/` | 위임 실행 단위의 영수증 |

### 4.2 실행 의미

- 활성 구성은 현재 진행 중인 원자 상태(task, wait)와 그 조상 복합 상태의
  집합이다. `par`와 `group` 아래에서는 원자 상태가 여럿일 수 있다.
- 원자 상태 하나는 `pending → active → done(outcome, value)`로 진행한다.
- 결과가 나오면 엔진은 부모로 올라가며 다음 활성 상태를 정한다(`advance`).
  `on` 매핑, `try`의 `catch`, 반복의 `continue`/`break`, `par`의 `settle`이
  이 단계에서 평가된다.
- 방문 수는 노드별로 런 전체에서 센다. `loop.max`를 넘으면 런이
  `waiting_approval`(reason `loop_limit`)로 멈춘다.
- `bind`가 있으면 결과 값을 `data`에 저장한다.

### 4.3 저널 이벤트

`started`, `entered`, `left`(outcome 포함), `selected`, `reported`, `bound`,
`delegate_started`, `delegate_returned`, `delegate_failed`, `fallback`(원인과 경로),
`group_fork`, `group_settled`, `monitor_violation`, `waiting`, `done`, `aborted`,
`verify_passed`, `verify_failed`(종료 코드, 출력 끝부분), `ask_routed`(그룹, 응답자),
`ask_answered`(결정, 답한 쪽, 경로), `ask_timeout`, `ask_deferred`,
`ask_undelivered`. ask가 그룹을 건너뛰거나 `otherwise`로 넘어갈 때는
`fallback`(`decision: ask`)을 남긴다.

- 런의 `waiting_approval`은 열린 승인 항목에서 계산한다: `loop_limit`,
  `monitor_violation`, `human` 단계의 ask가 하나라도 있으면 `waiting_approval`,
  없으면 `running`.

## 5. 에이전트 인터페이스(MCP)

v1 툴과 이름이 겹치지 않도록 `flow_` 접두어를 쓴다.

| 툴 | 의미 |
|---|---|
| `flow_start` | v2 런 시작(`workflow`, `params`, `scope`) |
| `flow_status` | 활성 구성, 렌더된 현재 작업 프롬프트, 고를 수 있는 옵션 |
| `flow_report` | 작업 결과 보고(`node`, `outcome`, `value`, `text`) |
| `flow_select` | `choose` 노드의 옵션 선택(`node`, `option`, `reason`) |
| `flow_fork` | `group`에 멤버 추가(`group`, `with`) |
| `flow_delegate` | 위임 노드의 브리프 조회와 결과 제출(`subagent` 실행 주체에서 사용) |
| `flow_rule` | 규칙의 `text`·`why` 조회 |
| `flow_abort` | 런 중단 |
| `flow_approve` | `self` 단계로 넘어간 자기 런의 ask에 답함(`node`, `option`, `decline`, `reason`). 다른 단계에서는 거절 |
| `flow_answer` | 다른 세션의 런이 라우팅한 ask에 응답자로서 답함(`ask_id`, `decision`, `reason`) |
| `flow_asks` | 이 세션의 답을 기다리는 ask 목록 |

## 6. capability와 provider 기술

### 6.1 원자 capability 목록

위임 실행이 기대는 기능을 서로 겹치지 않는 원자 단위로 나눈다.

| id | 의미 | 사용하는 곳 |
|---|---|---|
| `subagent.spawn` | 드라이버 에이전트가 하네스 안에서 새 컨텍스트의 하위 에이전트를 띄울 수 있다 | `subagent` 실행 주체 |
| `subagent.parallel` | 하위 에이전트 여럿을 동시에 띄울 수 있다 | `par`·`group` + `subagent` |
| `subagent.tools.restrict` | 하위 에이전트의 도구를 제한할 수 있다 | 읽기 전용 위임 |
| `subagent.worktree` | 하위 에이전트를 별도 git worktree에서 실행할 수 있다 | 쓰기 효과가 있는 병렬 위임 |
| `headless.run` | 비대화형으로 프롬프트 하나를 실행하고 끝나는 프로세스를 띄울 수 있다 | `headless` 실행 주체 |
| `headless.output.schema` | 비대화형 실행의 최종 출력을 JSON 스키마로 강제할 수 있다 | 구조화 반환 |
| `headless.output.json` | 비대화형 실행의 출력을 기계가 읽는 JSON으로 받을 수 있다 | 반환 추출 |
| `headless.tools.restrict` | 비대화형 실행의 도구를 허용·금지 목록으로 제한할 수 있다 | 읽기 전용 위임 |
| `headless.sandbox.readonly` | 비대화형 실행을 파일시스템 읽기 전용 샌드박스로 띄울 수 있다 | 읽기 전용 위임 |
| `headless.budget.usd` | 비대화형 실행에 비용 상한을 줄 수 있다 | `budget.usd` |
| `headless.resume` | 끝난 비대화형 실행을 이어서 재개할 수 있다 | `retry`의 이어하기 |
| `mcp.claunch` | 실행 주체 안에서 claunch MCP 툴을 부를 수 있다 | `flow_delegate`로 결과 제출 |
| `session.spawn` | claunch spawn으로 자식 세션을 만들 수 있다(데몬·정책 기능) | `session` 실행 주체 |

### 6.2 기술 위치와 합성

capability는 세 층에서 기술하고 아래 순서로 합성한다. 뒤의 층이 앞의 층을
덮어쓴다.

1. 하네스 기본값: 패키지 `harnesses.yaml`의 `capabilities:` (하네스의 CLI가
   제공하는 기능). 사용자 `harnesses:` 블록도 같은 키를 쓸 수 있다.
2. provider: `~/.claunch.yaml`의 `providers.<이름>.capabilities:` (모든 하네스에
   적용) 와 `providers.<이름>.harness_options.<하네스>.capabilities:`
   (그 하네스에만 적용).
3. profile: `profiles.<이름>.capabilities:` 와
   `profiles.<이름>.harness_options.<하네스>.capabilities:`.

```yaml
providers:
  ds4-provider-official:
    capabilities:
      headless.output.schema: false     # 이 백엔드는 구조화 출력을 지원하지 않음
    harness_options:
      claude:
        capabilities:
          subagent.parallel: false
```

값은 `true`, `false`, 또는 매핑 `{enabled: bool, args: [...]}`이다. `args`는
그 기능을 켤 때 하네스 명령에 붙일 인자 템플릿이다(`{schema}`, `{schema_file}`,
`{tools}`, `{usd}`, `{session_id}` 치환). `{tools}`에 목록을 주면 쉼표로 잇는다.
`true`만 적은 층은 아래 층의 `args`를 그대로 쓴다.

기술되지 않은 capability는 `false`로 본다.

### 6.3 패키지 기본값

`--help` 출력으로 확인한 것만 기본값을 `true`로 둔다(2026-10-06, 이 머신).

| capability | claude | codex | pi | devin | kimi, agent |
|---|---|---|---|---|---|
| `subagent.spawn` | true (Agent 도구) | false | false | false | false |
| `subagent.parallel` | true | false | false | false | false |
| `subagent.tools.restrict` | true | false | false | false | false |
| `subagent.worktree` | true (`isolation: worktree`) | false | false | false | false |
| `headless.run` | true (`-p`) | true (`exec`) | true (`-p`) | true (`-p`) | false |
| `headless.output.schema` | true (`--json-schema`) | true (`--output-schema`) | false | false | false |
| `headless.output.json` | true (`--output-format json`) | true (`--json`) | true (`--mode json`) | false | false |
| `headless.tools.restrict` | true (`--allowedTools`/`--disallowedTools`) | false | true (`--tools`/`--exclude-tools`) | false | false |
| `headless.sandbox.readonly` | false | true (`-s read-only`) | false | false | false |
| `headless.budget.usd` | true (`--max-budget-usd`) | false | false | false | false |
| `headless.resume` | true (`--resume`) | false | false | false | false |
| `mcp.claunch` | true | true | false | false | false |
| `session.spawn` | 데몬 정책(`spawn.*`)으로 결정 | 같음 | 같음 | 같음 | 같음 |

- `headless.output.schema`의 인자 형식은 하네스마다 다르다. claude의
  `--json-schema`는 스키마 JSON 문자열을 직접 받고(`{schema}`), codex의
  `--output-schema`는 파일 경로를 받는다(`{schema_file}`).
- `headless.run` 인자: claude `-p`, codex는 하위 명령 `exec`, pi `-p`, devin `-p`.
- `headless.resume`: codex(`codex exec resume <id>`, `exec`를 대체하는 하위 명령),
  pi(`--session <id>`), devin(`-r <id>`)에도 재개 수단이 있으나 연결하지 않았으므로
  기본값은 `false`다.
- 값과 인자는 `src/claude_launcher/harnesses.yaml`의 `capabilities:` 블록이 정본이다.

`claunch cflow2 caps [--profile P]`가 합성 결과와 각 값의 출처 층을 보여 준다.

### 6.4 fallback 경로

모든 분기는 실행 시점에 `fallback` 저널 이벤트(원인 capability, 선택된 경로)를
남긴다. 정적으로 결정 가능한 것은 `cflow2 validate --profile P`가 미리 보여 준다.

**실행 주체 선택.** 노드의 `executor` 목록을 앞에서부터 보고, 아래 조건을
만족하는 첫 항목을 쓴다. 하나도 만족하지 않으면 `inline`이다. `inline`은 어떤
capability에도 기대지 않으므로 마지막 경로는 항상 존재한다.

| 실행 주체 | 필요 조건 | 조건이 깨졌을 때 |
|---|---|---|
| `headless` | `headless.run` | 목록의 다음 항목 |
| `subagent` | `subagent.spawn` | 목록의 다음 항목 |
| `session` | `session.spawn` (데몬이 정책으로 판정) | 목록의 다음 항목 |
| `inline` | 없음 | — |

**반환값 수집.**

| 조건 | 경로 |
|---|---|
| headless + `headless.output.schema` | 스키마를 하네스에 넘긴다(codex는 파일 경로, claude는 JSON 문자열). 출력 JSON을 그대로 반환값으로 쓴다 |
| headless, schema 없음, `headless.output.json` | 프롬프트에 반환 형식을 적고, JSON 출력의 최종 텍스트에서 마지막 ```json 블록을 추출해 스키마로 검증한다 |
| headless, 둘 다 없음 | 텍스트 출력에서 마지막 ```json 블록을 추출해 검증한다 |
| 추출·검증 실패 | 오류를 실어 같은 실행 주체로 1회 재실행. 다시 실패하면 `delegate_failed`, 노드 결과는 `fail` |
| subagent + `mcp.claunch` | 하위 에이전트가 `flow_delegate`로 직접 제출. 엔진이 스키마 검증 |
| subagent, `mcp.claunch` 없음 | 하위 에이전트가 영수증 파일(`receipts/<id>.json`)을 쓰고, 드라이버가 `flow_delegate`로 그 경로를 제출. 엔진이 파일을 읽어 검증 |
| session | 자식 세션이 `flow_delegate`로 제출(자식이 claunch 세션이므로 MCP가 있다). 없으면 영수증 파일 |

**효과 제한(`effects`가 쓰기를 포함하지 않는 위임, 즉 읽기 전용).**

| 실행 주체 | 1순위 | 2순위 | 3순위 |
|---|---|---|---|
| headless | `headless.sandbox.readonly` | `headless.tools.restrict`로 쓰기 도구 금지 | 위임 시점 tip에 detach한 일회용 worktree에서 실행하고, 끝난 뒤 미커밋 변경을 버린다(쓰기는 일어나도 반영되지 않음) |
| subagent | `subagent.tools.restrict` | `subagent.worktree` | 브리프에 읽기 전용 규칙을 적고, 끝난 뒤 `git status --porcelain`이 위임 전과 같은지 검사. 다르면 `monitor_violation` |
| session | 자식 세션의 worktree | — | — |

**쓰기 효과가 있는 병렬 위임.**

| 조건 | 경로 |
|---|---|
| 분기들의 `paths`가 서로 겹치지 않음(정적 검사) | 그대로 병렬 |
| 겹치거나 `paths` 미선언, 실행 주체가 worktree 격리 가능(headless: 일회용 worktree, subagent: `subagent.worktree`, session: 항상) | 분기마다 worktree에서 실행하고 결과 브랜치를 순서대로 머지 |
| 격리 불가 | 직렬 실행(`settle` 의미는 유지) |

**병렬 실행 수.**

| 조건 | 경로 |
|---|---|
| headless | 데몬이 프로세스를 동시에 띄운다. 상한은 `daemon.cflow2_headless_max`(기본 3) |
| subagent + `subagent.parallel` | 드라이버에게 한 번에 여러 개를 띄우라고 지시 |
| subagent, 병렬 없음 | 직렬. `race`는 선언 순서대로 실행해 첫 성공에서 멈춘다 |

**예산.**

| 조건 | 경로 |
|---|---|
| `budget.seconds` | 모든 실행 주체에서 데몬이 벽시계 시간으로 강제한다(headless는 프로세스 종료, subagent·session은 드라이버에게 중단 통지 후 `timeout` 결과) |
| `budget.usd` + `headless.budget.usd` | 하네스 인자로 전달 |
| `budget.usd`, 기능 없음 | 강제하지 않고 `fallback` 이벤트에 「비용 상한 미강제」를 남긴다. 노드에 `strict_budget: true`가 있으면 그 실행 주체를 건너뛴다 |

**재시도 이어하기.**

| 조건 | 경로 |
|---|---|
| `retry.carry: resume` + `headless.resume` | 이전 실행을 `--resume`으로 이어서 실패 사유를 전달 |
| 그 밖 | 새 실행에 이전 결과의 요약(`carry`)만 실어 보낸다 |

## 7. 위임 실행

- 위임 지점은 `executor`가 붙은 노드다. 그 하위 트리 전체가 실행 주체 하나에서
  진행된다.
- 브리프: 노드의 렌더된 프롬프트 + 명시적으로 넘긴 값(`with`) + 반환 형식 +
  적용 규칙. 메인 런의 대화 맥락은 넘기지 않는다.
- 결과: `returns` 스키마의 값 + 영수증 경로. 메인 드라이버에게는 이 둘만 보인다.
- 엔진은 `spec.ensures`를 영수증에 대해 평가한다(8절).
- `headless`: 데몬이 하네스의 비대화형 명령을 프로필 환경으로 실행한다. 표준
  출력은 `receipts/<id>.out`에 저장된다.
- `subagent`: 드라이버가 `flow_status`에서 브리프를 받아 하네스의 하위 에이전트
  기능으로 실행하고 결과를 `flow_delegate`로 제출한다.
- `session`: 데몬이 자식 세션을 spawn하고 브리프를 오프닝 태스크로 준다.

## 8. 명세(spec)

```yaml
spec:
  invariants:
    no-master-merge: {never_effect: git.merge, target: master}
  paths:
    settle-before-end: {to: end, must_pass: [settle]}
  ensures:
    land.landed: "$result.land.value.merge != null"
  monitors:
    master-unmoved:
      always: "$probe.master_tip == $data.master_at_start"
      probe: {master_tip: "git rev-parse master"}
      every: 300
      on_violation: halt
    landing-answered:
      when: {left: integration-request}
      within: 15000
      expect: {entered: [landed, wrapup]}
```

| 종류 | 검사 시점 | 방법 |
|---|---|---|
| `invariants.never_effect` | 컴파일 | 어느 작업의 `effects`에도 그 효과가 없어야 한다 |
| `paths.must_pass` | 컴파일 | IR의 제어 흐름 그래프에서 시작부터 `to`까지의 모든 경로가 지정 노드를 지나야 한다 |
| 모든 반복의 상한 | 컴파일 | `loop`·`retry`에 `max`가 있어야 한다 |
| `ensures` | 실행(노드 결과 확정 시) | 식이 거짓이면 결과를 `fail`로 바꾸고 `monitor_violation` |
| `monitors.always` | 실행(데몬 주기) | probe 값으로 식을 평가. 위반 시 `on_violation`(`halt`, `notify`, `record`) |
| `monitors.within` | 실행(데몬 주기) | `when` 이벤트 뒤 `within`초 안에 `expect` 이벤트가 없으면 위반 |

## 9. 화면과 CLI

- CLI: `claunch cflow2 ls | show | validate | compile | caps | start | status | journal | abort`,
  사람의 경로 `select | extend | resume | approve [NODE] [--decline | --option O] [--reason R]`.
- ask 응답 API: `POST /api/cflow2/approve`(본문 `{cwd, scope, node?, decision?, reason?}`,
  처리 함수 `daemon/cflow2_approve.approve`, 이벤트 루프 밖에서 호출).
- API: `GET /api/cflow/workflows`가 v1·v2 항목을 함께 싣고 `version`(1 또는 2)과
  `deprecated`(v1이면 true)를 붙인다. v2 런 상세는 `GET /api/cflow2/run?cwd&scope`.
  - 같은 이름이 두 형식에 다 있으면 v1이 시작된다. v2를 시작하려면 요청에
    `workflow_version: 2`를 싣거나 이름을 `v2:<name>`으로 쓴다. v2에만 있는
    이름과 `.cflow2/generated/*.yaml` 경로(또는 `cflow: 2`로 시작하는
    `.cflow/generated/` 파일)는 그대로 v2다. 판정은 `daemon/cflow2_web.choose`.
  - v2 런의 params는 요청의 `workflow_params`로 넘긴다. 차트가 `context`
    param을 선언하면 `context`가 그 값이 된다.
  - 그 밖의 API: `GET /api/cflow2/runs`(v2 런 목록), `GET /api/cflow2/definition?name`
    (컴파일 트리와 검사 결과, 읽기 전용), `POST /api/cflow2/select`(사용자 선택,
    `caller=user`), `POST /api/cflow2/extend`(`loop_limit` 응답). v2 런만 있는
    슬롯에서 `GET /api/cflow/run`은 `v2: true`를 싣는다.
  - v2 부모 세션의 자식은 부모 런의 `chart.json`의 `meta.default_child_cflow`를
    물려받는다.
- 새 세션 폼과 spawn 모달(같은 폼, `index.html`의 `#new-session`): 워크플로 선택
  목록에 버전 표시를 붙이고, v1 항목에는 deprecated 표시를 붙인다. v2를 고르면
  데몬이 v2 런으로 시작한다.
- 실행 패널: v2 런이면 IR 트리를 들여쓰기 목록으로 그리고, 활성 구성의 노드와
  위임 실행 단위(실행 주체, 상태, 영수증)를 표시한다.

## 10. v1 deprecation

- `cflow ls`·`cflow show`, 정의 편집기, 목록 API에 v1 정의가 deprecated라는 표시를
  붙인다. 실행은 계속 지원한다.
- v1 정의 파일 자체의 `Workflow.deprecations`에는 넣지 않는다(번들 파일의
  deprecation을 금지하는 `tests/test_cflow_layers.py`의 검사는 v1 문법 내부의
  옛 표기를 잡는 용도이므로 그대로 둔다). 형식 자체의 deprecation은 별도 필드
  `format_deprecated`로 싣는다.

## 11. 단계

| 단계 | 내용 |
|---|---|
| 1 | IR, 표면 문법 컴파일러, 식 해석기, 정적 명세 검사, capability 합성과 fallback 계획 |
| 2 | 런타임(상태, advance, 저널), MCP 툴, CLI |
| 3 | 실행 주체: inline, subagent, headless, session |
| 4 | 데몬 연동(대기 probe, timer, 실행 시간 모니터, headless 프로세스), API, 웹 화면 |
| 5 | `improv-worker-v2` 정의, v1 deprecated 표시 |
