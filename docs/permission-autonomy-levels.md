# 권한·능동성 레벨 체계 — 직교 권한 축의 tuple-preset과 레벨 사슬

상태: 초안 (2026-11-09, claunch-ii4pz, 세션 s6842). 코드 변경 없음.
사용자 결정 출처: 스레드 th-e03e46 (2026-11-09) — 「8은 필수가 아니다. 요구되는
'능동성'을 여러 직교하는 권한의 tuple-preset으로 정의하고, 그 프리셋을
레벨로 나누어 몇 단계의 정확한 체계로 세운다.」

## 1. 목적과 범위

에이전트에 일을 위임할 때 「어디까지 묻지 않고 하는가」를 숫자 하나(레벨)로
선언하고, 그 숫자가 기존 메커니즘(cflow v2 시작 입력, chooser/ask, 데몬 승인
게이트, spawn 정책, 하네스 권한 모드, 프롬프트 팩)의 어느 값으로 펼쳐지는지를
표로 고정한다.

포함:
- 권한 축 6개와 각 축의 서열값 정의, 저장소 안의 근거.
- 축 사이의 독립성 검토와, 독립하지 않은 자리의 명시.
- 사슬 조건과 그 위의 레벨 표.
- 레벨 → 기존 메커니즘 변환표. (축, 값)마다 「엔진 보장 / 지시」 구분.
- 사슬 밖 조합(variant)과 일시 승격의 처리.

제외:
- 코드·워크플로 yaml 변경. 변환표를 코드로 구현하는 일은 후속 이슈.
- 보드 기록의 결정 빈도 전수 집계. 이 문서의 근거는 코드·yaml·게이트 구조다.

## 2. 용어

| 용어 | 정의 |
|---|---|
| 축 | 서로 독립적으로 값을 정할 수 있는 권한의 한 차원. 이 문서는 A~F 여섯 개를 둔다. |
| 서열값 | 축 안에서 낮음 → 높음으로 정렬된 값. 높을수록 사람이 개입하는 자리가 적다. |
| tuple | 여섯 축의 서열값을 한 줄로 적은 것. 예: (A4, B0, C2, D2, E2, F1). |
| 프리셋 | 이름을 붙여 고정한 tuple. |
| 레벨 | 사슬 조건(5절)을 만족하는 프리셋의 순서. L0부터 센다. |
| variant | 사슬 조건을 만족하지 않아 레벨 번호를 받지 않는 명명 프리셋. |
| 엔진 보장 | 데몬·cflow2 엔진·CLI가 그 값을 벗어나는 호출을 거절한다. |
| 지시 | 프롬프트·CLAUDE.md·워크플로 문구가 금지하지만 기계는 거절하지 않는다. |

## 3. 축 정의

각 축의 표에서 「근거」는 그 값이 저장소의 어느 코드·문서에서 구분되는지,
「강제」는 그 값의 경계를 엔진이 지키는지 지시만 있는지를 적는다.

### 3.1 A — 쓰기 범위 (한도)

세션이 자기 결정으로 수행할 수 있는 가장 넓은 쓰기 동작류. 그 위의 동작은
다른 세션에 요청한다(예: LANDING REQUEST). 서열의 기준은 영향 범위(누가
보는가)와 되돌림 비용이다.

