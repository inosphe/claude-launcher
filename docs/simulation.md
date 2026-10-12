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
다시 낸다(`sim_replay`). Sims 탭의 발견마다 있는 「재생」은 지금 모델에서 witness를 한 걸음씩 돌려 시작 상태, 걸음마다의 사건·
바뀐 상태·적용된 규칙·가정, 발견이 난 걸음을 보여 준다. 모델이 바뀌어 재생할 수 없으면 그 이유를 보여 준다.

## 구성

| 층 | 위치 | 하는 일 |
|---|---|---|
| 엔진 | `src/claude_launcher/sim/{expr,model,engine,rng}.py` | 식 언어(3값), 모델 검사, step·explore(Go-Explore 셀 보관소)·replay |
| 저장소 | `src/claude_launcher/sim/store.py` | 레코드·소유권·모델·라운드·발견 원장. 데몬이 유일한 기록자 |
| API | `src/claude_launcher/daemon/sim_api.py` | `/api/sims...`, 소유 세션에 알림 입력, 세션 종료 시 소유권 해제 |
| MCP | `src/claude_launcher/sim_mcp.py` | `sim_*` 도구. 바꾸는 호출은 `$CLAUNCH_SESSION`을 호출자로 보냄 |
| 화면 | `src/claude_launcher/web/static/sim-page.js` | `#/sims`(목록·새 시뮬레이션), `#/sims/<id>/<탭>`. 세션 터미널 머리줄의 「◬ sims」는 같은 화면을 터미널 아래 분할 창에 그 세션의 시뮬레이션만으로 연다 |
| 실행 세션 | `src/claude_launcher/sim/pi/{runner,campaign}.mjs` | 데몬이 띄우는 pi 세션의 확장. 형식화·판정·빈칸 해석 고리 |
| 런타임 | `src/claude_launcher/sim/runtime.py`, `sim/pi/package*.json` | 실행 세션의 Node 의존성(pi-durable 등) 설치 |

저장 위치는 `<런처 홈>/daemon/simulations/<id>/`(`sim.json`, `model.json`, `findings.json`, `rounds.jsonl`,
`journal.jsonl`, `submits.json`, 판정 기록 `verdicts.json`, 실행 세션의 durable 저장소 `durable.sqlite`와 사건 기록
`durable.events.jsonl`)이다. 세션이 어느 워크트리에 있든 같은 레코드를 읽는다.

## 세션 화면 (◬ sims)

세션 터미널 머리줄의 「⬒ run」 옆 「◬ sims」는 터미널 아래 분할 창에 그 세션이 소유한 시뮬레이션을 보여 준다.
분할 창 하나를 두 버튼이 나눠 쓴다: 켜진 버튼을 다시 누르면 창이 닫히고, 다른 버튼을 누르면 창의 내용이 바뀐다.
선택은 세션별로 기억된다(`sessLayoutFor().pane`).

- 목록은 `GET /api/sims?owner=<세션>`이다. 항목을 누르면 같은 창에서 상세 화면이 열리고, 탭·실행·중지·답하기·편집 제안
  결정을 `#/sims`와 같이 할 수 있다. 창 안의 이동은 주소(hash)를 바꾸지 않는다.
- 「새 시뮬레이션」은 그 세션을 소유자로 정해 만든다(맡길 세션 선택이 없다).
- 「전체 화면에서 열기」와 「모든 시뮬레이션」은 `#/sims` 화면으로 간다. 다른 세션의 시뮬레이션은 그쪽에서 읽는다.

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

## 목표 수립 단계의 실행 금지

- 워크플로 단계(v1)나 작업(v2)에 `sims: deny`가 있으면, 세션의 cflow 런이 그 단계에
  서 있는 동안 그 세션의 `sim_start`·`sim_round`를 거절한다(409, claunch-efnbt.9).
  에러에는 「목표 승인 후 실행 가능」과 현재 단계 이름이 들어 있고, 응답의
  `sims_denied`가 버전·워크플로·단계를 싣는다.
- 모델 작성(`sim_create`, `sim_patch`)과 조회는 허용한다. 다만 `running` 캠페인에
  패치를 넣어도 그 단계에서는 자동 라운드를 돌리지 않고 응답의 `round_skipped`에
  이유를 적는다.
