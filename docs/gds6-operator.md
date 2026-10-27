# gds6 operator — 분석과 설계

gds6로 만든 프로젝트의 실행을 **안내·설명·리뷰**하는 operator 세션을 claunch에 더한다.
이 문서는 (1) 그 operator가 읽는 gds6의 구조와 단계를 정리하고, (2) 왜 이 operator를
operator v2의 변형으로 만들었는지, (3) 무엇이 바뀌었는지를 적는다. designframe은 이번
범위 밖이다(사용자 지시, 2026-10-27).

## 1. gds6는 무엇인가

gds6는 기획 문서와 구현 사이의 거리를 재는 계측기다(`/home/yusanghyun/works/gds6`).
원장이 둘로 갈려 있고, 그 경계가 이 operator가 읽는 값의 출처를 정한다.

| 평면 | 소유하는 사실 | 화면 | API |
|---|---|---|---|
| 기획(plane) | 문서·문장·규칙·대상 이름·기능·시나리오·결정·작업 범위 | `/plane/#/...` | `/api/plane/*` |
| 실현(issue-gen) | 코드 실측·규칙-코드 연결·설계 일감·전달 실행·패킷·beads 이슈·구현 결정 | `/#/...` | `/api/*` |
| 서버 | 두 라우터를 한 프로세스·한 포트에 마운트 | `:8888` | 같음 |

근거: `README.md:20-40`(구조 표와 다섯 원칙 — 기획 사실은 평면이, 실현 사실은
issue-gen이 소유한다; 판단은 저장하고 파생은 저장하지 않는다; 없는 변경을 있다고
말하지 않는다; 모르는 것을 적을 자리를 만든다; LLM은 제안하고 코드가 판정한다).
방향은 한쪽이다: 실현이 기획을 인프로세스로 읽고, 기획은 실현을 부르지 않는다
(`server/src/main.ts:80-92`, `issue-gen/src/plane.ts`).

워크스페이스는 이 계기가 재는 프로젝트 하나이고, 원장·실측은
`data/workspaces/<slug>/`에 산다(`README.md`의 「워크스페이스」 절). 서버의 기본
별칭(`/api/...`)은 부트 워크스페이스의 것이고, 다른 워크스페이스는
`/w/<slug>/api/...`다(`server/src/runtime.ts:151-171`).

### 실행이 이어지는 자리 — claunch 세션

gds6는 분석·설계를 자기 LLM 호출로 끝내지 않고 claunch 세션에 맡긴다. 세션은
`POST /api/sessions`로 만들어지고 `workflow: gds-job`을 싣는다
(`shared/claunch.ts:280-307`, `shared/job-runner.ts:149`). 그 워크플로의 정본은
claunch 쪽 `src/claude_launcher/workflows/gds-job.yaml`이고, 터미널 없는 런으로
분류된다(`src/claude_launcher/daemon/beads.py:776-800`, `TERMINALLESS_WORKFLOWS`).
역할 어휘에도 그 세션을 위한 `analyst`가 있다(`src/claude_launcher/daemon/mesh_roles.py:528-539`,
별칭 `job`·`gds`·`analysis`).

그래서 gds6 프로젝트의 실행 상태는 두 곳에 나뉘어 있다: **무엇을 만들 것인가**는
gds6 원장에, **그 일을 누가 어디까지 했는가**는 claunch 세션·보드에. operator는 둘을
함께 읽어야 「다음 동작」을 말할 수 있다.

## 2. 단계 — 무엇이 다음인지 원장이 말한다

기획의 일곱 단계는 서버가 원장에서 계산해 `/api/plane/progress`로 낸다
(`plane/src/progress.ts:32-90`). 응답은 `steps[]`(`key`·`state`(done|stale|pending)·
`reason`·`effect`·`count`)와 `next`이고, `next`는 「결정 없는 어긋남이 있으면 결정,
아니면 앞에서부터 첫 stale·pending」이다(`plane/src/progress.ts:86-89`).