| 값 | 동작류 | 근거 | 강제 |
|---|---|---|---|
| A0 | 읽기, 테스트 실행 | `cflow2/ir.py` `READ_ONLY_EFFECTS = {fs.read, test.run}`; `cflow2/caps.py` `ReadonlyPlan` | 엔진 보장 (read-only 위임) |
| A1 | 공유 기록 추가 — 보드 코멘트·상태 변경, 스레드 글, 메시 송신 | effects `board.write`, `thread.write`, `mesh.send`; `claunch beads`; `thread_reply`; `mesh send` | 지시 (워커의 `'*'` 송신 거절만 엔진) |
| A2 | 로컬 작업 트리 쓰기 (미커밋) | effect `fs.write` | 지시 (하네스 권한 모드가 도구 호출 단위로 묻는 것은 별도, 3.3) |
| A3 | 자기 브랜치 커밋·브랜치 생성 | effects `git.commit`, `git.branch`; branch-setup 스텝(`<세션>-<요지>`) | 지시 |
| A4 | 자기 원격 브랜치 push, PR 열기 | effect `git.push`(명시 refspec `HEAD:refs/heads/<브랜치>`); remote-setup 스텝 | 지시 (`push.default` 검사는 스텝 문구) |
| A5 | 자기 스택 브랜치 머지 (`<세션>-stack`에 자식 브랜치 `--no-ff`) | stack-v2 side 런; work 스텝 「자식을 두면」 절 | 지시 |
| A6 | 통합 대상(master) 머지, 공유 원격 push, reposync | improv-leader 통합 단계; `daemon/reposync_gate.py`; 워커 규칙 `no-master-merge` | 혼합 — 워커의 master 머지는 지시; reposync push는 엔진 보장(승인 카드) |
| A7 | 인프라 — 데몬 재기동·정지, 설정 변경, 다른 세션 kill | CLAUDE.md 「데몬 재기동·정지는 사용자의 명령이다」; `daemon/restart_gate.py`; `daemon/kill_gate.py` | 혼합 — restart는 CLI 게이트(`CLAUNCH_SESSION` 셸만), kill은 엔진 보장, stop·API 직접 호출은 지시 |

### 3.2 B — 결정 권위 (다른 세션의 게이트에 답하는 권한)

이 세션이 다른 세션의 승인 요청에 어디까지 답할 수 있는가. 자기 동작의
한도(A)와 별개다.

| 값 | 권위 | 근거 | 강제 |
|---|---|---|---|
| B0 | 없음 — 자기 ask의 `otherwise: self`만 | `flow_approve`는 self로 떨어진 ask에만; 다른 런의 `flow_select`는 거절 | 엔진 보장 |
| B1 | 자문 응답 — ask `from: [{role: reviewer}]`의 응답자 | v2 yaml ask `from:` role reviewer 6곳; `flow_answer` | 엔진 보장 (라우팅된 역할만 답한다) |
| B2 | 자식 게이트 — 자식 런의 `chooser: user` 선택을 부모가 대신 | claunch-vjp4y; `flow_select(scope=<자식>)`; `flow_gates`; `request_child_goto` | 엔진 보장 (자기 하위 트리만) |
| B3 | 착지 승인·통합 판정 — 착지 큐 심사, master 머지 | improv-leader-v2; ask `from:` role leader 10곳; `landing_queue` | 지시 (리더 역할 문구) + 엔진 라우팅 |
| B4 | 사람 — 데몬 게이트(spawn·kill·restart·goto·reposync·resume) 승인, `chooser: user` 선택, 런 중단 | `daemon/*_gate.py`(에이전트 승인 거절); `claunch cflow2 approve/select/abort` CLI | 엔진 보장 (게이트는 웹 UI·사용자 셸만 settle) |

### 3.3 C — 승인 지속 범위

A 한도 안의 동작에 대해 승인이 어느 단위로 요구되는가. 값이 높을수록 한 번의
승인이 더 오래 유효하다.

| 값 | 단위 | 근거 | 강제 |
|---|---|---|---|
| C0 | 동작마다 — 도구 호출 하나하나 | 하네스 `permissions.defaultMode: default`(`store.py` `SHARED_SETTINGS_DEFAULTS` 주석: 「a profile with no value asks before every tool call」) | 엔진 보장 (하네스) |
| C1 | 노드마다 — cflow 스텝·ask 하나마다 | 코딩 승인 「이번만」(claunch-sxdnc); ask `otherwise: human` 14곳; 데몬 게이트는 요청마다 카드 | 엔진 보장 |
| C2 | 런마다 — 시작 입력으로 한 번 | `require_coding_approval`, `require_merge_approval`, `auto_merge`, `force_pr`, `force_no_pr`, `allow_work_on_main`; 코딩 승인 「앞으로도」 | 엔진 보장 (입력 변경은 `flow_input`·사람) |
| C3 | 세션·프로필 — 세션 생성 시 한 번 | `permissions.defaultMode: auto`(분류기); `spawn.*` 정책; `daemon.reposync_push: auto` | 엔진 보장 (프로필·설정 파일) |

