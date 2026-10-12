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
| 실행 주체 | `inline`, `subagent`, `session` 세 가지. 첫 단계에서 지원하던 `headless`는 claunch-xjbrh에서 제거했다(6.4) | 사용자 결정 |
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
  `.cflow/generated/`와 같은 규칙). 데몬은 그 경로가 요청 세션(또는 부모 세션)의
  `.cflow2/generated` 디렉터리 안에 있는지 확인한 뒤에만 읽는다
  (`daemon/cflow2_web.choose`).
- 정의는 이름으로만 찾는다. `include`, `call`, `group.run`, `GET /api/cflow2/definition`,
  이름으로 시작하는 런은 정의 이름(레이어 파일의 stem)만 받고, 절대 경로·상대
  경로·경로 구분자가 든 이름·`.yaml` 접미사는 거절한다(`layers.find`, 프런트엔드
  `_definition_name`). 경로로 읽는 유일한 경우는 위의 생성 파일을 시작하는 것이며,
  생성 파일 안의 `include`도 레이어에서 이름으로 찾는다. 포함하는 파일의 디렉터리
  (형제 파일)는 레이어가 아니므로 탐색하지 않는다.
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
| `kind` | `workflow`(기본), `procedure`(다른 정의가 `call`로 부르는 정의), `library`(`include`로만 쓰는 정의, 2.10) |
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
| `max_visits` | 한 노드가 런 전체에서 들어갈 수 있는 횟수의 상한. 정수 0 이상, 기본 200. IR의 `meta.max_visits`. 넘으면 그 인스턴스가 `waiting_approval`(reason `visit_limit`)로 멈추고 사람이 `extend --visits`(웹 UI의 「Allow one more visit」, `POST /api/cflow2/extend` 본문 `visits: true`)로 답한다. `0`은 상한 없음이다(세션 수명의 side 런처럼 회차 경계가 없는 정의). 검사 경고 `unbounded-visits`가 붙고, 반복의 `max`는 그대로 필요하다 |
| `inputs` | 시작 입력 선언(2.6). IR의 `meta.inputs` |
| `collections`, `collection_types` | 컬렉션 선언과 재사용할 선언(2.7). IR의 `meta.collections` |
| `question_types` | 질문 형식 선언(2.10). IR의 `meta.question_types` |
| `sides` | side 런 선언 `{<이름>: <정의 이름>}`(2.9). IR의 `meta.sides` |
| `recur` | `true` 또는 `{auto: true}`. 끝난 회차 뒤에 다음 회차를 요청한다(아래 「recur」). IR의 `meta.recur`(`{auto: bool}`) |

#### recur

상주 서비스 루프(v1 `recur`, 예: `improv-leader`)를 그래프 안의 반복 없이 쓰는
방법이다. 회차 하나는 끝까지 가고, 다음 회차는 런의 수명 규칙이 연다.

- 대상: `kind: workflow`만. `procedure`의 `recur`는 컴파일 오류다. 중첩 위임 런은
  요청을 남기지 않는다.
- 요청: 런이 결과 `ok`로 끝나면(acceptance 검사 통과 뒤) `state.json`에
  `next_round: {by: recur, auto, round, requested, not_before}`를 남기고 저널에
  `round_requested`를 쓴다. `failed`·`aborted`로 끝난 회차는 요청을 남기지
  않는다. 루프는 사람이 원인을 봐야 하는 자리에서 멈춘다. 차트의 `returns`가
  선언한 다른 결과로 끝난 회차(`done`)도 요청을 남기지 않는다. 정의가 루프를
  스스로 끝내는 방법이 이것이다(예: `improv-leader-v2`의 교대 종료는
  `return: wound_down`).
- 최소 간격: `recur: {auto: true, min_gap: S}`(초, 기본 60, 0 이상). 요청에
  `not_before`(회차가 끝난 시각 + `min_gap`)가 실리고, clock은 그 시각이 지나기
  전에는 다음 회차를 시작하지 않는다. 회차 본문이 곧바로 끝나는 정의(대기 없는
  본문)가 clock 틱마다 회차를 여는 것을 막는다. `recur: true`도 같은 값을 싣지만
  시작은 드라이버가 한다.
- 회차 번호: 슬롯의 상태 `round`. 새 런은 이전 런(같은 슬롯, 같은 정의 이름)에
  `next_round`가 있으면 그 번호 + 1, 없으면 1이다. `flow_status`가 `round`를 싣는다.
- `recur: true`: 드라이버가 다음 회차를 시작한다(`flow_start`). 끝난 런의
  `flow_status`가 `next_round`와 그 안내를 싣는다.
- `recur: {auto: true}`: 데몬의 cflow2 clock이 끝난 슬롯에서 `next_round.auto`를
  보면 `runtime.start_next_round`로 다음 회차를 시작하고(저널 `round_auto_started`),
  드라이버 세션에 「next round started」 블록을 보낸다. 정의는 이름(없으면 기록된
  생성 파일 경로)으로 다시 읽으므로 고친 정의는 회차 경계에서 적용된다. 읽지
  못하면 이전 회차의 `chart.json`을 쓴다. 이전 회차의 `params`, 시작 입력
  (`data.inputs`, 다시 읽은 정의일 때), 드라이버, capability를 그대로 넘긴다.
  컬렉션은 `lifecycle.inherit` 규칙대로 넘어간다(2.7). 요청은 한 번만 쓰인다:
  시작이 끝난 런을 보관하므로 다음 틱은 아무것도 시작하지 않는다. 그사이 다른
  시작이 슬롯을 차지했거나 요청이 취소됐으면 시작하지 않는다 — `start`가 슬롯
  잠금 안에서 그 런과 `next_round.auto`를 다시 확인한다(`next_round_of`). 정의를
  다시 읽는 사이에 들어온 `recur --cancel`이 이긴다.
- 멈춤: 사람이 `claunch cflow2 recur --cancel [--reason R]`로 끝난 런의 요청을
  지운다(`round_cancelled`). 드라이버 세션 안에서는 거절된다(사람의 명령, 2.2
  「사람의 명령」). 회차가 진행 중일 때는 그 회차가 끝난 뒤에 취소한다.

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
    executor: inline                  # 또는 선호 순서 목록 [session, subagent, inline]
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
  생략하면 런타임이 v1 기본값 900초를 쓴다.
- 값 참조: 아래 「명령 안의 값 참조」를 따른다. 명령 문자열과 argv 항목 안의
  `${...}`는 컴파일 오류이고, 엔진은 명령을 렌더하지 않는다. 값은 환경 변수로만
  전달된다(아래 「실행 위치와 환경」). 런타임은 다른 경로로 만들어진 차트(컴파일을
  거치지 않은 IR)에 대해 시작 시점에 한 번 더 검사한다. 그 검사에서 문자열 명령의
  `${params.NAME}`·`${data.NAME}`은 환경 변수 참조 `"${CFLOW_IN_NAME}"`·
  `"${CFLOW_DATA_NAME}"`로 바뀌고(큰따옴표 안에서는 따옴표 없이), 그 밖의 참조,
  작은따옴표 안의 참조, argv 항목 안의 참조는 시작 오류다. 이 런타임 검사는
  `${HOME}`처럼 런 값 이름이 아닌 참조를 셸의 전개로 보고 그대로 둔다(컴파일러는
  이 형태도 거절하므로 정의 파일에서는 `$HOME`으로 쓴다).
- 실행 시점: 작업이 `ok`로 보고될 때. 다른 결과(`fail` 등)는 verify를 거치지 않는다.
- 실행 위치와 환경: 런의 cwd. 프로세스 환경에 `CFLOW2_RUN`, `CFLOW2_NODE`,
  `CFLOW2_INSTANCE`, `CFLOW2_SCOPE`, param마다 `CFLOW_IN_<NAME>`(런의 params 위에
  작업이 속한 프로시저의 params를 덮은 값), 최상위 `data`마다
  `CFLOW_DATA_<NAME>`을 더한다. 스칼라는 텍스트로, 스칼라가 아닌 값(`bind`로
  묶인 결과 등)은 JSON 텍스트로 싣는다. JSON이 64KB를 넘는 값은 빠진다(wait·
  모니터의 probe와 같은 규칙, `runtime.verify_env`). 예: improv-leader-v2의
  sweep verify는 integrate가 묶은 `data.merged`를 `CFLOW_DATA_MERGED`로 읽어
  머지한 저장소를 판정한다(claunch-6xg).
- 비동기 실행: `flow_report`는 작업을 `verifying` 상태로 두고(저널
  `verify_started`) 바로 돌아온다. 응답에 `verifying`이 실린다. 명령은 MCP 서버
  프로세스의 백그라운드 스레드에서 런 잠금 밖으로 실행된다. 끝나면 잠금을 잡고, 같은
  인스턴스가 아직 같은 verify를 기다릴 때만 결과를 적용한다. 그사이 인스턴스가
  사라졌으면 아무것도 적용하지 않는다. `flow_status`의 `verifying`이 진행 중인
  verify를 보여 준다.
- 종료 코드 0: `verify_passed`와 `reported`를 남기고 보고를 적용한다. 0이 아니거나
  시간 초과(코드 124): `verify_failed`(출력의 마지막 40줄 포함)를 남기고 작업을 다시
  활성 상태로 둔다. `flow_status`의 그 작업에 `last_verify`(종료 코드, 사유, 출력 끝)가
  실린다.
- 프로세스 처리: 명령은 자기 프로세스 그룹에서 실행되고, 출력은 파이프 대신 임시
  파일로 받는다. 종료 후(시간 초과 포함) 그룹 전체를 종료한다. 명령이 남긴 자손
  프로세스가 출력을 붙잡아 결과 수집이 멈추는 일이 없다.
- 유실: verify를 실행하던 프로세스가 사라져 `timeout + 120`초가 지나도 결과가 없으면
  다음 시간 틱이 `verify_failed`(`lost`)를 남기고 작업을 다시 활성 상태로 둔다.
- halt 중에 통과한 verify는 결과를 보관했다가 halt가 해제될 때 적용한다.
- 위임 결과: 평탄한 위임(아래 7절)의 `ok` 결과는 하위 트리에서 `verify`를 선언한
  작업마다 그 명령을 순서대로 실행해 모두 통과해야 받아들인다(같은 비동기 방식,
  위임 레코드 상태 `verifying`). 실패는 잘못된 결과 1회로 센다(첫 번째는 재실행
  요청, 두 번째는 노드 `fail`). 중첩 위임 런의 작업은 그 런 안에서 보고되므로 보고
  시점에 각자 verify를 거친다.

#### restart

작업에 프로젝트 고유의 재기동 명령을 붙인다. 실행은 데몬이 하고 에이전트는 하지
않는다(AGENTS.md 「데몬 재기동·정지는 사용자의 명령이다」). v1 스텝의 `restart:`와
같은 형태다.

```yaml
nodes:
  reflect:
    prompt: …
    restart:
      windows: 'powershell -NoProfile -ExecutionPolicy Bypass -File tools/restart_live.ps1'
      linux: 'bash tools/restart_live.sh'
      timeout: 900          # 기본 120, 상한 900
```

- 표기: `windows`, `linux` 중 하나 이상(명령 문자열), `timeout`(초, 0 초과 900
  이하). 명령은 적힌 그대로 셸에서 실행하며 `${...}`는 컴파일 오류다(「명령 안의 값
  참조」).
- 허용 목록: 명령은 `tools/restart_live.sh` 또는 `tools/restart_live.ps1`을
  실행하는 형태만 받는다(`ir.RESTART_SCRIPTS`, `ir.restart_refusal`). 형태는
  `[<인터프리터> [플래그 [값]]...] <스크립트> [인자]`이고, 인터프리터는
  `bash`·`sh`(플래그 없음), `pwsh`·`powershell`(`-NoProfile`, `-NonInteractive`,
  `-NoLogo`, `-ExecutionPolicy <값>`, `-File`)이다. 셸 연산자(`; & | \` $ < > ( )`)가
  있으면 거절한다. 컴파일(그래서 `flow_start`)이 그 밖의 명령을 오류로 거절한다.
  데몬은 이 명령을 자기 권한으로 실행하므로, 에이전트가 쓴 정의(프로젝트 레이어,
  `.cflow2/generated/`)가 `claunch daemon stop` 같은 명령을 데몬에게 실행시키는 경로를
  막는다. 허용 목록 이전에 컴파일된 차트 스냅샷에 그 밖의 명령이 있으면 clock은
  실행하지 않고 기록을 `failed`(출력 `refused: …`, 저널 `restart_refused`)로 남긴다.
- 실행 시점: 작업 인스턴스가 활성(`await: task`, ask가 있으면 승인 뒤)이 되면 데몬의
  cflow2 clock이 다음 틱에 한 번 가져간다(`runtime.claim_restarts`). 인스턴스
  하나가 방문 하나이므로 반복으로 다시 들어온 작업은 다시 실행한다. halt 중인 런과
  중첩 위임 런의 작업은 가져가지 않는다.
- 플랫폼: 호스트의 `platform.system()`이 `Windows`(win*)면 `windows`,
  `Linux`면 `linux`. 해당 명령이 없으면 기록 `unsupported`(`restart_unsupported`).
- 실행: clock은 기록을 `running`(데몬 boot id, token 포함)으로 저장하고
  (`restart_requested`) 패스가 끝난 뒤 별도 작업에서 v1
  `RestartClock._execute`로 명령을 실행한다: 런의 cwd, 출력은 임시 파일,
  `timeout` 초과 시 종료 코드 없음. 환경의 `CLAUNCH_SESSION`은 런의 드라이버
  세션이다. 그래서 명령 안의 `claunch daemon restart`는 웹 UI의 승인 게이트로
  올라간다(`daemon/restart_gate.py`).
- 결과: `runtime.complete_restart`가 종료 코드 0이면 `succeeded`, 아니면
  `failed`로 기록하고 출력의 끝 4000자를 남긴다(`restart_completed`). 그사이
  인스턴스가 사라졌거나 token이 다르면 아무것도 쓰지 않는다. 실행기 자체가
  예외를 내면(임시 파일·프로세스 생성 실패 등) 종료 코드 없음과 예외 문구로
  `failed`를 기록한다 — 기록이 `running`에 남아 보고가 데몬 재기동까지 막히지 않게
  한다.
- 중단: 다른 boot가 남긴 `running` 기록은 `interrupted`가 되고 다시 실행하지
  않는다(`restart_interrupted`). 명령이 데몬 자신을 재기동했을 수 있기 때문이다.
- 통지: 가져갈 때와 결과가 나올 때 드라이버 세션에 블록을 보낸다.
- 보고: 기록이 끝난 상태(`succeeded`, `failed`, `interrupted`, `unsupported`)가
  되기 전에는 그 작업의 `flow_report`를 거절한다. 데몬 clock이 꺼져 있으면 기록이
  `pending`에 머물고, 이 경우 런은 사람이 `abort`한다.
- `flow_status`의 작업 항목에 `restart`(`status`, `requested_at`, `completed_at`,
  `exit_code`, `output`, `platform`)가 실린다.
- 위임 실행 단위 안의 작업에 `restart`가 있으면 정적 검사 오류
  `delegated-restart`다.

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
- 미리 보기(`claunch cflow2 responders <작업> [--role R] [--json]`, claunch-3va8h):
  질문을 열기 전에 그 작업의 `from` 그룹이 지금 누구에게 닿는지 그룹마다 한 줄로
  보여 준다(`asks.who_answers`). 명단을 읽기만 하고 연결은 만들지 않는다.
  `connect: true` 그룹은 질문이 열릴 때 연결될 응답 가능 멤버를 싣는다. `--role`은
  그 역할을 지명한 그룹만 남긴다. 종료 코드는 0(어느 그룹이 응답 가능한 멤버에
  닿음), 1(아무 그룹도 닿지 않음), 2(판정 불가: 이 디렉터리에 v2 런이 없음, 그런
  노드가 없음, 노드에 `from`이 있는 `ask`가 없음, 명단 무응답)다. v1의 `claunch
  cflow responders`와 같은 판정이며, improv-worker-v2의 reviewer-ensure가 이 명령을
  부른다.
- 런의 신원: 런은 시작 시점에 드라이버 세션을 `state.json`의 `driver`로 기록한다
  (시작한 셸의 `CLAUNCH_SESSION`, 없으면 scope가 가리키는 세션). 명단 조회의 기준
  세션, 자기 응답 거절, 질문 발신자, 통지 대상은 모두 이 값을 쓴다. scope는 신원으로
  쓰지 않는다(다른 이름의 slot에 런을 시작해도 기준 세션은 바뀌지 않는다).
- 응답 권한: `flow_answer`의 세션 신원은 MCP 서버 환경의 `CLAUNCH_SESSION`에서
  읽는다. 라우팅 시점에 기록된 그룹의 세션만 답할 수 있고, 드라이버 세션은 답할 수
  없다(드라이버의 하위 세션은 명단에서 이미 빠진다).
- 답의 이름: 옵션 이름은 대소문자를 구별하지 않고 비교하며, 기록과 결과에는 정의에
  적힌 철자를 쓴다(`LGTM`에 `lgtm`으로 답하면 결과 `LGTM`).
- 기권: `abstain`은 그 응답자만 질문에서 빠지게 한다. 같은 그룹의 다른 응답자는 계속
  답할 수 있다. 그룹의 모든 응답자가 기권하거나 그룹의 `deadline`이 지나면 다음
  그룹으로 간다. 이미 기권한 응답자의 두 번째 답은 거절된다.
- 질문 철회 통지: 질문을 받고 아직 답하지 않은 응답자에게는 질문이 닫힐 때 `fyi`
  메시지가 간다. 닫히는 경우는 사람의 답, 다른 응답자의 답, 기한 초과로 다음
  그룹으로 이동, 작업 인스턴스 취소(par 결정, abort 등), 그룹 전원 기권이다. 저널에
  `ask_withdrawn`이 남는다.
- 기한: 데몬 클럭이 매 틱 `asks.tick`을 호출한다. `asked` 그룹이 `deadline`을
  넘기면 `ask_timeout`을 남기고 다음 그룹으로 간다. 그룹이 남지 않으면 `otherwise`로
  간다. `human`·`self`로 넘어간 ask는 드라이버 세션에 한 번 통지된다. 런을 바꾸는
  모든 호출은 적용 전에 시간 틱을 먼저 돌리므로, 기한이 지난 응답은 다음 클럭 틱
  전이라도 거절된다.
- 사람의 명령: `claunch cflow2 approve|select|extend|resume|abort`와 살아 있는 런
  위의 `start --force`는 사람의 경로다. CLI는 셸의 `CLAUNCH_SESSION`이 그 런의
  드라이버(또는 중첩 위임 런의 담당 세션)이면 실행을 거절한다. 사람은 그 세션 밖의
  터미널이나 웹 UI에서 답한다. 이 검사가 실제로 적용되는 검사다. 하네스 거부 규칙
  (`install.GATE_DENY_RULES`)은 같은 명령을 다른 철자로 쓰는 경우(`uv run claunch`,
  절대 경로, `env X=… claunch`, `python -m claude_launcher`, `start … --force`)까지
  에이전트의 셸 호출을 막는 보조 장치다. 두 장치 모두 런타임을 직접 import하는 코드
  (`python -c`)는 막지 못한다. 보안 경계로 설계된 것은 없다. `claunch cflow`와
  `claunch cflow2`의 하위 명령은 옵션 약어를 받지 않는다(`allow_abbrev=False`).
  거부 규칙은 `--force` 같은 전체 철자에 맞춰져 있어, 약어(`--forc`)가 같은 옵션으로
  해석되면 규칙에 걸리지 않기 때문이다.
- 드라이버가 MCP로 ask에 답할 수 있는 경로는 `self` 해소 단계의 `flow_approve`
  하나다. 위의 셸 경로는 CLI 검사와 거부 규칙이 막지만, 런타임 직접 호출은 막지
  못한다.
- 질문과 선택지를 앞 작업의 결과에서 만드는 형태(`brief`, `format`, `choices`)와
  답 `reframe`은 2.10에 있다.
- 위임 실행 단위 아래의 작업에 `ask`가 있거나 `chooser: user` 선택이 있으면 정적
  검사 오류다. 런타임은 그런 위임 지점을 위임하지 않고 그 자리에서(inline) 실행하며
  `fallback`을 남긴다.

#### 프로시저 params 기본값

`call`이 펼친 `scope` 노드는 프로시저의 `params` 선언을 IR에 싣는다. 런타임은
scope에 들어갈 때 `with`에 없는 param에 `default`를 채우고, 기본값도 없으면
`null`을 넣는다. 호출 지점의 필수 param 누락은 컴파일 오류다. `group` 멤버는
`flow_fork`의 `with`가 param이 되며, 선언에 없는 키나 기본값 없는 필수 param이
빠진 fork는 거절된다.

#### `with`·`value`의 값 표기

`with`와 `return`의 `value` 항목은 `$`를 담은 문자열이면 식, 그 밖은 리터럴이다.
`$`를 담은 리터럴 문자열과 `$` 없는 식은 명시 표기로 쓴다: `{lit: V}`(V를 그대로),
`{expr: E}`(E를 식으로 평가). 키가 `lit` 또는 `expr` 하나뿐인 매핑만 명시 표기로
읽는다. 기존 `$` 감지가 기본으로 남는다. 그래서 그 모양의 dict 리터럴 자체(`{lit: 3}`라는
값)는 `{lit: {lit: 3}}`로 쓴다. `{expr: 5}`처럼 문자열이 아닌 값은 그 값의 식(`5`)이 된다.

```yaml
with:
  price: {lit: "cost $5"}        # 리터럴
  label: {expr: "'fixed'"}       # 식
  who: "$params.me"              # 식($ 감지)
```

#### 반환 값의 선택 필드

`returns`가 선택 필드(`str?` 등)로 선언한 필드를 `return`의 `value`에서 생략하면
그 필드는 `null`로 채워진다. 프로시저의 `return`(그 호출의 `returns`)과 런의
`return`(차트의 `returns`) 모두 같다. 레코드마다 `field: null`을 나열하지 않아도
된다(claunch-yq80p.11). 채우는 것은 명시적 `return`뿐이다: 작업의 보고와 `return`
없이 본문이 끝난 프로시저의 값은 받은 그대로다. 식에서 없는 필드는 어차피 `null`로
읽히므로 `== null` 비교는 두 경우가 같고, 다른 것은 값 전체(프롬프트에 보이는 dict)다.

#### 블록 인자와 splice (claunch-yq80p.11)

프로시저 param에 `type: block`을 선언하면 호출 지점이 그 param에 트리(작업 이름,
목록, 조합자 매핑)를 넘긴다. 프로시저 본문은 그 트리를 `{splice: <param>}`로 놓는다.
`call`과 같이 컴파일 시점에 펼친다.

```yaml
procedures:
  guarded:
    params:
      attempt: {type: block, required: true}
      note: {type: str, default: ""}
    nodes:
      recover: {prompt: …, returns: {again: {}, stop: {}}}
    body:
      id: tries
      loop:
        max: 3
        while: "true"
        do:
          try: [{splice: attempt}, {return: ok}]
          catch:
            fail:
              seq: [recover]
              on: {again: {continue: tries}, stop: {return: gave_up}}
body:
  call: guarded
  with:
    attempt: work          # 호출자의 작업
    note: n1
```

