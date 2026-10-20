# 세션 위계 관리 — 도구·역할·cflow 좌표 설계안

- 이슈: `claunch-fnnrk`
- 작성: 세션 `s1189`, 2026-10-21
- 상태: 설계안. 구현 전이다. D1·D3은 사용자가 2026-10-21에 결정했고(7절), 나머지 미확정 결정은 사용자 답을 기다린다.
- 근거 표기: 「사실」은 이 문서 작성 시점에 코드·문서·명령 출력으로 확인한 것이다. 「추정」은 확인하지 못한 판단이다. 원인을 확인하지 못한 것은 「원인 미상」으로 적는다.

## 0. 요약

현재 세션 위계는 `SessionDef.parent` 하나로 표현되고, 권한은 트리 아래 방향으로만 흐른다. 그래서 다른 부모 밑의 세션을 입양하거나, 다른 부모-자식 관계를 통째로 직계에 편입하거나, 남의 부모 밑 자식 하나만 가져오는 일은 에이전트에게 불가능하다. 운영자 CLI(`claunch reparent`)만 이 이동을 할 수 있다.

이 문서는 세 가지를 정렬한다.

1. `improv-coordinator-v2`가 위계를 정리할 수 있게 하는 MCP 도구 가족의 계약(권한, 거절 조건, 부수 효과, 브리핑 의무).
2. 그 권한을 담는 `coordinator` 역할의 분리. 현재 `coordinator`는 `pm` 역할의 alias다.
3. 이동 뒤 `improv-worker-v2`의 cflow 런 좌표(관계 입력, stack side 런, 착지 라우팅, 진행 중 착지 요청)를 정리하는 절차.

제안의 핵심은 이동을 세 단계로 나누는 것이다.

| 단계 | 범위 | 위험 | 선행 조건 |
|---|---|---|---|
| 1 | 고아 입양(부모가 종료·소멸한 세션, 사용자 승인 카드) + 읽기 도구 | 낮음. 잃는 좌표가 없다 | 없음 |
| 2 | 살아 있는 부모 밑 자식 입양(동의 또는 사용자 승인) + cflow 좌표 재지정 | 중간. 진행 중 런의 좌표를 고쳐야 한다 | 단계 1, relation 재지정 방식 결정 |
| 3 | 스택을 소유한 중간 워커의 입양 | 높음. 스택 기준점과 자식 전체가 함께 움직인다 | 단계 2, stack-v2 재정렬 절차 |

## 1. 현재 상태

### 1.1 트리와 권한

| 항목 | 현재 동작 | 근거 |
|---|---|---|
| 트리 표현 | `SessionDef.parent` 하나. 읽을 때마다 파생하며 캐시하지 않는다 | `docs/mesh-design.md:1354-1362` |
| 권한 방향 | 아래 방향만. 자기 자손만 명령한다. 형제·부모는 불가 | `daemon/manager.py:1540-1562` |
| 이동 규칙 | `reparent(child, parent, actor)`: 사이클 금지, 종료된 부모 금지, `spawn.max_depth` 유지, actor가 child와 parent를 모두 명령해야 함 | `daemon/manager.py:1564-1631` |
| 운영자 경로 | `POST /api/sessions/{name}/parent`에 actor를 빼면 권한 검사를 건너뛴다 | `daemon/api.py:9968-10003` |
| 관계 해제 | `detach(child, actor)`: child를 루트로 만든다. 자손은 그대로 아래에 남는다 | `daemon/manager.py:1649-1675` |
| mesh 연결 | 이동 시 child↔새 부모 edge를 공유 mesh마다 연다. 옛 부모 edge는 그대로 둔다 | `daemon/mesh.py:5794-5826` |
| 트리 깊이 한도 | `children`이 `depth`/`max_depth`를 보고한다. 이 세션의 현재 값은 `max_depth=3` | `mcp__claunch__children` 출력 |

### 1.2 현재 MCP 도구

| 도구 | 하는 일 | 권한 |
|---|---|---|
| `spawn` | 자식 세션 생성·mesh 등록·브리핑 | 자기 자신의 자식. 사람 승인 게이트를 거친다 |
| `children` | 자기 서브트리, 런 요약, 깊이, spawn 가능 여부 | 읽기 |
| `kill` | 자손 종료 | 자기 자손. 승인 게이트 |
| `connect`/`disconnect` | mesh edge 열기·끊기 | 한쪽 끝이 자기 서브트리 |
| `detach` | 자기 자손을 루트로 만든다 | 자기 자손 |
| `reparent` | 자기 자손(서브트리 포함)을 자신 또는 자기 자손 밑으로 옮긴다 | actor가 child와 parent를 모두 명령해야 한다 |

