# 일감 명세(work spec)

상태: 설계와 검증기(`src/claude_launcher/workspec.py`)까지 구현됨. 실제로 세션을
띄우고 보드에 쓰는 실행기는 후속 작업이다(아래 「구현 범위」).

일감 명세는 YAML 문서 하나다. 복잡한 일감 하나에 필요한 다음 항목을 모두 담는다.

- 에이전트 계층: 리더, 2nd leader(sub-lead), 워커, 리뷰어, 서브롤 프롬프트를 가진 워커
- 사전에 정해 둔 일감 목록과 그 의존 관계
- 참고 자료
- 계약(일감 사이의 경계 규약, 일감마다의 완료 조건)

명세를 import 하면 메시 생성, 역할 어휘 업로드, beads 이슈 생성, 리더 결정, 세션
spawn, 연결이 한 번에 실행된다. 명세 하나가 메시 하나를 이루며, 리더는 import
시점에 새로 띄울 수도 있고(own) 기존 세션을 공유할 수도 있다(shared).

예시: `docs/examples/work-spec.example.yaml`. 테스트: `tests/test_workspec.py`.

## 1. gds6 issue-gen 이 내는 것

명세의 일감·계약 부분은 issue-gen 의 출력 형태를 그대로 받도록 설계했다. 출처는
`~/works/gds6` 이다.

| 항목 | issue-gen 의 위치 | 명세에서의 자리 |
|---|---|---|
| frame → epic | `issue-gen/src/beads.ts:239-273` `frameIssueDrafts` | `issues[]`, `type: epic` |
| 설계 일감 `ImpactWork` | `issue-gen/src/impact.ts:47` | `issues[]`, `type: task` |
| 일감별 계약 `WorkContract` (accept·stores·seams·events·gates·allowlist·done_when) | `impact.ts:31-45` | `issues[].contract`(키 그대로) |
| 선행 일감 `after` / `depends_on` | `impact.ts:47`, `impact-design.ts:405` | `issues[].blocked_by` |
| 변경 목록 `ImpactChange` | `impact.ts:18` | `issues[].changes` |
| 규칙 id | `ImpactWork.rule_ids` | `issues[].rule_ids` |
| 크기 S/M/L | `ProposedWork.size` | `issues[].size` |
| 구현 결정 `ImplDecision` / `ProposedDecision` | `impl-decisions.ts:39`, `impact-design.ts:405-453` | `decisions[]` |
| 사람 몫 단계 | `impact.ts:383` `humanSteps` | `human_steps[]` + `assignee: human` 이슈 |
| 규칙 문장·출처 인용·시나리오 | 패킷 본문(`packet.ts:107`)에 텍스트로 포함 | `references[]` (`kind: text`) |

issue-gen 이 내지 않는 항목은 다음과 같다. 명세는 이 항목들을 직접 기술한다.

- 에이전트 역할·계층. issue-gen 에 있는 「역할」은 frame 항목의 역할(target/context/
  excluded)과 결정의 owner(programmer/animation/graphics/sound)뿐이다.
- 리뷰어 배정, 서브롤 프롬프트(예: unreal)
- 파일로 붙는 참고 자료
- 일감 사이의 계약. issue-gen 의 계약은 일감 하나에 속한다.

현재 `data/` 에는 실제 생성 출력이 없다. `impact_work_revisions`·`beads_issues` 테이블이
0행이다. 그래서 예시는 `issue-gen/tests/impact-design.test.ts:44-48` 의 픽스처(W1·W2)를
옮겨 만들었다.

## 2. 최상위 구조

```yaml
spec: 1                 # 형식 버전
name: <slug>            # 메시 이름·외부 참조의 접두사
title: <한 줄>
source: {...}           # 출처 기록(generator, workspace, frame, scan, origin_board) — 동작에 영향 없음
workspace: <이름>       # 세션 cwd·보드가 있는 등록 워크스페이스
mesh: {mode, name}
leader: {mode, own, shared}
roles: {...}            # 메시 역할 어휘 업로드 문서
subroles: {<이름>: {prompt}}
agents: [...]
references: [...]
contracts: [...]
issues: [...]
decisions: [...]
human_steps: [...]
```