- 제한하지 않는 호출자: 사용자(바디에 `session`이 없는 호출), 데몬이 띄운 실행
  세션(`<시뮬레이션 id>-run<n>`, 또는 시뮬레이션에 붙은 runner), cflow 런이 없는 세션.
- 판정은 런 자신의 스냅숏(v1 워크플로 스냅숏, v2 차트)에서 표시를 읽는다. 표시가
  생기기 전에 시작한 런은 제한되지 않는다.
- 정본 워크플로의 표시 위치: improv-worker(v1)의 intake·issue-check·issue-auto·
  issue-search·issue-decision·issue-adopt·issue-create, improv-worker-v2의 intake·
  prior-context·prior-context-fallback·intake-issue·issue-auto·issue-search·
  issue-adopt·issue-create, improv-leader와 improv-leader-v2의 intake.
- cflow에서 `sim-run` 절차를 부르는 경로: 정적 `call`은 결국 `sim_start`를 거치므로
  위 판정을 받는다. 동적 호출(claunch-z17c9)은 작업의 `calls: [...]` 허용 목록에
  없는 절차를 거절하고, `sims: deny` 작업이 `calls`에 `sim-run`을 선언하면 검사기가
  `sims-deny-call`로 거절한다.

## 캠페인과 알림

- 상태는 `idle`, `running`, `stopped`, `fault`이고 `state_reason`이 이유를 적는다. `fault`는 쓸 수 있는 해석 모델
  설정이 없어 시작이 거절된 상태다(아래 「실행 세션」). 그 시작에서는 아무것도 실행하지 않았고, 다음에 시작이 성공하면
  `running`으로 바뀐다.
- `running` 동안에는 받아들여진 모델 변경마다 라운드를 다시 돈다.
- 라운드 뒤 열린 발견이 없으면 `idle`로, `max_rounds`에 닿으면 `stopped`로 끝난다.
- 캠페인이 끝나면(위 두 경우와 중지) 데몬이 소유 세션 터미널에 `[claunch simulation] ... finished` 한 줄을
  입력한다. 소유 세션은 `sim_show`/`sim_findings`로 결과를 읽고 사용자와 논의를 이어 간다. 사용자 결정을
  기다리는 편집 제안이 있으면 그 수도 같은 줄에 적는다. 같은 줄에 목적 표시(아래 「목적」)와 임시 규칙 수,
  경고가 있으면 경고도 적는다.
- 사람 결정이 필요한 빈칸은 소유 세션이 `sim_resolve`의 `ask`로 올린다. 사용자가 Sims 탭에서 답하면 그 답이
  소유 세션에 입력되고, 세션은 출처 `user:<발견 키>`로 모델에 반영한다.
- 웹의 「새 시뮬레이션」은 레코드를 만들고 고른 세션에 생성 요청을 입력한다. 「소유 세션에 요청」은 임의의 말이나
  계속 진행 요청을 입력한다.

## 실행 세션

- 실행(`sim_start`, 화면의 「실행」)은 첫 라운드를 바로 돌리지 않고 데몬이 실행 세션을 띄운다. 그 조건은 설정의
  `interpreter_profile`이 pi로 실행할 수 있는 프로필이고 런타임이 설치되어 있는 것이다. 세션 이름은 `<시뮬레이션 id>-run<n>`, kind는
  `pi-durable`, 작업 디렉터리는 시뮬레이션 루트다.
- 실행 세션은 붙어 있는 동안 소유 세션과 같은 권한으로 모델을 바꾸고 라운드를 돌린다. 실행 세션이 붙어 있으면 모델
  변경마다 라운드를 다시 돌리지 않고, 실행 세션이 라운드를 직접 요청한다.