- 블록은 **호출 지점의 범위**에서 컴파일한다(hygiene). 규칙:
  - 작업 이름: 호출 지점 파일의 `nodes`에서 찾는다. 프로시저의 작업은 보이지 않는다.
  - `continue`·`break`: 호출 지점을 감싸는 반복을 가리킨다. 프로시저의 반복은
    보이지 않는다(라벨 없는 `continue`도 호출 지점의 가장 안쪽 반복이다).
  - `return`: 블록이 쓰인 자리의 호출(호출 지점이 프로시저면 그 프로시저, 워크플로
    본문이면 런)을 끝낸다. 블록을 받은 프로시저를 끝내지 않는다. IR의 `control`
    노드가 `target`으로 그 scope id(런이면 `$run`)를 갖는다.
  - `$params`, foreach `item`, waterfall `$prev`: 호출 지점의 환경이다. 런타임이
    `call`의 scope에 들어갈 때 호출 지점의 환경을 저장하고, splice 노드가 그 환경으로
    들어간다.
  - `$result.<작업>`, `$data`: 런 전체의 이름 공간이므로 그대로다.
  - `raise`: 결과처럼 splice 위치에서 위로 올라가므로, 컴파일 검사도 splice 위치를
    감싸는 `try`(프로시저의 것과 호출 지점의 것)로 한다.
  - `par` 분기: 분기는 자기 영역이다(직접 쓴 `continue`가 분기 밖 반복에 닿지 않는
    규칙). 블록이 프로시저의 `par` 분기 안에 놓이면 블록의 `continue`·`break`도 호출
    지점의 반복에 닿지 않고 컴파일 오류다.
  - 오류 위치: 블록 안의 오류는 호출 지점의 파일:줄로 보고된다. 블록 노드의 `src`도
    호출 지점이다.
- 결과(outcome)는 lexical하지 않다: 블록 노드의 결과는 splice 위치에서 프로시저의
  구조를 따라 올라간다. 그래서 프로시저의 `try`·`on`이 블록의 `fail`을 잡는다 —
  골격 프로시저(시도 → 실패 처리)가 이것으로 만들어진다.
- 노드 id는 호출의 접두(`<호출 id>/`)를 갖는다. 같은 작업이 여러 호출에 쓰여도 id가
  겹치지 않는다(결과는 라벨로 모인다).
- 표시 라벨: 상태(`flow_status`의 `tasks`·`choices`·`asks` 등), 위임 brief,
  질문 기록이 싣는 `label`은 노드의 라벨이 차트에서 하나뿐이면 그대로이고, 둘 이상의
  노드가 같은 라벨을 가지면(같은 프로시저를 여러 곳에서 호출) 노드 id의 경로 구간마다
  그 구간이 가리키는 작성자 id를 앞에 붙인다(`settle/end-decision/ask-pose`).
  생성된 접미사(`issue-decision.call`)는 그것이 붙은 작성자 id(`issue-decision`)로
  읽고, 작성자 노드가 없는 생성 경로(`body.0`)는 붙지 않는다. 노드를 참조로 받는
  호출(`flow_report`·`flow_select`·질문 승인)은 노드 id와 라벨로 찾지 못한 참조를 표시
  라벨로 한 번 더 찾는다. 결과와 방문 수는 여전히 노드 자신의
  라벨 아래에 모인다(claunch-tm5ki).
- `default`: 블록 param의 기본 트리는 프로시저 자신의 텍스트이므로 splice 위치에서
  프로시저의 범위로 컴파일하고 실행한다 — 프로시저의 작업, 그 자리를 감싸는
  프로시저의 반복, 프로시저의 `$params`. splice 노드는 호출 기록(`call`)을 갖지 않는다.
  기본값이 없는 블록 param을 넘기지 않으면 컴파일 오류다.
  `group`의 멤버 프로시저는 호출 지점이 없으므로 기본값 없는 블록 param을 가질 수
  없다.
- 블록을 다른 호출에 넘길 수 있다: `with: {b: {splice: b}}`. 그 블록은 처음 쓰인
  호출 지점의 범위를 유지한다.
- 재귀 금지는 그대로다: 블록 안의 `call`도 지금 펼치는 프로시저 호출 스택으로
  검사한다(블록이 자신을 받은 프로시저를 다시 부를 수 없다). 펼침이 유한한 경우
  (`call p, with: {b: {call: p, with: {b: w}}}`)도 거절한다 — 보수적 규칙이다.
- 워크플로의 `params`에는 `type: block`을 쓸 수 없다(런은 호출되지 않는다).
- 정적 검사는 펼친 IR을 본다. `return`의 `target`과 splice 노드의 호출 기록을 따라
  호출 지점의 scope를 찾는다.
- 하지 않는 것: 컴파일 시점 계산(조건부 펼침, 반복 생성). 정의를 읽는 사람이 결과
  IR을 계산해야 하므로 넣지 않는다.

### 2.3 조합자

조합자 트리의 항목은 셋 중 하나다: 작업 이름(문자열), 조합자 매핑, 제어 매핑.

| 조합자 | 형태 | 의미 |
|---|---|---|
| `seq` | `seq: [a, b, c]` | 순서대로 실행 |
| `waterfall` | `waterfall: [a, b]` | `seq`와 같고, 각 항목의 결과 값을 다음 항목의 `$prev`로 넘긴다 |
| `cond` | `cond: [{if: EXPR, then: T}, …, {else: T}]` | 식으로 기계가 고르는 분기 |
| `choose` | `choose: {prompt, chooser, options: {name: {description, then: T}}}` | 에이전트나 사용자가 고르는 분기. 옵션에 `when: EXPR`을 붙이면 거짓일 때 고를 수 없다. `interval: SECONDS`를 붙이면 에이전트의 선택이 그 간격으로 제한된다(아래 「choose interval」) |
| `foreach` | `foreach: {in: EXPR, as: item, do: T, par: false}` | 목록의 항목마다 실행 |
| `loop` | `loop: {while: EXPR \| until: EXPR, max: N, do: T}` | 조건 루프. `max` 필수 |
| `par` | `par: {branches: {a: T, b: T}, settle: all}` | 병렬 실행. `settle`은 `all`, `allSettled`, `race`, `any`, `quorum:K` |
| `retry` | `retry: {do: T, max: N, every: SECONDS, until: EXPR, between: T}` | 재시도. `between`은 시도 사이에 실행하는 트리 |
| `timeout` | `timeout: {do: T, seconds: N, else: T}` | 기한 초과 시 `else`로 감 |
| `try` | `try: T, catch: {OUTCOME: T}, finally: T` | 결과 종류로 예외 경로를 처리하고, 마지막에 정리 트리를 실행 |
| `call` | `call: PROC, with: {…}, bind: VAR` | 프로시저 호출. 컴파일 시 펼친다 |
| `wait` | `wait: {probe: CMD, poll: S, seconds: N, undecided_max: K, describe: …}`, `wait: {checks: [{id, describe, probe}], …}` 또는 `wait: {seconds: N}` | 외부 신호 대기(아래 「wait」) |
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
사람이 런의 위치를 직접 바꾸는 경로는 2.11의 강제 이동 하나이고, 정의가
`force_target: true`로 표시한 노드로만 갈 수 있다.

#### wait

- `probe`가 없으면 `seconds`초 뒤에 결과 `ok`로 끝나는 타이머다.
- `probe`가 있으면 데몬 클럭이 `poll`초(기본 60)마다 셸 명령을 실행하고 종료 코드로
  판정한다.

  | 종료 코드 | 판정 | 다음 |
  |---|---|---|
  | 0 | 조건이 성립함 | 결과 `ok`로 끝남 |
  | 1 | 아직 아님 | 다음 poll까지 대기. 연속 판정 불가 횟수를 0으로 되돌림 |
  | 그 밖(2, 124, 127, 128 등) | 판정 불가 | 연속 판정 불가 횟수를 1 늘림(저널 `probe_undecided`). `undecided_max`(기본 3)에 닿으면 결과 `undecidable`로 끝남 |

- `probe`와 `seconds`를 함께 쓰면 `seconds`는 상한이다. 그 안에 0이 나오지 않으면
  결과 `timeout`으로 끝난다.
- `undecidable`과 `timeout`은 비-ok 결과이므로 처리하는 곳(`on`, `catch`)이 없으면
  런이 `failed`로 끝난다. 정의는 둘을 명시적으로 처리한다 — 예: `improv-worker-v2`의
  `cut-wait`·`landed-wait`는 둘 다 `chooser: user` 선택(`cut-unreachable`,
  `landed-unclear`)으로 보내 사람이 확인하거나 `held`로 마감하게 한다.
- `undecided_max`는 `probe`가 있을 때만 쓸 수 있다(정수 1 이상).
- `probe`는 명령 문자열이나 argv 목록이다. 아래 「명령 안의 값 참조」를 따른다.

#### wait checks

`checks`는 probe 여러 개를 한 대기에서 함께 판정한다. v1 스텝의 `checklist:`를
대신한다.

```yaml
- wait:
    poll: 30
    seconds: 600
    checks:
      - {id: deployed, describe: 라이브 데몬이 이번 머지 이후에 재기동됐다,
         probe: 'uv run --no-sync python tools/deploy_check.py --branch master'}
      - {id: clean, probe: 'git diff --quiet'}
  on: {timeout: reflect-retry, undecidable: reflect-retry}
```

- 항목: `id`(이름, 대기 안에서 유일), `describe`(선택), `probe`(명령 문자열이나
  argv). `probe`·`until`과 함께 쓸 수 없다. `poll`, `seconds`, `undecided_max`는
  `probe`와 같은 의미다.
- 판정: poll마다 모든 항목을 실행한다. 모든 항목이 0이면 결과 `ok`(값
  `{checks: [...]}`, 저널 `checks_passed`). 판정 불가(0, 1이 아닌 코드)인 항목이
  하나라도 있으면 그 poll은 판정 불가로 세고, 그 밖에는 「아직」이다.
- `seconds`가 지나면 결과 `timeout`(값에 마지막 항목 상태, 저널 `checks_expired`).
- 항목마다 마지막 판정(`verdict`, `exit`, 출력 끝, `measured_at`)이
  `flow_status`의 대기 항목 `checks`에 실린다.

v1 `checklist:`의 대응:

| v1 checklist | v2 |
|---|---|
| `items[].check`(종료 코드 0이 참, 그 밖은 거짓, 실행 실패·시간 초과는 unknown) | `checks[].probe`. 1은 「아직」, 그 밖은 판정 불가로 센다 |
| `then`(모두 참이면 가는 곳) | 대기 뒤의 다음 항목(`seq`) |
| `otherwise: {after, then}` | `seconds`와 `on: {timeout: …}`(또는 감싸는 `try`의 `catch`) |
| report가 제출돼야 열림 | 대기 앞에 작업을 둔다(`seq: [작업, wait]`). 작업의 report가 끝나야 대기가 시작된다 |
| `next:` 거절(에이전트 이탈 없음) | 대기는 에이전트가 보고할 수 없는 노드다. 사람의 우회는 `abort` 뒤 재시작 |
| `checklist_passed` / `checklist_expired` 저널 | `checks_passed` / `checks_expired` |
| 같은 방문 안에서 재기동은 한 번 | 작업의 `restart`(2.2)는 인스턴스마다 한 번. 다시 시도하려면 반복으로 작업에 다시 들어간다 |
| `by:`로 사람이 표시하는 항목(`editable`) | 대응 없음. 사람이 정할 조건은 `chooser: user` 선택이나 `ask`로 쓴다 |

#### choose interval

옵션의 `interval: SECONDS`는 에이전트가 그 옵션을 고를 수 있는 간격이다(v1
`waiting_window`). 「모인 것을 N분마다 통합한다」를 에이전트 안의 타이머 없이 쓴다.

- `chooser: agent`인 선택에서만 쓴다(그 밖은 컴파일 오류). 값은 0보다 큰 수.
- 기록: 옵션을 실제로 택한 시각을 `state.json`의 `windows`
  (`<노드 id>:<옵션>` → 시각)에 남긴다. 슬롯의 기록이라 같은 정의의 다음 런(다음
  회차 포함)으로 넘어간다. 첫 선택은 비교할 기록이 없으므로 바로 지나간다.
- 보류: 에이전트가 마지막 선택 뒤 `interval`이 지나기 전에 그 옵션을 고르면 거절하지
  않고 보류한다. 선택 인스턴스의 `held`(`option`, `reason`, `opens_at`)를 남기고
  `select_held`를 쓴다. 응답과 `flow_status`의 `waiting_window`가 창이 열리는
  시각을 싣는다. 런의 `status`는 그대로다(사람을 기다리지 않는다).
- 보류 중: 같은 옵션을 다시 고르면 `reason`만 바꾼다(`select_held`, `updated`).
  다른 옵션을 고르면 보류를 취소하고(`hold_cancelled`) 그 옵션을 처리한다.
- 확정: `opens_at`이 지나면 시간 틱(데몬 clock, 또는 런을 바꾸는 다음 호출)이 마지막
  `reason`으로 그 옵션을 택한다(`selected`의 `by: window`). 그때 옵션의 `when`이
  거짓이면 택하지 않고 보류만 지운다(`hold_cancelled`, 사유). 데몬 clock이 옮겼으면
  드라이버를 깨운다(6.5 「wake-up」).
- 사람의 선택(`claunch cflow2 select`, 웹)은 간격을 따르지 않는다. 창 안에서 택하면
  `selected`에 `override: true`가 붙는다. 사람의 선택도 기록을 갱신한다.
- 고를 수 있는 옵션 목록의 항목에 `interval`과, 창이 닫혀 있으면
  `window_opens_at`이 실린다.

#### 명령 안의 값 참조

엔진이 실행하는 명령(작업의 `verify`, wait의 `probe`, 모니터의 `probe`)은
템플릿이 아니다. 이 문자열 안의 `${...}`는 컴파일 오류다. 명령이 런의 값을 읽어야
하면 환경 변수로 읽는다: param은 `CFLOW_IN_<NAME>`, 최상위 `data`의 스칼라 값은
`CFLOW_DATA_<NAME>`(이름은 대문자, 영숫자 외 문자는 `_`). 작업의 `verify`와 wait·
모니터의 `probe`는 스칼라가 아닌 최상위 `data`도 JSON 텍스트로 받는다(64KB 이하,
`runtime.verify_env`·`runtime._value_env`). verify가 스칼라만 받던 때는 `bind`로 묶인
결과를 verify가 읽을 길이 없었다(claunch-6xg).
wait·모니터의 `probe`는 데몬의 시계가 실행하므로, 엔진이 `CLAUNCH_SESSION`을 런의
driver로 덮어 싣는다(driver가 없으면 scope, 관리되지 않는 런이면 빈 값). 데몬
프로세스의 `CLAUNCH_SESSION`은 데몬을 띄운 터미널의 세션 하나이고, 그 이름을
물려받은 `landed_check`는 남의 체크아웃이나 master 위의 런 cwd를 재서 착지한 tip에
판정 불가(exit 2)를 냈다(claunch-4zc51.1.4, v1은 claunch-04ru). 판정 불가가 되면
저널 `probe_undecided`에 프로브 출력의 끝부분이 함께 남는다.
예: improv-worker-v2의 착지 대기는 값 없는 `--landing`으로 `landed_check`가
`CFLOW_DATA_LANDING`을 직접 읽게 한다. 셸이 하는 `$VAR` 전개는
엔진이 값을 끼워 넣는 것이 아니므로 허용된다. 단 문자열 명령은 Windows에서
cmd.exe가 실행하므로 `$VAR`가 전개되지 않는다 — 이식성이 필요하면 명령이 환경을
직접 읽거나 argv 형태를 쓴다. argv 명령(작업의 `verify`, wait·모니터의 `probe`,
컬렉션 `watch.probe`)은 셸 없이 실행되므로 엔진이 각 항목 안의 `$CFLOW_<NAME>`을
그 명령의 환경 값으로 바꾼다(없으면 빈 문자열, `runtime.expand_argv`). 값은 그
항목 하나 안에 머물고 명령으로 해석되지 않는다. `CFLOW_`로 시작하지 않는 이름
(`$HOME` 등)은 그대로 둔다(claunch-yq80p.14). 이름은 `$` 뒤의 영숫자와 `_`가
이어지는 데까지다(`$CFLOW_A.b`의 이름은 `CFLOW_A`). `${CFLOW_A}` 형태는 바꾸지 않는다
— 컴파일러가 명령 안의 `${...}`를 거절한다. 값이 `-`로 시작하면 받는 프로그램이
옵션으로 읽을 수 있다. 값의 형식이 정해져 있지 않으면 프로그램이 받는 경우 `--`
뒤에 두거나, 그 필드의 형식(커밋 sha 등)을 쓰는 쪽에서 보장한다. 근거: 렌더한 값을 셸 문자열에
붙이면 값(`x || true` 등)이 명령이 되어 verify를 우회할 수 있다(리뷰 E1).

### 2.4 식(expression)

`cond.if`, `loop.while`, `choose.options.*.when`, `foreach.in`, `with` 값에 쓰는
식이다. 실행은 엔진 안의 해석기가 하고, 파이썬 `eval`을 쓰지 않는다.

- 값: 문자열, 정수, 실수, `true`/`false`/`null`, 목록 `[…]`
- 참조: `$params.x`, `$data.x.y`, `$prev`, `$item`, `$result.<node>`,
  `$visits.<node>`, `$caps.<capability>`. 시작 입력은 `$data.inputs.<name>`
  (프롬프트에서는 `${data.inputs.<name>}`)
- 연산: `== != < <= > >=`, `in`, `not in`, `and`, `or`, `not`, 괄호
- 함수: `len(x)`, `empty(x)`, `guide(name)`(질문 형식의 작성 지침, 2.10)

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
- `include`는 정의 이름만 받는다(1절).
- `include`는 포함한 파일의 `collections`와 `sides`도 가져온다. 그 파일의
  프로시저가 자기 컬렉션에 쓰거나 side 런을 읽기 때문이다(예: `improv-worker-v2`의
  `land`가 `signals`에 쓴다). 포함하는 파일이 같은 이름을 선언하면 그 선언이
  쓰인다. 같은 이름을 여러 포함 파일이 선언하면 먼저 포함된 것이 쓰인다(2.7
  「include로 따라오는 컬렉션」과 같은 규칙).

### 2.6 시작 입력(inputs)

정의는 런을 시작하는 사람이 채울 입력을 형식과 함께 선언할 수 있다. 새 세션 폼과
spawn 모달은 이 선언으로 입력 칸을 그리고, 데몬은 같은 선언으로 값을 검사한다.
구현은 `src/claude_launcher/cflow2/inputs.py`다.

```yaml
inputs:
  goal:       {type: text, label: 목표, required: true, help: …, placeholder: …}
  issue:      {type: string, default: ""}
  rounds:     {type: int, default: 3}
  dry_run:    {type: bool}
  mode:       {type: enum, choices: [full, quick], default: full}
  scope:      {type: list}
  invariants: {type: invariants}
  acceptance: {type: acceptance}
```

| type | 값 | 폼의 칸 |
|---|---|---|
| `string` | 한 줄 문자열 | 한 줄 입력 |
| `text` | 여러 줄 문자열 | textarea |
| `int` | 정수 | 숫자 입력 |
| `bool` | `true`/`false` | 체크박스 |
| `enum` | `choices` 중 하나 | 선택 목록 |
| `list` | 문자열 목록 | 행 추가·삭제 |
| `invariants` | `[{text, probe?}]` | 행마다 문장과 probe 명령 |
| `acceptance` | `[{text, probe?}]` | 행마다 문장과 probe 명령 |

- 선언 키: `type`(기본 `string`), `label`, `help`, `required`(기본 false),
  `default`, `choices`(`enum`만), `placeholder`, `precheck`, `set_by`(아래
  「시작 뒤에 채우는 입력」). 이름은 식별자 규칙을 따른다.
  모르는 type, type과 맞지 않는 `default`, `enum`이 아닌 입력의 `choices`는
  컴파일 오류다.
- 값 검사(`inputs.normalize`): 폼·CLI에서 온 문자열을 type으로 바꾼다(`int`,
  `bool`의 `true/false/yes/no/1/0`, `list`는 줄바꿈 또는 쉼표로 나눔,
  `invariants`·`acceptance`는 JSON 목록 또는 한 줄에 문장 하나). 비었거나 없는
  값은 `default`를, 없으면 형식의 빈 값(`null`, `[]`, `false`)을 쓴다. 필수 입력은
  `null`, 빈 문자열, 빈 목록으로 채워지지 않는다. 선언에 없는 이름은 거절한다.
  오류는 입력 이름별로 모아 돌려준다.
- 런에 싣기(`inputs.apply`): 런타임이 시작할 때 차트에 적용하고 그 결과를
  `chart.json`으로 저장하므로 스냅샷이 시작 입력을 기록한다.
  - 모든 값이 `data.inputs`에 실린다. 프롬프트는 `${data.inputs.goal}`로 읽는다.
  - `invariants`의 항목마다 규칙 `input-invariant-<n>`이 생긴다(`why`: user-declared
    invariant, 모든 작업에 전달). `probe`가 있는 항목은 같은 이름의 `always`
    모니터도 된다: probe의 종료 코드 0이 지켜짐이고, 어기면 런이 멈춘다
    (`on_violation: halt`, 300초마다).
  - `acceptance` 항목은 `spec.acceptance`(`[{id, input, text, probe}]`)에 실린다.
    런타임은 런이 `ok`로 끝나기 직전에 probe가 있는 항목을 실행해 종료 코드 0을
    충족으로 본다. 모든 항목은 규칙 `input-acceptance` 하나로 모든 작업에
    전달되므로, probe가 없는 항목은 마지막 작업의 프롬프트에서 에이전트가 스스로
    대조한다.
  - probe는 사람이 입력한 셸 명령이며 적힌 그대로 실행한다. 런의 값을 끼워 넣지
    않는다(2.3 「명령 안의 값 참조」).
- 전달 경로:
  - `GET /api/cflow/workflows`의 v2 항목과 `GET /api/cflow2/definition`이
    `inputs`(선언 목록)를 싣는다.
  - 새 세션(`POST /api/sessions`), spawn(`POST /api/sessions/<부모>/children`),
    대시보드 시작(`POST /api/cflow/start`)은 본문 `cflow_inputs`(객체)를 받는다.
    검사에 실패하면 400과 함께 `errors: {이름: 이유}`를 돌려준다. v2가 아닌
    워크플로에 `cflow_inputs`를 보내면 거절한다.
  - MCP `spawn` 도구(`mesh_mcp.py`)의 `cflow_inputs`, CLI `claunch new-session`·
    `claunch spawn`의 `--input NAME=VALUE`(반복 가능, 같은 이름을 두 번 쓰면 목록)와
    `--inputs-file PATH.json`, `--wizard`는 선언된 입력을 한 줄씩 묻는다.

#### 시작 뒤에 채우는 입력(set_by)