`reparent` 도구 설명은 "Move a session you spawned (or a descendant of one) — with its whole subtree — under another session you command"라고 적고(`mesh_mcp.py:936-949`), `mesh-delegate` 스킬 설명은 같은 경계를 "only your own subtree can be moved"로 한 줄로 적는다(`mesh_topology.py:98`). 이 경계가 이번 요구의 출발점이다.

### 1.3 `reparent`가 바꾸는 것과 남기는 것

| 대상 | 이동 뒤 | 근거 |
|---|---|---|
| 세션 트리 | 바뀐다 | `manager.py:1623-1625` |
| mesh edge | 새 부모와의 edge가 열린다. 옛 부모 edge는 남는다 | `mesh.py:5794-5826` |
| 세션의 터미널·대화·핸들·워크트리·cflow 런 | 그대로다 | `manager.py:1583-1586` |
| 세션의 오프닝 브리핑 | 옛 부모를 계속 말한다. `rebrief`만 새 부모를 보여 준다 | `mesh_topology.py:159-167` |
| 보드 assignee | 그대로다 | `manager.py:reparent`는 보드를 쓰지 않는다 |
| 보드 부모 링크 | 그대로다. 절차 문서가 손으로 고치라고 적는다 | `mesh_topology.py:327-332` |
| 진행 중 착지 요청 | 옛 부모의 큐에 남는다 | 1.4절 |
| cflow 시작 입력 | 그대로다. 채워진 입력은 다시 쓸 수 없다 | 1.5절 |

### 1.4 착지 라우팅

착지 요청을 받을 세션은 요청 시점의 트리와 역할로 정해진다(`daemon/landing_route.py`).

- `stacked`(v1 전체, v2 `parent:landing`): 부모에서 위로 올라가며 **머지할 수 있는 가장 가까운 직계 조상**을 찾고, 없으면 mesh 리더로 간다(`landing_route.py:168-184`).
- `independent`(v2 `leader:landing`): mesh 리더만 본다(`landing_route.py:186-192`).
- "머지할 수 있다"는 조건: 살아 있고, 워커의 mesh에서 `merger` 역할(리더는 자동 보유)을 갖고, 워커 저장소의 체크아웃에서 착지 큐를 가진 런을 돌린다(`landing_route.py:147-166`).

그래서 부모가 바뀌어도 라우팅이 곧바로 깨지지는 않는다. 착지 큐가 없는 부모를 만나면 조상이나 mesh 리더로 넘어간다. 깨지는 것은 **기준 브랜치**다. `relation`과 부모의 스택 유무가 브랜치 기준을 정하고, 그 값은 시작 뒤에 바꿀 수 없다.

### 1.5 cflow 런 좌표

| 좌표 | 사는 곳 | 이동 뒤 | 다시 쓸 수 있는가 |
|---|---|---|---|
| `relation` | `improv-worker-v2` 시작 입력(`improv-worker-v2.yaml:191-200`) | 그대로다 | 불가. `set_by`가 없고 값이 차 있다 |
| 회차 브랜치의 기준 | 시작 시 `relation`과 부모의 스택으로 정한다(`branch-setup`, 844-860행) | 그대로다 | 브랜치를 손으로 rebase해야 한다 |
| stack side 런 입력 `issue`/`base`/`remote`/`repo` | `stack-v2` 시작 입력(`stack-v2.yaml:43-63`) | 그대로다 | 불가. 시작 입력이다 |
| 스택 브랜치 `<세션>-stack` | git | 옛 기준 위에 남는다 | `git rebase --rebase-merges`로 옮긴다(`stack-v2` restack/rebase-notice) |
| 착지 라우팅 | 트리에서 매번 계산(`landing_route.py`) | 다음 요청부터 새 트리를 본다 | 자동 |
| 진행 중 착지 레코드 | 옛 부모의 `landing` 컬렉션 | 옛 부모에 남는다 | 손으로 정산해야 한다 |
| 부모 런의 `parent:landing` push | `improv-worker-v2.yaml:2779-2795` | 다음 회차부터 새 부모로 간다 | 자동 |
| 위임 자식과의 대기 | `delegate-work`는 mesh 보고를 기다린다(`improv-worker-v2.yaml:1304-1352`) | 부모 노드가 계속 기다린다 | 부모에게 알려야 한다 |
| coordinator의 `jobs` 컬렉션 | `improv-coordinator-v2.yaml:35-54` | 입양된 세션의 일감은 등록돼 있지 않다 | `flow_fork`로 등록해야 한다 |