### 3.4 D — 무응답 기본값

승인을 기다리는 동작에서 사람이 답하지 않을 때의 처리. 데몬 게이트는 이 값을
동작류마다 상수로 고정한다(6.3절). 레벨의 D 값은 레벨이 새로 만드는 게이트
(cflow ask, 협의 hold)의 기본값이고, 고정된 동작류에는 적용되지 않는다.

| 값 | 처리 | 근거 | 강제 |
|---|---|---|---|
| D0 | 기한 없이 대기 | `resume_gate.py`(「There is no deadline」); `kill_gate.py`(「stays pending」); `chooser: user` 선택; 협의 hold | 엔진 보장 |
| D1 | 시간 초과 시 거절 | `reposync_gate.py` `GATE_TIMEOUT = 1800.0` → `expired` = rejected; spawn `root` 요청은 자식으로 강등(`spawn_gate.py`) | 엔진 보장 |
| D2 | 시간 초과 시 승인 | `spawn_gate.py` `GATE_TIMEOUT = 300.0`(자식 요청); `restart_gate.py` 300.0; `goto_gate.py` 300.0 | 엔진 보장 |
| D3 | 게이트 없음 | reviewer 역할 자식 spawn(`spawn_gate.py` 독스트링 「except a reviewer child」); `chooser: agent` 21곳 | 엔진 보장 |

### 3.5 E — 자원 생성 (세션 생성)

| 값 | 허용 | 근거 | 강제 |
|---|---|---|---|
| E0 | 없음 | `children`이 슬롯 0 또는 스폰 거절(`spawn.<key>`) | 엔진 보장 |
| E1 | reviewer 역할 자식만 (승인 카드 없음) | `spawn_gate.py`; `mesh` 스킬 「unless its role is reviewer」 | 엔진 보장 |
| E2 | 자식 N·깊이 D 이내, 승인 카드 경유 | `spawn.py` `SpawnPolicy` 기본값 `max_children: 4`(soft), `max_depth: 3`(hard), `allow_profile: false`, `allow_cwd: false` | 엔진 보장 |
| E3 | 워크스페이스·워크트리·프로필 선택 | `allow_workspace: true`, `allow_worktree: true`(기본), `allow_profile: true`(설정) | 엔진 보장 |

### 3.6 F — 범위 재량

| 값 | 재량 | 근거 | 강제 |
|---|---|---|---|
| F0 | 지시 그대로 — 범위 밖 발견은 이슈로만 남긴다 | work 스텝 `scope-discipline` 규칙; `found-issue` 프로시저 | 지시 |
| F1 | 같은 이슈 안의 인접 수정과 자명한 판정의 즉시 실행 | `prompt_packs/obvious-decisions.md` 「자명함, 되돌릴 수 있음, 실행 권한이 이 세션에 있음 → 바로 실행」 | 지시 |
| F2 | 보드에서 다음 일감 자가 선택 | `beads: "none-auto"`(NONE_AUTO 오프닝); 회차 루프의 queue recheck | 지시 (오프닝 문구) |
| F3 | 일감 생성·분할·위임 | 이슈 생성(`claunch beads create`), `flow_fork group: found`, `spawn --issue`, 리더의 배정 | 지시 + 위임은 E에 종속 |

## 4. 독립성 검토

여섯 축이 서로 다른 값을 가질 수 있음을 기존 조합으로 보인다.