시작 입력은 런이 만들어질 때 한 번 기록된다. 선언에 `set_by: [agent]`가 있는
입력만 예외로, 런이 시작된 뒤에 그 런의 드라이버가 한 번 채울 수 있다
(claunch-7d4nt). 시작 입력은 런을 시작한 요청의 값이므로, 이 쓰기는 시작 때
비워 둔 값을 나중에 사용자나 부모가 정해 주었을 때를 위한 것이다.
improv-worker-v2의 `inputs.issue`가 그 예다: 이슈를 비워 두고 시작한 런에 뒤늦게
시작 이슈가 지정되면 에이전트가 그 id를 채운다. 런이 진행 중에 스스로 정한
값(예: 회차가 확정한 이슈)은 시작 입력이 아니며, 워크플로가 `bind`로 다른
`$data` 이름에 기록한다. 그 값을 `inputs`로 옮겨 적는 경로는 두지 않는다.

```yaml
inputs:
  issue: {type: string, default: "", set_by: [agent]}
```

- `set_by`의 값은 `agent` 하나다. `set_by`가 없는 입력은 시작 뒤에 바꿀 수 없다.
  `set_by`를 둘 수 있는 type은 `string`, `text`, `int`, `enum`, `list`다. `bool`은
  빈 값이 없고(채우지 않은 값이 `false`다), `invariants`·`acceptance`는 시작할 때
  규칙과 모니터로 접히므로 시작 뒤 쓰기가 그것을 다시 만들지 못한다. 그 밖의
  type에 `set_by`를 쓰면 컴파일 오류다.
- 쓰기 도구: MCP `flow_input`(`name`, `value`, `scope`/`cwd`/`side`는 다른 `flow_*`
  도구와 같다)과 CLI `claunch cflow2 input <이름> <값> [--json-value] [-t 슬롯]`.
  둘 다 엔진 함수 `runtime.set_input` 하나를 부른다. 호출자는 런의 드라이버여야
  한다(`runtime.may_drive`, 다른 `flow_*` 도구와 같은 판정). 멈춘(halt) 런과 끝난
  런은 거절한다.
- 덮어쓰기 금지: 현재 값이 비어 있을 때(`null`, 빈 문자열, 빈 목록)만 쓴다. 값이
  있으면 거절하고 오류에 현재 값을 싣는다. 시작할 때 데몬이 넣은 Board 이슈도
  이 규칙으로 보호된다. `default`가 비어 있지 않은 입력은 시작 때 그 값을 가지므로
  쓸 수 없다(검사 경고 `set-by-default`). `required: true`인 입력도 시작 때
  반드시 채워지므로 같다(검사 경고 `set-by-required`). 사람이 값을 덮어쓰는 경로는 없다.
- 값 검사는 시작 입력과 같다(`inputs.settle`이 `inputs.normalize`와 같은 형식
  변환과 `enum` 검사를 쓴다). 빈 값은 쓰지 않는다. 그 입력을 읽는
  `input_constraints`는 새 값을 넣은 전체 입력으로 다시 평가해 어기면 거절한다.
- 쓰는 자리는 `data.inputs.<선언된 이름>` 하나다. 도구는 그 밖의 `$data` 경로를
  쓰지 않는다. 반대로 노드는 `data.inputs`를 쓰지 않는다: `bind: inputs`와 최상위
  `data:`의 `inputs` 키는 컴파일 오류다.
- 저널: `input_set {name, value, by, was}`(`by`는 쓴 세션, `was`는 이전 값).
- 쓴 값은 `data.inputs.<이름>`에 들어간다. 그 뒤에 렌더링되는 프롬프트의
  `${data.inputs.<이름>}`, `flow_status`·`claunch cflow2 status`·웹의 `inputs`가 새
  값을 보인다. `chart.json` 스냅샷은 시작 때 값 그대로다. 다음 회차(2.1 recur)는
  런 상태의 `data.inputs`를 이어받는다.

#### 입력 사이의 제약(input_constraints)

입력 하나의 형식으로 표현할 수 없는 규칙(두 입력이 함께 켜질 수 없음, 한 입력이
있으면 다른 입력도 필요함)은 최상위 `input_constraints:`에 식으로 쓴다
(claunch-yq80p.5).

```yaml
input_constraints:
  - assert: not ($inputs.force_pr and $inputs.force_no_pr)
    fields: [force_pr, force_no_pr]
    message: PR 강제와 PR 금지는 함께 켤 수 없다
  - "$inputs.rounds == null or $inputs.rounds > 0"   # 문자열만 쓰면 assert
```

- `assert`는 2.4의 식이고 `$inputs.<이름>`만 읽는다. 다른 루트(`$data` 등), 컬렉션
  함수, 선언되지 않은 입력 이름, 파싱되지 않는 식은 컴파일 오류다.
- `fields`는 식이 거짓일 때 폼이 표시할 입력이다. 생략하면 식이 읽는 입력이다.
  `message`를 생략하면 `constraint not met: <assert>`다.
- 컴파일 결과는 `chart.meta['input_constraints']`에 실린다. `inputs.normalize`가
  입력별 검사 뒤에 평가하므로 시작(`POST /api/sessions`·spawn·`POST
  /api/cflow/start`·CLI·MCP)은 모두 같은 값을 같은 문구로 거절한다. 식이 읽는 입력
  중 하나라도 자체 검사에 실패했으면 그 제약은 평가하지 않는다(그 입력의 오류가
  먼저다). 거짓인 제약의 문구는 `fields`의 입력마다 오류로 붙고, `fields`가 빈
  목록이면 `_constraints` 키에 붙는다.
- 폼은 입력을 바꿀 때마다 `POST /api/cflow2/inputs/check`(본문 `workflow`,
  `workflow_version`, `cwd`, `cflow_inputs`)를 부른다. 응답은 `{ok, values,
  errors}`이고 시작과 같은 검사다. 정의 목록 항목과 `GET /api/cflow2/definition`은
  `input_constraints`(제약 목록)를 싣고, 폼은 입력 칸 아래에 그 문구를 보여 준다.

#### 사전 확인(precheck)

입력 선언의 `precheck: gh`는 그 입력을 켜기 전에 환경을 확인한다는 표시다. 새 세션
폼은 `GET /api/tools/gh?cwd=<세션 디렉터리>`(설정 화면의 gh 카드와 같은
`ghcli.status`, 그 디렉터리 하나로 좁힌 것)를 불러 입력 칸 아래에 gh 버전과
로그인 상태를 보여 준다. gh가 설치되어 있고 그 디렉터리가 push하는 모든 호스트에
로그인되어 있을 때만 `bool` 입력을 켤 수 있다. 아니면 체크박스를 끄고 비활성화하며
설치·로그인 명령을 함께 보여 준다. 다시 확인 버튼이 있다. 이 확인은 안내이고, 값을
쓰는 쪽(워크플로의 작업)이 실행 시점에 다시 확인한다. 현재 `precheck` 값은 `gh`
하나다.

### 2.7 컬렉션(collections)

런이 여러 단계와 여러 런에 걸쳐 쌓고 고치는 기록을 형식과 함께 선언한다. v1의
`landing_queue`(착지 큐)와 `publishes`/`awaits`(런 사이 신호)는 각각 전용 기능이었다.
v2에서는 둘 다 컬렉션 하나와 그 연산으로 쓴다. 구현은
`src/claude_launcher/cflow2/collections.py`다.

```yaml
collections:
  landing:
    kind: queue
    key: issue
    item: {issue: str, branch: str, tip: str, requested_by: str, note: "str?"}
    status:
      values: [requested, waiting, deferred, landed, rejected]
      initial: requested
      settled: [landed, rejected]
    access:
      push: [child, daemon, driver]   # driver: 다른 머신 워커의 요청을 리더가 등록
      set: [driver]
      set.landed: [daemon]
      remove: [driver]
    env: {TARGET: master}
    watch:
      every: 60
      probe: [git, merge-base, --is-ancestor, "$CFLOW_ITEM_TIP", "$CFLOW_TARGET"]
      on: {0: {set: {status: landed}}}
    lifecycle:
      reset_at: [finish]
      drop: settled
      remap: {deferred: requested}
      inherit: true
  signals:
    kind: log
    item: {name: str}
    access: {push: [driver, peer]}
    lifecycle: {inherit: true}
```

**종류(`kind`)**

| kind | 구조 | 용도 |
|---|---|---|
| `queue` | `key` 필드로 구분되는 레코드의 순서 있는 목록. 같은 key로 `push`하면 그 레코드를 바꾸고 상태를 `initial`로 되돌린다 | 착지 큐, 작업 목록 |
| `log` | 추가만 하는 레코드 목록. 레코드마다 일련번호(`seq`), 시각(`at`), 쓴 주체(`by`)가 붙는다. 읽는 런은 자기 처리 위치(cursor)를 따로 가진다 | 런 사이 신호 |

**키**

| 키 | 의미 |
|---|---|
| `key` | `queue`에서 필수. `item`의 필드 이름 |
| `item` | 필드 이름 → 형식(`str`, `int`, `bool`, `list`, `map`, `any`). 끝에 `?`가 붙은 형식은 생략할 수 있다. 선언에 없는 필드는 거절한다 |
| `status` | 선택, `queue`만. 두면 레코드마다 `status` 필드가 생긴다. `initial`은 `values`의 원소, `settled`는 `values`의 부분집합이다 |
| `access` | 연산별로 쓸 수 있는 주체 목록. `set.<상태값>`은 그 상태로 바꾸는 `set`에만 적용되고 `set`보다 우선한다. 생략한 연산은 `[driver]`다. 읽기는 같은 세션 트리 안의 주체 모두에게 열려 있다 |
| `env` | `watch` probe에 `CFLOW_<NAME>`으로 전달되는 값. 값 안의 `{session}`은 런의 드라이버 세션 이름으로 바뀐다 |
| `watch` | 데몬이 정해진 간격으로 레코드마다 확인하는 명령. 아래 「watch」 |
| `lifecycle` | 비우는 시점과 다음 런으로 넘기는 규칙. 아래 「lifecycle」 |
| `type` | 포함한 파일(또는 같은 파일)의 `collection_types` 이름. 그 선언을 복사한 뒤 이 매핑의 다른 키로 덮어쓴다. `env`만은 이름별로 합치고, 타입에서 값이 `null`인 `env` 이름은 사용하는 쪽이 채워야 한다(빠지면 컴파일 오류) |

**주체(principal)**

| 주체 | 누구 |
|---|---|
| `driver` | 런의 드라이버 세션. 중첩 위임 런이면 그 위임의 담당 세션 |
| `daemon` | 데몬의 clock(watch, trigger 실행) |
| `child` | 부모가 이 런의 드라이버인 세션. trigger의 `push`로만 쓴다 |
| `peer` | 예약. 같은 세션의 다른 런(메인 런과 side 런, 2.9)은 서로의 컬렉션을 읽기만 하므로 지금 이 주체로 실행되는 연산은 없다 |
| `person` | 사람의 CLI(`claunch cflow2 collection …`)와 웹 UI(`set`, `remove`만) |

**주소**

`[대상:]컬렉션`. 대상은 `self`(생략 시), `main`, `side:<이름>`, `parent`다.
`main`과 `side:<이름>`은 같은 세션의 런을 가리킨다(2.9). 읽기(식 함수, `wait
until`)와 `consume`에만 쓰고, `push`·`set`·`remove`는 자기 런의 컬렉션에만 한다
(검사 오류 `peer-write`). `parent`는 부모 세션의 런 중 그 이름의 컬렉션을 선언한
런이며 trigger(2.8)에서만 쓴다.

**연산**

| 연산 | 형태 | 효과 |
|---|---|---|
| `push` | `{push: {to: ADDR, item: {필드: EXPR}}}` | 추가. `queue`는 key가 같으면 교체 |
| `set` | `{set: {in: ADDR, key: EXPR, fields: {필드: EXPR}}}` | 레코드의 필드나 상태 변경. `queue`만 |
| `remove` | `{remove: {in: ADDR, key: EXPR}}` | 레코드 삭제. `queue`만 |
| `consume` | `{consume: ADDR}` 또는 `{consume: {from: ADDR, name: NAME}}` | 이 런의 cursor를 그 `log`의 끝으로 옮긴다. `name`을 주면 그 이름의 레코드에 대한 cursor만 옮긴다(아래 「이름별 cursor」) |

- 노드의 `do:` 목록에 쓴다. 각 항목에 `at: enter|leave`(기본 `leave`)와
  `when: EXPR`을 붙일 수 있다. `leave`에서는 식에서 `$outcome`을 읽을 수 있다.
- 필드 값은 식(2.4)이다. 문자열 상수는 따옴표로 쓴다.
- 위임된 하위 트리(실행 주체가 `inline`이 아닌 하위 트리) 안의 노드에는 `do:`를
  쓸 수 없다(검사 오류 `effect-in-delegation`). 위임 결과는 하위 트리의 루트가
  받으므로 효과는 그 루트에 둔다.

**읽기(식 함수)**

| 함수 | 값 |
|---|---|
| `items(ADDR)` | 레코드 목록(`queue`는 순서대로, `log`는 전부) |
| `items(ADDR, STATUS)` | 그 상태의 레코드 목록 |
| `count(ADDR)`, `count(ADDR, STATUS)` | 개수 |
| `fresh(ADDR)` | `log`에서 이 런의 cursor 뒤에 레코드가 있으면 참 |
| `fresh(ADDR, NAME)` | 그 중 `name` 필드가 NAME인 레코드가 있으면 참. 위치는 log 전체 cursor와 NAME의 cursor 중 뒤의 것 |
| `exists(ADDR)` | 주소의 런이 살아 있고 그 컬렉션을 선언했으면 참 |
| `side(NAME)` | 이 세션의 side 런 NAME: `{name, scope, live, status, run, workflow}`. 경로로 읽는다: `side('stack').live` |

`ADDR`은 문자열이다(`items('landing')`, `fresh('side:stack:signals', 'cut')`).
프롬프트에서는 `${col.<이름>}`이 `self`의 레코드 목록을 JSON으로 렌더한다.

**이름별 cursor.** 한 log에 여러 이름의 신호가 섞이면(`cut-wanted`, `frozen`,
`landed`) 한 이름을 처리하며 log 전체 cursor를 옮기면 아직 처리하지 않은 다른
이름이 사라진다. `{consume: {from: ADDR, name: NAME}}`은 NAME의 cursor만 옮긴다.
`fresh(ADDR, NAME)`은 두 cursor 중 뒤의 것 다음에 NAME 레코드가 있는지 본다.
`fresh(ADDR)`(이름 없음)은 전체 cursor만 본다. `wait until`의 `consume` 목록도 같은
두 형태를 받는다.

**wait until**

`wait: {until: EXPR, seconds: N, consume: [ADDR]}`는 셸 명령 없이 엔진이 판정하는
대기다. 런을 바꾸는 호출과 clock의 시간 틱마다 식을 평가하고, 참이 되면 결과
`ok`로 끝나며 `consume`에 적은 주소의 cursor를 옮긴다. 식이 거짓이고 식이 읽은
다른 런의 주소 중 하나라도 런이 없거나, 끝났거나, 그 컬렉션을 선언하지 않았으면
결과 `absent`로 끝난다(끝난 런의 log도 레코드는 남아 있으므로 식이 참이면 `ok`다).
`seconds`가 지나면 `timeout`이다. `probe`와 함께 쓸 수 없다. 같은 세션의 다른 런이
컬렉션을 바꾸면 그 런의 저장 직후 이 런의 `wait until`을 다시 평가한다(2.9).

**watch**

- 데몬 clock이 `every`초(최소 15, 기본 60)마다 `queue`의 레코드 중 `settled`가 아닌
  것마다 `probe`를 실행한다.
- 레코드의 필드는 `CFLOW_ITEM_<FIELD>`, 컬렉션 `env`는 `CFLOW_<NAME>`, 드라이버
  세션은 `CFLOW_SESSION`으로 전달한다. probe 문자열 안의 `${...}`는 컴파일
  오류다(2.3 「명령 안의 값 참조」).
- `on`은 종료 코드 → `{set: {필드: 상수}}` 또는 `{remove: true}`다. 적지 않은 종료
  코드는 아무것도 바꾸지 않는다. watch가 하는 변경의 주체는 `daemon`이다.
- 실행 위치는 런의 cwd이고 제한 시간은 30초다(넘으면 그 회차를 건너뛴다).

**lifecycle**

| 키 | 의미 |
|---|---|
| `reset_at` | 노드 id 목록과 `finish`. 그 노드를 떠날 때(`finish`는 런이 어떤 상태로든 끝날 때) 정리를 실행한다 |
| `drop` | 정리 때 버리는 레코드: `settled`(기본), `all`, `none` |
| `remap` | 정리 때 남는 레코드의 상태 변환 |
| `inherit` | 같은 slot(cwd, scope)에서 같은 이름의 정의로 새 런이 시작되면 이전 런의 컬렉션을 정리한 뒤 넘겨받는다. `log`의 레코드와 이 런의 cursor도 넘겨받는다 |

정리는 저널 `collection_reset`(버린 것, 남긴 것)으로 남는다.

**include로 따라오는 컬렉션**

`include`한 파일이 최상위 `collections:`를 선언하면 포함하는 정의도 그 컬렉션을
가진다. 포함된 파일의 프로시저가 그 컬렉션에 연산(`do:`)을 쓰기 때문이다(예:
`improv-worker-v2`의 `pr-open`이 쓰는 `prs`). 포함하는 정의가 같은 이름을 직접
선언하면 그 선언이 쓰인다. 같은 이름을 여러 포함 파일이 선언하면 먼저 포함된 것이
쓰인다.

**재사용**

`include`한 파일의 최상위 `collection_types:`(이름 → 컬렉션 선언)를 `type:`으로
가져온다. 패키지의 `improv-common`은 `landing-queue`(위 예의 `landing`, `env.TARGET`
필수)와 `signals`(위 예의 `signals`)를 제공한다.

```yaml
include: [improv-common]
collections:
  landing: {type: landing-queue, env: {TARGET: master}}
```

### 2.8 trigger

노드에 들어가거나 떠나는 순간 데몬에게 부수 작업을 요청한다. v1의 `triggers:`와 같은
형태이며 v1 구현(`daemon/cflow_clock.TriggerClock`)의 동작을 그대로 쓴다.

```yaml
nodes:
  integration-request:
    prompt: …
    triggers:
      - briefing                          # at: enter
      - {do: checks, at: leave}
      - {do: enqueue-landing, at: leave}
      - at: leave
        push:
          to: parent:landing
          each: "$facts.in_review"
          item: {issue: "$each", tip: "$facts.head", branch: "$facts.branch",
                 requested_by: "$facts.session"}
```

| 동작 | 내용 | 건너뛰는 경우(`trigger_skipped`의 사유) |
|---|---|---|
| `briefing` | 드라이버 세션의 LLM 브리핑을 다시 구성 | llm 설정 없음, 엔드포인트 실패, 드라이버 세션 없음 |
| `checks` | 드라이버 세션에 상태 점검 재보고 요청을 전달 | 켜진 상태 점검 없음, 전달 실패, 드라이버 세션 없음 |
| `push` | 다른 세션 런의 컬렉션에 레코드 추가(주체 `child`). 대상은 `parent:<이름>` 또는 `leader:<이름>` | 부모 세션 없음, 그 컬렉션을 가진 부모 런(`leader:`면 조상의 메인 런)이 없음 또는 둘 이상(`landing`은 아래 「착지 요청의 대상」), 접근 거절, `each`가 빈 목록, 형식 오류 |
| `enqueue-landing` | 아래 `push`의 축약형: `to: parent:landing`, `each: $facts.in_review`, `item: {issue: $each, tip: $facts.head, branch: $facts.branch, requested_by: $facts.session}` | `push`와 같음. in_review 이슈 없음 |

- 실행 시점: 엔진은 노드 인스턴스에 들어가거나 떠나는 변경 안에서 요청을
  `state.triggers`에 기록하고(`trigger_queued`) 바로 다음으로 진행한다. 반복으로 같은
  노드를 여러 번 지나면 지날 때마다 기록한다.
- 실행: 최소 1회. cflow2 clock이 기록을 가져갈 때 기록을 지우지 않고 `claimed_at`과
  `claim_boot`(데몬 부팅 id)를 표시한다. 실행이 끝나면 `trigger_done` 또는
  `trigger_skipped`(사유)를 남기고 기록을 지운다. 다른 부팅이 표시한 기록(완료 전에
  데몬이 재기동되거나 멈춘 경우)과 `claimed_at`이 실행 제한 시간(120초)의 2배를 넘은
  기록은 표시를 풀고 `trigger_requeued`(사유)를 남긴 뒤 다시 실행한다.
- 중복 실행 처리: `push`의 레코드마다 `_tid`(`<런 id>/<trigger id>:<each 순번>`)를 싣는다.
  넣을 자리의 레코드(`queue`는 같은 key의 레코드, `log`는 모든 레코드)가 같은 `_tid`를
  가지면 아무것도 바꾸지 않고 `trigger_done`(detail에 `replay`)으로 끝난다. 그래서 리더가
  이미 옮긴 상태가 되돌아가지 않는다. `_tid`가 다른 push는 2.7대로 `initial`로 되돌린다.
  `briefing`과 `checks`는 두 번 실행될 수 있다.
- trigger는 런을 멈추지 않는다. 실패는 사유를 남기고 끝난다.
- `push`는 `each`의 레코드를 모두 시도한다. 하나라도 거절되면 `trigger_skipped`(거절 수와 사유)이고,
  이미 들어간 레코드는 남는다.
- `push` 식은 `$data`도 읽는다. 엔진은 trigger를 기록할 때 식이 읽는 `$data`의
  최상위 키만 그 순간의 값으로 기록(`triggers[].data`)에 함께 남기고, 데몬은 그
  값으로 평가한다. 그래서 기록과 실행 사이에 런이 데이터를 바꿔도 push는 노드가
  바뀐 순간의 값을 쓴다(claunch-yq80p.5, improv-worker-v2의 착지 요청이 PR url을
  싣는 경로).
- `$facts`는 trigger의 `push` 식에서만 읽는다(다른 식에서 `$facts`·`$each`를 읽으면 검사 오류
  `facts-outside-trigger`, push 식에서 컬렉션 함수를 쓰면 `trigger-collection-read`). 데몬이 실행 시점에 잰다:
  `head`(드라이버 세션 checkout의 HEAD. 런의 cwd에서 잰다), `branch`, `in_review`(드라이버 세션이
  assignee인 in_review 이슈 id 목록), `session`. `$each`는 `each` 목록의 항목이다.
- 위임된 하위 트리 안의 노드에는 `push`와 `enqueue-landing`을 쓸 수 없다(검사 오류
  `trigger-in-delegation`). `briefing`과 `checks`는 쓸 수 있고 런의 드라이버 세션을
  대상으로 실행한다.
- `parent:<이름>`의 대상 찾기: 부모 세션이 드라이버인 v2 런(메인 slot과 side 런) 중
  그 이름의 컬렉션을 선언한 런, 그리고 이름이 `landing`이면 부모 세션의 v1 런(메인과
  하위 런) 중 `landing_queue:`를 선언한 런. 정확히 하나여야 한다. v1 런이면 v1 엔진의
  `enqueue_landing(issues, branch, tip, by=…)`로 넣는다(10절 「v1 연결」).
  이름이 `landing`이면 이 항목 대신 아래 「착지 요청의 대상」을 따른다.