| # | 단계(key) | 원장 판정 | 읽는 곳 |
|---|---|---|---|
| 1 | `extract` 규칙 추출 | `workspaceProgress`의 `relations.chain` | `GET /api/plane/progress`, `GET /api/plane/jobs?kind=extract`(`plane/src/server.ts:1995`) |
| 2 | `anchors` 대상 이름 통합 | 같음 | `GET /api/plane/anchors`(`plane/src/server.ts:1287`) |
| 3 | `catalog` 기능 이름 묶기 | 같음 | `GET /api/plane/catalog`·`/features`(`:1246`, `:1242`) |
| 4 | `review` 문서 간 묶음 검토 | 같음 | `GET /api/plane/relations`(`:1324`) |
| 5 | `scenarios` 시나리오 구성 | `allScenarios`의 `hole_steps` | `GET /api/plane/scenarios`(`:1732`) |
| 6 | `decisions` 결정 | `decisionsView`의 열린 질문·미정 규칙·값 없는 데이터 | `GET /api/plane/decisions`(`:1497`) |
| 7 | `frames` 작업 범위 | `listFrames`의 `drifted` | `GET /api/plane/frames`(`:1942`) |

실현 평면은 진행 띠에 없다 — 프레임 하나를 고른 뒤의 일이라서다. 순서와 읽는 곳:

| # | 단계 | 읽는 곳 | 끝났다는 근거 |
|---|---|---|---|
| 8 | 코드 실측 | `GET /api/scan`(`issue-gen/src/server.ts:110`) | `scan`이 null이 아니다 |
| 9 | 규칙-코드 연결·설계 일감 | `GET /api/impact/<frame_id>`(`:183`), `GET /api/wires`(`:370`), `GET /api/decls`(`:357`) | `edges`에 `kind: wire`가 있고 `works`가 늘었다 |
| 10 | 전달 실행(연결→설계→패킷→이슈) | `GET /api/delivery?frame_id=`(`:206-224`, `diagnosis`가 시작점을 말한다) | 활성 `run.stage`가 전진하고 `run`이 끝났다 |
| 11 | beads 이슈 | `GET /api/beads?frame_id=`(`:189`), `GET /api/frames/summary`(`:177`) | `issues_repo`가 늘었다 |
| 12 | 구현 | `opv2_sessions`·`opv2_session`(claunch), `GET /api/impl-decisions?frame_id=&status=open`(`:269`) | 이슈가 닫히고 세션이 보고한다 |

`GET /api/backlog`(`:140`)는 어느 작업 범위에도 들어가지 않은 규칙의 크기를 낸다 —
「이 단계가 끝났다」가 아니라 「이 프로젝트가 아직 덮지 않은 것이 얼마나 되는가」다.
`GET /api/packet/<work_id>`(`:342`)는 설계 일감 하나의 계약서다 — 리뷰에서 그 일감이
무엇을 약속했는지 확인할 때만 읽는다.

## 3. operator가 하는 일과 하지 않는 일

하는 일 넷:

1. **nudge** — 파이프라인 순서에서 가장 앞선 미완료 단계 하나를, 근거 값과 함께,
   어디로 가서 무엇을 하면 되는지까지 적어 올린다.
2. **설명** — 그 단계가 무엇이고 왜 지금인지(원장의 `reason`·`effect`)를 옮긴다.
3. **리뷰** — 직전 관찰과 견줘 끝난 단계가 남긴 값을 이전→지금으로 적는다.
4. **답** — 사용자가 물은 것에 기록으로 답한다.

하지 않는 일: gds6 원장에 쓰기(API에 POST·PATCH·PUT·DELETE 금지), 판정(이름 통합·
기능 묶기·어긋남 해소·프레임 선택), 다른 세션의 cflow 게이트 승인. 근거는 gds6의
원칙이다 — 판단은 저장하고 파생은 저장하지 않으며(`README.md:34`), 저장은 검증을
통과한 제안과 사람의 승인뿐이다(`README.md:40`, `notes/llm-jobs.md`).

## 4. 왜 operator v2의 변형인가

operator v2는 이미 이 일의 기계를 갖고 있다(`src/claude_launcher/workflows/v2/operator-v2.yaml`,
`src/claude_launcher/operator_v2_mcp.py`, `src/claude_launcher/prompt_packs/role-operator.md`):
범위가 정해진 관찰(`opv2_poll`), 사용자와의 thread 대화(`opv2_post`·`opv2_ask`·
`opv2_inbox`), 지시의 전달(`opv2_dispatch`), 세션의 생성·재개 요청(`opv2_spawn`·
`opv2_resume`), 기록으로 답하기(`opv2_sessions`·`opv2_session`·`opv2_issue`·
`opv2_commits`·`opv2_transcript_search`·`opv2_history`), 착지 판정(`opv2_landing`).