### 1.6 역할 어휘

- `pm` 역할의 alias가 `[project-manager, planner, coordinator]`다(`daemon/mesh_roles.py:452-468`). 오늘 `role: coordinator`는 `pm`으로 정규화된다.
- 역할별 프롬프트 묶음은 `prompt_packs.DEFAULT_ROLE_PACKS`에 있다(`prompt_packs.py:649-662`). `coordinator` 항목은 없다.
- `improv-coordinator-v2`는 `filter_roles: {type: whitelist, roles: [worker]}`이고 `default_role`이 없다(`workflows/v2/improv-coordinator-v2.yaml:9-11`).
- `filter_roles`는 세 곳에서 읽힌다. (a) v1 cflow 시작(`cflow/engine.py:3449`)과 요청(`3675`) 경로가 `_enforce_role_filter`(`3265-3297`)로 강제한다 — 운전 세션의 mesh 역할을 읽어 필터가 거절하면 `CflowError`다. (b) 답할 사람을 고르는 지원(volunteering) 조회가 읽는다(`cflow/engine.py:3104-3167`, 강제가 아니라 항목에 `refused` 사유를 실어 보고한다). 데몬의 워크플로 목록·피커 응답도 같은 필터를 항목에 싣는다(`daemon/api.py:4988-4990`, `5306-5309`). (c) 위저드·폼의 워크플로 선택이 읽는다(`wizard.py:1142-1175`). v2 런타임(`cflow2/`)에는 이 강제 지점이 없다(확인: `cflow2/`의 `filter_roles` 참조는 파싱과 메타 노출뿐이고 `_enforce_role_filter` 호출이 없다). 그래서 v2 정의인 `improv-coordinator-v2`의 `filter_roles` 변경은 오늘의 시작 게이트가 아니지만, v1 정의와 같은 어휘를 쓰므로 역할 분리 때 함께 검토한다.

## 2. 요구사항 정렬

### N1. 입양 권한의 근거

**문제**: 입양은 "권한이 아래로만 흐른다"는 정본 규칙의 예외다. 예외의 근거를 역할에 둘지, 당사자 동의에 둘지, 사용자 승인에 둘지 정해야 한다.

**선택지**

| 안 | 동작 | 얻는 것 | 잃는 것 |
|---|---|---|---|
| A. 역할 전역 | `coordinator` 역할 세션은 같은 project의 아무 세션이나 입양한다 | 한 번의 호출로 끝난다 | 두 coordinator가 같은 세션을 두고 다툴 수 있다. 원 부모가 모르는 사이에 자식을 잃는다 |
| B. 동의 핸드셰이크 | 원 부모가 승인해야 입양된다 | 정본 규칙의 예외를 당사자 합의로 좁힌다 | 원 부모가 종료됐으면 성립하지 않는다. 이동마다 두 턴이 든다 |
| C. 고아만 자동 | 부모가 종료·소멸한 세션만 자동 입양, 살아 있는 트리는 사용자 승인 | 실제로 막히는 경우(중간 워커 사망)를 즉시 푼다 | 살아 있는 트리 정리는 사람이 매번 승인한다 |
| D. 프로젝트 전역 + 건별 승인 | 모든 입양에 사용자 카드 | 감사 가능 | 세션이 많아지면 카드가 병목이 된다 |

**결정(D1, 사용자, 2026-10-21)**: D. 모든 입양에 사용자 승인 카드를 거친다. 고아 입양도 예외가 아니다. 카드는 기존 spawn·kill 게이트와 같은 형태다(3.3절). 살아 있는 원 부모의 동의를 카드와 병행할지(D2)는 아직 열려 있다. 판정은 다음 순서다.

1. 대상의 부모가 없거나 종료됐으면 → 사용자 승인 카드 한 장을 거쳐 입양.
2. 부모가 살아 있고 호출자가 `coordinator`면 → 사용자 승인 카드 한 장을 거치고, D2가 원 부모 동의를 병행하기로 결정되면 동의도 함께 받아 입양.
3. 그 밖 → 거절.