- `leader:<이름>`의 대상 찾기(claunch-yq80p.9): 부모 세션부터 위로 조상을 차례로
  보며, 그 세션의 **메인 slot** v2 런(side 런과 중첩 런은 세지 않는다)이 그 이름의
  컬렉션을 선언했거나 이름이 `landing`이고 그 세션의 v1 메인 런이 `landing_queue:`를
  선언했으면 그 세션이 대상이다. 그 세션 안에서 정확히 하나여야 한다. 8단계 안에서 찾지
  못하면 `trigger_skipped`(지나간 조상 목록)다. 워커의 independent 자식이 부모 워커의
  stack 런을 건너뛰고 리더의 `landing`에 착지를 요청하는 경로다. `parent:`와 같이
  trigger에서만 쓴다(다른 자리는 검사 오류 `parent-outside-trigger`). 이름이
  `landing`이면 이 항목 대신 아래 「착지 요청의 대상」을 따른다.
- 착지 요청의 대상(`parent:landing`, `leader:landing`, `enqueue-landing`. claunch-6i0tp,
  claunch-9wwbb): 데몬의 `daemon/landing_route.py`가 v1과 함께 정한다.
  - **병합 가능**: 세션이 살아 있고, 워커가 속한 mesh에서 `merger` 역할(primary 또는
    subrole. `leader`는 가진 것으로 본다, claunch-lhgfx. 착지 큐를 가진 런을 구동하는
    세션에는 데몬이 1분 안에 `merger`를 부여하고 그 사실을 알린다. 사람이 제거하면 다시
    부여하지 않는다, claunch-nie6o)을 가지며, 워커와 같은
    저장소(`git --git-common-dir`가 같은 checkout)에서 착지 큐를 가진 런(v2 `landing` 컬렉션을 선언한 런 또는 v1
    `landing_queue:`를 선언한 런)을 구동한다. 그런 런이 여럿이면 묻지 않고 하나를
    고른다: 메인 런이 먼저이고(순서와 무관), 그다음은 디렉터리·런 이름 순서에서 처음 것이다.
  - `parent:landing`(stacked): 부모부터 직계 조상을 위로 차례로 보고 병합 가능한 가장
    가까운 세션의 런에 넣는다. leader 역할은 따지지 않는다. 그런 조상이 없으면 mesh
    leader(아래)에 넣는다.
  - `leader:landing`(independent): mesh leader에만 넣는다. 조상의 메인 런이라도 leader가
    아니면 대상이 아니다.
  - mesh leader: 워커가 속한 mesh의 `leader` 역할 멤버 중 워커의 형제(같은 부모. 둘 다
    부모가 없으면 형제로 본다)이거나 직계 조상인 세션만 대상이다. 삼촌이나 자식 관계인
    leader는 대상이 아니다. 병합 가능한 leader가 정확히 하나가 아니면 `trigger_skipped`다.
  - 대상이 없으면 `trigger_skipped`(지나간 세션과 사유)를 남기고 데몬 로그에 경고를
    쓴다. 대체 대상으로 넣었으면 `trigger_done`의 사유가 지나간 세션과 대상
    (`fell back to '<세션>' at <cwd>`)을 적는다.
- `when`(push trigger, claunch-yq80p.9): 엔진이 trigger를 기록하는 순간 런의 환경으로
  평가하는 식이다(at leave면 `$outcome`도 읽는다. `$facts`·`$each`는 없다). 거짓이면
  기록하지 않고 `trigger_not_queued`(사유 `when (…) is false`)를 저널에 남긴다. 평가가
  오류면 기록하지 않고 `trigger_skipped`(사유 `when (…): …`)를 남긴다. 입력에 따라 대상을 가르는 데 쓴다:

  ```yaml
  triggers:
    - at: leave
      when: "$data.inputs.relation != 'independent'"
      push: {to: parent:landing, each: "$facts.in_review", item: {…}}
    - at: leave
      when: "$data.inputs.relation == 'independent'"
      push: {to: leader:landing, each: "$facts.in_review", item: {…}}
  ```

### 2.9 side 런

세션이 끝날 때까지 메인 런 옆에서 도는 런이다(v1의 하위 런, 예: `stack`). 한
세션은 메인 런 하나와 이름이 다른 side 런 여럿을 가질 수 있다. 구현은
`cflow2/runtime.py`(`start_side`, `abort_sides`, `_ColResolver`)와
`daemon/cflow2_clock.py`(세션 종료)다.

```yaml
# 메인 정의 (improv-worker-v2)
sides:
  stack: stack-v2
collections:
  signals: {type: signals}        # 메인 -> stack 신호
nodes:
  stack-cut:
    do:
      # 지난 요청에 늦게 온 컷을 버린다(아래 「요청과 응답」)
      - {consume: {from: "side:stack:signals", name: cut}, when: "side('stack').live"}
      - push: {to: signals, item: {name: "'cut-wanted'"}}
        when: "$outcome == 'ok'"
body:
  - cond:
      - if: "side('stack').live"
        then:
          - stack-cut
          - wait:
              until: "fresh('side:stack:signals', 'cut')"
              consume: [{from: "side:stack:signals", name: cut}]
              seconds: 14400
```

**요청과 응답.** 신호에는 요청과 응답을 잇는 번호가 없다. 응답을 기다리던
`wait`가 응답 없이 끝나면(timeout 뒤 사람이 진행을 고른 경우) 그 뒤에 온 응답은
다음 요청의 `wait`를 바로 풀 수 있다. 그래서 요청하는 노드는 요청을 쓰기 전에 응답
이름을 `consume`한다(위 예의 `stack-cut`). 남는 경우는 지난 요청의 응답이 새 요청을
쓴 뒤에 도착하는 것이다. 응답하는 쪽의 `consume`(이름별)은 cursor를 log 끝으로
옮기므로 그 응답 하나가 두 요청을 함께 소비하고, 응답의 내용(컷이면 그 시점의 스택
tip)이 새 요청에 쓰인다.

**선언과 slot.**

- 메인 정의의 `sides: {<이름>: <정의 이름>}`에 적은 것만 시작할 수 있다. 이름은
  영문자나 `_`로 시작하고 영숫자·`_`·`-`로 이어진다.
- slot(scope)은 `<세션>+<이름>`이다(`.cflow2/runs/w1+stack/`). 메인 런과 같은
  디렉터리(cwd)에 놓인다. side 런의 중첩 위임 런은 `<세션>+<이름>~<위임 id>`다.
- side 런은 일반 `start`로 시작할 수 없다(그 slot을 주면 거절). 정의는 보통의
  `kind: workflow`다.

**시작.** `runtime.start_side(name, main=<세션>)`, MCP `flow_start`의 `side`
인자(`workflow`는 생략 가능, 주면 선언과 같아야 한다).

- 메인 런이 살아 있고(`running`, `waiting_approval`) 그 이름을 선언해야 한다.
  메인 런이 없거나 끝났으면 거절한다.
- side 런의 드라이버는 메인 런의 드라이버다. `state.json`의 `side`에 `{name, main,
  main_run}`이 실린다(저널 `started`의 `side`).
- 같은 slot에 살아 있는 side 런이 있으면 거절한다(`force`는 일반 시작과 같다).
  끝난 side 런은 보관(archive)되고, 같은 정의면 `lifecycle.inherit` 컬렉션을
  넘겨받는다.

**도구.** 모든 `flow_*` 툴(`flow_start`, `flow_status`, `flow_report`,
`flow_select`, `flow_fork`, `flow_approve`, `flow_collection`, `flow_rule`)이
`side` 인자를 받는다. `side: stack`은 호출 세션의 slot `<세션>+stack`이다. `scope`와
함께 주면 거절한다. 권한은 일반 런과 같다(드라이버만).

**서로 읽기.** 같은 세션의 런은 주소 `main:<컬렉션>`과 `side:<이름>:<컬렉션>`으로
서로의 컬렉션을 읽는다(2.7 「읽기」, `wait until`). 메인 런에서 `main:`은 자기
자신이다.

- 쓰기는 자기 컬렉션에만 한다. 신호를 보내는 런이 자기 log에 쓰고, 받는 런이
  읽는다. 다른 런의 log를 `consume`하면 바뀌는 것은 읽는 런의 cursor뿐이다.
- 다른 런의 log에 대한 cursor는 그 log의 `epoch`(log를 연 런의 id)와 함께
  저장된다. 읽는 시점의 log epoch가 다르면(상대가 log를 넘겨받지 않고 새로 시작한
  런) cursor는 0으로 본다. 넘겨받은 log는 epoch가 유지되므로 cursor가 계속 맞는다.
  메인 런이 같은 정의로 새로 시작해도 다른 런의 log에 대한 cursor를 넘겨받는다.
- 가드·효과의 식(strict)에서: 상대 런이 없으면 빈 log로 읽는다. 살아 있는 상대가
  그 컬렉션을 선언하지 않았으면 식 오류다. 끝난 상대는 남은 레코드를 읽는다.
- `wait until`에서는 위 경우가 모두 `absent` 후보다(2.7).
- 즉시 반영: 컬렉션을 바꾸거나(`collection_pushed`, `collection_set`,
  `collection_removed`, `collection_reset`, `collection_watch`) 끝난(`done`,
  `aborted`) 런은 저장 뒤, 잠금을 놓은 다음에 같은 세션의 다른 런 중 `wait`가 있는
  런의 시간 틱(`wait until` 재평가)을 한 번 돌린다(`poke_peers`). 그 런이 바빠 잠금을
  0.1초(`POKE_LOCK_TIMEOUT`) 안에 얻지 못하면 기다리지 않는다 — 그 런의 다음 저장이나
  데몬 clock의 다음 틱이 한다.

**`side(NAME)`.** 식 함수. 이 세션의 side 런 NAME에 대해 `{name, scope, live,
status, run, workflow}`를 돌려준다. 런이 없으면 `live: false`, 나머지는 `null`이다.
`side('stack').live`처럼 경로로 읽는다(함수 호출 뒤의 `.필드`와 `[식]`).

**상태 표시.** 메인 런의 `flow_status`에 `sides: {<이름>: {name, scope, live,
status, run, workflow, declared, how?}}`가 실린다(아직 없으면 `how`가 시작 방법을
적는다). side 런의 `flow_status`에는 `side`가 실린다.

**세션 종료.** 세션 프로세스가 끝나면(일시정지 제외) 데몬 clock이 그 세션의 살아
있는 side 런을 `aborted`(reason `session <세션> ended`)로 끝낸다. 종료 훅
(`Cflow2Clock.session_exited`)은 기록만 하고 다음 패스 시작(`end_sides`)에서
처리한다. 그때 세션이 다시 살아 있으면(redefine, respawn) 중단하지 않는다. 데몬
재기동은 종료가 아니다. `daemon.cflow2_clock`이 꺼져 있으면 이 처리도 없다. 메인
런은 건드리지 않는다. 종료 기록은 데몬 메모리에만 있다: 세션이 끝나고 다음 패스
전에 데몬이 내려가면 그 side 런은 `running`으로 남는다(부팅 시 정리 없음). 같은 세션
이름이 다시 쓰이면 `side(NAME).live`가 참으로 읽힌다.

**데몬 통지.** side slot(`<세션>+<이름>`)과 그 아래 중첩 런의 통지(ask, monitor)와
세션 조회(session 위임의 부모)는 slot의 세션 이름(`+`·`~` 앞)으로
간다 — 그 세션이 side 런의 드라이버다.

**검사.**

| 코드 | 내용 |
|---|---|
| `unknown-side` | `sides:`를 선언한 차트에서 `side:<이름>:` 주소나 `side('<이름>')`이 선언에 없는 이름을 씀. side 런 정의 쪽의 `main:`·형제 주소는 실행 시점에 해석한다 |
| `peer-write` | `do:`의 `push`·`set`·`remove`가 `main:`·`side:` 주소를 씀 |
| `unbounded-visits` (경고) | `max_visits: 0` |
| `set-by-default` (경고) | `set_by`를 선언한 입력에 비어 있지 않은 `default`가 있다. 시작 때부터 그 값을 가지므로 시작 뒤에 쓸 수 없다(2.6) |
| `set-by-required` (경고) | `set_by`를 선언한 입력이 `required: true`다. 시작 검사가 빈 값을 거절하므로 시작 뒤에 쓸 수 없다(2.6) |

**v1과의 차이.** v1 하위 런(`runs/<세션>/sub/<이름>/`)은 `publishes`/`awaits`의
게시 횟수로 만났다. v2는 log 컬렉션과 이름별 cursor로 만난다. v1 하위 런 도구의
`run: stack` 인자는 v2의 `side: stack`이다. v1 `landing_queue:`는 side 런의
`landing` 컬렉션이다(자식의 `parent:landing` push는 부모 세션의 v2 런 중 그 컬렉션을
선언한 런, 즉 side 런으로 간다. 2.8).

### 2.10 질문 형식(question_types)과 공용 프로시저 ask-user (claunch-yq80p.8)

사용자(또는 `from` 응답자)에게 결정을 묻는 질문을 정해진 구조로 쓰게 하는 기능이다.
질문의 구조는 형식(question type) 선언 하나에서 정해지고, 검증·텍스트 렌더링·웹 UI
렌더링·작성 지침이 모두 그 선언을 읽어 동작한다. 형식에 항목을 더하는 작업은 선언
한 곳만 고친다. 구현은 `src/claude_launcher/cflow2/question.py`, 공용 정의는
`src/claude_launcher/workflows/v2/ask-common.yaml`이다.

이 절이 더하는 것은 네 가지다: (a) 앞 작업이 만든 값으로 ask의 질문과 선택지를
만드는 경로(`ask.brief`), (b) 질문 구조의 선언과 검증(`question_types`), (c) CLI·메시
decide 메시지·`flow_status`·웹 UI가 같은 구조를 그리는 공용 렌더러, (d) 그 구조를
탐색해 채우는 공용 프로시저 `ask-user`.

#### 형식 선언

최상위 키 `question_types:`(이름 → 선언). 재사용 방식은 `collection_types`(2.7
「재사용」)와 같다: 이 파일과 `include`한 파일(포함의 포함까지)의 선언을 이름으로
찾고, 같은 이름이 두 파일에 있으면 컴파일 오류다.

```yaml
question_types:
  decision:
    description: 여러 선택지 중 하나를 고르는 결정
    options: options              # 답이 되는 선택지 목록 필드
    recommendation: recommendation  # 권장안 필드(선택)
    fields:
      problem:
        type: text
        required: true
        label: 문제
        help: 결정할 문제를 한두 문장으로 단순화해 쓴다. 배경 설명은 넣지 않는다
      options:
        type: list
        required: true
        min: 2
        label: 선택지
        help: 실제로 고를 수 있는 선택지를 모두 쓴다
        item:
          name:    {type: name, required: true, label: 이름, help: 답으로 쓰는 짧은 식별자}
          summary: {type: str, required: true, label: 내용, help: 이 선택지가 하는 일}
          risk:    {type: text, required: true, label: 위험, help: 이 선택으로 생길 수 있는 문제와 그 가능성}
          outcome: {type: text, required: true, label: 결과, help: 이 선택으로 얻는 상태}
          reversibility:
            type: enum
            choices: [reversible, costly, irreversible]
            required: true
            label: 가역성
            help: 되돌릴 수 있는가. costly는 되돌릴 수 있으나 비용이 드는 경우
          reverse_how: {type: text, label: 되돌리는 방법, help: reversible·costly일 때 되돌리는 절차}
      recommendation:
        type: ref
        of: options
        required: true
        label: 권장안
        help: options의 name 중 하나
      why:
        type: text
        required: true
        label: 권장 이유
        help: 권장안을 고른 근거. 다른 선택지보다 나은 점을 위험·가역성 기준으로 쓴다
```

형식 선언의 키:

| 키 | 의미 |
|---|---|
| `description` | 형식 설명. 작성 지침의 첫 줄이 된다 |
| `fields` | 필드 이름 → 필드 선언. 순서가 렌더링 순서다 |
| `options` | 필수. `fields` 안의 `list` 필드 이름. 그 항목 목록이 답의 선택지가 된다. 그 필드의 `item`에는 `type: name`인 `name` 필드가 있어야 한다 |
| `recommendation` | 선택. `fields` 안의 `ref` 필드 이름. 렌더러가 그 선택지를 권장안으로 표시한다 |

필드 선언의 키: `type`(필수), `required`(기본 false), `label`, `help`, `choices`
(`enum`만), `item`(`list`만, 항목의 필드 선언 매핑), `min`·`max`(`list`의 항목 수),
`of`(`ref`만, 같은 형식의 `list` 필드 이름).

| type | 값 |
|---|---|
| `str` | 한 줄 문자열 |
| `text` | 여러 줄 문자열 |
| `name` | 옵션 이름 규칙(ask 옵션과 같은 정규식, `abstain`·`approve`·`decline` 금지)을 따르는 문자열. 같은 목록 안에서 대소문자를 무시하고 중복 금지 |
| `int`, `bool` | 정수, 참/거짓 |
| `enum` | `choices` 중 하나 |
| `list` | `item` 선언을 따르는 매핑의 목록. 항목 안에 다시 `list`를 둘 수 없다(한 단계) |
| `ref` | `of`가 가리키는 목록의 항목 `name` 중 하나(대소문자 무시 비교, 정의된 철자로 정규화) |

컴파일 오류: 모르는 type, `enum`에 `choices` 없음, `list`에 `item` 없음,
`ref`의 `of`가 같은 형식의 `list` 필드가 아님, `options`·`recommendation`이 가리키는
필드가 없거나 type이 맞지 않음, `options` 필드의 `item`에 `name` 필드 없음, 형식
이름이 returns 기본 형식 이름(`str`, `int`, …)과 같음.

항목을 더하는 방법: `fields`(또는 `options` 필드의 `item`)에 필드 선언 한 줄을
더한다. 검증, 렌더러, 웹 UI, `ask-user`의 작성 지침이 모두 따라간다. 선언에 없는
필드가 들어온 brief는 거절한다(오타가 조용히 버려지는 것을 막는다).

#### 검증과 정규화(`question.validate`)

`validate(brief, qtype) -> (normalized, errors)`. `errors`는 `["options[1].risk:
required", …]` 형태로 필드 경로를 단다. 정규화는 `ref`·`name` 철자 맞춤과 문자열
앞뒤 공백 제거다. 검증은 두 곳에서 같은 함수를 쓴다:

- returns 형식 `question:<형식>`(아래)으로 보고된 값 — `flow_report` 시점.
- `ask.brief`의 값 — ask가 열릴 때.

렌더러는 열린 ask 레코드에 저장된 정규화된 값을 그리며 다시 검증하지 않는다.

#### returns 형식 `question:<형식>`

작업의 `returns` 필드 형식으로 `question:NAME`을 쓸 수 있다. NAME은 형식 이름이거나
`$params.<p>`다. 보고된 값의 그 필드는 해당 형식으로 검증되고, 실패하면 보고가
거절된다(다른 형식 오류와 같은 경로, 드라이버가 고쳐서 다시 보고한다).
`$params.<p>`는 보고 시점에 작업이 속한 프로시저의 params(2.2 「프로시저 params
기본값」)로 해석한다. 리터럴 이름은 컴파일 시점에 존재를 검사하고, params 참조는
호출 지점의 `with` 값이나 기본값이 리터럴이면 컴파일 시점에, 그렇지 않으면 보고
시점에 검사한다. 끝에 `?`를 붙이면 생략할 수 있다.

#### ask.brief와 ask.format

```yaml
ask:
  brief: "${result['ask-frame'].value.brief}"   # 앞 작업 결과의 참조. 한 개의 ${...}만
  format: decision                # 형식 이름 또는 ${params.format}
  # prompt: 생략 가능. 쓰면 brief 앞에 붙는 머리말이 된다
  # choices: [proceed, hold]      # 선택. 선택지 이름 집합을 고정한다(아래)
  # require_reason: true          # 선택. 모든 답에 이유를 요구한다
  from: [...]
  otherwise: human
  timeout: 900
```

- `brief`는 문자열 하나이며 전체가 `${...}` 값 참조 하나여야 한다(문자열 렌더가
  아니라 값을 그대로 읽는다). 안은 식(2.4)이고 `$`는 생략할 수 있다. 작업 결과는
  `result['<작업>'].value.<필드>`로, `waterfall` 안에서 바로 앞 작업의 값은
  `prev.<필드>`로 읽는다.
- `format`이 있으면 `brief`가 있어야 하고, 반대도 같다.
- `brief`를 쓰면 `prompt`는 선택이다. `options`·`on_decline`은 함께 쓸 수 없다
  (컴파일 오류 — 선택지는 brief에서 온다).
- `choices`(선택): 선택지 이름 목록. 두면 brief의 선택지 이름 집합이 이 목록과
  같아야 한다(대소문자 무시). 정의가 답에 따라 분기해야 하는 지점에서 쓴다 — 선택지
  이름이 정의에 고정되어야 `cond`가 그 이름으로 갈라질 수 있다. 없으면 brief가
  선택지 이름을 정한다.
- `require_reason`(선택, 기본 false): 참이면 선택지 답에도 reason이 필요하다(없으면
  답을 거절한다). `choose`의 `require_reason`과 같은 역할이다. brief가 없는 ask에는
  쓸 수 없다(컴파일 오류). 이 요구는 에이전트와 응답자(`flow_answer`)의 답에만
  적용된다. 사람의 채널(`claunch cflow2 approve`·`select`, 웹 UI, `caller=user`)로 들어온
  답은 reason을 생략할 수 있다. `choose`의 `require_reason`과 `reframe`도 같다.
- `format`·`choices`·`require_reason`·`from`은 리터럴 대신 `${params.<p>}` 참조
  하나를 받을 수 있고, 런타임이 ask를 열 때 프로시저 params로 해석한다(아래
  `ask-user`).
- `otherwise: self:<option>`은 `choices`가 있을 때만 쓸 수 있고 `<option>`은 그 안에
  있어야 한다.
- ask가 열릴 때(`_open_ask`): 참조를 평가하고 형식으로 검증한다. 참조가 null이거나
  검증에 실패하면 ask를 열지 않고 작업이 `fail`로 끝난다(value `{reason, errors}`,
  저널 `ask_brief_invalid`). 성공하면 레코드에 `format`, `brief`(정규화된 값),
  `options`(brief의 선택지에서 만든 `[{name, outcome: ok, description: summary}]`)를
  저장한다. 레코드의 `kind`는 `decision`이다.
- 답: 선택지 이름. 작업은 결과 `ok`로 끝나고(옵션별 결과로 갈라지지 않는다 — 이름이
  정의에 고정되지 않으므로) value는 `{decision, reason, by, ask, brief}`다. 분기는
  다음 노드가 `$result.<작업>.value.decision`으로 한다.
- 답 `reframe`: brief가 있는 ask는 선택지 외에 `reframe`을 하나 더 받는다(사람과
  `from` 응답자 모두). 「제시된 선택지가 문제에 맞지 않는다」는 답이며 reason이
  필수다. 작업은 결과 `reframe`(value `{reason, by, ask, brief}`)으로 끝난다.
  `ask-user`는 이 결과를 받아 탐색을 다시 한다(아래). 그래서 형식의 선택지 이름으로
  `reframe`은 쓸 수 없다(`name` 형식의 금지 목록에 포함).