| 조합 | 실례 |
|---|---|
| A 낮고 B 높음 | 리뷰어: 기록만 쓰고(A1) ask 응답자(B1) |
| A 높고 B 낮음 | 표준 워커: push·PR까지(A4) 하지만 남의 게이트에 답하지 않음(B0) |
| A 같고 C 다름 | 코딩 승인 「이번만」(C1)과 「앞으로도」(C2)의 워커는 A 한도가 같다 |
| D 같고 A 다름 | spawn 자식(E 영역)과 restart(A7)가 둘 다 300초 승인(D2) |
| E 높고 F 낮음 | 스택 부모가 자식에 워크트리를 주지만(E3) 자기 회차 범위는 지시대로(F0) 가능 |

독립하지 않은 자리 두 곳은 정의에 그대로 적는다.

1. F3(위임)은 E ≥ E2를 요구한다. 이슈 생성·분할(F3의 일부)은 E0에서도
   가능하므로 F3을 쪼개지 않고, F3 가운데 위임만 E에 종속한다고 적는다.
2. D는 승인을 기다리는 동작에만 뜻이 있다. C3(세션 단위 승인)이고 한도 안이면
   기다리는 동작이 없어 D는 쓰이지 않는다. 사슬 위에서는 D를 그대로 두어
   단조성을 유지한다.

## 5. 사슬 조건과 레벨 표

tuple의 집합은 부분순서다. 레벨 번호를 붙이려면 다음 조건을 둔다.

> L(n+1)의 모든 축 값은 L(n)의 같은 축 값보다 같거나 크다.

이 조건을 만족하는 프리셋 열을 사슬이라 하고, 사슬의 각 점이 레벨이다.
레벨 수는 사슬 위에서 「묻는 횟수」나 「되돌림 비용」이 실제로 달라지는 자리의
개수로 정한다. 아래 표는 그 기준으로 7단계(L0~L6)를 낸다. L6은 사람이다.

| 레벨 | 이름 | A | B | C | D | E | F | 한 줄 정의 | 사람이 결정하는 자리 |
|---|---|---|---|---|---|---|---|---|---|
| L0 | 읽기 전용 위임 | A0 | B0 | C0 | D0 | E0 | F0 | 읽기·테스트만. 쓰기 요청은 전부 사람에게 돌아간다 | 모든 쓰기 |
| L1 | 기록자 | A1 | B0 | C1 | D0 | E0 | F0 | 보드·스레드·메시에 기록을 남긴다. 저장소는 바꾸지 않는다 | 저장소 쓰기 전부; 협의 hold는 기한 없이 대기 |
| L2 | 감독형 작업자 | A4 | B0 | C1 | D0 | E1 | F0 | 코딩·착지 요청을 하되 노드마다 사람 승인. reviewer 자식만 둔다 | 코딩 시작(이번만), 머지 승인, 범위 변경 |
| L3 | 표준 작업자 | A4 | B0 | C2 | D2 | E2 | F1 | 자기 브랜치 커밋·push·PR까지 자기 결정. 승인은 런 시작 입력으로 한 번. 자식 spawn은 카드(5분 → 승인) | 머지(리더), spawn 카드, kill·reposync(고정) |
| L4 | 스택 부모 | A5 | B2 | C2 | D2 | E3 | F2 | 자식 브랜치를 자기 스택에 모으고 자식의 user 게이트에 답한다. 다음 일감을 보드에서 고른다 | master 머지(리더), kill·reposync(고정) |
| L5 | 리더 | A6 | B3 | C3 | D2 | E3 | F3 | master 머지·착지 큐 심사·일감 생성과 배정. 승인은 세션 단위 | 인프라(A7), reposync push(고정), restart 카드(300초 → 승인) |
| L6 | 운영자 | A7 | B4 | C3 | D3 | E3 | F3 | 사람. 모든 게이트의 최종 결정자 | — |

단조성 확인: A 0·1·4·4·5·6·7, B 0·0·0·0·2·3·4, C 0·1·1·2·2·3·3,
D 0·0·0·2·2·2·3, E 0·0·1·2·3·3·3, F 0·0·0·1·2·3·3. 모든 축이 감소하지 않는다.