### N2. "부모-자식 통째 편입"과 "자식만 가져오기"의 구분

**사실**: 트리 연산으로는 둘이 같다. `reparent`는 대상과 그 서브트리를 함께 옮긴다(메서드 계약은 `manager.py:1564-1587`, 실제 변경은 `1623-1625`). 사용자가 말한 두 동작의 차이는 **원 부모를 어떻게 처분하는가**다.

| 요청 | 트리 연산 | 원 부모 처분 |
|---|---|---|
| 세션 하나 입양 | 대상과 서브트리를 옮긴다 | 그대로 둔다. 자식이 없어지면 유휴 상태가 된다 |
| 부모-자식 통째 편입 | 부모와 서브트리를 옮긴다 | 해당 없음(부모가 대상이다) |
| 자식만 가져오기 | 자식을 옮긴다 | 원 부모에 남은 자식과 런 상태를 확인해야 한다 |

**제안**: 도구 인자에 `source` 처분을 둔다: `keep`(기본), `retire`(이동 뒤 원 부모에게 살아 있는 자식과 진행 중 런이 없으면 종료를 요청), `adopt_parent`(원 부모도 함께 옮긴다). `retire`는 종료 요청이므로 기존 kill 승인 게이트를 그대로 쓴다.

### N3. `coordinator` 역할

**문제**: `coordinator`는 지금 `pm`의 alias다. 새 역할을 같은 이름으로 만들면 어휘 충돌이 난다. 이름을 바꾸면 사용자가 요청한 이름과 달라진다.

**선택지**

| 안 | 내용 | 영향 |
|---|---|---|
| A. alias 이동 | `pm`에서 `coordinator` alias를 빼고 `coordinator`를 실제 역할로 만든다 | `role: coordinator`로 들어온 멤버의 행동이 바뀐다. 저장된 멤버 역할은 정규화된 이름이라 `pm`으로 남아 있으면 영향이 없다 |
| B. 새 이름 | `hierarchy` 등 다른 이름으로 만든다 | alias 충돌이 없다. 사용자가 요청한 이름과 다르다 |
| C. pm의 subrole | `coordinator`를 `pm`의 subrole로 둔다 | 역할 어휘 변경이 작다. `pm`과 권한이 섞인다 |

**제안**: A. `pm`은 `project-manager, planner`를 유지하고, `coordinator`에는 `hierarchy-manager` 정도의 alias 하나를 준다. `prompt_packs.DEFAULT_ROLE_PACKS`에 `role-coordinator`를 추가하고, `improv-coordinator-v2`에 `default_role: coordinator`를 둔다. `filter_roles`는 `[coordinator, worker]`로 두어 기존 worker 세션이 계속 이 런을 몰 수 있게 한다.

**권한을 어디서 읽는가**는 별도 결정이다.

| 안 | 읽는 곳 | 장점 | 단점 |
|---|---|---|---|
| A. mesh 멤버 역할 | 호출자가 속한 mesh의 멤버 레코드 | 이미 있는 어휘를 쓴다 | 호출자와 대상이 같은 mesh에 없으면 판정이 어렵다 |
| B. 세션 레코드 | `SessionDef`에 역할 집합을 저장 | mesh와 무관하다 | 역할의 정본이 둘이 되어 갈린다 |
| C. 런 메타 | 호출자의 실행 중인 v2 런 메타 | 구현이 간단하다 | 사용자가 지적한 대로 workflow 구분만으로는 부족하다 |

**제안**: A. 판정은 "호출자가 속한 어느 mesh에서든 `coordinator` 역할을 갖고, 대상과 같은 project에 있다"로 한다. 같은 mesh에 있을 것을 요구하지 않는다. 대상이 어느 mesh에도 없어도 입양할 수 있게 하기 위해서다.

### N4. 이동 뒤 cflow 좌표

5절에서 따로 다룬다.

### N5. 착지·스택과의 관계

`improv-coordinator-v2`의 정본은 "조율 부모는 착지 큐나 스택을 직접 운영하지 않는다"고 적는다(`docs/cflow-v2.md:1963-1968`). 그런데 착지 라우팅은 `stacked`일 때 부모를 먼저 본다. coordinator가 부모가 되면 다음 중 하나가 일어난다.

