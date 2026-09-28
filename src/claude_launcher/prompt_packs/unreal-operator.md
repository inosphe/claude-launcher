---
title: Unreal — operator 위임
description: 코드만으로 끝낼 수 없는 Unreal 에디터 작업(에셋, Blueprint, Project Settings, PIE 검증)을 beads 이슈로 사람(operator)에게 넘기는 규칙.
---
## Unreal Operator 위임 지침

이 Unreal 프로젝트에는 코딩 에이전트가 코드 수정만으로 안정적으로 끝낼 수 없는 작업이
많다. 에셋 생성, Blueprint 연결, 실제 리소스 선택, PIE 테스트, Project Settings 설정처럼
사람의 판단이나 에디터 조작이 필요한 작업은 beads 이슈로 `operator`에게 넘긴다.

여기서 `operator`는 **사람**(에디터 작업자)의 assignee 이름이다. claunch의 operator
세션(프로젝트를 지켜보는 에이전트)과는 다르다. 이 이슈를 에이전트 세션에 배정하지 않는다.

operator는 한국어 화자이므로, `operator`에게 배정하는 이슈의 제목과 본문은 한국어로 쓴다.
코드 심볼, 파일 경로, 에셋 클래스명, 콘솔 명령은 원문 그대로 써도 되지만 설명 문장은
한국어로 쓴다.

### operator 이슈를 만들어야 하는 경우

다음 작업은 에이전트가 직접 처리하려 하지 말고 `--assignee operator` 이슈를 만든다.

- DataTable, DataAsset, Blueprint, Widget, Material, Montage, Niagara, Sound, Map, Input Action,
  Gameplay Tag 같은 Unreal 에셋을 만들거나 실제 프로젝트 에셋 중 하나를 골라야 하는 경우
- Blueprint 기본값, class reference, soft object reference, component reference, socket,
  widget class, montage section, animation notify, collision profile, input action을 에디터에서
  연결해야 하는 경우
- `UDeveloperSettings` 값을 `Project Settings`에서 설정해야 하는 경우
- 여러 후보 에셋 중 어느 것이 실제 production/test map용 리소스인지 코드만으로 판단하기
  어려운 경우
- Play/PIE/network PIE 실행, 맵 열기, viewport 확인, 로그 확인, 애니메이션·오디오·VFX·HUD
  육안 검증이 필요한 경우
- 에셋 교체, 마이그레이션, redirector 정리, 대량 resave처럼 되돌리기 어렵거나 에디터 상태에
  크게 의존하는 경우
- MCP나 에디터 도구로 조작할 수는 있어도, 올바른 리소스 선택이 불명확해서 사람의 판단이
  필요한 경우

이런 항목은 최종 응답에 TODO로만 남기지 말고 이슈를 만든다.

### 명령

```powershell
claunch beads create "Operator: <한국어 작업 제목>" `
  --type task `
  --priority P2 `
  --assignee operator `
  --labels operator,unreal,manual `
  --description "<한국어 markdown 본문>"
```

우선순위 기준:

- `P1`: 런타임 정확성, 서버·클라이언트 권위, 출시·검증 경로를 막는 작업
- `P2`: 기능 동작에 필요하지만 코드 작업은 따로 진행할 수 있는 작업
- `P3`: 정리, 선택적 polish, 문서화, 막지 않는 검증

필요하면 `asset`, `blueprint`, `settings`, `data`, `animation`, `hud`, `network`, `playtest`
같은 라벨을 더한다.

### 이슈 본문

operator가 이전 대화를 복원하지 않고도 바로 수행할 수 있어야 한다.

```markdown
## 위임 사유
<에디터 전용 작업, 에셋 선택, 리소스 판단, 수동 플레이테스트 등>

## 작업 내용
<Unreal Editor 또는 외부 UI에서 수행할 구체적 작업>

## 대상
- 설정/에셋/클래스:
- 예상 위치: <Content Browser 경로 또는 Project Settings 경로>
- 관련 코드: <파일 경로, class/function 이름, commit/issue ID>

## 완료 조건
<operator가 완료 여부를 판단할 수 있는 관찰 가능한 결과>

## 검증 절차
<PIE, network PIE, 로그, 시각 확인, DataTable 확인 등>

## 참고 / 열린 선택지
<후보 에셋, 위험, operator가 결정할 질문>
```

### 작업 규칙

- 서로 독립적인 에셋 설정, Blueprint 연결, 플레이테스트는 별도 이슈로 나눈다.
  한 기능의 검증에 반드시 함께 해야 하는 작업만 한 이슈로 묶는다.
- 같은 내용의 operator 이슈가 이미 있으면 새로 만들지 않고 기존 이슈를
  `claunch beads comments add` 또는 `update`로 고친다.
- 코드 변경이 operator 작업에 의존하면 최종 응답에 이슈 ID를 적고, operator가 끝낼
  때까지 무엇이 막혀 있는지 명확히 쓴다.
- operator 이슈를 만든 뒤에도 독립적인 코드 작업은 계속 진행한다.
- operator가 수동 작업을 끝냈거나, 믿을 수 있는 에디터·도구 피드백으로 완료가 확인된
  경우가 아니면 에이전트가 operator 이슈를 닫지 않는다.
