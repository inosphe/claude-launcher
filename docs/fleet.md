# Fleet 프리셋

Fleet 은 에이전트 트리 하나를 프리셋으로 정의해 두고, 대시보드 `#/fleet` 에서 몇 가지 입력만
받아 한 번에 띄우는 기능이다. 프리셋은 실행 시점에 work spec(`docs/work-spec.md`)으로 전개되고,
`#/spec`·`claunch spec import` 와 같은 실행기(`workspec_import.execute`)로 import 된다.
그래서 Fleet 페이지가 그리는 트리와 plan 은 실제 import 가 실행하는 action 과 같다.

| 구성 요소 | 위치 |
|---|---|
| 형식·검증·전개·저장 | `src/claude_launcher/fleet.py`, `tests/test_fleet.py` |
| HTTP 경로 | `src/claude_launcher/daemon/fleet_api.py`, `tests/test_fleet_api.py` |
| 페이지 | `src/claude_launcher/web/static/fleet-page.js`, `tests/web/fleetpage_check.js` |
| 명세 쪽 확장 | `leader: none`, `parent: none`, kind `coordinator`, `inputs`, `start_issue`, `project` (`docs/work-spec.md` 3절 「리더 없는 트리」) |

## 1. 내장 프리셋 `coordinator-leads-workers`

```
coord (coordinator, improv-coordinator-v2)            ← 루트 epic: 오프닝 태스크
├─ lead1 (sub-lead, improv-worker-v2, independent)    ← lane epic (루트 epic 의 자식)
│  ├─ lead1-w1 (worker, improv-worker-v2, stacked)    ← 시작 이슈 대기
│  └─ lead1-w2
└─ lead2 …
```

리드 수(`leads`, 기본 2, 1–6)와 리드당 워커 수(`workers`, 기본 2, 0–6)는 실행 시 고른다.

## 2. 실행 탭의 입력

| 입력 | 의미 |
|---|---|
| 프리셋 | 카드에서 고른다. 오류가 있는 프리셋은 고를 수 없다 |
| Project | 루트 세션의 프로젝트. 고르면 그 프로젝트의 `default_workspace`·`default_mesh` 가 채워진다 |
| Workspace | 보드와 세션 cwd 가 있는 등록 워크스페이스. 없으면 시작 버튼이 비활성이다 |
| Mesh | 비우면 `spec-<이름>` 새 메시. 이미 있는 메시 이름이면 그 메시에 합류(`mesh.mode: attach`)하고, independent 리드는 그 메시 리더의 착지 큐로 착지한다 |
| 오프닝 태스크 | 루트의 태스크. 첫 줄이 루트 epic 의 제목이다 |
| 개수 | 프리셋의 `params` |
| 프로필 | 기본 프로필(루트에 필요), 그룹(레벨)별 프로필, 「멤버별 프로필」을 켜면 트리 카드마다 하나 |
| 고급 | spec 이름(기본 `fleet-<프리셋>-<MMDDHHMM>`), 핸들 접두사 |

프로필 결정 순서는 멤버 지정 → 그룹 지정 → 프리셋 노드의 `profile` → 노드의 `tier`
(`model_tiers.resolve` 로 profile·model) → (루트만) 기본 프로필이다. 아무것도 없는 자식은
부모 프로필을 상속한다. 기존 메시에 합류할 때는 다른 세션의 핸들과 겹치지 않도록 모든 멤버의
핸들에 접두사(기본 `f<MMDDHHMM>`)를 붙인다 — import 는 같은 핸들을 가진 기존 세션을
재사용하기 때문이다. 시작 버튼은 미리보기에서 정해진 이름과 접두사를 그대로 보내므로, 중간에
멈춘 import 를 같은 버튼으로 이어서 실행할 수 있다.

「Spec에서 열기」는 전개된 work spec 을 `#/spec` 편집기로 넘긴다(`SpecPage.load`).