알 수 없는 키는 검증에서 거절된다(최상위·agents·issues·issues[].contract).
값의 형태(매핑이어야 할 자리, 문자열 목록이어야 할 자리)는 다른 검사보다 먼저 확인되고,
틀리면 그 오류만 돌려준다. 생성기가 만든 입력에 형식 오류가 있어도 검증기는 예외로 끝나지
않고 오류 목록을 돌려준다.

## 3. 블록별 의미

### mesh

| 키 | 값 | 의미 |
|---|---|---|
| `mode` | `new`(기본) | 이 명세 전용 메시를 만든다 |
| | `attach` | 기존 메시에 붙인다. 리더는 그 메시의 현 리더로 고정된다(5절 「attach」) |
| `name` | 문자열 | 생략하면 `spec-<name>` 이다. `attach` 에서는 필수다 |

### leader

| 키 | 의미 |
|---|---|
| `mode` | `ask`(기본) / `own` / `shared`. import 가 선택을 주지 않을 때 쓰는 값이다. `ask` 는 기본값이 없으므로 선택 없이 import 하면 거절된다 |
| `own` | 새 리더 세션 정의(`name`, `profile`, `model`, `workflow` — 기본 `improv-leader`) |
| `shared.candidates` | 공유를 허용하는 기존 세션 목록. 비우면 어느 세션이든 지정할 수 있다. 하나뿐이면 `--leader shared` 만으로 그 세션이 선택된다 |
| `shared.handle` | 공유 리더가 이 메시에서 쓰는 핸들(기본 `leader`). 메시마다 다른 핸들을 쓸 수 있다 |

리더는 `agents` 에 적지 않는다. `agents[].parent` 에서 리더는 예약 id `leader` 로 가리킨다.

### roles · subroles

- `roles` 는 `claunch mesh roles --file` 이 받는 문서와 같은 형식이다. 검증기는 데몬과 같은
  파서(`daemon/mesh_roles.resolve`)로 읽는다. 따라서 명세가 통과하면 메시도 그 어휘를
  받는다. 커스텀 주 역할과 그 stance 는 여기에 둔다.
- `subroles.<이름>.prompt` 는 서브롤 프롬프트다. import 는 서브롤마다 두 가지를 한다.
  1. 같은 이름의 메시 역할을 어휘에 추가한다(stance = prompt). 데몬은 어휘에 없는 서브롤을
     거절하기 때문이다(`daemon/mesh.py:4314-4318`).
  2. 그 서브롤을 가진 에이전트의 오프닝 태스크 앞에 prompt 를 붙인다. 현재 브리핑에는 주
     역할의 stance 만 실리고(`daemon/mesh.py:3093-3136`) 서브롤의 stance 는 전달되지 않기
     때문이다.
- 서브롤 이름이 기존 역할 이름이나 별칭과 같으면 거절된다. 그 이름은 이미 다른 의미로 쓰이고
  있다.

### agents

| 키 | 의미 |
|---|---|
| `id` | 명세 안의 이름(slug). `leader` 는 예약어다 |
| `kind` | `sub-lead` / `worker`(기본) / `reviewer` |
| `parent` | `leader`(기본) 또는 `sub-lead` 인 에이전트의 id |
| `handle`, `profile`, `model`, `harness`, `worktree`, `task` | spawn 인자로 그대로 전달된다 |
| `role` | kind 의 기본 역할을 바꿀 때만 쓴다. exclusive 역할(leader)은 거절된다 |
| `workflow` | kind 의 기본 워크플로를 바꿀 때만 쓴다 |
| `subroles` | `subroles` 블록에 정의된 이름 |
| `connect` | 부모·자식 외에 직접 대화할 에이전트 id |

에이전트 큐는 `agents` 에 적지 않는다. `issues[].assignee` 에만 적는다. 큐의 정본은 보드
하나이고, 두 자리에 적으면 서로 달라질 수 있기 때문이다.

### references

`id`, `title`, `kind` 와, kind 이름의 키에 그 값을 둔다.