- coordinator가 착지 큐를 갖지 않으면 라우팅이 조상이나 mesh 리더로 넘어간다(사실, `landing_route.py:168-184`). 이때 워커의 브랜치 기준은 여전히 옛 부모의 스택일 수 있다.
- coordinator가 스택을 소유하면 정본 규칙과 충돌한다.

**제안**: 입양은 **보고 위계**를 바꾸고 **통합 위계**는 바꾸지 않는다는 규칙을 둔다. 구체적으로:

- `relation: independent` 워커는 그대로 입양할 수 있다. 기준은 master, 착지는 리더 큐다.
- `relation: stacked` 워커는 다음 중 하나를 요구한다: (a) 새 부모가 스택을 소유한 워커다, (b) 입양과 함께 `relation`을 `independent`로 재지정한다, (c) 입양을 거절한다.
- 스택을 소유한 중간 워커는 단계 3 전까지 입양하지 않는다.

### N6. 운영 절차와 관측

정리가 필요해지는 시점은 네 가지다(`mesh_topology.py:267-287`와 같다): 계층 종료, 부모 사망, 잘못된 계층, 위임 취소. coordinator가 쓰는 절차는 6절에 적는다.

## 3. 도구 계약 (제안)

새 도구는 `reparent`를 넓히지 않고 별도 이름으로 둔다. `reparent`의 의미를 바꾸면 리더가 쓰던 기존 도구의 계약이 달라진다.

| 도구 | 입력 | 권한 | 트리 효과 | 부수 효과 | 거절 |
|---|---|---|---|---|---|
| `orphans` | `project?` | 읽기 | 없음 | 없음 | 없음 |
| `adopt` | `session`, `source?: keep\|retire\|adopt_parent`, `reason` | `coordinator` + 같은 project + 사용자 승인 카드(D1). 살아 있는 원 부모의 동의 병행 여부는 D2 | 대상과 서브트리를 호출자 밑으로 | mesh edge 열기, 이동 공지, 보드 코멘트, 런 재지정 계획 반환 | `not-a-coordinator`, `not-in-scope`, `no-consent`, `approval-denied`, `cycle`, `depth`, `parent-exited`, `mid-landing`, `relation-conflict` |
| `adopt_request` | `session`, `reason` | 자손 아님·coordinator 아님도 가능 | 없음 | 원 부모 또는 사용자에게 승인 요청 | 같은 목록 |
| `give` | `session`, `parent` | 자기 자손, 그리고 호출자가 coordinator | 자기 자손을 다른 세션 밑으로 | `adopt`와 같다 | `reparent`와 같다 |

단계 배분: `orphans`와 `adopt`(고아 경로)는 1단계, `adopt_request`와 `give`는 2단계다(5.5절).

### 3.1 `orphans`

반환: `[{session, previous_parent, status, project, run: {workflow, node, waiting}, issue, branch}]`.

`children`은 자기 서브트리만 보여 준다. 종료된 부모의 자식은 어느 자식 목록에도 나오지 않으므로, 오늘은 `claunch sessions`의 들여쓰기를 눈으로 읽어야 한다. 이 도구가 그 조회를 대신한다.

### 3.2 `adopt`

반환: `{session, parent, previous, depth, connected: [...], run: {relation, stack: bool, landing_target, in_flight: [...]}, plan: [...], warnings: [...]}`.

`plan`은 이동 뒤에 사람이 하거나 다음 단계가 해야 할 일의 목록이다: rebase할 브랜치와 기준, 원 부모 큐에서 회수할 착지 레코드, 보드에서 고칠 부모 링크, 재지정할 입력. 도구가 트리만 바꾸고 이 목록을 남기지 않으면, 이동은 성공하고 런은 조용히 낡는다.

### 3.3 승인 게이트

모든 `adopt` 호출은 사용자 승인 카드를 거친다(D1). 고아 입양도 예외가 아니다. 카드는 기존 spawn·kill 게이트와 같은 형태를 쓴다(spawn은 `daemon/spawn_gate.py`, `claunch-iw5i8.16`). 살아 있는 원 부모가 있는 호출은, D2가 병행으로 결정되면 원 부모 동의를 카드와 함께 받는다. 사용자가 카드를 거절하면 `approval-denied`다.

### 3.4 거절 조건의 근거