## 3. beads 정렬

| 멤버 | 보드 |
|---|---|
| 루트 | epic 하나(`root`). 제목은 오프닝 태스크 첫 줄, 목표는 태스크 전체. 루트가 assignee |
| `issue: lane` 그룹(내장의 리드) | 인스턴스마다 epic `lane-<id>` 를 부모 이슈 아래 만들고 assignee 로 둔다 |
| `issue: pending` 그룹(내장의 워커) | 이슈 없이 시작한다(`beads: "existing-pending"`). 부모가 자기 이슈 아래 일감 이슈를 만들어 id 를 알려 주면 `flow_input` 으로 시작 입력을 채운다 |
| `issue: none` | 이슈 없이 시작하고 보드를 뒤지지 않는다(`beads: false`) |

부모의 오프닝 태스크에는 미리 띄운 자식 목록(핸들, relation, 이슈 또는 「시작 이슈 대기」)이
실린다.

## 4. 프리셋 형식(`fleet: 1`)

```yaml
fleet: 1
name: <slug>              # 파일 이름이 된다
title: <한 줄>
description: <여러 줄>
params:                   # 실행 시 고르는 개수
  <이름>: {label, default, min, max}
root:                     # 그룹 노드
  id: <slug>              # 그룹 id. 프로필 지정의 키
  kind: coordinator | sub-lead | worker | reviewer
  label: <표시 이름>
  count: <정수 0-12> | <param 이름>      # 루트는 1
  handle: "<템플릿>"      # {n} 인스턴스 번호, {parent} 부모 핸들, {id} 그룹 id
  workflow: <이름>
  inputs: {relation: independent | stacked, ...}   # v2 시작 입력
  issue: lane | pending | none          # 루트는 항상 epic 을 가진다
  profile / tier / model: <값>
  task: <오프닝에 덧붙일 문장>
  children: [<그룹 노드>...]
```

검증 규칙: 루트는 reviewer 가 될 수 없다. 자식을 가지는 것은 루트, sub-lead, coordinator 다.
깊이는 `spawn.max_depth`(3) 이하다. `lane` 은 부모가 이슈를 가질 때만 쓸 수 있다. 인스턴스
핸들이 겹치면 거절한다. 기본 개수로 전개한 멤버가 40개를 넘으면 거절한다.

## 5. 저장 위치와 HTTP

사용자 프리셋은 `<런처 홈>/fleet-presets/<name>.yaml` 에 저장된다. 내장 프리셋과 같은 이름으로
저장하면 그 파일이 내장을 대신하고, 지우면 내장이 다시 보인다.

| 경로 | 동작 |
|---|---|
| `GET /api/fleet/presets` | 프리셋 목록(그룹·기본 개수의 멤버 포함), kind·issue 모드·tier 목록 |
| `PUT /api/fleet/presets/{name}` | `{yaml}` 또는 `{doc}` 저장. 이름이 주소와 다르면 400 |
| `DELETE /api/fleet/presets/{name}` | 사용자 프리셋 삭제 |
| `POST /api/fleet/check` | 편집기의 실시간 검증. 읽을 수 없는 텍스트만 400 |
| `POST /api/fleet/expand` | `{preset|yaml|doc, launch}` → work spec, 멤버, plan, 보드 |
| `POST /api/fleet/launch` | 같은 입력으로 import. 에이전트 호출자는 403. 중간 정지는 409 + `log`, `resumable` |

launch 는 사람의 동작이다. 자식은 데몬 안에서 `api.spawn_child_as_person` 으로 만들어지고,
`spawn.check(person_fields=True)` 로 사람이 고른 profile·model·effort 가 `spawn.allow_profile`
설정과 무관하게 적용된다. 그 밖의 spawn 정책(args, 깊이 한도 등)은 그대로다. 에이전트가
fleet 을 원하면 work spec 을 써서 사용자에게 import 를 요청한다.