| kind | 값 |
|---|---|
| `file` | 워크스페이스 기준 경로 |
| `url` | 주소 |
| `text` | 본문(규칙 문장, 출처 인용 등) |
| `issue` | 기존 beads 이슈 id |

### contracts

일감 사이에 걸친 규약이다. `id`, `kind`(seam / event / store / interface / policy),
`title`, `text`(필수), `parties`(이 계약을 지켜야 하는 에이전트), `refs` 로 구성된다.
import 는 계약마다 `--type doc` beads 이슈를 하나씩 만든다. 정본 규칙은 doc 이슈 본문에
둔다는 보드 규칙(improv-worker intake 의 「beads(br) 규칙 — 공통」)을 따른 것이다.

일감 하나에만 속하는 계약은 `issues[].contract` 에 둔다. 이 자리는 gds6 `WorkContract` 를
키 그대로 받는다.

### issues

| 키 | 의미 |
|---|---|
| `key` | 명세 안의 이름. 외부 참조 `spec:<name>:<key>` 가 된다 |
| `type` | epic / task(기본) / bug / doc / chore / feature (`daemon/beads.py:214` 와 같은 목록) |
| `title`, `priority`(0-4, 기본 2) | |
| `goal`, `scope`, `done_when` | 이슈 본문의 `## 목표 / ## 범위 / ## 완료 증거 기준` 이 된다 |
| `parent` | 이 명세 안의 epic |
| `blocked_by` | 선행 이슈 key. `br dep add <me> <dep> -t blocks` 가 된다 |
| `assignee` | 에이전트 id, 또는 `human`(assignee 없이 `human` 라벨) |
| `review` | 리뷰어 에이전트 id |
| `refs`, `contracts` | references·contracts id |
| `contract`, `changes`, `rule_ids`, `size` | gds6 출력 그대로 |

epic 은 sub-lead 만 가질 수 있다. sub-lead 는 epic 을 주 이슈로 잡고 자식 결과를 stack
브랜치로 모은다. doc 은 배정하지 않는다.

### decisions · human_steps

`decisions[]` 는 아직 답이 없는 구현 결정이다(`key`, `owner`, `question`, `options`,
`recommended`, `gates`). `gates` 에 적힌 이슈는 그 결정에 답이 나기 전에는 착수하지
않는다. import 는 결정마다 해당 이슈에 `BLOCKED: decision <key>` 코멘트를 남기고 리더에게
넘긴다. `human_steps[]` 는 issue-gen 형식 그대로이며 `assignee: human` 이슈 본문에 실린다.

## 4. 에이전트 계층과 claunch 대응

| kind | 메시 역할 | 기본 워크플로 | 만드는 방법 |
|---|---|---|---|
| leader | `leader` (exclusive) | `improv-leader` | own: spawn / shared: 기존 세션을 `mesh join` |
| sub-lead (2nd leader) | `worker` | `improv-worker` + `stack` 서브 런 | 리더의 자식으로 spawn |
| worker | `worker` | `improv-worker` | 부모(리더 또는 sub-lead)의 자식으로 spawn |
| worker + 서브롤 | `worker` + 서브롤 | `improv-worker` | 위와 같고, `subroles` 와 태스크 앞 prompt 가 붙는다 |
| reviewer | `reviewer` | 없음(ask 응답) | 리더의 자식으로 spawn. 리뷰어-워커 연결은 어휘의 `auto_link` 규칙이 만든다(`daemon/mesh_roles.py:377-397`) |

2nd leader 를 메시 역할 `leader` 로 두지 않는 이유는 다음과 같다. 메시 하나에서 `leader` 는
exclusive 역할이고 살아 있는 보유자는 한 명뿐이다(`daemon/mesh.py:4275-4299`, 서브롤로
가져도 같다 `:4301-4331`). 패키지 리더 stance 도 두 번째 지휘선은 nested worker 가 된다고
적는다(`daemon/mesh_roles.py:427-431`). 그래서 `sub-lead` 는 worker 역할로 `stack` 서브 런을
돌린다(`workflows/stack.yaml`, 스킬 `mesh-delegate`). 자식의 착지 요청은 sub-lead 가 받아
스택 브랜치로 모은다. 리더는 sub-lead 의 브랜치 하나만 통합한다.