- 고리(`campaign.mjs`의 `runCampaign`):
  0. `stale_sources`가 있으면 B가 그 규칙을 다시 인용하거나 고치거나 뺀다.
  1. 모델이 비어 있으면 해석 모델(B, 실행 세션 프로필의 모델)이 파일 도구(`read_file`, `list_files`,
     `search_text`, 루트 밖은 거절)로 원천을 읽고 모델 전체를 제출한다. 거절되면 거절 사유를 붙여 다시 묻는다.
  2. 판정이 없는 인용 규칙·불변식을 판정 모델(C, `judge_profile`의 모델)이 원천 파일과 대조한다. `confirmed`는 기록만
     하고, `fabricated`는 모델에서 빼고, `ambiguous`는 남기고 기록한다. 규칙 본문이 바뀌면 판정을 다시 받는다.
  3. 사용자가 답한 질문을 규칙으로 옮긴다(출처 `user:<발견 키>`). 그 키를 인용한 변경이 받아들여지면 그 발견에
     `applied`가 붙는다.
  4. 라운드를 돌린다. 캠페인이 끝나면(열린 발견 0, `max_rounds`) 고리도 끝난다.
  5. 빈칸 묶음마다, 설정 `triage`가 켜져 있으면(기본값) 먼저 판정 모델이 발견을 분류한다: `model`(원천은 정하고
     있는데 모델이 잘못 읽음), `spec`(원천에 정해진 내용이 없어 사람이 결정), `intended`(결함이 아님, 그 이유로 제외).
     분류는 발견의 `triage`에 남는다. 그 다음 B가 남은 발견에 패치, `dismiss`, `ask` 중 하나로 답한다. `spec`으로 분류된
     발견에는 원천에서 근거를 찾지 못하면 질문하고, 임시 규칙을 만들지 않는다.
     경로가 임시 규칙을 지나간 발견(`provisional` 목록이 비어 있지 않음)은 다른 분류를 쓴다: `design`(설계 결함 — 규칙
     추가 필요), `intended`(의도된 제외, 그 이유로 제외), `decide`(사람 결정). 그 경로에는 잘못 읽을 원천 문장이 없기
     때문이다. B는 `design`에 규칙을 더하고(원천 문장을 찾으면 인용, 못 찾으면 임시 규칙과 이유), `decide`에는 질문한다.
     데몬(`SimStore.triage`)이 발견마다 맞는 분류 집합인지 확인하고 다른 집합의 값은 거절한다.
  6. 연속 두 번의 반복에서 받아들여진 변경이 없으면 멈춘다.
  7. 고리가 실패 없이 끝났고 설정 `max_edits`가 1 이상이면, B가 남은 질문·답한 질문에 대해 원천 편집을 제안한다
     (아래 「편집 제안」).
- 물음 하나는 durable 작업 하나이고 새 대화 하나를 쓴다. 작업 열쇠와 제출 열쇠는 물음의 대상(라운드 번호, 자리,
  시도 번호)에서 나오므로, 데몬 재기동으로 세션이 복원되면 같은 작업을 기다리고 같은 제출은 한 번만 반영된다.
- 생성 수(12), 한 요청 문맥(200000 토큰), 대화 입력 합(1000000 토큰)에 닿으면 파일 도구가 읽기를 멈춘다. 생각이 같은
  낱말을 되풀이하면 그 생성을 끊는다(gds6의 `cutLoops`).
- 판정 모델의 공급자는 실행 세션이 시작할 때 데몬(`GET /api/sims/<id>/runner`, 붙어 있는 실행 세션만 호출 가능)에서
  받아 프로세스 안에서만 등록한다. 키는 세션 정의에 저장하지 않는다. `judge_profile`이 비었거나 해석과 같으면 B 모델이
  판정도 한다.
- 고리가 끝나면 실행 세션이 `runner/done`을 보내고 종료한다. 데몬은 캠페인을 끝내고 소유 세션에 끝남 알림을 입력한다.
  실행 세션 프로세스가 어떤 이유로든 사라져도 같은 알림이 간다. 실행 세션이 붙어 있는 동안 라운드에서 캠페인이 끝나면
  알림은 그때 가지 않고 `runner/done`(또는 프로세스 종료) 때 한 번 간다. 마지막 라운드 뒤의 편집 제안이 알림보다
  먼저 기록된다(claunch-efnbt.4). 「중지」는 실행 세션을 종료한다. 소유 세션이 종료되면
  실행 세션도 종료된다.