| 조건 | 근거 |
|---|---|
| `mid-landing` | `mesh-retopology`가 이미 "착지 중에는 옮기지 않는다"고 적는다(`mesh_topology.py:285-287`) |
| `depth` | `manager.reparent`의 `spawn.max_depth` 검사(`manager.py:1611-1622`)를 그대로 쓴다 |
| `parent-exited` | `manager.reparent`가 **새 부모**의 종료를 거절한다(`manager.py:1597-1601`, `new_parent.exited`). 대상 세션 자신의 종료 여부는 검사하지 않는다 — 종료된 세션의 레코드도 옮길 수 있다 |
| `cycle` | `manager.py:1602-1606` |
| `relation-conflict` | N5의 규칙. stacked 워커를 스택 없는 부모 밑으로 옮기려 할 때 |

## 4. 역할 분리 (제안)

| 항목 | 값 |
|---|---|
| 역할 이름 | `coordinator` |
| alias | `hierarchy-manager` |
| stance | "You may re-draw the session tree within your project: adopt orphans, take a child from a live parent only with that parent's consent or your user's approval, and brief every move." |
| `task_poll` | pm과 같은 형태. 배정되지 않은 일감과 부모 없이 남은 자식을 본다 |
| `cflow_reminder` | "a step that has not moved is usually a session whose parent changed and whose run coordinates did not" |
| 역할 묶음 | `prompt_packs/role-coordinator.md` 신규 |
| 워크플로 메타 | `improv-coordinator-v2`에 `default_role: coordinator`, `filter_roles: [coordinator, worker]` |

**마이그레이션 위험**: 지금 `role: coordinator`로 참여한 멤버는 `pm`으로 정규화된다. alias를 옮기면 같은 명령이 다른 역할을 만든다. 저장된 멤버 역할은 정규화된 이름(`pm`)이라 영향이 없지만, 다시 join하는 세션은 새 역할을 받는다. 이 변경은 릴리스 노트에 적어야 한다.

## 5. cflow 좌표 조작 (제안)

### 5.1 좌표 표

1.5절의 표를 다시 쓴다. 이동 뒤에 손대야 하는 것은 다음 넷이다.

| 좌표 | 담당 | 방법 |
|---|---|---|
| `relation` 입력 | 데몬(권한 있는 재지정) | 5.2 |
| 회차 브랜치 기준 | 이동한 세션 | `git rebase <새 기준>` |
| stack side 런의 기준점 | stack 런 | 5.3 |
| 진행 중 착지 레코드 | 원 부모 또는 coordinator | 5.4 |

### 5.2 `relation` 재지정의 세 가지 경로

| 안 | 방법 | 장점 | 단점 |
|---|---|---|---|
| A. 데몬의 권한 있는 재지정 | `adopt`가 같은 호출에서 `data.inputs.relation`을 다시 쓰고 저널에 남긴다 | 값이 한 곳에서 바뀌고 즉시 적용된다. 기존 읽기 지점을 고치지 않는다 | "시작 입력은 바꿀 수 없다"는 규칙에 예외를 둔다. `flow_input`의 거절 규칙과 감사를 함께 고쳐야 한다 |
| B. 워크플로 재정렬 노드 | `improv-worker-v2`에 `rehome` force_target 노드를 두고 `data.rehome`을 읽게 한다. `data.inputs.relation`을 읽는 지점을 전부 파생 값으로 바꾼다 | 시작 입력 규칙을 지킨다 | 읽는 지점이 많다(`branch-setup`, `work`, `integration-request`, `rebase`, `landing-review`, stack-v2의 자식 판정). 빠뜨린 지점이 조용히 낡는다 |
| C. 새 런 | 현재 회차를 마감하고 새 입력으로 런을 다시 시작한다 | 구현이 가장 단순하다 | 대화·회차 이력·진행 중 작업을 잃는다. 같은 scope의 실행 중 런은 다시 시작할 수 없다 |

**결정(D3, 사용자, 2026-10-21)**: A. 재지정은 다음 조건에서만 허용한다. (1) 같은 호출에서 세션의 부모가 실제로 바뀌었다. (2) 호출자가 그 이동을 승인받았다. (3) 대상 입력이 `relation`처럼 재지정 허용 목록에 있다. (4) 저널에 `rehome` 레코드가 남는다. 감사와 회귀 테스트를 같은 이슈에서 다룬다.

D3은 2026-10-21에 사용자가 A로 결정했다(7절).

### 5.3 stack side 런

스택을 소유한 워커가 이동할 때 손대는 값은 세 가지다.