레벨 경계의 근거:
- L0→L1: 쓰기가 처음 생긴다(READ_ONLY_EFFECTS 경계).
- L1→L2: 저장소 쓰기와 push가 생긴다. 되돌림 비용이 로컬에서 원격으로 넘어간다.
- L2→L3: 승인 단위가 노드에서 런으로 바뀐다. 묻는 횟수가 회차당 여러 번에서 0으로 줄어든다.
- L3→L4: 머지 권한(A5)과 결정 권위(B2)가 처음 생긴다.
- L4→L5: master에 쓴다(A6). 되돌림 비용이 다른 세션 전체에 미친다.
- L5→L6: 인프라(A7)와 모든 게이트의 최종 결정(B4).

## 6. 레벨 → 기존 메커니즘 변환표

### 6.1 위임 시점의 설정값

| 레벨 | cflow 런·시작 입력 | 하네스 권한 모드 | spawn 정책 | beads 시작 답 | 비고 |
|---|---|---|---|---|---|
| L0 | 런 없음. cflow2 위임 노드의 `effects` ⊆ `{fs.read, test.run}` → `ReadonlyPlan` | `default` | 해당 없음 | `beads: false` | 서브에이전트·read-only 위임 |
| L1 | 런 없음 또는 협의 hold 상태(`consult-check → consult`) | `default` | 슬롯 0 | `beads: false` | 분석·협의 세션 |
| L2 | `improv-worker-v2`, `require_coding_approval: true`, `require_merge_approval: true`, `auto_merge: false` | `auto` | 기본값 (reviewer 자식만 쓰도록 지시) | `issue: <id>` | 코딩 승인은 「이번만」으로 답한다 |
| L3 | `improv-worker-v2` 기본값 (`require_*: false`, `auto_merge: true`, `relation: stacked`) | `auto` | 기본값 (`max_children 4`, `max_depth 3`) | `issue: <id>` | 현재 워커의 기본 |
| L4 | L3 + `sides: stack`(stack-v2) 기동, `relation` 지정 | `auto` | 기본값 + `allow_workspace`/`allow_worktree` true | `beads: "none-auto"` 허용 | mesh-delegate로 선 중간 워커 |
| L5 | `improv-leader-v2` | `auto` | 기본값 + `allow_profile` 설정 가능 | 리더 큐 | 착지 큐·머지 |
| L6 | CLI (`claunch cflow2 approve/select/abort`, `claunch daemon restart`) | — | — | — | 사람 |

### 6.2 (축, 값)별 강제 수단 요약

| 축 | 엔진이 보장하는 경계 | 지시로만 지키는 경계 |
|---|---|---|
| A | A0(read-only 위임), A6의 reposync push, A7의 kill·restart(CLI 게이트) | A1~A5 전부, 워커의 master 머지, `claunch daemon stop`, API 직접 호출 |
| B | B0~B4 전부 (라우팅·scope·게이트 settle 주체) | — |
| C | C0~C3 전부 | — |
| D | 데몬 게이트 상수, `chooser: user` 대기 | 협의 hold의 대기 |
| E | E0~E3 전부 (`SpawnPolicy`) | — |
| F | — | F0~F3 전부 |

A와 F의 경계가 지시에 머무는 것이 이 체계의 강제력 한계다. 레벨을 코드로
구현할 때 우선 보강할 자리다(8절).

### 6.3 레벨과 무관하게 고정된 동작류

다음 동작류는 데몬이 결정자(B)와 무응답 기본값(D)을 상수로 고정한다. 레벨
선언이 이 값을 바꾸지 않는다.

| 동작 | 결정자 | 무응답 | 근거 |
|---|---|---|---|
| 다른 세션 kill | 사람 | 기한 없이 대기 | `kill_gate.py` |
| 종료된 세션 resume | 사람 | 기한 없이 대기 | `resume_gate.py` |
| reposync push | 사람 (`daemon.reposync_push: ask`) | 1800초 후 거절 | `reposync_gate.py` |
| 데몬 restart (세션 셸) | 사람 | 300초 후 승인 | `restart_gate.py`, `store.py` `restart_approval_timeout` |
| 자식 spawn (reviewer 제외) | 사람 | 300초 후 승인 | `spawn_gate.py` |
| 루트 spawn | 사람 | 300초 후 자식으로 강등 | `spawn_gate.py` |
| 리더의 자식 런 goto | 사람 | 300초 후 승인 | `goto_gate.py` |