- 진행 상황은 세션 진행 API(`stage`: formalize, judge, answers, round, gaps)로 세션 목록과 대시보드에 보인다.
- kind가 `pi-durable`인 세션의 터미널 머리줄에는 「◇ chart」 버튼이 보이고, 누르면 분할 창에 그 세션의 실행 기록을
  흐름도로 보여 준다(`web/static/durable-flow.js`, claunch-neeya, claunch-neeya.1). 「⬒ run」은 이 세션에서도 워크플로
  런을 보여 준다. 읽는 곳은 `GET /api/sessions/<세션>/durable?since=<n>`이고, 기록을 찾는 경로는 둘이다.
  - 세션 정의의 `CLAUNCH_SIM_ID`(세션이 사라졌으면 이름 `<시뮬레이션 id>-run<n>`)가 있으면 그 시뮬레이션의
    `durable.events.jsonl`에서 그 세션의 줄만 돌려준다.
  - 그것이 없고 세션 정의에 `SIM_RUN_DIR`이 있으면(gds6의 sim2 실행기) 그 디렉터리의 `<디렉터리 이름>.events.jsonl`을
    돌려준다. 응답의 `sim`은 디렉터리 이름, `run_dir`은 그 경로다. 둘 다 없으면 404다.
  `since`는 이미 받은 사건 수이고 응답은 그 뒤의 사건만 싣는다. `live`, `progress`가 함께 온다.
  시뮬레이션 전체의 기록은 `GET /api/sims/<id>/events?session=&since=`이다.
  - 열은 고리가 들어간 단계 순서다: 시작, 형식화, 재인용, 판정, 답 반영, 라운드 N, 편집 제안, 끝. 앞서 지난 단계에 다시
    들어가면 새 열이 생긴다. 열 안의 노드는 시간 순서다.
  - 물음(`ask`) 하나가 노드 하나다. 결과 사건(`formalize`, `judge`, `triage`, `gap_patch`, `edits` 등)은 새 노드를 만들지
    않고 그 물음의 노드 상태를 바꾼다(초록 성공, 빨강 실패·해석 실패, 노랑 일부 오류). 노드 위에 마우스를 올리면 시각,
    물음 열쇠, 토큰 내역이 보인다.
  - sim2 기록(`ask`에 `askRole`이 있다)은 한 실행에 물음이 수백~천 개이고 동시에 돌기 때문에 물음 하나가 노드 하나가
    아니라 단계 묶음 하나가 노드 하나다: `B:<단계>`는 열 「모델 구성」(효과, 충돌, 형식화 등), `C:triage`는 「분류」,
    `A:<N>:p<k>`·`C:r<N>:j`·`C:r<N>:f…`는 「라운드 N」(플레이, 판정, 발견 검토), `D:<cr>`는 「편집 제안」에 들어간다.
    노드에는 물음 수(`×N`)와 토큰 합이 붙고, 잘린 답(`stop`이 `stop`이 아님)이 있으면 노랑이다. 단계가 처음 연 열을
    계속 쓴다. `run_end`가 끝 노드다(done 초록, stopped 노랑 「중단」, 그 밖은 빨강 「실패」).
  - 세션이 살아 있고 `done`이 없으면 마지막 노드 뒤에 점선 「진행 중」 노드가 진행 API의 단계와 문구를 보여 준다.
  - 2초마다 뒤쪽 사건만 받아 이미 그린 SVG에 새 노드와 선을 덧붙이고, 상태가 바뀐 노드의 글자와 class만 고친다. SVG를
    다시 만들지 않으므로 스크롤 위치와 마우스 올림이 유지된다. 세션이 종료되면 폴링을 멈춘다.
