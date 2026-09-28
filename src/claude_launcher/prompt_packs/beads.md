---
title: Beads board (br · bv)
description: 이 워크스페이스의 beads 보드를 claunch beads로 읽고 쓰는 규칙, bv로 다음 일감을 고르는 방법. Agent Mail은 다루지 않는다.
---
## beads 보드 — 이 워크스페이스의 일감 기록

이 워크스페이스의 일감은 beads 이슈로 존재한다. 이 세션이 끝난 뒤에도 남아야 하는
일(범위, 완료 기준, 누가 맡았는가, 어디까지 했는가)은 이슈와 그 코멘트에 적는다.
대화에만 남긴 결정은 다음 세션이 읽지 못한다.

### 호출

- 보드 명령은 `claunch beads <br 인자>`로 부른다. claunch가 이 워크스페이스의 보드
  파일을 찾아 `--db`를 붙이고, 쓰기에는 `--actor $CLAUNCH_SESSION`을 붙이도록 한다.
  claunch 세션 밖(데몬이 없는 셸)에서만 `br`을 직접 쓴다.
- 조회는 항상 `--json`으로 받는다: `claunch beads show <id> --json`,
  `claunch beads list --status open --json`, `claunch beads ready --json`.
- 보드 파일 위치는 `claunch beads where`의 `database:` 줄이다.
- **`.beads/issues.jsonl`을 직접 고치지 않는다.** 보드의 실체는 DB이고 jsonl은
  내보낸 산출물이다. 손으로 쓴 줄은 다음 동기화에서 경고 없이 사라진다.

### 이슈를 만들 때

1. 만들기 전에 중복을 확인한다: `claunch beads search "<핵심어>" --json`.
   같은 일이 이미 있으면 새로 만들지 않고 `claunch beads comments add <id>`로 덧붙인다.
2. 제목은 한 줄로 「무엇을 어떻게」를 적는다.
3. 본문(`--description`)에는 `## 목표`, `## 범위(포함·제외)`, `## 완료 기준`,
   `## 출처`를 둔다. 이전 대화를 모르는 사람이 읽고 바로 시작할 수 있어야 한다.
4. 우선순위: P1은 실행 중인 기능의 고장이나 다른 작업을 막는 일, P2는 지시받은
   일(기본값), P3은 정리·후속·발견한 개선, P4는 아이디어다.
5. 선후 관계가 있으면 `claunch beads dep add <뒤> <앞>`으로 적는다.

### 맡고, 진행하고, 닫을 때

- 맡을 때: `claunch beads update <id> --status in_progress --assignee $CLAUNCH_SESSION`.
- 기록은 코멘트로 남긴다: 무엇을 바꿨는가, 커밋 해시, 테스트 결과, 남은 문제.
  문단보다 항목 단위의 줄로 적는다.
- 닫을 때는 근거와 함께 닫는다: `claunch beads close <id> --reason "<완료 근거>"`.
  완료 기준을 채우지 못했으면 닫지 않고 남은 것을 코멘트로 적는다.

### 사람에게 넘긴 이슈

assignee가 사람인 이슈(이 워크스페이스의 다른 프롬프트 묶음이 그 이름을 정한다.
예: Unreal 묶음의 `operator`)는 에이전트가 맡지 않는다.
`claunch beads ready`의 결과에도 이런 이슈가 섞여 나오므로, 일감을 고를 때 이
이슈들은 건너뛴다. 사람이 작업을 끝냈다는 확인이 없으면 에이전트가 닫지 않는다.

### bv로 다음 일감 고르기 (bv가 설치되어 있을 때)

bv(beads viewer)는 보드의 의존 그래프를 분석해서 다음에 할 일을 추천한다.

- **`bv`를 인자 없이 실행하지 않는다.** 대화형 화면(TUI)이 떠서 세션이 멈춘다.
  에이전트는 `--robot-*` 옵션만 쓴다.
- 보드 파일을 명시한다. `claunch beads where`의 `database:` 경로를 `--db`로 넘긴다:
  - 한 건만: `bv --db "<database 경로>" --robot-next -f json`
  - 전체 분류: `bv --db "<database 경로>" --robot-triage -f json --robot-max-results 10`
  - 실행 순서: `bv --db "<database 경로>" --robot-plan -f json`
- bv 출력의 `claim_command`(`br update ... --status=in_progress`)는 그대로 실행하지
  않는다. 위의 `claunch beads update`로 맡는다.
- bv의 추천은 참고 자료다. 사람에게 넘긴 이슈, 이미 다른 세션이 맡은 이슈는
  추천되더라도 건너뛴다.
- 이슈가 많은 보드에서는 첫 실행에 수십 초가 걸릴 수 있다. 같은 턴 안에서
  반복해 부르지 않는다.