#### 공용 렌더러(`cflow2/question.py`)

| 함수 | 쓰는 곳 | 내용 |
|---|---|---|
| `render_text(brief, qtype, *, width=None)` | `asks.question_text`(메시 decide 메시지), `claunch cflow2 status`·`approve`의 질문 출력, `flow_status`의 `asks[].question_text` | 아래 순서의 평문 |
| `to_json(brief, qtype)` | `GET /api/cflow2/run`의 `asks[].question`, 웹 UI | `{format, sections: [...], options: [...], recommended}` |
| `guide(qtype)` | 식 함수(2.4), `ask-user`의 탐색 프롬프트 | 형식 선언에서 만든 작성 지침 |

렌더링 순서는 선언 순서를 따르고, 필드의 역할로 표현이 정해진다:

1. `options` 필드보다 앞의 필드: `label` 머리말과 값.
2. `options` 필드: 선택지마다 한 행. 열은 `item`의 필드 순서(`name` 제외). 텍스트
   렌더는 선택지마다 블록(`- <name>: <summary>` 뒤에 `  <label>: <값>` 줄들)이고,
   JSON은 `{kind: "table", columns: [{field, label}], rows: [...]}`다.
3. `recommendation` 필드: 권장안. 텍스트는 `권장: <name>`, JSON은
   `recommended: <name>`이고 해당 선택지 행에 `recommended: true`.
4. 나머지 필드: 1과 같은 머리말과 값.

`to_json`의 `sections` 항목은 `{field, label, kind: "text" | "table" | "choice",
value}`다. 웹 UI는 이 목록을 순서대로 그리고, 답 버튼은 `options` 순서로 놓으며
권장안 버튼에 표시(`recommended` 클래스와 「권장」 라벨)를 단다. `reframe` 버튼은 맨
끝에 두고 reason 입력을 요구한다. 웹 UI는 형식 이름별 코드를 두지 않는다.

`guide(qtype)`의 출력: `description` 한 줄, 그 아래 필드마다
`- <label>(<field>, <type>, 필수|선택): <help>`, `list`는 항목 필드를 들여 쓰고
`min`·`max`·`choices`를 덧붙인다. 마지막 줄은 보고 형식(`{<field>: …}` JSON 뼈대)이다.

#### 식 함수 `guide`

`guide(NAME)`은 형식의 작성 지침 문자열이다(2.4의 함수 목록에 추가). 프롬프트에서
`${guide($params.format)}`처럼 쓴다. 프롬프트의 값 참조 렌더는 `${...}` 안이 함수
호출로 시작하면 `$`를 앞에 붙이지 않는다. 형식 선언은 차트에 실려 있으므로(IR
`meta.question_types`) 런타임이 형식 이름으로 찾는다. 모르는 이름이면 렌더하지 않고
참조를 그대로 둔다(2.2 렌더 규칙).

#### `kind: library`

정의 종류에 `library`를 더한다. `fragments`, `rules`, `procedures`,
`collection_types`, `question_types`만 가질 수 있고 `body`·`nodes`가 없다. 시작할 수
없고 `call`의 대상이 될 수 없으며, `include`의 대상으로만 쓴다. 목록 API와 새 세션
폼의 워크플로 목록에 나오지 않는다.

#### `ask-common.yaml`

`kind: library`. `question_types.decision`(위 선언)과 프로시저 `ask-user`를 둔다.
`improv-common`이 `include: [ask-common]`으로 가져오므로 `improv-common`을 포함하는
정의는 따로 포함하지 않아도 된다.

```yaml
procedures:
  ask-user:
    params:
      topic:   {type: str, required: true}   # 무엇을 결정하는가
      context: {type: str, default: ""}      # 탐색에 쓸 근거·위치
      format:  {type: str, default: decision}
      choices: {type: list, default: null}   # 선택지 이름을 고정할 때
      from:    {type: list, default: null}   # ask.from과 같은 후보 목록. null이면 사람만
      require_reason: {type: bool, default: false}
    returns:
      ok:      {decision: str, reason: "str?", by: str, brief: dict}
      reframe: {reason: str, brief: dict}    # 세 번째 답도 reframe인 경우
    nodes:
      ask-frame:   {prompt: …${guide($params.format)}…, returns: {ok: {brief: "question:$params.format"}}}
      ask-reframe: {prompt: …앞 답의 이유와 앞 질문…, returns: {ok: {brief: "question:$params.format"}}}
      ask-pose:
        ask: {brief: "${prev.brief}", format: "${params.format}", choices: "${params.choices}",
              from: "${params.from}", otherwise: human}
    body:
      - try: {waterfall: [ask-frame, ask-pose]}
        catch:
          reframe:
            try: {waterfall: [ask-reframe, ask-pose]}
            catch:
              reframe:
                try: {waterfall: [ask-reframe, ask-pose]}
                catch: {reframe: {return: reframe, value: …}}
      - {return: ok, value: …}   # ask-pose의 decision, reason, by, brief
```

- `ask-frame`은 드라이버가 실행한다(inline). 반환한 brief는 returns 형식
  `question:$params.format`으로 검증되므로, 형식에 맞지 않는 brief는 보고 시점에
  거절되고 드라이버가 고친다.
- `body`는 `ask-frame` → `ask-pose`를 최대 3회(첫 탐색과 reframe 2회) 반복한다.
  반복 조합자(`loop`) 대신 `try`/`catch`를 세 단으로 쓴 이유: `loop`가 상한에 닿으면
  `loop_limit`으로 사람을 기다리는데, 여기서는 세 번째 reframe이 결과 `reframe`으로
  끝나야 한다.
  `ask-pose`가 `ok`로 끝나면 프로시저가 `ok`(value는 `ask-pose`의 value에서
  `decision, reason, by, brief`)로 끝난다. `reframe`으로 끝나면 그 reason을 다음
  회차의 `ask-frame` 프롬프트에 넣고 다시 탐색한다. 3번째 `reframe`이면 프로시저가
  결과 `reframe`으로 끝나고, 처리는 부르는 쪽의 `on`·`catch`가 한다(없으면 비-ok
  결과의 일반 규칙). 프롬프트 문구의 정본은 `ask-common.yaml`이다.
- `ask-pose`는 `format`·`choices`·`from`·`require_reason`을 params 참조로 받는다.
  `from`은 ask를 열 때 v1 파서로 검증한다. 리터럴이면 컴파일 시점에 검증한다.
- `ask-frame`·`ask-reframe`은 `effects: [fs.read]`, `ask-pose`는 `effects: []`(답을
  기다리기만 한다)를 선언한다.

부르는 쪽 — 답으로 갈라질 때는 `cases`를 쓴다(claunch-yq80p.11):

```yaml
- call: ask-user
  with:
    topic: 이 회차의 범위를 어디까지로 할지
    context: "$result['intake'].value.summary"
  bind: scope_decision           # 선택: ask-user의 ok 값(decision, reason, by, brief)
  cases:
    narrow: work-narrow
    full: [work-full, report]
    skip: []                     # 아무것도 하지 않는 선택지도 명시한다
  on: {reframe: …}               # on은 결과 처리기로만 쓴다
```

#### `cases`: 선택지별 분기

`call: P` 옆의 `cases: {<선택지>: <트리>}`는 선택지 이름과 그 분기를 한 자리에
쓴다. 손으로 쓴 `cond`(`$data.x.decision == '…'`)와 달리 선택지 이름의 오타가
컴파일을 통과하지 못한다 — 이름이 정의되는 곳이 `cases`의 키 하나뿐이다.

- 펼침: `seq[call P(with.choices = cases의 키), cond(P의 decision마다 그 트리)]`.
  IR에서 묶음 `seq`가 `cases: [키…]`를 싣는다. 노드의 `id`·`on`은 묶음에 붙고,
  `bind`는 안쪽 `call`에 붙는다(P의 ok 값이 묶인다).
- `cases`의 키가 `choices`의 유일한 출처다. 같은 `call`에 `with.choices`가 있으면
  컴파일 오류다.
- 표기: 답은 정의의 표기로 돌아온다. brief의 선택지 이름은 고정된 `choices`와 대소문자를
  무시하고 맞춰 보고, ask가 열릴 때 정의의 표기(`local`)로 바꿔 저장한다(권장안도).
  그래서 brief가 `Local`이라 써도 결정은 `local`이고 `cases`의 가드(정확 비교)가 맞는다.
- 예약 이름: `fail`과 P의 `returns`가 선언한 ok 외 결과 이름(`ask-user`의 `reframe`)은
  선택지로 쓸 수 없다(컴파일 오류, 대소문자 무시). 그 결과는 `on`·`catch`로 다룬다.
- `else`가 없다: P가 `cases` 밖의 `decision`으로 끝나면 런은 `no cond branch matched`로
  실패한다. 실제 ask는 답이 `choices`로 제한되어 이 경우가 없고, 선언 모양만 맞춘
  다른 프로시저에서 생길 수 있다.
- `cases` 호출에 `id`를 붙이면 `$result.<id>.value`는 고른 분기의 값이다(묶음 `seq`의
  값). `decision`은 `bind`(안쪽 `call`의 값)로 읽는다. `on: {ok: …}`은 분기 뒤에 돈다.
- 모든 선택지에 트리가 있어야 한다. 아무것도 하지 않는 선택지는 `[]`로 쓴다
  (`null`은 오류). 이름 규칙은 결과 이름과 같고 대소문자를 무시한 중복은 오류다.
- 대상: `choices` param과 `decision` 필드를 담은 `ok` 반환을 선언한 프로시저만
  (`ask-user`가 그렇다). 선언 기반이며 일반 디스패치는 이 범위 밖이다.
- 트리는 호출 지점의 텍스트이므로 호출 지점의 범위에서 컴파일한다(작업 이름,
  `continue`·`break` 대상, `return`). 2.2 「블록 인자와 splice」와 같은 규칙이다.
- `on`은 그대로 결과 처리기다: P가 `ok`가 아닌 결과(`reframe`, `fail`)로 끝나면
  `cases`가 아니라 `on`·`catch`로 간다.

#### 검사(`check.py`)

| 코드 | 수준 | 조건 |
|---|---|---|
| `unknown-question-type` | 오류 | `ask.format` 또는 returns `question:NAME`의 리터럴 이름(또는 컴파일 시점에 리터럴로 해석된 params 값)이 차트의 `question_types`에 없음 |
| `ask-format-without-brief` | 오류 | `format`만 있거나 `brief`만 있음 |
| `ask-brief-with-options` | 오류 | `brief`와 `options`(또는 `on_decline`)를 함께 씀 |
| `ask-brief-ref` | 오류 | `brief`가 `${...}` 참조 하나가 아님 |
| `ask-choices-mismatch` | 오류 | `otherwise: self:<option>`의 옵션이 `choices`에 없음, 또는 `choices` 없이 `self:<option>` |
| `delegated-ask` | 오류(기존) | 위임 단위 아래의 ask. `ask-user` 호출도 ask를 포함하므로 같은 규칙에 걸린다 |

오류 중 컴파일 단계에서 판정하는 것(형식 선언 자체의 오류, `brief`/`options` 동시
사용)은 `FrontendError`이고, 차트 전체를 봐야 하는 것(형식 이름 해석)은 `check`의
Finding이다.

#### 저장과 전달

- IR: `meta.question_types`에 컴파일러가 해석한 형식 선언 전체를 싣는다(포함한
  파일의 것 포함). 작업의 `ask`에 `brief`, `format`, `choices`가 실린다.
- ask 레코드(`state.json`의 `asks`): `format`, `brief`, `options`. `kind`는
  `decision`.
- `flow_status`의 ask 항목(`GET /api/cflow2/run`의 `run.asks`도 같다): `format`,
  `brief`, `question_text`(`render_text`), `question`(`to_json`), `intro`(ask의
  `prompt`, 없으면 빈 문자열), `require_reason`. 레코드의 `prompt`는 `intro`와
  `question_text`를 이은 것이라 `question`을 모르는 화면도 질문 전체를 보인다.
- CLI `claunch cflow2 status`는 `question_text` 전체와 답 목록을 출력한다.
- 메시 decide 메시지(`asks.question_text`): brief가 있으면 질문 본문 자리에
  `render_text`를 넣고 답 목록에 선택지와 `reframe`을 싣는다.
- `flow_answer`·`flow_approve`·`POST /api/cflow2/approve`·`claunch cflow2 approve
  --option`이 선택지 이름과 `reframe`을 받는다. `reframe`에 reason이 없으면 거절한다.

#### 적용 범위

`improv-worker-v2`의 `chooser: user` 선택 여섯 개(`issue-decision`,
`cut-unreachable-choice`, `landed-unclear-choice`, `rework-decision`, `settle-stall`,
`end-decision`)는 같은 id의 `call: ask-user`(`choices`로 원래 옵션 이름을 고정,
`require_reason: true`, `from` 없음)와 `$data.<bind>.decision`으로 갈라지는 `cond`로
바뀌었다. 원래 옵션의 설명은 `topic`에 「선택지:」 줄로 옮겼다.
`workspace-gate`는 바꾸지 않았다: 답이 승인 하나이고 보류는 답하지 않는 것이므로
선택지가 둘 이상인 `decision` 형식에 맞지 않는 승인(ask, `options` 없음)이다.
`landing-review`·`peer-review`의 ask는 사용자가 아니라 다른 세션에 묻는 것이라
대상이 아니다. `improv-leader-v2`(claunch-yq80p.6)의 교대 종료 승인도 같은 방식으로
바꿨다(`wind-down-decision`: `call: ask-user`, choices `[end-shift, keep]`).

### 2.11 사람의 강제 조작: force_target, 강제 이동, 강제 결과 보고 (claunch-huwmp)

v1의 run 페이지에는 그래프에서 스텝을 골라 그 스텝으로 런을 옮기는 Force 버튼이
있었다(`cflow goto`). v1의 위치는 스텝 id 하나지만, v2의 위치는 살아 있는 인스턴스의
트리다(예: `try(body) > loop(iter 1) > seq(idx 0) > scope > seq(idx 0) > cond(branch 1)
> seq(idx 1) > task`). 그래서 v2는 임의의 노드로 이동하는 기능을 두지 않고, 다음
두 조작을 사람에게 준다.

1. **강제 결과 보고**: 활성 작업 하나를 사람이 선언된 결과로 끝낸다. 결과는 작업의
   `returns`로 검사되고(`fail`은 항상 받는다), verify가 있으면 verify를 거치며,
   드라이버의 보고와 같은 경로(`on`, `catch`, 상위 조합자)로 라우팅된다. 저널의
   `reported`(와 `verify_started`)에 `by: user`가 남는다.
2. **강제 이동**: 정의가 허용한 노드로 런을 옮긴다. 허용은 노드 옵션
   `force_target: true`로 표시한다. 작업 정의(`nodes:`의 항목)와 조합자 매핑(`seq`,
   `cond`, `loop` 등의 매핑) 모두에 쓸 수 있고, 값은 불리언이다. 작업 정의에 쓰면
   그 작업을 쓰는 모든 자리가 표시된다.

```yaml
nodes:
  intake: {title: 목표 수립, prompt: …, force_target: true}
body:
  seq:
    - intake
    - {id: review-loop, loop: {while: "true", max: 3, do: review}, force_target: true}
```

강제 이동이 다시 세울 수 있는 경로(`ir.force_path`, `ir.force_slot`):

| 부모 조합자 | 자식의 자리 | 이동 시 부모의 상태 |
|---|---|---|
| `seq` | k번째 자식 | `idx = k`. `waterfall`이면 자식의 `prev`는 비어 있다 |
| `cond` | i번째 가지 | `branch = i`. 가지의 조건식은 평가하지 않는다 |
| `choose` | 옵션의 `then` | `option`이 그 옵션 이름이 된다. 조건식(`when`)은 평가하지 않는다 |
| `loop`(while·until·retry) | `do` | 살아 있으면 현재 회차를 유지하고, 새로 세우면 1회차다. retry면 `attempt`가 회차 번호다 |
| `try` | 본문 | `phase = body` |
| `timeout` | `do` | `phase = body`. 살아 있으면 기존 기한을 유지하고, 새로 세우면 지금부터 `seconds`다 |
| 프로시저 호출(`scope`) | 본문 | `params`를 호출 지점의 `with`로 다시 계산한다 |

거절하는 자리: `par` 가지, `group`의 `do`·멤버(형제·멤버의 상태를 만들 수 없음),
`foreach`의 본문(항목이 없음), `loop`의 `between`, `try`의 `catch`·`finally`,
`timeout`의 `else`, `on` 핸들러(그 자리에 이른 결과가 없음), 위임 하위 트리
(`executor`가 `inline`이 아닌 조상 아래, 하위 트리는 실행 주체가 통째로 실행함),
루트.

- 조합자 매핑에 쓴 표시는 자리 하나를 가리키므로, 그 자리가 위의 거절 대상이면
  컴파일이 실패한다(`frontend._narrow_task_marks`).
- 작업 정의에 쓴 표시는 그 작업을 쓰는 자리 중 다시 세울 수 있는 자리에만 남는다.
  거절 대상인 자리(예: 같은 작업을 `catch`에서도 쓰는 경우)에서는 표시를 떼고 차트
  경고(`warnings`)를 남긴다.
- 런타임은 스냅숏을 읽을 때 `ir.validate`로 표시를 다시 검사한다.

그래서 컴파일된 차트에 남은 표시는 언제나 이동할 수 있다.

이동의 실행(`runtime.force_move`):

- 루트에서 대상까지의 노드 사슬에서, 살아 있는 인스턴스가 있는 가장 깊은 노드를
  기준점으로 삼는다. 기준점 아래의 모든 인스턴스를 취소한다(저널 `left`의
  `why: forced`). 열린 작업, 위임(중첩 런 포함), ask(응답자에게 철회 통지),
  보류(`loop_limit`, `visit_limit`)가 함께 정리된다.
- 기준점부터 대상까지 사슬의 조합자를 위 표대로 자리에 앉히고, 그 사이의 노드는
  새 인스턴스로 들어간다(방문 수 증가, `do`(enter)와 trigger(enter) 실행). 대상은
  보통의 진입(`_enter`)으로 들어간다. 대상이 이미 살아 있으면 새로 들어간다.
- 이동이 건너뛴 노드는 실행되지 않는다. 그 노드의 `bind`, `do`, trigger는 이전
  진행이 남긴 값 그대로다. 앞으로 건너뛰면 대상의 프롬프트가 읽는 `$data.*`가 비어
  있을 수 있다. 이 위험은 정의 작성자가 표시할 노드를 고를 때 판단한다.
- 저널 이벤트 `forced`: `node`(대상), `label`, `by`(`user`), `reason`, `at`(기준점
  노드), `left`(이동 직전에 기다리던 노드 목록).
- halt 모니터에 걸린 런과 끝난 런은 거절한다(`_require_progress`).

경로와 권한:

| 조작 | CLI | 웹 API | MCP |
|---|---|---|---|
| 강제 이동 | `claunch cflow2 move <노드> [--reason R]` | `POST /api/cflow2/move {cwd, scope, node, reason}` | 없음 |
| 이동·보고 요청에 답함 | `claunch cflow2 move --approve\|--deny [--reason R]` | `POST /api/cflow2/move/resolve {cwd, scope, decision, reason}` | 없음(요청은 `flow_request_move`) |
| 강제 결과 보고 | `claunch cflow2 report <작업> <결과> [--value JSON] [--reason R]` | `POST /api/cflow2/report {cwd, scope, node, outcome, value, reason}` | 없음(요청은 `flow_request_move`의 `outcome`) |
| 중단 | `claunch cflow2 abort --reason R` | `POST /api/cflow2/abort {cwd, scope, reason}` | 없음 |
| 보관 | `claunch cflow2 archive` | `POST /api/cflow2/archive {cwd, scope}` | 없음 |
| 재촉 | 없음 | `POST /api/cflow2/nudge {cwd, scope}` | 없음 |

- CLI의 `move`·`report`는 런을 드라이브하는 세션의 셸에서 실행하면 거절한다
  (`_refuse_own_session`, `select`·`approve`와 같다).
- 웹 API는 다른 사람용 엔드포인트(`/api/cflow2/select`, `/api/cflow2/approve`)와 같이
  데몬 토큰을 사람의 채널로 취급하고 호출자를 구분하지 않는다. 관리 세션은 그 토큰에
  접근할 수 있으므로, 세션이 이 엔드포인트를 직접 호출해 자기 런을 옮기는 경로가
  기술적으로 열려 있다. 이 경로는 에이전트에게 금지다. 같은 구조의 문제가
  reposync 승인 카드에 있으며 사용자 결정을 기다린다(claunch-sne7a). 그 결정이
  호출자 구분으로 나면 이 엔드포인트들에도 같이 적용한다.
- 이동·결과 보고·중단 뒤에는 런의 세션에 안내 한 줄이 타이핑된다(api.py의
  `NUDGE_CFLOW2_FORCED`, `NUDGE_CFLOW2_REPORTED`, `NUDGE_CFLOW2_ABORTED`). 재촉 버튼은
  `NUDGE_CFLOW2_CONTINUE`를 보낸다.
- 보관은 끝난 런만 받는다. 살아 있는 런은 먼저 중단한다. 보관 뒤 슬롯은 비고 run
  페이지는 시작 화면을 그린다.

#### 드라이버의 이동 요청 (claunch-kgfr5)

v1의 `request_goto`에 해당한다. 드라이버는 런을 직접 옮길 수 없고, 사람에게
`force_target` 노드로의 이동을 요청할 수 있다. 예: 워커 런이 종료 결정(ask)을
기다리는 동안 사용자가 새 작업을 주면, 드라이버는 런 밖에서 그 작업을 하지 않고
`round`(목표 수립부터 새 회차)로의 이동을 요청한다.

- 요청: MCP `flow_request_move {node, reason}`(`runtime.request_move`). 아무것도
  옮기지 않는다. `reason`은 필수이고 대상은 강제 이동과 같은 규칙으로 검사한다
  (`force_target` 표시, `ir.force_path`). 런 상태 `move_request`에 한 건이 실리고
  (`id`, `node`, `label`, `title`, `reason`, `by`, `from`(그 순간 기다리던 노드),
  `at`), 저널에 `move_requested`가 남는다. 다시 요청하면 앞 요청을 바꾼다
  (`replaces`).
- 대기: 요청이 열려 있는 동안 드라이버의 `flow_report`·`flow_select`·`flow_fork`·
  `flow_approve`는 거절된다(`_require_no_move_request`). 사람의 조작(강제 결과 보고,
  선택, ask 답)은 막지 않는다. `flow_status`는 `move_request`와 `next`의 첫 안내로
  이 사실을 알린다.
- 철회: `flow_request_move {cancel: true}` → 저널 `move_withdrawn`.
- v1 도구의 안내: v2 런만 드라이브하는 세션이 v1 MCP 도구(`request_goto`, `select`,
  `report`, `next`, `status` 등)를 호출하면, v1 런이 없다는 오류 대신 대응하는 v2
  도구를 알리는 오류를 받는다(`cflow/mcp.py`의 `call_tool`). `request_goto`의 대응은
  `flow_request_move`다. 이 안내가 없을 때 v2 세션은 "no active cflow run"을 받고
  사용자에게 `claunch cflow2 move`를 입력하라고 안내했으며, 그 명령은 세션 셸에서
  거절되었다(2026-10-11, s991).