- 조건이 맞지 않으면 시작은 오류다(claunch-efnbt.8). 데몬은 `sims.start`도 라운드도 실행하지 않고, 소유 세션(또는
  사용자) 권한을 확인한 뒤 상태를 `fault`로, `state_reason`을 이유로 두고 기록에 `fault`를 남긴다. 응답은 409이고
  바디의 `fault`가 원인 코드, `error`가 이유와 고칠 방법이다.

  | `fault` | 조건 | 고칠 방법 |
  |---|---|---|
  | `no-profile` | `interpreter_profile`이 비어 있음 | 설정 탭이나 `sim_configure`의 `settings.interpreter_profile`로 프로필을 고른다 |
  | `profile-unusable` | 그 프로필이 `GET /api/sims/profiles` 목록에 없음(pi로 실행할 수 없음) | pi로 실행할 수 있는 프로필을 고른다 |
  | `no-runtime` | 런타임이 없거나 최신이 아님 | `claunch sim runtime install` |

  판정 모델이 해석 모델과 같아도 시작은 허용된다. 제목·목표를 채우기 전(`fill_pending`)이면 이 검사보다 채우기
  검사가 먼저 409를 낸다. 화면은 상태 배지 「오류」와 `state_reason`을 보이고, 해석 프로필이 비어 있으면 「실행」
  옆에 안내를 둔다. 예전의 소유 세션 해석 경로(응답의 `manual`, `sim_start`의 `runner: false`)는 없어졌다.
  바디에 `runner: false`를 주면 400이다. 소유 세션이 직접 한 라운드를 걷는 일은 `sim_round`로 계속 할 수 있다.

### 런타임 설치

claunch에는 npm 단계가 없어서 실행 세션의 의존성(`@earendil-works/pi-durable`, `pi-ai`, `chord`, 1.1.0 고정)을
`sim/pi/package-lock.json`으로 고정하고 `<런처 홈>/sim-runtime`에 `npm ci`로 한 번 설치한다.

```
claunch sim runtime install      # 설치 또는 갱신 (--force: 다시 설치)
claunch sim runtime status       # 설치·최신 여부, 아니면 종료 코드 1
```

`claunch install --global`도 같은 설치를 시도하고, npm이 없으면 한 줄 안내만 남긴다. lock이 바뀌면 `status`가
`stale`을 보이고 실행 세션은 띄우지 않는다. 확장 파일(`runner.mjs`, `campaign.mjs`)은 실행 세션을 띄울 때마다
최신본으로 복사된다.

## 이어 받기 (carry)

한 시뮬레이션의 원장(모델, 발견, 판정)은 실행 사이에 그대로 남는다. 실행을 시작할 때마다 데몬이 지금의 원천과 모델로
다시 확인한다(`SimStore._carry`, 시작 응답의 `carry`).

| 대상 | 확인 | 결과 |
|---|---|---|
| 인용 규칙·불변식 | 인용이 지금 파일에 있는가 | 없으면 `stale_sources`에 올린다. 실행 세션이 먼저 다시 인용하거나 고치거나 뺀다. 걸음은 그동안에도 그 규칙을 쓴다 |
| 질문 | 질문할 때 기록한 원천 파일의 해시(`question.docs`)가 그대로인가 | 바뀌었으면 발견을 `open`으로 되돌리고 질문은 `prev_question`에 남긴다 |
| 질문 | 지금 모델이 witness로 같은 발견을 다시 내는가 | 내지 않으면 `resolved`(처리자 `carry`) |
| 열린 발견·질문 | witness가 지금 모델에서 재생되는가 | 안 되면 `stale`에 이유를 적는다 |

모델 변경의 인용 검사는 그 변경이 쓰는 규칙에만 적용된다. 인용을 잃은 옛 규칙이 다른 변경을 막지 않는다.

화면의 생성 폼에서 제목·목표를 비워 두고 「세션에 맡기기」를 누르면 지연 생성이다(`POST /api/sims`의
`fill: true`). 데몬은 제목·목표가 빈 레코드를 `fill_pending: true`로 만들고(원천·설정·이어 받기는 폼에서 고른
대로), 소유 세션에 「지금 하는 일에서 무엇을 시뮬레이션할지 정해 `sim_configure`로 제목·목표를 채우고 실행하라」는
요청을 입력한다. 제목과 목표가 모두 채워지면 표시가 지워지고, 그 전에는 `sim_start`와 `sim_round`가 409로 거절한다. 소유
세션이 없으면 400이다.

다른 시뮬레이션에서 이어 받아 만들 수도 있다(`sim_create`의 `from`, 화면의 「이어 받기」): 그 시뮬레이션의 모델, 판정,
답한 질문을 복사하고 설정은 복사하지 않는다. 첫 실행 때 위 확인이 새 루트를 기준으로 돈다.

## 편집 제안