1. **기준점(`based`)**: `stack-v2`는 `open`/`cut`/`restack`/`rebase-notice`가 `based` 신호를 갱신한다고 적는다(`stack-v2.yaml:319-325`). 신호는 엔진이 쓰므로 손으로 쓰지 않는다. 이동을 계기로 `based`를 갱신하는 노드가 필요하다. `stack-based` 노드가 사람이 이전을 마친 뒤 기준점을 기록하는 자리이므로(`stack-v2.yaml:406-422`), 이동 절차도 이 노드를 지나게 하는 것이 자연스럽다.
2. **스택 브랜치**: `git rebase --rebase-merges --onto <새 기준> <기준점> <세션>-stack`.
3. **자식 공지**: stacked 자식에게 새 기준과 rebase 명령을 batch send한다(`stack-v2.yaml:332-336`과 같은 문구).

**제안**: `stack-v2`에 `rehome` 프로시저를 추가한다. 입력은 `{parent, base}`이고, 하는 일은 (a) 기준점 확인, (b) `--rebase-merges` 이전, (c) 자식 batch 공지, (d) `STACK @ <새 tip>` 코멘트다. `adopt`는 이 프로시저가 필요한지 판정해 `plan`에 넣는다. 프로시저 실행은 사람이 승인한 이동 뒤에 그 워커의 stack 런이 한다.

### 5.4 진행 중 착지 레코드와 보드

- 원 부모의 `landing` 컬렉션에 남은 레코드는 회수하거나 철회해야 한다. 철회 표시는 이미 있는 `WITHDRAWN @ <tip>` 코멘트 규약을 쓴다(`improv-worker-v2.yaml`의 landing-withdraw).
- 보드의 부모 링크는 `mesh-retopology` 7단계와 같은 규칙으로 고친다(`mesh_topology.py:327-332`).
- coordinator가 입양한 일감은 `flow_fork`로 `jobs`에 등록해야 한다(`improv-coordinator-v2.yaml:97-99`). 등록하지 않으면 coordinator의 정산이 그 일감을 보지 못한다.
- 부모의 런이 위임 자식을 기다리는 중이면(`delegate-work`), 이동 전에 부모에게 알린다. 이 대기는 런 기록이 아니라 mesh 보고를 기다리는 상태라서, 데몬이 자동으로 감지할 수 있는지 확인하지 못했다. **원인 미상**이 아니라 **감지 수단 미확인**이다.

### 5.5 단계별 계획

| 단계 | 내용 | 완료 조건 |
|---|---|---|
| 1 | `orphans` 읽기 도구, 고아 입양(사용자 승인 카드, D1), 보드·mesh 부수 효과, 브리핑 문구, `coordinator` 역할과 묶음. 이 단계는 `relation`을 재지정하지 않고 `relation-conflict`로 거절한다: stacked 고아를 스택을 소유하지 않은 부모 밑으로 옮기려는 호출, 그리고 착지·스택 상태를 정산할 수 없는 호출이다. `adopt_request`·`give`는 이 단계에 넣지 않는다(단계 2) | 사용자가 카드를 승인하면 고아 입양이 한 번의 호출로 끝나고, 이동 공지·보드 코멘트·`plan`이 남는다. 거절 코드가 테스트로 고정된다. 표적 테스트 green |
| 2 | 살아 있는 트리 입양(`adopt_request`, `give`, 원 부모 동의·사용자 승인 게이트), `relation` 재지정, 착지 레코드 회수, coordinator `jobs` 등록 | `independent` 워커 입양 뒤 다음 착지 요청이 올바른 큐로 간다 |
| 3 | 스택 소유 워커 입양, stack-v2 `rehome` 프로시저 | 입양 뒤 스택이 새 기준 위로 옮겨지고 자식이 재정렬된다 |

## 6. 위계 정리 운영 절차 (제안)

coordinator가 쓰는 절차는 다음과 같다.

1. `orphans`로 대상과 런 상태를 읽는다. 이동할 세션의 런이 사람의 선택을 기다리는 중이면 먼저 그 사실을 사용자에게 알린다.
2. 이동을 한 줄로 적는다: 누가, 어디서, 어디로, 왜. 이것이 공지와 보드 코멘트가 된다.
3. `adopt`를 호출한다. 모든 호출은 사용자 승인 카드를 거친다(D1). 살아 있는 원 부모가 있고 D2가 동의 병행으로 결정되면 동의도 함께 받는다.
4. 반환된 `plan`을 실행한다: rebase, 착지 레코드 회수, 보드 링크, `jobs` 등록.
5. 이동한 세션·새 부모·옛 부모에게 한 번의 batch send로 알린다.
6. 정리한 위계를 보드 상위 이슈 코멘트에 남긴다.