- 승인: run 페이지의 Move 버튼, 터미널 띠(strip)의 Move 버튼, 또는
  `claunch cflow2 move --approve`(`runtime.resolve_move`). 저널에 `move_approved`가
  남고, 강제 이동과 같은 경로(`_move_run`)로 옮긴다. 그 `forced` 이벤트에
  `granted`(요청 id)와 `asked_by`가 붙는다.
- 거절: Refuse 버튼 또는 `--deny`. 런은 그대로이고 `move_request`에
  `decision: denied`, `decided_by`, `decided_reason`이 남아 드라이버가
  `flow_status`로 읽는다. 다음 요청이나 이동이 이 기록을 지운다. 저널
  `move_denied`.
- 대체: 요청이 열린 동안 사람이 `move <노드>`로 런을 옮기면 요청은 답을 받은
  것으로 보고 지운다(저널 `move_superseded`).
- 승인·거절 뒤에는 런의 세션에 안내 한 줄이 타이핑된다(api.py의
  `NUDGE_CFLOW2_MOVE_APPROVED`, `NUDGE_CFLOW2_MOVE_DENIED`).
- 세션 레일은 열린 요청을 사람이 처리할 항목(`owed`의 `move: <노드>`)으로 세고,
  `answers`의 `kind: move` 행이 Move·Refuse 버튼을 싣는다.

#### 드라이버의 결과 보고 요청 (claunch-vwmoj)

강제 결과 보고도 드라이버가 요청할 수 있다. 이 경로가 없을 때 드라이버는 사용자에게
`claunch cflow2 report <작업> ok --reason …`을 입력하라고 문장으로 부탁했다(2026-10-12,
s214: `local-landing-policy`의 verify가 `/usr/bin/python3`에서 `claude_launcher`를
찾지 못해 실패). 그 명령은 verify를 CLI 프로세스의 스레드에서 시작했고, CLI가 끝나면서
verify가 사라져 작업이 `verifying`으로 남았다.

- 요청: `flow_request_move {node, reason, outcome, value?}`. `outcome`이
  있으면 이동 요청 대신 결과 보고 요청이다(`runtime.request_move` → `_request_report`).
  `node`는 활성 작업의 인스턴스 id나 라벨이다. 결과는 요청할 때 작업의 `returns`로
  검사한다. `move_request`에 `kind: report`, `instance`, `outcome`, `value`,
  직전 verify 실패(`last_verify`)가 실린다. 이동 요청은 `kind: move`다.
  두 종류는 같은 자리 하나를 쓰므로 새 요청은 앞 요청을 바꾼다.
- 승인한 보고도 verify를 거친다. verify 면제는 이 요청의 범위에서 제외한다(보드
  claunch-vwmoj의 DECISION, 2026-10-12).
- 대기·철회·거절은 이동 요청과 같다. 거절된 뒤 드라이버는 그 작업을 다시 보고할 수
  있다.
- 승인: run 페이지의 Report 버튼, 터미널 띠의 Report 버튼, 또는
  `claunch cflow2 move --approve`. 저널에 `move_approved`가 남고, 사람의 강제 결과
  보고와 같은 경로(`_report_task`, `by: user`)로 보고한다. `reported`의 `text`에
  요청 id와 요청자가 남는다. 그 사이 작업이 더 이상 보고를 기다리지 않으면 승인은
  거절되고 요청은 `--deny`로 정리한다.
- 대체: 요청이 열린 동안 사람이 같은 작업을 직접 보고하면 요청을 지운다(저널
  `move_superseded`의 `reported`).
- 안내: 승인 뒤 런의 세션에 `NUDGE_CFLOW2_REPORT_APPROVED`가 타이핑된다. 레일의
  `owed`에는 `report: <작업>`으로 실리고, `answers`의 `kind: move` 행에
  `report: true`, `outcome`이 붙는다.
- CLI의 `report`와 `move --approve`는 자기가 시작한 verify가 끝날 때까지 기다린 뒤
  종료한다(`cli_cflow2._after_verify`). verify는 그 명령을 실행한 프로세스의
  스레드에서 돌기 때문이다.

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

`meta`에는 위 네 키 외에 `max_visits`(2.1)와 `inputs`(2.6, 컴파일러가 채운
선언)가 실린다. 런타임이 시작 입력을 적용한 스냅샷에는 `meta.inputs_applied:
true`가 붙는다. 작업 노드에는 `effects_declared`(작성자가 `effects` 키를 썼는지)가
붙는다 — `effects: []`(효과 없음 선언)와 키 생략(알 수 없음)을 구분하기 위해서다.
wait 노드에는 `undecided_max`가 붙는다.

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
| `wait` | 외부 이벤트 대기 원자 상태 | `probe`, `poll`, `seconds`, `undecided_max`, `describe`; `wait until`이면 `until`, `consume` |
| `group` | 동적 병렬 영역 | `child`, `member`(펼친 프로시저의 루트), `limit`, `settle`, `name` |
| `control` | 전이 | `op`(continue/break/raise/return/end), `target`, `outcome`, `value` |
| `scope` | `call`을 펼친 복합 상태 | `proc`, `child`, `with`, `bind`, `params`(블록 param 제외), `returns` |

`seq`는 두 가지 출처 표시를 더 가질 수 있다: `splice: {block, call}`(블록 인자를
놓은 자리. `call`은 블록을 받은 호출의 scope id), `cases: [선택지…]`(`call … cases`를
펼친 `[call, cond]` 묶음, 2.10). `control`의 `return`이 `target`을 가지면 그 scope
(또는 `$run`)를 끝낸다. 없으면 가장 가까운 scope다.

모든 노드에 공통: `id`, `type`, `parent`, `src`(원본 파일:줄), `executor`(위임
지점이면), `bind`, `on`. 선언한 노드에만: `do`(효과 목록 `[{op, at, when, …}]`),
`triggers`(`[{do, at, push?}]`, 2.8).

노드 id는 작성자가 준 `id`·작업 이름에서 만들고, 없으면 부모 id와 위치에서
만든다. 같은 정의를 다시 컴파일하면 같은 id가 나온다.

## 4. 런타임

### 4.1 상태 저장

`<repo>/.cflow2/runs/<scope>/` (v1의 `.cflow/`와 분리):

| 파일 | 내용 |
|---|---|
| `chart.json` | 시작 시점의 IR 스냅샷 사본. 시작 입력(2.6)은 스냅샷 전에 차트에 반영된다 |
| `state.json` | 활성 구성(active configuration), 데이터 모델, 노드별 방문 수, 위임 실행 단위, 드라이버 세션(`driver`), 중첩 위임 런이면 상위 런 위치(`parent`) |
| `journal.jsonl` | 추가만 하는 이벤트 로그 |
| `receipts/` | 위임 실행 단위의 영수증 |
| `checkpoint.sqlite3` | 상태·정의·이벤트·영수증의 트랜잭션 정본. 기존 정상 런은 다음 저장 시 전환된다 |

체크포인트가 있으면 런은 DB의 확정 내용을 읽고 JSON 파일은 조회용 사본으로
유지한다. 상태와 이벤트를 한 트랜잭션에 저장하며 DB 손상 시 JSON으로 자동
전환하지 않는다. 비정상 종료 시 보장 범위와 `claunch cflow2 recover` 절차는
[저장 내구성과 손상 복구](cflow2-recovery.md)에 설명한다.

중첩 위임 런(7절)은 같은 디렉터리의 `.cflow2/runs/<scope>~<위임 id>/`에 놓인다.

### 4.2 실행 의미

- 활성 구성은 현재 진행 중인 원자 상태(task, wait)와 그 조상 복합 상태의
  집합이다. `par`와 `group` 아래에서는 원자 상태가 여럿일 수 있다.
- 원자 상태 하나는 `pending → active → done(outcome, value)`로 진행한다.
- 결과가 나오면 엔진은 부모로 올라가며 다음 활성 상태를 정한다(`advance`).
  `on` 매핑, `try`의 `catch`, 반복의 `continue`/`break`, `par`의 `settle`이
  이 단계에서 평가된다.
- 반복 상한: `loop.max`(`retry`·`foreach`의 `max` 포함)는 반복에 한 번 들어갈
  때마다의 회차 상한이다. 반복을 다시 들어가면 다시 0부터 센다. 상한에 닿았는데
  조건이 계속을 요구하면 그 반복 인스턴스가 `waiting_approval`(reason `loop_limit`)로
  멈춘다(`claunch cflow2 extend`). 사람의 답은 세 가지다. `--by N`(웹 「Allow one
  more」는 1)은 상한을 N만큼 올린다. `--reset`(웹 「Reset the count」, 본문
  `reset: true`)은 회차 수를 0으로 되돌려 반복이 선언된 `max`만큼 다시 돌게 하고,
  그때까지 올린 상한은 버린다. 반복의 위치는 유지된다: `foreach`는 다음 항목부터
  이어 가고, `retry`의 `attempt`와 모은 값은 그대로다. 한 회차도 돌지 않은 상한
  (항목 수가 `max`보다 많은 병렬 `foreach`)에는 `--reset`을 거절한다. `--stop`은
  그 반복을 `fail`로 끝낸다.
- 방문 상한: 방문 수는 노드별로 런 전체에서 센다. 차트 최상위 `max_visits`(기본
  200)를 넘는 진입은 그 노드의 본문이 시작되기 전에 멈추고, 런은
  `waiting_approval`(reason `visit_limit`)가 된다. 사람이
  `claunch cflow2 extend --visits [--by N | --stop]`(웹 `POST /api/cflow2/extend`,
  `kind: visits`)로 그 노드의 허용 방문 수를 늘리거나 그 인스턴스를 `fail`로 끝낸다.
- 멈춤의 범위:

  | 대기 | 범위 |
  |---|---|
  | `halt` 모니터 위반(`monitor_violation`) | 런 전체. 보고, 선택, fork, 위임 결과 제출, 응답, 시간 기반 진행(wait, timeout, 반복 간격)이 모두 거절되거나 보류된다. 데몬 실행기가 가져온 위임 결과와 통과한 verify는 보관했다가 `claunch cflow2 resume` 시점에 적용한다 |
  | `human` 단계의 ask | 그 ask를 연 작업 인스턴스만. 다른 분기는 계속 진행한다 |
  | `loop_limit`, `visit_limit` | 그 반복 또는 노드 인스턴스만 |
  | `acceptance`(2.6) | 런의 마지막 단계. 진행 중인 인스턴스는 이미 없다 |

- 런 전체 대기(halt)가 기록된 채 런이 끝나는 경로는 없다. 끝난 런에서도 그 기록은
  남고 `resume`으로 지울 수 있다. 인스턴스에 묶인 대기는 인스턴스와 함께 지워진다.
- 취소: 어떤 경로로 인스턴스가 사라지든(timeout 만료, par 결정, group 취소,
  break/continue/return 전파, abort) 그 인스턴스의 대기·위임·ask가 함께 정리되고,
  런의 `status`/`reason`은 남은 대기 항목에서 다시 계산된다.
- `bind`가 있으면 노드 자신의 결과 값을 `data`에 저장한다. `spec.ensures`가 실패해
  결과가 `fail`로 바뀌어도 저장되는 값은 노드가 낸 값이다. 부모가 받는 값은
  `{reason, original}`이다.
- 병렬 `foreach` 안의 `break`는 그때까지 `ok`로 끝난 항목들의 값을 항목 순서대로
  모은 목록을 반복의 값으로 남긴다.
- 식 오류: 식 평가는 `ExprError`만 낸다(깊이 64 초과, 30자리를 넘는 정수 리터럴,
  목록·매핑을 키로 쓴 색인, 그 밖에 Python이 거절한 연산 포함). 렌더는 해석하지
  못한 참조를 원문 그대로 둔다. 가드(`cond`, `loop`, `retry.until`), `with`,
  `foreach.in`, `return` 값에서의 오류는 그 노드를 `fail`(값의 `reason`에 오류 문구)로
  끝낸다. `spec.ensures`·`monitors`의 오류는 위반으로 기록된다(`error` 필드).
  선택지 가드 오류는 그 선택을 거절한다.
- 동시 호출: 런을 바꾸는 모든 호출은 런 잠금 안에서 먼저 시간 틱(기한이 지난
  wait·timeout·ask)을 적용한 뒤 요청을 적용한다.

### 4.3 저널 이벤트

`started`(params, inputs, driver), `entered`, `left`(outcome 포함), `selected`,
`reported`, `bound`, `delegate_started`, `delegate_returned`, `delegate_failed`,
`delegate_held`, `nested_started`, `fallback`(원인과 경로), `group_fork`,
`group_settled`, `monitor_violation`, `waiting`, `done`, `aborted`, `verify_started`,
`verify_passed`, `verify_failed`(종료 코드, 출력 끝부분, `lost`), `probe_undecided`(종료 코드, 횟수, 출력 끝부분),
`ask_routed`(그룹, 응답자), `ask_answered`(결정, 답한 쪽, 경로), `ask_timeout`,
`ask_deferred`, `ask_undelivered`, `ask_withdrawn`, `acceptance_checking`,
`acceptance_passed`, `acceptance_failed`, `acceptance_waived`, `round_requested`,
`round_auto_started`, `round_cancelled`, `select_held`, `hold_cancelled`,
`restart_requested`, `restart_completed`, `restart_interrupted`,
`restart_unsupported`, `checks_passed`, `checks_expired`. ask가 그룹을
건너뛰거나 `otherwise`로 넘어갈 때는 `fallback`(`decision: ask`)을 남긴다.

- 런의 `waiting_approval`은 열린 대기 항목에서 계산한다: `loop_limit`,
  `visit_limit`, `monitor_violation`, `acceptance`, `human` 단계의 ask가 하나라도
  있으면 `waiting_approval`, 없으면 `running`. halt가 있으면 reason은
  `monitor_violation`이다.

### 4.4 완료 기준(acceptance) 검사

시작 입력의 `acceptance` 항목 중 `probe`가 있는 것(`spec.acceptance`)은 런이
`ok`(또는 차트가 선언한 결과)로 끝나기 직전에 검사한다.

- 검사는 백그라운드에서 런 잠금 밖으로 실행한다(`acceptance_checking`). probe는
  wait·모니터 probe와 같은 방식(환경 변수로 값 전달, 종료 코드 0이 충족)으로 실행한다.
- 모두 충족: `acceptance_passed` 후 런이 끝난다.
- 하나라도 미충족: 런은 끝나지 않고 `waiting_approval`(reason `acceptance`)가 된다.
  `flow_status`의 `acceptance.failed`에 항목(`id`, `text`, 종료 코드, 출력 끝)이
  실린다. 해소 경로는 둘이다. (1) 작업을 계속해 원인을 고친다. `flow_status`(30초
  간격) 또는 데몬 클럭 틱이 검사를 다시 실행하고, 모두 충족되면 런이 끝난다.
  (2) 사람이 `claunch cflow2 approve acceptance`(웹 approve의 `node: "acceptance"`)로
  그대로 받아들인다(`acceptance_waived`). 에이전트의 `flow_approve`는 거절된다.
- `probe`가 없는 항목은 엔진이 검사하지 않는다. 규칙 `input-acceptance`로 모든
  작업 프롬프트에 실린다.
- 중첩 위임 런은 이 검사를 하지 않는다.

### 4.5 컬렉션과 trigger의 실행

**상태.** `state.json`에 `collections: {이름: {kind, items: [...], seq, epoch}}`,
`cursors: {주소: seq}`(자기 log는 이름, 다른 런의 log는 주소 전체, 이름별 cursor는
`<키>#<이름>`), `cursor_epochs: {키: epoch}`(다른 런의 log), `triggers: [{id, run, node, instance, at, do, push?, queued_at, claimed_at?, claim_boot?}]`를
둔다. 컬렉션 선언은 `chart.json`의 `meta.collections`, trigger와 `do:` 효과는 노드의
`triggers`, `do` 필드다.

**적용 순서.** 노드 인스턴스에 들어갈 때: `do`(at enter) → trigger(at enter) 기록 →
본문 시작. 떠날 때: 결과 확정 → `do`(at leave) → trigger(at leave) 기록 →
`lifecycle.reset_at`의 정리 → 부모로 진행. 런이 끝날 때: `reset_at: [finish]` 정리.
`do` 효과가 실패하면(접근 거절, 형식 오류, 식 오류) 그 노드의 결과를 `fail`로
바꾸고 값의 `reason`에 사유를 적는다.
- at leave `do`는 결과가 정해진 직후, 그 노드의 `on` 처리기보다 먼저 실행한다. 그래서
  `do` 실패로 바뀐 `fail`은 노드 자신의 `on`과 감싸는 `try`의 `catch`가 일반 `fail`처럼 처리한다.
- 중첩 위임 런은 컬렉션을 갖지 않는다. 위임된 하위 트리 안의 노드는 컬렉션을 읽을 수도
  없다(검사 오류 `collection-read-in-delegation`). 하위 트리 루트의 `do`와 trigger는 위임하는
  런이 한 번만 실행한다.
- at enter `do`가 실패해 본문 없이 `fail`로 끝난 노드도 떠나는 단계(at leave `do`,
  trigger, `reset_at`)를 거친다. 실패한 노드에서 건너뛰려면 `when: "$outcome == 'ok'"`를 쓴다.
- trigger는 생략하면 `at: enter`다. 문자열 축약형(`briefing`)과 매핑 모두 같다.
- `reset_at`은 노드 id와 라벨을 모두 받는다.

**저널.** `collection_pushed`, `collection_set`, `collection_removed`,
`collection_reset`, `collection_inherited`, `collection_watch`(코드, 바꾼 필드),
`cursor_moved`, `trigger_queued`, `trigger_requeued`(사유), `trigger_done`, `trigger_skipped`(사유).

**구현 계약** (모듈 사이의 경계. 이름이 바뀌면 이 표도 바꾼다)

| 함수 | 위치 | 하는 일 |
|---|---|---|
| `collections.normalize(spec, types) -> dict` | `cflow2/collections.py` | 선언 검사와 `type:` 펼치기. 프런트엔드가 부른다 |
| `collections.apply_push/apply_set/apply_remove(col_state, spec, ..., principal)` | 같은 곳 | 순수 함수. 접근·형식 검사 후 상태를 바꾸고 이벤트 dict를 돌려준다 |
| `collections.reset(col_state, spec) -> summary` | 같은 곳 | lifecycle 정리 |
| `runtime.start_side(name, *, cwd, main, workflow, params, inputs, caps, force, driver, caller)` | `cflow2/runtime.py` | side 런 시작(2.9) |
| `runtime.abort_sides(session, cwd, reason) -> list` | 같은 곳 | 세션의 살아 있는 side 런 중단(데몬) |
| `runtime.poke_peers(cwd, scope, now) -> list` | 같은 곳 | 같은 세션의 다른 런의 `wait until` 재평가 |
| `runtime.side_info(cwd, main, name) -> dict` | 같은 곳 | `side(NAME)`의 값 |
| `runtime.collection_op(op, name, *, cwd, scope, principal, session, key=None, item=None, fields=None, tid=None)` | `cflow2/runtime.py` | 외부(데몬, MCP, CLI)의 연산. 런 잠금 안에서 적용하고 status를 돌려준다. `tid`는 trigger push의 `_tid` |
| `runtime.collection_view(cwd, scope) -> dict` | 같은 곳 | 컬렉션 요약(웹, MCP, status) |
| `runtime.claim_triggers(cwd, scope, *, boot, now, held=()) -> list` | 같은 곳 | 표시되지 않은 trigger와 풀린 trigger에 표시하고 돌려준다(기록은 남는다). `held`는 호출자가 아직 들고 있는 id |
| `runtime.complete_trigger(cwd, scope, tid, performed, detail)` | 같은 곳 | 기록을 지우고 `trigger_done`/`trigger_skipped` 기록 |
| `runtime.watch_due(cwd, scope, now) -> list` | 같은 곳 | 지금 확인할 (컬렉션, key, probe, env, on) 목록 |
| `runtime.watch_apply(cwd, scope, name, key, code, now)` | 같은 곳 | probe 결과를 `on`대로 적용(주체 daemon) |
| `runtime.holders(name, session, cwds) -> list` | 같은 곳 | 드라이버가 `session`이고 컬렉션 `name`을 선언한 살아 있는 v2 런의 (cwd, scope) |
| `cflow2_triggers.perform(...)` | `daemon/cflow2_triggers.py` | trigger 실행(briefing, checks, push, enqueue-landing), facts 측정, 부모 대상 찾기, v1 연결 |

## 5. 에이전트 인터페이스(MCP)

v1 툴과 이름이 겹치지 않도록 `flow_` 접두어를 쓴다.

| 툴 | 의미 |
|---|---|
| `flow_start` | v2 런 시작(`workflow`, `params`, `inputs`, `force`). 호출 세션의 slot에만 시작한다. 그 세션이 런의 드라이버가 된다 |
| `flow_status` | 활성 구성, 렌더된 현재 작업 프롬프트, 고를 수 있는 옵션, 시작 입력, 진행 중인 verify, 대기 항목 |
| `flow_report` | 작업 결과 보고(`node`, `outcome`, `value`, `text`). verify가 있으면 `verifying`으로 바로 돌아온다 |
| `flow_select` | `choose` 노드의 옵션 선택(`node`, `option`, `reason`) |
| `flow_fork` | `group`에 멤버 추가(`group`, `with`) |
| `flow_delegate` | 위임 노드의 브리프 조회와 결과 제출(`delegation`, `outcome`, `value`, `receipt_path`, `scope`, `cwd`) |
| `flow_rule` | 규칙의 `text`·`why` 조회 |
| `flow_approve` | `self` 단계로 넘어간 자기 런의 ask에 답함(`node`, `option`, `decline`, `reason`). 다른 단계에서는 거절 |
| `flow_answer` | 다른 세션의 런이 라우팅한 ask에 응답자로서 답함(`ask_id`, `decision`, `reason`) |
| `flow_asks` | 이 세션의 답을 기다리는 ask 목록 |
| `flow_collection` | 컬렉션 조회와 연산(`action`: `list`, `push`, `set`, `remove`; `collection`, `key`, `item`, `fields`). 주체는 `driver`이고 `access`를 따른다(2.7) |
| `flow_input` | 시작 뒤에 채우는 입력 쓰기(`name`, `value`). `set_by: [agent]`로 선언되고 비어 있는 입력만, 드라이버만(2.6) |
| `flow_request_move` | `force_target` 노드로의 이동(`node`, `reason`) 또는 활성 작업의 결과 보고(`outcome`, `value`)를 사람에게 요청, 또는 철회(`cancel`). 아무것도 옮기지 않고, 답이 올 때까지 드라이버의 진행을 막는다(2.11) |

런 중단(abort), 보관(archive), 강제 이동(move), 이동 요청에 대한 답, 사람의 강제 결과
보고는 MCP 툴이 없다. 사람이 CLI나 웹 UI로 한다(2.11). 드라이버는 이동과 결과 보고를
요청만 할 수 있다(`flow_request_move`).