시뮬레이션이 찾은 빈칸 중 원천이 정하지 않은 것(질문으로 남았거나 사용자가 답한 것)은 결국 원천 문서를 고쳐야
닫힌다. 설정 `max_edits`가 1 이상이면 실행 세션이 고리 끝에 그 편집을 제안한다(`POST /api/sims/<id>/proposals`,
`campaign.mjs`의 `proposeEdits`). 데몬은 원천 파일을 고치지 않는다.

- 대상(`work`의 `edit_targets`): 상태가 `question` 또는 `answered`이고, 거절되지 않은 제안이 아직 다루지 않은 발견.
  다음 두 경우는 대상에서 빠지고, 데몬이 그런 제안을 거절한다(claunch-efnbt.10).
  - 경로가 임시 규칙만으로 된 발견. 데몬이 witness를 현재 모델에 재생해 경로에서 발동한 규칙을 모으고(충돌 발견의
    두 규칙, 불변식 발견의 불변식 포함), 그 전부가 임시 규칙이면 해당한다. 원천 문장이 경로에 없으므로 원천에서 고칠
    자리가 없다. 재생할 수 없는 witness는 임시 규칙을 지났으면 해당하는 것으로 본다. 인용 규칙과 임시 규칙을 함께
    지난 경로는 대상에 남는다.
  - 고칠 원천 파일이 없는 시뮬레이션: 지정한 `sources`(glob 전개)에도, 인용 규칙이 가리키는 파일에도 루트 안의
    파일이 없다.
- 제안 하나: `{file, old, new, kind, reason, findings}`. `kind`는 `clarify`(모호한 문장을 명료하게), `complete`(빠진
  경우를 채움), `decide`(사람의 결정이 필요한 자리를 표시). `old`는 그 파일에 글자 그대로 있어야 하고, 비우면 파일
  끝에 덧붙인다.
- 데몬의 검사: 파일이 루트 안에 있고 읽을 수 있는가, `kind`와 `reason`이 있는가, `new`가 `old`와 다른가, `old`가 파일에
  있는가, `findings`가 있는 발견인가. 통과하면 편집을 적용한 본문에서 인용을 잃는 규칙을 `check.breaks`에 적는다.
  거절된 제안은 사유를 붙여 한 번 다시 묻는다.
- 결정은 사용자만 한다(화면 「편집 제안」 탭, `POST /api/sims/<id>/proposals/<pid>/decide`). 수락하면 소유 세션에
  그 편집을 적용하고 다시 실행하라는 요청이 입력된다. 다음 실행의 carry가 바뀐 원천으로 질문을 다시 확인한다.
- 세션은 `sim_proposals` 도구로 제안 목록을 읽는다.

## 실행 기록과 하위 절차 sim-run

- 실행(`start`)마다 번호가 하나 늘고 `sim.run`에 기록된다: `{seq, by, started_at, model_hash_in, runner, ended_at,
  state, reason, model_hash_out, failed}`. 끝남의 정본은 `run.ended_at`이다. 끝남 알림은 다시 확인하라는 신호다.
  - 실행 세션이 없으면 캠페인을 끝내는 라운드·중지·소유권 해제가 기록을 닫는다.
  - 실행 세션이 있으면 그 세션의 `runner/done`이나 종료가 닫는다.
  - `runner/done`은 그 세션이 받은 `run`을 함께 보내고, 지금 실행과 다르면 거절된다(늦은 보고가 새 실행을 닫지 않는다).
- `workflows/v2/sim-run.yaml`(`kind: procedure`)은 시뮬레이션 한 번을 cflow v2 절차로 정의한다: 범위 → 모델 → 실행 →
  (커버리지 미달이면 보강 후 재실행, 최대 2회) → 정리. 지금은 정적 `call: sim-run`으로 부른다.
  - 반환 `ok`는 `assessment`(consistent·gaps·inconclusive)와 그 근거인 `findings`(clear·gaps)·`adequacy`
    (adequate·inadequate), 커버리지, 미결 항목 수, 한계를 싣는다.
  - 기술적으로 실행할 수 없을 때만 `fail`, 시간 예산을 넘기면 `timeout`이다.
  - 실행 세션이 없으면 절차가 직접 빈칸에 답해 라운드를 진행한다.
  - 전수 탐색과 오류 주입 시험은 아직 없으므로 `exhaustive`·`controls`는 null이고 한계에 적힌다.
  - 기본값(도달 칸 비율 0.5, 1800초)은 검증된 기준이 아닌 제안값이다.
  - 동적 호출과 호출 허용 정책은 claunch-4avda의 범위다.

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
- 시나리오가 아니라 업무 영역을 모델로 만든다. 업무 상태와 사용자 행동은 하나의 집합을 재사용하고, 시나리오 번호는
  경로의 메타데이터(checkpoint 이름, 출처 인용)로 둔다. 시나리오마다 전용 단계·사건을 만들면 칸 지도 대부분이 빈다.