깊이 한도는 `spawn.max_depth`(기본 3, 리더가 0)이고, 검증기가 같은 값으로 거절한다.
이 계산은 리더가 spawn 트리의 뿌리(깊이 0)라고 가정한다. own 은 항상 그렇다. shared 나
attach 의 리더가 이미 깊이 d 인 세션이면 실제 한도는 3-d 이고, 그 차이는 실행 시점의
spawn 정책이 거절한다. 부모마다
자식 수의 소프트 한도(`max_children` 4)는 거절하지 않는다. 그 값은 import 실행 시점의 spawn
정책이 판정한다.

## 5. 리더: own 과 shared

import 하는 사람이 `--leader own` 또는 `--leader shared:<세션>` 을 준다. 주지 않으면 명세의
`leader.mode` 를 쓰고, 그 값이 `ask` 면 import 는 거절된다(`workspec.resolve_leader`).

### shared — 한 세션이 여러 메시의 리더

`leader` 의 exclusive 판정은 메시 하나의 멤버만 본다. 그래서 한 세션이 여러 메시에 동시에
리더로 속할 수 있다(`daemon/mesh.py:1511` `meshes_for_session`). shared 는 기존 리더 세션을
새 메시에 `mesh join --role leader` 로 합류시킨다. `mesh.mode: new` 의 메시에는 리더가
없으므로 exclusive 충돌은 생기지 않는다. 이 성질은 new 에서만 성립한다(아래 attach).

shared 를 고를 때 감수하는 충돌은 다음과 같다.

| 항목 | 내용 |
|---|---|
| mesh 생략 spawn 거절 | 부모가 둘 이상의 메시에 속하면 메시를 지정하지 않은 spawn 은 거절된다(`daemon/onboard.py:182-189`). 공유 리더가 이후 자식을 만들 때마다 메시를 명시해야 한다. import 실행기는 항상 명시한다 |
| cflow 런 하나 | 세션의 주 런은 하나다. 공유 리더의 `improv-leader` 런 하나가 모든 메시의 착지 요청(landing queue)을 받는다. 메시별로 런을 나눌 수 없다 |
| 터미널 하나 | 모든 메시의 메시지가 같은 터미널에 입력되므로 리더의 컨텍스트 사용량이 메시 수에 비례해 늘어난다 |
| 보드 공유 | 같은 저장소면 보드도 하나다. 명세의 이슈는 외부 참조 `spec:<name>:` 와 라벨 `spec:<name>` 으로 구분한다 |
| 원격 보유자 | 다른 머신의 리더는 항상 살아 있는 것으로 계산된다(`daemon/mesh.py:4427-4450`). 공유 리더가 원격이면 그 메시에서 리더를 교체하려면 먼저 그 멤버를 내보내야 한다 |

통합 지점이 하나라는 점은 shared 의 장점이다. 같은 master 에 머지하는 리더가 하나이므로
명세 사이의 머지 순서를 그 리더 한 명이 정한다.

### own — 명세 전용 리더

명세 메시에서만 리더인 새 세션을 spawn 한다. 컨텍스트는 분리된다. 대신 같은 저장소의 master
를 두 리더가 각자 통합하므로 머지 경합이 생긴다. 이 경합은 각 리더의 머지 단계
(`mergecheck`, 통합 프리뷰 스윕)가 머지 시점에 처리하고, 명세 import 는 처리하지 않는다.
`mesh.mode: attach` 에서는 own 을 고를 수 없다.

### attach — 기존 메시의 리더

기존 메시에는 이미 리더가 있다. 다른 세션을 리더로 합류시키면 exclusive 검사
(`daemon/mesh.py:4275-4299`)가 거절하고, 현 리더를 합류시키면 이미 멤버이므로 중복이다.
그래서 attach 에서 리더는 선택 대상이 아니고 그 메시의 현 리더로 고정된다
(`resolve_leader` 가 `mode: existing` 을 돌려준다).