그 도구들은 **바인딩이 연다** — 세션이 어떤 operator의 세션인지만 확인하고 워크플로를
가리지 않는다(`daemon/operators_v2.py:752-766`의 `operator_of_session`). 그래서 새 도구
계열을 만들지 않고, 같은 표면 위에서 **하는 일**만 바꾼다. 이 선택의 값은 셋이다:
사용자는 같은 Operator 화면에서 gds6 operator를 쓰고, 승인 게이트·범위 규칙·감시
스위치가 그대로 적용되며, operator 지침의 한 벌(무거운 일은 자식에게, 지시는 사용자의
말로만 전달, 게이트는 대신 답하지 않는다)이 두 워크플로에 같이 선다.

## 5. 무엇이 바뀌었는가

| 자리 | 바뀐 것 |
|---|---|
| `daemon/operators_v2.py` | operator 레코드에 `workflow`(기본 `operator-v2`), `qa`(기본 false), `gds6_url`(기본 null). `start`가 그 워크플로를 세우고, gds6 워크플로면 `gds6_url`을 시작 입력으로, `gds6-operator` 묶음을 오프닝에, `qa`면 `CLAUNCH_OPERATOR_QA=1`을 세션 환경에 싣는다. `qa_allowed(name)`이 데몬 쪽 판정이다 |
| `workflows/v2/gds-operator-v2.yaml` | 새 워크플로: `greet → {watch-gate → observe → rest}`. `observe`가 입력 처리·gds6 읽기·리뷰·nudge를 한 번에 한다. 규칙에 `stage-order`·`evidence-before-nudge`·`review-before-nudge`·`one-action-per-post`·`qa-when`·`no-decisions` |
| `prompt_packs/gds6-operator.md` | 새 묶음: 두 평면, 단계 표(끝났다는 근거 포함), nudge·리뷰의 모양, 질문 채널 셋, 읽기 전용 |
| `mcp_server.py`·`session_env.py` | operator 세션이라도 레코드가 QA를 켰으면 `qa_*` 도구를 숨기지 않는다(`OPERATOR_QA_ENV`) |
| `daemon/qa_api.py` | 같은 스위치를 데몬이 집행한다 — 레코드가 QA를 켰으면 세션 QA 쓰기를 거절하지 않는다 |
| `web/static/observer_operator.js`·`observer.css` | operator 폼에 워크플로 선택·gds6 주소·「세션 QA도 함께 쓰기」. 탭에 `gds6` 표시 |

### QA를 두 채널로 쓰는 규칙

operator는 질문을 세 곳으로 보낸다: 단일 결정은 `opv2_ask`, 여러 항목을 걸쳐 답하거나
사용자가 QA 창에서 다시 열어 볼 질문은 `qa_ask`, gds6 화면에서 사람이 직접 고르는
판정은 질문을 만들지 않고 화면을 nudge에 적는다. 같은 질문을 두 채널에 함께 올리지
않는다(워크플로 규칙 `qa-when`, 묶음의 「질문의 채널」).

## 6. 쓰는 법

1. gds6 서버를 띄운다(`npm run dev`, 기본 `http://localhost:8888`).
2. Observer 페이지에서 새 operator를 만든다: 범위에 gds6 프로젝트를 고르고,
   워크플로를 「gds6 프로젝트 안내」로, 서버 주소를 채운다(QA가 함께 켜진다).
3. 프로필을 고르고 시작한다. 세션은 `gds-operator-v2` 런을 돌며 180초마다 한 번
   관찰한다.
4. `POST /api/operators/{id}/start`로도 같은 일을 할 수 있다. 레코드에
   `workflow: gds-operator-v2`, `qa: true`, `gds6_url`을 실으면 된다.

감시가 꺼져 있으면 operator는 답만 한다 — nudge도 리뷰도 올리지 않는다
(`watch-gate`, `claunch operators watch`의 종료 코드 0/1).

## 7. 한계

- gds6 API는 이 저장소의 계약이 아니다. `progress`의 단계 키나 `frames/summary`의
  모양이 바뀌면 operator의 단계 표가 낡는다 — 표를 고치는 자리는 이 문서와
  `workflows/v2/gds-operator-v2.yaml`의 `survey`·`order` 조각이다.
- gds6 서버에 닿지 않으면 operator는 「읽지 못했다」만 말한다. 추측으로 단계를
  채우지 않는다.
- designframe(설계 편집기)의 회차는 이 표에 없다.