## 7. variant와 일시 승격

### 7.1 variant

사슬 조건을 만족하지 않는 프리셋은 레벨 번호 없이 이름만 가진다.

| 이름 | A | B | C | D | E | F | 설명 | 사슬 밖인 이유 |
|---|---|---|---|---|---|---|---|---|
| 심사자(reviewer) | A1 | B1 | C2 | D0 | E0 | F0 | 기록만 쓰고 ask 응답자로 판정을 낸다 | B1 > L1·L2·L3의 B0이면서 A1 < L2의 A4 — L2와 비교 불가 |
| 운영자 대행 에이전트(operators_v2) | A1 | B0 | C1 | D1 | E2(root) | F3 | 사용자 메시지를 받아 루트 세션 생성을 요청한다 | E2·F3이 L1·L2보다 높고 A1이 L2보다 낮다 |

### 7.2 일시 승격

레벨은 세션의 기본값이고, 한 스텝에서만 한 축을 올리는 일은 승격으로 기록한다.
실례: `landing-target-gate`에서 사람이 `merge-by-agent`를 고르면 그 회차의
`self-merge-apply` 스텝에서만 워커의 A가 A6으로 오른다(`$data.self_merge.armed`).
승격은 (대상 세션, 축, 값, 유효 범위, 결정자)를 가지며 저널에 남는다. 레벨
자체를 바꾸지 않는다.

## 8. 수정 필요 항목

| 항목 | 현재 상태 | 레벨 체계와의 관계 |
|---|---|---|
| spawn 정책의 단위 | `spawn.*`는 런처 홈 설정의 전역 값 | E를 세션·레벨별로 줄 수 없어 L2의 E1은 지시로만 성립 |
| 게이트 시간 초과의 단위 | `GATE_TIMEOUT` 상수와 `daemon` 설정 키 | D를 레벨별로 줄 수 없음. 6.3의 고정표로 처리 |
| 호출자 구분 | `X-Claunch-Actor` 헤더는 선언이며 증명이 아님(`actors.py`); cflow2 call·packs 핸들러가 agent/user를 구분하지 않음(claunch-8t5r0.7) | B의 엔진 보장이 이 구분에 기댄다 |
| A7의 강제 | restart만 CLI 게이트, stop·API 직접 호출은 지시 | L5 이하의 A 한도가 지시에 머문다 |
| F의 강제 | 전부 지시 | 레벨 선언이 범위 재량을 기계적으로 막지 못한다 |
| 입양·coordinator 권한 모델 D1~D4 | 사용자 결정 대기(claunch-fnnrk.1/.5) | 그 결정이 B2·B3 사이에 새 값을 요구할 수 있음 |

## 9. 사용 예

위임 문구는 레벨 하나와 필요하면 축 보정으로 쓴다.

```
level: L3                 # 표준 작업자
override: {E: E1}         # 이 일감은 reviewer 자식만
```

보정이 사슬을 벗어나면 그 세션의 프리셋은 variant이고, 보고와 저널에
레벨 대신 tuple 전체를 적는다.

## 10. 선행 맥락

claunch-vjp4y(부모의 자식 게이트 위임), claunch-vjp4y.2.1(선택지 단위 허용
표시, 미착수), claunch-fnnrk.1/.5(D1 권한 모델, 사용자 결정 대기),
claunch-8vtd4(obvious-decisions), claunch-sxdnc(코딩 승인 이번만/앞으로도),
claunch-5aysr(kill 게이트), claunch-iw5i8.16(spawn 게이트), claunch-ub83(restart
게이트), claunch-8t5r0.7(호출자 구분 누락), claunch-vmcg7(plan default 선택),
claunch-dh73y(reposync 게이트), claunch-yjhgx(resume 게이트), claunch-ny72(goto
게이트).