| import 선택 | 결과 |
|---|---|
| 없음 / `ask` / `shared` | `leader-check` (session 없음) — 실행기가 메시의 현 리더를 읽어 그 세션을 부모로 쓴다. 현 리더가 없으면 중단한다 |
| `shared:<세션>` | `leader-check` (session 지정) — 실행기가 그 세션이 메시의 살아 있는 리더인지 확인하고, 아니면 아무것도 만들지 않고 중단한다 |
| `own` | 거절 |

`leader-check` 는 확인만 하는 action 이고, `mesh-join` 은 attach 에서 생성되지 않는다.

## 6. import 절차

`workspec.plan(doc, leader_choice)` 가 실행 순서를 action 목록으로 돌려준다. 실행기는 이
목록을 차례로 실행한다.

1. **검증** — `validate` 오류가 하나라도 있으면 아무것도 하지 않는다.
2. **references** — `file` 은 존재를 확인하고, `text`/`url` 은 epic 본문의 참고 절에 싣는다.
3. **계약 doc 이슈** — `contracts[]` 마다 `type: doc` 하나.
4. **이슈** — epic 이 먼저 생성되고, 그 뒤 나머지 이슈가 생성된다. 각 이슈는
   `--external-ref spec:<name>:<key>` 를 갖는다.
5. **의존** — `blocked_by` 마다 `br dep add`. claunch API 에 의존 생성이 없으므로
   `claunch beads dep add` 로 실행한다.
6. **메시** — `mode: new` 면 `POST /api/mesh`. 이어서 `roles` 와 서브롤 역할을
   `PUT /api/mesh/{m}/roles` 로 업로드한다.
7. **리더** — own: spawn(`role: leader`, `workflow: improv-leader`) / shared: `mesh join` /
   attach: 이 자리에는 아무것도 없다. attach 의 `leader-check` 는 plan 의 맨 앞(1단계 직후)에
   오고, 실패하면 보드 쓰기를 포함한 뒤의 action 을 하나도 실행하지 않는다.
8. **에이전트** — 부모가 먼저 생성된다. 각 spawn 은 `mesh` 를 명시하고, 큐의 첫 이슈
   (priority 오름차순)를 `issue` 로 받는다. 이슈가 이미 있으므로 데몬은 새 이슈를 만들지
   않고 take 한다(`daemon/beads.py:839-881`). 큐의 나머지는 `update --assignee` 로 배정한다.
   sub-lead 의 오프닝 태스크에는 stack 서브 런을 열라는 지시가 붙는다.
9. **연결** — `connect` 마다 `PATCH /api/mesh/{m}/members/{a}/links/{b}`.

이슈를 메시보다 먼저 만드는 이유는 spawn 이 `--issue` 로 기존 이슈를 가리키게 하기
위해서다. 반대 순서면 데몬이 오프닝 태스크에서 이슈를 새로 만든다.

재실행: 이슈의 외부 참조, 메시 이름, 메시 안의 에이전트 handle 이 고정되어 있으므로
실행기(`workspec_import.execute`)는 이미 존재하는 항목을 재사용하고 새로 만들지 않는다.
의존과 결정 코멘트는 쓰기 전에 보드에서 같은 의존·같은 문구의 코멘트가 있는지 확인한다. 그래서 중간에 멈춘 import 는 같은
명령을 다시 실행하면 이어서 진행된다.

### 실행기와 CLI

```
claunch spec check  <file>                         # 검증 오류 목록, 없으면 ok
claunch spec plan   <file> --leader shared:s29     # action 목록(쓰기 없음)
claunch spec import <file> --leader own --profile <p>
```

대시보드의 Spec 페이지(`#/spec`)는 같은 세 동작을 `POST /api/spec/check`·`plan`·`import` 로
부른다. 바디는 `{"yaml": <명세 본문>, "leader": "own|shared:<세션>", "profile": "<p>",
"workspace": "<등록 워크스페이스 이름>"}` 이다. `workspace` 는 명세에 `workspace` 가 없을 때만
쓰고, 명세의 값과 다르면 거절한다. check·plan 의 `spec` 에는 import 가 쓸 보드 디렉터리
(`board_dir`)나 그것이 없는 이유(`board_error`)와 등록 워크스페이스 목록(`workspaces`)이 실린다.
검증 오류는 400 과 `errors` 목록으로, 리더 선택 오류는 400 과 `leader_error: true` 로, 보드를
정할 수 없는 import(명세와 바디 어디에도 `workspace` 가 없거나 등록되지 않은 이름)는 400 과
`workspace_error: true` 로 답하며 아무것도 쓰지 않는다.
import 가 중간에 멈추면 409 에 그때까지의 `log` 와 `resumable: true` 를 싣는다.