- 식·효과 형식 전체는 `sim_guide` 도구가 돌려준다.

## 칸 지도

칸은 한 상태 기계의 상태 × 그 기계를 언급하는 규칙이 받는 사건이다(`GET /api/sims/<id>/coverage`, Sims 탭의
「칸 지도」). 칸마다 두 출처를 따로 싣는다(claunch-efnbt.7).

- 정적 판정(`cells[k].rule`, `cells[k].available`: `yes`|`maybe`|`no`): 그 기계의 상태만 고정하고 나머지 기계·변수·
  `prose`는 모름으로 둔 3값 평가(`expr.eval_partial`). `rule`은 그 사건의 규칙 중 `when ∧ event.available`이 참인
  것이 있으면 `yes`, 모름이면 `maybe`. `not`·`any` 아래의 원자도 극성대로 평가하므로 `{not: {sm, is: s}}`가 `s` 칸을
  「규칙 있음」으로 만들지 않는다. 상태 조건이 `event.available`에만 있는 규칙도 셈에 들어간다. `available: no`
  칸은 「가용하지 않음」(×)이다. 표본에서 안 나왔다는 이유로는 제외하지 않는다.
- 관측(`cells[k].observed`: `{handled, unhandled}`): 마지막 라운드의 걸음이 그 칸에서 사건을 처리한 횟수와 처리하지
  못한 횟수. `run_round`가 `coverage.json`(`round`, `at`, `model_hash`, `knobs`, `cells`)에 덮어쓴다. 누적하지
  않는다. `observed_from.current`가 거짓이면(모델이 그 뒤 바뀜) 관측은 `null`로 내려가 처리 증거로 쓰이지 않는다.
  `coverage.json`이 없는 과거 실행은 `observed_from: null`로 「관측 자료 없음」을 표시하고 복원하지 않는다.
- 표시: ✓ 처리됨, ! 규칙 없음(열린 발견), ± 처리와 미처리가 모두 관측됨(변수에 따라 다름), · 마지막 라운드
  미관측(전체 실행에서 방문하지 않았다는 뜻은 아님), ? 질문·답, – 제외·해결, × 가용하지 않음. 테두리는 정적
  판정(실선 = 규칙 있음, 점선 = 조건부)이다. 한 번 처리되었다는 사실은 그 칸의 모든 변수 조합이 안전하다는
  증명이 될 수 없다.
- 분모: 전체 칸(`counts.cells`), 가용 후보(조건부 포함, `counts.candidates` = 정적 거짓만 뺀 칸), 마지막 라운드
  관측 칸(`counts.observed`).
- 기존 키 `grid`·`handled`·`gaps`는 유지한다. `handled`의 뜻은 「정적 판정 `rule: yes`」로 좁혀졌다.

## 설정