권한(`cflow2/mcp.py`, `runtime.may_drive`):

- 호출 세션은 MCP 서버 환경의 `CLAUNCH_SESSION`이다. 인자로 정하지 않는다.
- `scope`·`cwd`를 생략하면 호출 세션의 slot이다. 모든 툴은 대상 런의 드라이버만
  쓸 수 있다. 예외는 중첩 위임 런이다. 그 런은 해당 위임의 담당 세션(`subagent`면
  상위 런의 드라이버, `session`이면 배정된 자식 세션)이 `scope`(와 필요하면 `cwd`)를
  지정해 쓸 수 있다.
- `flow_start`는 다른 scope나 `cwd`를 받지 않는다. `force`는 기존 런의 드라이버만
  쓸 수 있고, 기존 런이 사람을 기다리는 동안(`chooser: user` 선택, 열린 ask, halt,
  `loop_limit`, `visit_limit`, `acceptance`) 거절된다. 열린 중첩 위임 런이 사람을
  기다리는 경우도 같다. 부모를 교체하면 그 중첩 런이 중단되기 때문이다. 이때 부모
  런의 상태는 `running`이고, `flow_status`의 `waiting_in_nested`가 중첩 런 안의 대기를
  보여 준다.
- 에이전트의 `force` 시작은 교체되는 런의 `invariants`·`acceptance` 형식 입력 항목을
  새 입력 앞에 합쳐 유지한다. 에이전트는 자기 run에 검사를 더할 수 있고, 사람이 준
  검사를 뺄 수는 없다. 사람의 CLI `start --force`는 주어진 입력만 쓴다.
- `flow_delegate` 제출: `subagent` 위임은 런의 드라이버만, `session` 위임은 배정된
  자식 세션(`assignee`, 없으면 `child`)만 제출한다. 데몬이 가져온 자식의 영수증
  파일은 데몬 내부 경로로만 들어온다. 중첩 위임 런으로
  실행되는 위임은 어느 경로로도 제출을 받지 않는다(데몬 내부 함수 포함). 결과는
  중첩 런이 끝날 때만 돌아온다. 브리프 조회는 드라이버와 담당 세션이 할 수
  있다.
- `receipt_path`는 실제 경로(심볼릭 링크 해석 후)가 런의 `receipts/` 또는 호출
  세션의 `CLAUNCH_SCRATCH` 아래일 때만 읽는다.

## 6. capability와 provider 기술

### 6.1 원자 capability 목록

위임 실행이 기대는 기능을 서로 겹치지 않는 원자 단위로 나눈다.

| id | 의미 | 사용하는 곳 |
|---|---|---|
| `subagent.spawn` | 드라이버 에이전트가 하네스 안에서 새 컨텍스트의 하위 에이전트를 띄울 수 있다 | `subagent` 실행 주체 |
| `subagent.parallel` | 하위 에이전트 여럿을 동시에 띄울 수 있다 | `par`·`group` + `subagent` |
| `subagent.tools.restrict` | 하위 에이전트의 도구를 제한할 수 있다 | 읽기 전용 위임 |
| `subagent.worktree` | 하위 에이전트를 별도 git worktree에서 실행할 수 있다 | 쓰기 효과가 있는 병렬 위임 |
| `mcp.claunch` | 실행 주체 안에서 claunch MCP 툴을 부를 수 있다 | `flow_delegate`로 결과 제출 |
| `session.spawn` | claunch spawn으로 자식 세션을 만들 수 있다(데몬·정책 기능) | `session` 실행 주체 |

`headless.*` 일곱 개는 `headless` 실행 주체와 함께 제거했다(claunch-xjbrh). 설정에
남아 있는 `headless.*` 키는 경고 없이 무시한다(`caps.REMOVED_FAMILIES`).

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
      mcp.claunch: false                # 이 백엔드에서는 claunch MCP를 쓰지 않음
    harness_options:
      claude:
        capabilities:
          subagent.parallel: false
```

값은 `true`, `false`, 또는 매핑 `{enabled: bool, args: [...]}`이다. 남은
capability는 모두 하네스·데몬의 기능이라 `args`를 읽지 않는다(하네스 명령에 인자를
붙이던 것은 제거된 `headless.*`뿐이었다). `true`만 적은 층은 아래 층의 `args`를
그대로 보존한다.

기술되지 않은 capability는 `false`로 본다.

### 6.3 패키지 기본값

`--help` 출력으로 확인한 것만 기본값을 `true`로 둔다(2026-10-06, 이 머신).

| capability | claude | codex | pi | devin | kimi, agent |
|---|---|---|---|---|---|
| `subagent.spawn` | true (Agent 도구) | false | false | false | false |
| `subagent.parallel` | true | false | false | false | false |
| `subagent.tools.restrict` | true | false | false | false | false |
| `subagent.worktree` | true (`isolation: worktree`) | false | false | false | false |
| `mcp.claunch` | true | true | false | false | false |
| `session.spawn` | 데몬 정책(`spawn.*`)으로 결정 | 같음 | 같음 | 같음 | 같음 |

- 값과 인자는 `src/claude_launcher/harnesses.yaml`의 `capabilities:` 블록이 정본이다.

`claunch cflow2 caps [--profile P]`가 합성 결과와 각 값의 출처 층을 보여 준다.

### 6.4 fallback 경로

모든 분기는 실행 시점에 `fallback` 저널 이벤트(원인 capability, 선택된 경로)를
남긴다. 정적으로 결정 가능한 것은 `cflow2 validate --profile P`가 미리 보여 준다.

**실행 주체 선택.** 노드의 `executor` 목록을 앞에서부터 보고, 아래 조건을
만족하는 첫 항목을 쓴다. 하나도 만족하지 않으면 `inline`이다. `inline`은 어떤
capability에도 기대지 않으므로 마지막 경로는 항상 존재한다.

위임 서브트리가 평탄하지 않으면(task·seq 이외의 제어 구조를 포함) 플래너
(`caps.plan_node(node, caps, flat=False)`)는 `mcp.claunch`가 꺼져 있으면
`subagent`·`session`을 건너뛴다. 건너뛴 사유는
`subtree not flat`으로 `fallback` 이벤트에 남는다.

`executor` 목록에 `headless`가 있으면 컴파일러가 경고를 남기고 그 항목을 뺀다.
제거 전에 시작된 런의 위임 기록이 `headless`로 남아 있으면 clock이 노드의 다음
실행 주체로 넘기고 `fallback`(`headless -> <다음>`)을 남긴다.

| 실행 주체 | 필요 조건 | 조건이 깨졌을 때 |
|---|---|---|
| `session` | `session.spawn` (데몬이 정책으로 판정) | 목록의 다음 항목 |
| `subagent` | `subagent.spawn` | 목록의 다음 항목 |
| `inline` | 없음 | — |

**반환값 수집.**

| 조건 | 경로 |
|---|---|
| subagent + `mcp.claunch` | 하위 에이전트가 `flow_delegate`로 직접 제출. 엔진이 스키마 검증 |
| subagent, `mcp.claunch` 없음 | 하위 에이전트가 영수증 파일(`receipts/<id>.json`)을 쓰고, 드라이버가 `flow_delegate`로 그 경로를 제출. 엔진이 파일을 읽어 검증 |
| session | 자식 세션이 `flow_delegate`로 제출(자식이 claunch 세션이므로 MCP가 있다). 없으면 영수증 파일 |

**효과 제한(`effects`가 쓰기를 포함하지 않는 위임, 즉 읽기 전용).**

| 실행 주체 | 1순위 | 2순위 | 3순위 |
|---|---|---|---|
| subagent | `subagent.tools.restrict` | `subagent.worktree` | 브리프에 읽기 전용 규칙을 적고, 끝난 뒤 `git status --porcelain`이 위임 전과 같은지 검사. 다르면 `monitor_violation` |
| session | 자식 세션의 worktree | — | — |

**쓰기 효과가 있는 병렬 위임.**

| 조건 | 경로 |
|---|---|
| 분기들의 `paths`가 서로 겹치지 않음(정적 검사) | 그대로 병렬 |
| 겹치거나 `paths` 미선언, 실행 주체가 worktree 격리 가능(subagent: `subagent.worktree`, session: 항상) | 분기마다 worktree에서 실행하고 결과 브랜치를 순서대로 머지 |
| 격리 불가 | 직렬 실행(`settle` 의미는 유지) |

**병렬 실행 수.**

| 조건 | 경로 |
|---|---|
| session | 데몬이 분기마다 자식 세션을 띄운다 |
| subagent + `subagent.parallel` | 드라이버에게 한 번에 여러 개를 띄우라고 지시 |
| subagent, 병렬 없음 | 직렬. `race`는 선언 순서대로 실행해 첫 성공에서 멈춘다 |

**예산.**

| 조건 | 경로 |
|---|---|
| `budget.seconds` | 모든 실행 주체에서 데몬이 벽시계 시간으로 강제한다(드라이버에게 중단 통지 후 `timeout` 결과) |
| `budget.usd` | 비용 상한을 강제하는 실행 주체가 없다. 강제하지 않고 `fallback` 이벤트에 「비용 상한 미강제」를 남긴다. 노드에 `strict_budget: true`가 있으면 그 실행 주체를 건너뛴다 |

**재시도 이어하기.**

| 조건 | 경로 |
|---|---|
| 모든 경우 | 새 실행에 이전 결과의 요약(`carry`)만 실어 보낸다. `carry: resume`은 끝난 실행을 재개할 수단이 없어 요약으로 내려가고 `fallback`을 남긴다 |

### 6.5 데몬 실행기의 수명

데몬의 cflow2 clock(`daemon/cflow2_clock.py`)과 실행기(`daemon/cflow2_exec.py`)가
지키는 규칙이다.

**session 자식.**

- spawn 호출 전에 위임을 `running` + `runner.phase: spawning`으로 저장한다. 오프닝
  태스크 끝줄에 표식 `[cflow2-delegation <scope>/<run_id>/<did>]`을 넣는다.
- 자식 이름을 받으면 `child`와 `assignee`(제출할 수 있는 세션)를 기록한다.
- spawn 호출이 실패(타임아웃 포함)하면 먼저 부모의 자식 목록에서 표식을 가진
  세션을 찾는다. 있으면 그 자식을 채택하고, 없으면 다음 실행 주체로 넘긴다. 넘긴
  뒤 `LOST_WATCH`(900초) 동안 표식을 가진 자식이 나타나면 부모-자식 kill 경로
  (`POST /api/sessions/<parent>/children/<child>/kill`)로 종료하고 `orphan_child`
  이벤트를 남긴다. 같은 브리프로 두 곳에서 작업이 진행되는 것을 막는 쪽을 택했다.
- 부팅 시 자식 기록이 없는 `running` 위임: 표식을 가진 자식이 있으면 채택하고,
  없으면 `queued`로 한 번 되돌린다(`respawns`). 두 번째에는 다음 실행 주체로 넘긴다.
- 영수증 파일(`receipts/<did>.child.json`): MCP 없이 실행되는 평탄한 `session`
  위임에만 쓴다. spawn 직전에 위임마다 키를 만들어 자식의 오프닝 태스크에만 적고,
  위임 기록에는 그 SHA-256 해시(`runner.receipt_key`)만 남긴다. 영수증은 `key`
  필드가 그 해시와 맞을 때만 제출된다. 키가 없거나 다른 파일, 그리고 키를 받지 않는
  위임(중첩 런, MCP로 제출하는 자식)의 파일은 읽지 않고 `<did>.refused.<ms>.json`으로
  옮긴 뒤 `receipt_refused` 이벤트와 자식 통지를 남긴다. 부모 드라이버도
  `receipts/`에 쓸 수 있으므로 이 키가 자식의 결과임을 구분한다. 부모가 자식의
  오프닝 태스크 원문을 읽을 수 있는 경로가 남아 있으면 이 구분은 성립하지 않는다.
  자식은 `.tmp`에 쓴 뒤 이름을 바꾼다.
  clock은 `outcome`을 가진 완전한 JSON일 때, 또는 `RECEIPT_SETTLE`(30초) 동안
  바뀌지 않았을 때만 읽는다. 읽기 전에 다른 이름으로 옮기고, 옮기기에 실패하면
  제출하지 않고 다음 tick에 다시 본다.

**heartbeat.** 한 패스가 probe(최대 60초)나 spawn 호출(최대 120초)에서 오래
멈춰도 `HEARTBEAT_STALE`(90초)를 넘기지 않도록, 패스 중에도 20초마다 다시 쓴다.
패스가 `PROGRESS_MAX`(600초) 동안 진척이 없으면 쓰기를 멈춰 다른 프로세스가
fallback하게 한다.

**rounds·restarts·wake-up** (claunch-yq80p.6).

| 상황 | 처리 |
|---|---|
| 끝난 런에 `next_round.auto` | `runtime.start_next_round`로 다음 회차 시작, 드라이버에 「next round started」 블록(2.1 「recur」) |
| 활성 작업에 `restart` | `runtime.claim_restarts`(boot id)로 가져가고 「restart」 블록. 패스 뒤 별도 작업에서 실행, `runtime.complete_restart`로 기록하고 「restart result」 블록(2.2 「restart」). 스레드에서 실행하므로 다음 패스는 기다리지 않는다 |
| 패스의 시간 틱이 런을 옮김(`wait_tick`의 `ticked` > 0) | 런이 이제 드라이버의 작업이나 에이전트 선택(보류 아님)을 기다리면 드라이버에 「the run moved」 블록. wait 통과·시간 초과, 선택 창 확정이 여기에 든다 |

블록은 런의 드라이버 세션(없으면 scope 이름의 세션)에 `queue_delivery`로 보낸다.

**probe.** wait·monitor probe는 적힌 명령 그대로 자기 프로세스 그룹에서 실행하고,
타임아웃이면 그룹 전체를 죽인다(종료 코드 124). 런의 값은 명령 문자열에 넣지 않고
환경 변수로만 전달한다(`CFLOW_IN_<NAME>`/`CFLOW_DATA_<NAME>`, 런타임이 정함).
종료 코드의 뜻(0 성립, 1 아직, 그 밖 판정 불가)과 판정 불가 횟수 세기는 런타임이
정한다.

## 7. 위임 실행

- 위임 지점은 `executor`가 붙은 노드다. 그 하위 트리 전체가 실행 주체 하나에서
  진행된다.
- 브리프: 노드의 렌더된 프롬프트 + 명시적으로 넘긴 값(`with`) + 반환 형식 +
  적용 규칙. 메인 런의 대화 맥락은 넘기지 않는다.
- 결과: `returns` 스키마의 값 + 영수증 경로. 메인 드라이버에게는 이 둘만 보인다.
- 엔진은 `spec.ensures`를 영수증에 대해 평가한다(8절).
- 하위 트리의 모양에 따라 실행 방식이 둘로 나뉜다(`ir.subtree_is_flat`).

  | 하위 트리 | 실행 방식 | 실행 주체 |
  |---|---|---|
  | 평탄함(task·seq만, 루트 아래에 `on`·`bind`·`ensures` 없음) | 브리프 하나, 결과 제출 하나. `ok` 결과는 하위 작업의 verify가 통과해야 받아들인다(2.2) | `session`, `subagent` |
  | 그 밖(loop, cond, choose(agent), par, try, on, ensures 등) | 중첩 위임 런 | `session`, `subagent`(`mcp.claunch`가 있을 때) |
  | `chooser: user` 선택이나 `ask`가 있음 | 위임하지 않음. 그 자리에서 실행하고 `fallback`을 남긴다(정적 검사 오류이기도 하다) | `inline` |

- 중첩 위임 런: 엔진이 같은 디렉터리에 scope `<scope>~<위임 id>`로 새 런을 시작한다.
  차트는 위임 노드의 하위 트리다(루트의 `executor`·`on`·`bind`·루트 대상 `ensures`는
  상위 런이 적용하므로 빠진다. 모니터는 상위 런만 돈다). 규칙, 하위 트리 대상
  `ensures`, `max_visits`, 위임 지점의 params, 상위 런 `data`의 사본을 가져간다.
  담당 세션이 그 scope를 지정해 `flow_status`·`flow_report`·`flow_select` 등으로
  진행한다(브리프의 `nested`와 `submit` 안내). 작업 보고는 각자 verify를 거친다.
  중첩 런이 끝나면 루트의 결과·값이 위임 결과가 되고 `returns`로 검증된다(중첩 런은
  다시 실행할 수 없으므로 잘못된 결과는 바로 노드 `fail`). 중첩 런이 abort되면 위임은
  `fail`로 끝난다. 상위 런에서 위임이 취소되면(취소 경로 4.2) 중첩 런도 abort된다.
  결과 전달은 중첩 런을 끝낸 호출이 잠금을 놓은 뒤 하고, 놓친 경우 상위 런의
  `flow_status`가 다시 전달한다.
- `subagent`: 드라이버가 `flow_status`에서 브리프를 받아 하네스의 하위 에이전트
  기능으로 실행하고 결과를 `flow_delegate`로 제출한다(중첩 런이면 하위 에이전트가
  그 런을 진행한다).
- `session`: 데몬이 자식 세션을 spawn하고 브리프를 오프닝 태스크로 준다. 자식
  세션은 상위 런의 `scope`·`cwd`를 지정해 `flow_delegate`로 제출하거나(평탄),
  중첩 런을 진행한다. 다른 실행 주체로 재배정되면 `assignee`가 지워져 이전 자식의
  제출은 거절된다. 자식의 `$CLAUNCH_SESSION`은 상위 런의 scope와 다르므로, 런의
  기록(저널, 커밋, 회차 보고서)을 읽거나 쓰는 명령은 그 scope를 명시해야 한다
  (`-t`/`--session <scope>`). 오프닝 태스크(`executors.session_task`)가 이것을
  안내한다.

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

정적 검사(`cflow2/check.py`)의 코드:

| 코드 | 수준 | 내용 |
|---|---|---|
| `unbounded-loop` | 오류 | `max`가 없는 반복 |
| `never-effect` | 오류 | 불변식이나 규칙 `effect`가 금지한 효과를 선언한 작업 |
| `must-pass` | 오류 | `to`에 닿는 경로 중 `must_pass` 노드를 지나지 않는 것이 있음(메시지에 경로) |
| `no-end` | 오류 | 완료에 닿는 경로가 없음 |
| `unknown-capability`, `unknown-rule` | 오류 | 모르는 capability·규칙 이름 |
| `delegated-ask` | 오류 | `ask`가 있는 작업이 위임 실행 단위(비-inline `executor`를 가진 노드나 그 조상의 하위 트리) 안에 있음 |
| `delegated-user-gate` | 오류 | `chooser: user`인 `choose`가 위임 실행 단위 안에 있음. `delegated-ask`와 같은 규칙(사람을 기다리는 게이트는 위임 안에 둘 수 없다 — 위임받은 쪽이 답하거나 건너뛴다)이며, 기존 코드를 유지하려고 두 코드로 나눴다 |
| `par-write-overlap` | 경고 | 병렬 분기가 겹치는(또는 선언되지 않은) 경로에 씀 |
| `effects-undeclared` | 경고 | `never_effect` 불변식이나 규칙 `effect`가 있는 차트에서 `effects` 키가 없는 작업. 그 작업은 `never-effect` 검사에 보이지 않는다. 효과가 없으면 `effects: []`로 선언한다 |

`must-pass`·`no-end`가 쓰는 제어 흐름 그래프는 런타임의 결과 전파를 따른다:

- 원자 노드의 가능한 결과는 `returns`의 키, `ask`의 결과(옵션의 `outcome` 또는
  `on_decline`), `fail`, `timeout`이다. probe가 있는 wait는 `undecidable`이 더해진다.
- `ok`가 아닌 결과는 노드 자신과 조상의 `on` 처리기, 감싸는 `try`의 `catch`
  (`*` 포함) 중 가장 가까운 것으로 간다. 처리하는 곳이 없으면 런이 끝나며, 차트의
  `returns`가 그 결과를 선언할 때만 완료로 본다. `retry`는 다시 시도하고, `par`는
  `settle`에 따라 정한다.
- 결과 전파와 제어(`end`, `return`, `break`, `continue`)는 빠져나가는 모든
  `finally`를 먼저 지난다. 그래프는 그 경로마다 `finally` 하위 트리의 사본을 두어
  `finally`가 끝난 뒤 원래 목적지로만 이어지게 한다(정상 경로가 제어의 목적지로
  새지 않는다).
- `else`가 없는 `cond`에서 참인 가드가 없으면 `fail`로 끝난다.

## 9. 화면과 CLI

- CLI: `claunch cflow2 ls | show | validate | compile | caps | start | status | journal | abort | archive`,
  사람의 경로 `select | extend | resume | approve [NODE] [--decline | --option O] [--reason R]`,
  `move <노드> [--reason R]`, `move --approve | --deny [--reason R]`(드라이버의 이동 요청에
  답함), `report <작업> <결과> [--value JSON] [--reason R]`(2.11),
  `recur [--cancel] [--reason R]`(다음 회차 요청 확인·취소, 2.1 「recur」).
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
    (컴파일 트리, 검사 결과, 시작 입력 선언, 읽기 전용. `name`은 정의 이름만 받고
    경로면 400), `POST /api/cflow2/select`(사용자 선택, `caller=user`),
    `POST /api/cflow2/extend`(`loop_limit` 응답: `by`·`reset`·`stop`, 본문에
    `visits: true`면 `visit_limit` 응답이며 `reset`은 400), `POST /api/cflow2/nudge | abort | archive | report | move`
    (사람의 조작, 2.11). v2 런만 있는 슬롯에서 `GET /api/cflow/run`은 `v2: true`를
    싣는다. 런 상세의 `chart.nodes[*].force_target`은 강제 이동 대상 표시다.
  - `GET /api/cflow2/node?cwd&scope&node=<id>`(`daemon/cflow2_web.node_detail`): 런
    차트의 노드 하나. 정의가 준 필드 전부(작업의 `prompt`, `done_when`, `returns`,
    `rules`(본문 포함), `verify`, `ask`, 선택지의 `options`와 guard 등), 유형별 동작 설명
    (`how`, `NODE_HOW`), 자식과 역할, 살아 있는 인스턴스, 방문 수, 마지막 결과, 저널
    꼬리 중 그 노드의 줄. 런이나 노드가 없으면 404.
  - `GET /api/cflow/definitions`의 v2 목록은 레이어 디렉터리의 파일 목록·수정
    시각으로 만든 스탬프로 캐시하며, 이벤트 루프 밖 스레드에서 컴파일한다.
  - 요청이 v2를 명시하지 않았는데(`workflow_version` 없음, `v2:` 접두어 없음, 생성
    파일 경로 아님) v2 레이어를 읽는 중 예외가 나면 데몬은 경고를 남기고 v1 요청으로
    처리한다. v2 레이어의 문제가 v1 spawn을 거절시키지 않는다.
  - v2 부모 세션의 자식은 부모 런의 `chart.json`의 `meta.default_child_cflow`를
    물려받는다.
- 새 세션 폼과 spawn 모달(같은 폼, `index.html`의 `#new-session`): 워크플로 선택
  목록에 버전 표시를 붙이고, v1 항목에는 deprecated 표시를 붙인다. v2를 고르면
  데몬이 v2 런으로 시작한다. 고른 v2 항목이 `inputs`를 선언하면 Workflow 행 아래에
  입력 칸을 그린다(2.6). 워크플로를 바꾸면 칸을 비우고 다시 그린다. 400 응답의
  `errors`는 해당 칸 아래에 표시한다.