## 7. 결정 상태

2026-10-21 사용자 답: D1은 D(모든 입양에 사용자 승인 카드), D3은 A(데몬의 조건부 재지정)로 결정됐다. D2·D4·D5·D6은 미확정이다.

| id | 결정 | 선택지 | 권장 | 상태 |
|---|---|---|---|---|
| D1 | 입양 권한의 기본 모델 | N1의 A/B/C/D | C(고아 자동) + 살아 있는 트리는 동의 또는 사용자 승인 | **결정됨(사용자, 2026-10-21): D — 모든 입양(고아 포함)에 사용자 승인 카드** |
| D2 | 살아 있는 트리 입양의 승인 경로 | 원 부모 동의 / 사용자 카드 / 둘 다(부모 무응답 시 사용자) | 둘 다 | 미확정. D1에 따라 사용자 카드가 기본이고, 원 부모 동의를 카드와 병행할지가 남은 질문이다 |
| D3 | `relation` 재지정 방식 | 5.2의 A/B/C | A(데몬 재지정, 조건부) | **결정됨(사용자, 2026-10-21): A** |
| D4 | `coordinator` 이름 처리 | N3의 A/B/C | A(alias 이동) | 미확정 |
| D5 | coordinator의 스택 소유 | 금지 유지 / 허용 | 금지 유지. stacked 워커는 `relation` 재지정 또는 거절 | 미확정 |
| D6 | 1단계 범위 | 고아만 / 고아+살아 있는 트리 | 고아만 먼저 | 미확정 |

## 8. 후속 이슈

| 이슈 | 내용 | 선행 |
|---|---|---|
| `claunch-fnnrk.1` | 1단계: coordinator 역할·묶음·워크플로 메타 | 없음 |
| `claunch-fnnrk.2` | 1단계: `orphans`/`adopt`(고아) 도구, 부수 효과, `relation-conflict` 거절 | `.1` |
| `claunch-fnnrk.5` | 2단계: 살아 있는 트리 입양(`adopt_request`·`give`·동의/승인 게이트) | `.2` |
| `claunch-fnnrk.3` | 2단계: `relation` 재지정, 착지 레코드 회수, `jobs` 등록 | `.2`, D3 |
| `claunch-fnnrk.4` | 3단계: stack-v2 `rehome`과 스택 소유 워커 입양 | `.3` |

`.3`과 `.5`는 `.2` 위에서 병렬로 진행할 수 있다. `.4`는 `.3`의 재지정 경로를 전제로 한다.

## 9. 근거

- `src/claude_launcher/daemon/manager.py:1540-1675` — commands, reparent, detach
- `src/claude_launcher/daemon/api.py:9968-10030` — reparent·detach 경로
- `src/claude_launcher/daemon/mesh.py:5794-5826` — link_lineage
- `src/claude_launcher/daemon/landing_route.py:1-192` — 착지 라우팅
- `src/claude_launcher/daemon/mesh_roles.py:411-574` — 역할 어휘
- `src/claude_launcher/prompt_packs.py:649-662` — 역할별 묶음
- `src/claude_launcher/cflow/engine.py:3265-3297,3449,3675` — v1 cflow의 `filter_roles` 강제(`_enforce_role_filter`)
- `src/claude_launcher/mesh_mcp.py:912-971` — detach·reparent 도구 설명
- `src/claude_launcher/mesh_topology.py:238-358` — mesh-retopology 절차
- `src/claude_launcher/workflows/v2/improv-worker-v2.yaml` — `relation`(191-200), `branch-setup`(829-881), `delegate-work`(1304-1352), `integration-request`(2647-2800)
- `src/claude_launcher/workflows/v2/stack-v2.yaml` — 입력(43-63), 넘겨받은 자식(116-122), restack/rebase-notice(313-404), stack-based(406-422)
- `src/claude_launcher/workflows/v2/improv-coordinator-v2.yaml:9-11,35-54,97-99,181-214`
- `docs/mesh-design.md:1354-1382,1739-1790`
- `docs/cflow-v2.md:1914-1990`