| 키 | 기본값 | 뜻 |
|---|---|---|
| `interpreter_profile` | 데몬 `simulation_interpreter_profile` | 해석(B)에 쓸 pi 프로필 |
| `judge_profile` | 데몬 `simulation_judge_profile` | 판정(C)에 쓸 pi 프로필. 비우면 해석 프로필 |
| `interpreter_model`, `judge_model` | 데몬 `simulation_interpreter_model`·`simulation_judge_model`, 비었으면 빈 값(프로필 기본 모델) | 그 프로필이 pi에 주는 모델 중 하나. 같은 프로필에서도 판정 모델을 다르게 고를 수 있다. 요청이 기본과 다른 프로필을 고르고 모델을 주지 않으면 기본 모델은 쓰지 않는다 |
| `interpreter_reasoning`, `judge_reasoning` | `low` | `none`·`low`·`medium`·`high` |
| `seed`, `budget`, `horizon` | 1, 2000, 12 | 걸음 씨앗, 라운드당 걸음 수, 셀 하나에서 이어 가는 걸음 수 |
| `max_rounds` | 20 | 한 번 실행의 라운드 상한 |
| `triage` | true | 해석 전에 판정 모델이 빈칸을 분류한다 |
| `max_edits` | 0 | 고리 끝에 만들 편집 제안의 상한(0~50). 0이면 만들지 않는다 |
| `purpose` | `spec` | `spec`(원천 확인) 또는 `design`(설계 검토 — 원천 확인 아님). 아래 「목적」 |
| `language` | `한국어` | 사용자가 읽는 글의 언어: 질문·선택지·제외 이유·분류 이유·판정 이유·편집 제안의 이유, 소유 세션에 보내는 생성·끝남 알림의 보고 언어. 편집 제안의 `new`는 그 파일의 언어로 쓴다 |

### 목적

`purpose`는 이 시뮬레이션이 원천 문서를 확인하는지(`spec`), 원천에 아직 없는 설계를 검토하는지(`design`)를 기록한다.
목적에 따라 바뀌는 것은 표시와 경고뿐이다.

- 표시: 레코드의 `purpose`(`GET /api/sims/<id>`, `sim_show`)가 `{purpose, label, rules, provisional, provisional_text,
  warnings}`를 싣는다. `label`은 「원천 확인」 또는 「설계 검토 — 원천 확인 아님」이고 `provisional_text`는 "규칙 N개
  중 M개가 원천 없음(provisional)"이다. 화면 상세 머리와 끝남 알림에 둘 다 나온다.
- 경고: `spec`인데 `sources`가 비었으면 "원천 확인용인데 원천이 없음 — sources를 지정하거나 purpose를 design으로
  바꾸세요"가 `warnings`에 들어간다. 생성 응답(`warnings`), 시작 응답의 `sim`, 화면, 생성 요청 알림, 끝남 알림에
  나온다. 생성과 시작은 거절하지 않는다. `design`이면 경고가 없다.
- 바뀌지 않는 것: 데몬의 인용 확인, 판정 모델의 인용 검사, 걸음. 이들은 목적과 관계없이 규칙 단위로 동작한다
  (`tests/test_sim_store.py::test_design_purpose_keeps_quote_checks_and_the_judge_per_rule`). 분류(위 「실행 세션」 5)와
  편집 제안 대상(「편집 제안」)도 목적이 아니라 발견마다 그 경로가 지난 규칙으로 정해진다.

화면의 프로필 선택지는 pi 하네스로 실행할 수 있는 프로필만 보여 준다(`GET /api/sims/profiles`). 판정 프로필은 그중
공급자를 pi에 넘길 수 있는 프로필(`judges`)만이다. Anthropic OAuth만 쓰는 프로필은 pi를 자체 로그인으로 실행하므로 두
번째 공급자로 등록할 수 없다. 해석과 판정이 같은 프로필·같은 모델이면 화면이 경고한다. 설정은 언제든 바꿀
수 있고 다음 라운드·실행부터 적용된다.

데몬 기본값 네 개(`simulation_interpreter_profile`, `simulation_interpreter_model`, `simulation_judge_profile`,
`simulation_judge_model`)는 Settings 페이지(General)의 「Simulation models」 카드에서 고른다(`PUT /api/sims/defaults`).
데몬은 저장할 때 프로필이 pi를 실행할 수 있는지, 모델이 그 프로필(판정 프로필이 비었으면 해석 프로필)이 주는
모델인지 확인한다. 빈 값은 설정 파일에서 키를 지운다.

## 단계

- S1(claunch-efnbt.1): 엔진, 저장소, 소유권, API, MCP, Sims 탭, 알림. 해석은 소유 세션이 직접 한다.
- S2(claunch-efnbt.2): 위 「실행 세션」 절. 데몬이 실행마다 띄우는 pi-durable 실행 세션이 형식화, 판정(C), 빈칸
  해석(B)을 물음마다 새 durable 대화로 돌린다.
- S3(claunch-efnbt.3): witness 재생 화면, carry, triage, 편집 제안.