- 실행 패널: v2 런이면 머리(링크와 런 조작 버튼, 이름, 상태, 런 정보) 아래에 두
  열을 둔다. 왼쪽은 사람의 응답을 기다리는 카드(선택, 질문, 반복 상한), 진행 중인
  작업, 에이전트의 보고(저널의 `reported`, 최신 것이 펼쳐진 markdown), 위임 표,
  컬렉션, 트리거, 저널이다. 오른쪽은 차트다. 너비가 980px보다 좁으면(분할 창, 세션
  접이식 영역) 한 열이다. 차트는 안내선, 노드 유형 칩, 역할 태그(`do`, `option x`,
  `catch fail`)를 가진 접는 트리이고, 루트와 살아 있는 노드·선택한 노드의 조상이
  펼쳐진다. 「Expand all」과 「Collapse」로 전체를 펼치거나 처음 상태로 돌린다.
  질문 카드의 선택지 표는 선택지마다 카드로 그린다(짧은 열은 태그, 긴 열은
  markdown). 권장안은 텍스트가 아닌 data 값(`data-value`, `data-recommended`)으로
  싣고, 그 값으로 권장 라벨 카드와 버튼의 「권장」 배지를 그린다. 위임 실행 단위는
  실행 주체, 상태, 영수증을 표시한다. 그 밖에 런의 시작 입력
  (`data.inputs`, 접힌 블록), 완료 기준 확인 상태, `verifying` 중인 작업과 그
  명령, `visit_limit` 대기(「Allow one more visit」/「Stop here」 버튼), probe가
  연속으로 판정하지 못한 횟수와 `undecidable`로 끝난 wait를 표시한다.
- v2 run 페이지의 조작(v1 페이지의 대응 기능): 머리의 왼쪽에 「← All runs」 링크,
  소유 세션 칩, attach 링크, 오른쪽에 Nudge·Abort(살아 있는 런)와 Archive(끝난 런)
  버튼(분할 창과 세션 접이식 영역에는 「Open the run page」 버튼도). 세션 접이식
  영역에는 Abort·Archive를 두지 않는다(v1과 같음). 활성 작업 카드마다 결과 선택
  상자, reason 칸, 「Force outcome」 버튼이 한 줄에 있다. 선택·질문 카드의 답 버튼도
  reason 칸과 한 줄이다. reason은 언제나 생략할 수 있다(ask의 `require_reason` 항목 참조).
  입력 중인 칸이 있으면 폴링 재그리기를 건너뛰고, 입력한 값은 화면별로 기억한다.
- 노드 패널: 트리의 행을 누르면 화면 오른쪽 가장자리에 패널이 열린다(× 버튼 또는
  Esc로 닫고, 같은 행을 다시 누르면 닫힌다). 패널은 `GET /api/cflow2/node`를 읽어
  유형의 동작 설명, 「Force run to …」(`force_target` 노드에서만), 상태(id, 방문 수,
  살아 있는 인스턴스, 정의 위치), 프롬프트와 완료 기준(markdown), returns, effects,
  규칙, 선택지와 guard, 그 밖의 정의 필드, 자식 목록(누르면 그 노드로 이동), 마지막
  결과, 그 노드의 저널 줄을 보여 준다. `force_target` 노드의 행에는 ⇥ 표시가 붙는다.
  선택·펼침 상태는 화면(page, split, fold)마다 따로 기억해 폴링 재그리기 뒤에도
  유지한다.

## 10. v1 deprecation

- 정의 교체와 진행 중인 런: v2 정의가 바뀌면(예: claunch-yq80p.11의 `cases`·`pr-or-local`
  교체) 노드 id가 달라질 수 있어, 이전 정의의 노드(예: 이전 ask 노드)에서 대기 중인 런은
  새 정의로 이어지지 않을 수 있다. 정의 교체 뒤 진행 중인 런은 재시작한다.
- `cflow ls`·`cflow show`, 정의 편집기, 목록 API에 v1 정의가 deprecated라는 표시를
  붙인다. 실행은 계속 지원한다.
- v1 정의 파일 자체의 `Workflow.deprecations`에는 넣지 않는다(번들 파일의
  deprecation을 금지하는 `tests/test_cflow_layers.py`의 검사는 v1 문법 내부의
  옛 표기를 잡는 용도이므로 그대로 둔다). 형식 자체의 deprecation은 별도 필드
  `format_deprecated`로 싣는다.


### v1 연결 (착지 큐)

- v2 워커 → v1 부모: `enqueue-landing`이나 `push: {to: parent:landing}`의 대상이
  v1 런(`landing_queue:` 선언)이면 데몬이 v1 `engine.enqueue_landing`을 부른다. 레코드
  필드 `issue`, `branch`, `tip`, `requested_by`가 v1 항목의 같은 이름 필드가 된다.
- v1 워커 → v2 부모: v1 `TriggerClock._enqueue_landing`은 부모 세션에 v1 큐 런이 없고
  v2 `landing` 런이 있으면 `runtime.collection_op("push", "landing", principal="child")`로
  넣는다. v2 런이 여럿이면 메인 런이 먼저이고, 그다음은 순서에서 처음 것이다.
- v1 워커의 부모가 병합을 수행할 수 없으면 v1 `TriggerClock.landing_target`이 2.8의
  「착지 요청의 대상」과 같은 규칙(`landing_route.Router.stacked`)으로 직계 조상, 그다음
  형제이거나 직계 조상인 mesh leader의 v1 큐 런을 찾는다. 이 경로는 조부모 이상의
  v2 `landing` 런은 보지 않는다.
- MCP `landing_queue` 도구는 호출 세션의 v1 런에 `landing_queue`가 없고 v2 런이
  `landing` 컬렉션을 가지면 그 컬렉션을 보여 주고 바꾼다(`status` 값은 v1과 같다).
- 한 세션에서 v1 `landing_queue`와 v2 `landing` 컬렉션을 동시에 가진 런이 있으면
  v2 착지 요청은 같은 선택 규칙(메인 런 먼저, 그다음 순서에서 처음 것)으로 하나를
  고르고, v1 워커의 요청은 v1 큐 런으로 간다.

## 10a. PR 착지 (improv-worker-v2, claunch-yq80p.5)

v2 워커의 착지 요청은 기본으로 pull request를 거친다. 로컬 착지(상위가 워커
브랜치를 `--no-ff`로 머지)도 시작 입력으로 고를 수 있는 경로로 남는다.

| 입력 | 착지 매체 | PR을 만들 수 없을 때 |
|---|---|---|
| 둘 다 끔(기본) | PR | 사유를 이슈 코멘트(`LANDING MEDIUM: local (…)`)에 남기고 로컬로 대체 |
| `force_pr` | PR | 대체하지 않는다. 사람의 선택(`retry` / 이번 회차만 `local`)을 기다린다 |
| `force_no_pr` | 로컬 | 해당 없음 |

두 입력은 `input_constraints`의 `not ($inputs.force_pr and $inputs.force_no_pr)`로
함께 켤 수 없고, `force_pr`은 `precheck: gh`다(2.6).

- 판정 시점: 회차의 `branch-setup` 뒤 프로시저 `landing-medium`(작업
  `remote-setup`). 사실은 `claunch cflow2 pr-preflight`가 낸다 — `prflow.preview`
  (세션 패널 PR 마법사와 같은 확인: 저장소, 커밋, GitHub 호스트의 원격, gh 설치,
  그 호스트에 로그인)를 JSON 한 줄(`ready`, `remote`, `repo`, `base`, `blockers` …)로
  출력하고 종료 코드 0이 ready, 1이 아님이다. 원격은 `git config claunch.pr.remote`,
  없으면 `origin`이고 base는 `claunch.pr.base`, 없으면 master다. `--base <브랜치>`는
  base를 바꾸고 그 브랜치가 원격에 있는지 `git ls-remote`로 재어 `base_sha`를
  싣는다(없으면 blocker) — 스택 자식이 쓴다(아래 「stacked PR」).
- 원격 편성(상위 세션이 다른 머신)에서는 로컬 착지가 성립하지 않으므로 PR을 만들 수
  없으면 `force_pr`과 같이 사람의 선택을 기다린다. 머신을 넘는 커밋 참조는
  `<sha> @ <remote>/<ref>`로 쓰고(docs/remote-commit-protocol.md), rebase 기준은
  `<remote>/<base>`(브랜치 upstream)다.
- 결과는 `data.landing`(`medium`, `reason`, `remote`, `repo`, `base`, `pr_url`,
  `pr_number`)에 묶인다.
- 게시: 동료 리뷰 통과 뒤 `medium == 'pr'`이면 프로시저 `pr-publish`(작업
  `pr-open`)가 명시 refspec으로 push하고 `git ls-remote`로 원격 SHA를 확인한 뒤 PR을
  열거나(이미 같은 브랜치의 OPEN PR이 있으면 갱신만) 결과를 `data.landing`에 다시
  묶는다. 실패하면 `force_pr`이 꺼져 있을 때는 사유를 남기고 로컬로 바꾸고, 켜져
  있으면 사람의 선택을 기다린다.
- PR 모음: 런의 `prs` 컬렉션(queue, key `number`, 상태 open/merged/closed,
  `inherit`)이 이 세션이 연 PR을 회차를 넘어 기록한다. `pr-open`의 leave `do:`가
  넣고, watch가 `gh pr view`로 MERGED를 재서 상태를 옮긴다. 재작업·후속 수정은 같은
  브랜치의 열린 PR에 push로 모이고, 다른 브랜치의 연관 작업은 새 PR 본문 첫 줄의
  `Follow-up of #N`과 이전 PR의 링크 코멘트로 이어진다.
- 착지 요청: `integration-request`의 leave push가 착지 큐 레코드에 `medium`,
  `pr_url`, `pr_number`, `base`를 싣는다(`$data.landing`, 2.8). `improv-common`의
  `landing-queue` 타입이 이 필드를 선언한다. `pr_url`이 있는 행은 리더의 PR 후보
  (`gh pr merge --merge --match-head-commit <tip>`, claunch-yq80p.6)이고 없으면 로컬
  후보다. 마커는 `LANDING REQUEST @ <sha> @ <remote>/<브랜치> pr: <url>`이다.
- 착지 판정: 착지 대기 프로브는 `tools/landed_check.py --landing`이다(값 없이 주면
  `CFLOW_DATA_LANDING`을 읽는다).
  기록의 `medium`이 `pr`이면 `--target <remote>/<base> --fetch`와 같은 질문이 된다 —
  리더가 원격 base에서 `gh pr merge`로 만든 머지는 fetch 전에는 이 저장소에 없다.
  `pr`인데 `remote`·`base`가 없으면 판정 불가(2)다. 그 밖의 매체와 기록이 없을 때는
  로컬 질문(머지 부모 검사) 그대로다(claunch-yq80p.4). v1 `improv-worker-remote`는
  이 경로로 대체되었다.
- 웹 UI: 런 패널의 컬렉션 표는 값이 http(s) URL인 필드를 텍스트에서 빼고 새 탭
  링크로 보인다. `url`·`pr_url`이 `/pull/<N>` 형태면 링크 이름은 `PR #N`(번호는
  같은 행의 `number`·`pr_number`가 우선)이고, 그 밖의 URL은 필드 이름이다. 그래서
  워커 런의 `prs`와 리더 런의 `landing` 행에서 PR로 바로 이동한다. 상태 열은
  `prs`의 open/merged/closed를 색으로 구분한다. v1 리더의 `landing_queue:`는 PR
  필드를 싣지 않으므로 그 경우 PR 주소는 착지 요청 마커에서만 읽힌다.

### 자식 작업의 관계: stacked / independent (claunch-yq80p.9)

워커가 자식 워커를 둘 때, 자식 런의 시작 입력 `relation`(improv-worker-v2, enum
`stacked` | `independent`, 기본 `stacked`)이 그 자식의 기준 브랜치와 착지 경로를
정한다. 관계는 spawn 필드가 아니다. spawn과 웹 폼이 이미 전달하고 검사하는
`cflow_inputs`로 준다(`cflow_inputs: {relation: independent}`).

| 항목 | stacked (기본) | independent |
|---|---|---|
| 기준 브랜치 | 부모의 스택 브랜치(`<부모>-stack`, 부모가 리더면 master) | 대상 브랜치(master) |
| 착지 요청 | `parent:landing` — 부모 워커의 stack 런(부모가 리더면 리더 런). 부모가 병합할 수 없으면 직계 조상, 그다음 mesh leader(2.8) | `leader:landing` — mesh leader(형제 또는 직계 조상)의 착지 큐(2.8) |
| PR base | 부모의 원격 스택 브랜치(아래 「stacked PR」) | master |
| 부모의 역할 | 리뷰, 기준 이동 통지, 컷 | 상태 관제(정지·실패·완료 확인) |
| 부모 기준이 바뀔 때 | stack 런이 새 tip과 `rebase --onto` 명령을 통지(아래) | 영향 없음 |
| 보드 | 부모 회차 이슈의 자식(`--parent`), 회차 이슈가 자식 이슈에 blocks | `--parent` 없이, related |

- 판단 기준(부모 워커의 work 문구): 자식이 아직 master에 없는 코드(부모의 회차
  브랜치나 스택 브랜치에만 있는 것)를 필요로 하면 stacked, 아니면 independent. 고른
  관계와 근거는 자식 이슈의 `RELATION: <관계> — <근거>` 코멘트로 남긴다.
- 착지 요청: `integration-request`의 leave push trigger 둘이 `when`으로 갈린다(2.8).
  independent 자식은 리더에게 nudge하고 부모 워커에게는 fyi만 보낸다.
- 착지 여부 질문: 워커의 기계 확인이 깨져 `landing`이 escalate하면, 묻는 상대는 착지
  요청을 받을 세션이다(claunch-yq80p.12). stacked는 `landing-review`(응답자: 조상 워커,
  조상 리더, 형제 리더 순), independent는 `landing-review-leader`(조상 리더, 형제 리더
  순)다. 두 노드는 질문 문구·선택지·작업 프롬프트를 YAML 앵커로 공유하고 `from`만
  다르다. 30분 안에 답이 없으면 둘 다 사람 앞에 선다.
- stack 런(stack-v2)은 stacked 자식만 다룬다. 표에 올리기 전에 자식 런의 입력
  (`claunch cflow2 status -t <자식> --json`의 `inputs.relation`)을 읽는다. independent
  자식만 두면 stack 런을 세우지 않는다.
- 기준 이동 통지: 메인 런의 `rebase`가 회차 브랜치를 다시 썼으면 보고의 `old_tip`에
  옛 tip을 싣고, 엔진이 `signals`에 `rebased`(tip = 새 tip, from = 옛 tip)를 쓴다
  (`old_tip`이 없거나 tip과 같으면 쓰지 않는다). stack 런의 standby는 그 신호로
  `rebase-notice`를 고를 수 있다. 그 작업이 스택 브랜치를 「스택 기준점」 뒤부터
  `--rebase-merges --onto <새 회차 tip>`으로 옮기고, stacked 자식에게
  `git rebase --onto <부모>-stack <옛 스택 tip>`을 한 번의 batch로 알린다(앞선 통지를
  적용하지 않은 자식은 가장 오래된 통지의 옛 tip을 쓴다). 그 신호가 새 것인 동안
  `cut`은 막힌다. 착지 뒤 `restack`은 스택을 master 위로 옮기므로 그 전의 `rebased`도
  소비한다. 충돌·원격 거절은 `restack-conflict`(ask-user)로 가고, resolved면 사람이
  옮긴 기준을 `stack-based`가 기록한 뒤 같은 신호들을 소비한다.
- 스택 기준점: stack 런 자기 `signals`에서 이름이 `cut` 또는 `based`인 마지막 레코드의
  tip이다. `open`(입력 base), `cut`(컷 tip), `rebase-notice`·`restack`(옮긴 대상 tip,
  보고의 `onto`), `stack-based`가 쓴다. `restack`과 `rebase-notice`의 upstream이 이
  값이다. 마지막 컷 tip을 그대로 쓰면 스택을 한 번 옮긴 뒤 그 컷 tip이 스택 브랜치의
  조상이 아니어서 옮긴 커밋을 다시 적용한다(claunch-yq80p.9 리뷰 B1·B2).

### stacked PR (stack-v2, claunch-yq80p.2)

자식을 둔 워커(메인 런 + side 런 `stack`, 2.9)에서는 자식의 PR이 master가 아니라
부모의 스택 브랜치로 향한다.

- 원격의 스택 브랜치: 메인 런은 side 런을 세울 때 착지 매체가 pr이면 `remote`·
  `repo` 입력을 넘긴다. stack 런의 `open`이 `<세션>-stack`을 그 원격에 명시
  refspec으로 push하고 `git ls-remote`로 같은 SHA인지 확인한다. 원격에 같은
  이름이 다른 tip으로 있으면 덮지 않고 원격 스택 없이 진행한다. `remote`가 없으면
  스택은 로컬에만 있다.
- 자식의 base: 자식 워커의 `remote-setup`은 intake가 정한 스택 기준을
  `pr-preflight --base <부모 세션>-stack`으로 준다. 그 브랜치가 원격에 없으면
  blocker(`… is not on the remote`)이고, 판정 규칙대로 로컬 착지로 간다(부모의
  stack 런이 자식 브랜치를 `--no-ff`로 머지한다). 있으면 출력의 `base_sha`가 그
  원격 tip이고, PR의 base와 브랜치 upstream이 `<remote>/<부모 세션>-stack`이 된다.
- 자식의 착지 요청: 자식의 `integration-request` leave push가 부모 세션에서
  landing을 선언한 런, 즉 stack 런의 `landing`에 `pr_url`·`pr_number`·`base`를
  싣는다(2.8). 그 런의 `land`가 PR 후보를 리더와 같은 절차로 머지한다: 원격 스택
  브랜치가 로컬 tip과 같은지 확인, `gh pr view`(UNKNOWN이면 재조회),
  `gh pr merge --merge --match-head-commit <tip>`, `git fetch` 뒤 스택 워크트리에서
  `--ff-only`, 머지 커밋의 둘째 부모가 레코드 tip인지 확인. 레코드의 `base`가 스택
  브랜치가 아니면 머지하지 않는다. 로컬 후보를 머지했으면 원격 스택 브랜치를
  fast-forward push로 맞춘다. 어느 경로든 원격 스택 브랜치는 land 뒤 로컬과 같은
  tip이고, landing 레코드의 landed 표시는 로컬 브랜치를 보는 watch가 한다.
- 컷과 이전: `cut`은 원격 스택 브랜치가 컷 tip과 같은지 확인하고 코멘트에
  `<컷 tip> @ <remote>/<세션>-stack`으로 적는다. 메인 런이 master에 착지한 뒤의
  `restack`만 원격 스택 브랜치를 `--force-with-lease=<세션>-stack:<옛 tip>`으로
  다시 쓴다. 열린 자식 PR은 base 이름이 같으므로 그대로 남는다.
- 부모 회차의 PR: 메인 런의 `stack-merge`가 컷 tip을 회차 브랜치에 머지하므로
  부모의 PR(base master)에는 자식들의 머지 커밋이 들어 있다. 리더는 그 PR 하나를
  머지한다.
- 미실측: 이 경로(스택 브랜치로 향하는 PR의 `gh pr merge`, 스택 restack 뒤 열린
  자식 PR)는 실제 원격에서 아직 재지 않았다. 아래 실측은 base가 master 대신 전용
  브랜치였다는 점에서 같은 명령을 다루지만, 스택 브랜치 재작성 뒤의 PR 상태는
  포함하지 않는다.

### 실측 (2026-10-06, 실제 원격 github.ncsoft.com/RnIAgentDev/claunch, base = 전용 브랜치 `cflow2-pr-measure`)

| 항목 | 결과 |
|---|---|
| `--match-head-commit`에 틀린 SHA | 거절(exit 1, `Head branch was modified`) |
| 리뷰 뒤 head에 새 커밋 push, 이전 SHA로 머지 | 거절(같은 문구) |
| 올바른 SHA | 머지됨. merge commit의 부모 = base tip, PR head |
| 충돌하는 PR | 직후 `mergeable` UNKNOWN, 수 초 뒤 CONFLICTING. 머지 거절(`not mergeable`) |
| 새 fetch 뒤 head가 base의 조상인가 | 참 |
| gh 상태별 종료 코드 | 정상 0, 미로그인(`auth status`) 1, 미설치 127 |
| 이미 올린 브랜치를 rewrite한 뒤 일반 push | 거절(exit 1, `non-fast-forward`) |
| `--force-with-lease`(기대값 없음), 추적 ref == 원격 | 강제 갱신됨(exit 0). 명시 refspec push도 `refs/remotes/<remote>/<브랜치>`를 갱신하므로 재요청 경로가 이것이다 |
| `--force-with-lease`, 다른 곳에서 원격이 움직여 추적 ref가 낡음 | 거절(exit 1, `stale info`) |
| 강제 push 직후 `gh pr view --json headRefOid` | 이전 SHA가 남아 있었다. 재조회 필요 |
| `gh pr create`에 없는 base | 거절(exit 1, `Base ref must be a branch`) |
| 같은 head·base로 `gh pr create` 두 번째 | 거절(exit 1, `already exists:` 다음 줄에 기존 PR url) |
| `gh pr list --head <브랜치> --state open` | 기존 PR 번호와 headRefOid를 낸다 |

실패 경로 측정은 PR #3(base `cflow2-pr-measure`, head `s83-prm-3`)에서 했고, 측정 뒤
PR은 닫고 브랜치는 지웠다.

`mergeable`이 UNKNOWN이거나 push 직후 `headRefOid`가 HEAD와 다르면 판정하지 않고 수 초 뒤 다시 조회한다.

리더 쪽 머지(`improv-leader-v2`의 `pr-merge` 절차: `gh pr review`, `gh pr merge
--merge --match-head-commit`, 머지 뒤 fetch·둘째 부모·포함 확인)는 위 표의 명령으로
이루어지지만, **leader-v2 경유 미실측**이다. 리더와 워커가 같은 gh 계정일 때
`gh pr review --approve`·`--request-changes`가 거절되는지(GitHub는 작성자의 approve를
거절한다)도 이 표에 없다. 절차는 그 거절에서 `gh pr review --comment`로 판정을
남기고 판정의 정본을 보드 코멘트로 둔다. leader-v2의 실제 첫 사용에서 확인해 이
표에 행을 더한다.

## 11. 단계

| 단계 | 내용 |
|---|---|
| 1 | IR, 표면 문법 컴파일러, 식 해석기, 정적 명세 검사, capability 합성과 fallback 계획 |
| 2 | 런타임(상태, advance, 저널), MCP 툴, CLI |
| 3 | 실행 주체: inline, subagent, session (headless는 claunch-xjbrh에서 제거) |
| 4 | 데몬 연동(대기 probe, timer, 실행 시간 모니터, session 자식), API, 웹 화면 |
| 5 | `improv-worker-v2` 정의, v1 deprecated 표시 |