| 항목 | 동작 |
|---|---|
| 보드 위치 | 명세의 `workspace`(등록 워크스페이스 이름)가 가리키는 디렉터리. CLI 는 없으면 현재 디렉터리를 쓴다. 웹 경로는 바디의 `workspace` 를 쓰고, 둘 다 없으면 거절한다(데몬의 현재 디렉터리는 데몬을 띄운 셸의 위치이고 데몬은 모든 프로젝트를 다루므로 기본값으로 쓰지 않는다). `file` 참고 자료도 이 디렉터리 기준이다. 웹 경로에서 고른 `workspace` 는 spawn 되는 세션의 워크스페이스로도 쓰인다 |
| 보드 쓰기 | `claunch beads create/dep add/comments add/update` 를 하위 프로세스로 실행한다 |
| 이슈 본문 | `## 목표 / ## 범위 / ## 완료 증거 기준` 뒤에 참고 자료(epic 은 전부), 계약 doc 이슈 id, gds6 필드(`rule_ids`·`size`·`changes`·`contract`), 사람 몫, 미결 결정, `## 출처` 를 붙인다 |
| own 리더 | `POST /api/sessions` 로 만드는 최상위 세션이다. 세션에는 profile 이 필요하므로 `--profile` 이 없으면 거절한다. 리더 세션에는 이슈를 만들지 않는다(`beads: false`) |
| 에이전트 spawn | `POST /api/sessions/{부모}/children`. reviewer 가 아닌 에이전트는 각자 worktree `spec-<name>-<id>` 를 받는다(`worktree` 로 바꿀 수 있다). 워크플로가 비어 있는 에이전트(reviewer)는 `-` 로 보내 부모 런의 기본 자식 워크플로를 받지 않게 한다. 큐가 빈 에이전트는 바디 `beads: false` 로 보내 이슈를 만들지 않는다(MCP spawn 툴의 `no_issue` 는 API 에 닿기 전에 이 값으로 바뀐다, `mesh_mcp._SPAWN_RENAMED`) |
| 역할 업로드 | 메시의 현재 사용자 정의 역할 문서에 명세의 `roles` 와 서브롤(`stance` = prompt)을 덮어 `PUT /api/mesh/{m}/roles` 로 올린다 |
| 중단 | `leader-check` 실패, 없는 `file` 참고 자료, 데몬 오류에서 멈춘다. 그 전에 실행된 action 은 남고, 재실행이 이어서 진행한다 |

## 7. 구현 범위

| 항목 | 상태 |
|---|---|
| 형식, 검증(`validate`), 리더 결정(`resolve_leader`), 실행 순서(`plan`) | 구현됨, `tests/test_workspec.py` |
| 실행기(보드 쓰기, 메시 생성, spawn, 연결)와 `claunch spec check/plan/import` CLI | 구현됨, `src/claude_launcher/workspec_import.py`, `tests/test_workspec_import.py` |
| 웹 UI 에서 명세 검증·plan·import | 구현됨. 대시보드 `#/spec`(`web/static/spec-page.js`), API `POST /api/spec/check·plan·import`(`daemon/spec_api.py`), `tests/test_spec_api.py`, `tests/web/specpage_check.js` |
| 서브롤 stance 를 브리핑에 싣기(현재는 태스크 앞 prompt 로 대신함) | 후속 작업. 관련: `claunch-a6azt` |
| gds6 issue-gen 이 이 명세를 직접 내보내는 출력기 | gds6 브랜치 `s90-gds6-workspec-export` 에 있음, gds6 master 미머지(`claunch-j8hhs`) |
| 참고 자료 파일을 세션 워크트리에 복사하는 방식 | 미정 |
