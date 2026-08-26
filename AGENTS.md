# AGENTS.md — 이 저장소에서 에이전트가 지키는 규칙

## 워크플로(cflow)는 패키지 안이 정본이다

- 정본은 `src/claude_launcher/workflows/*.yaml` 하나뿐이다. 워크플로 문구·스텝·
  게이트를 바꿀 일이 있으면 **여기만 고친다.**
- 세션이 실제로 읽는 사본은 두 층이다:
  - 전역 레이어 `~/.claude-launcher/workflows/` — `claunch install --global`이
    처음 심고, `claunch cflow update [--force]`가 패키지 기준으로 갱신한다
    (에이전트 세션은 물어볼 수 없으므로 편집된 사본을 덮을 때 `--force`가 필요하다).
  - 프로젝트 레이어 `<repo>/.claunch/workflows/` — 있으면 전역보다 우선한다.
    이 저장소의 프로젝트 레이어는 **정본 + `verify:`(저장소 특화 스윕 명령)** 이외의
    것을 담지 않는다. 정본과 그 이상으로 다르면 드리프트이고, 같은 이름으로 두
    규칙이 도는 사고다 — `tests/test_project_layer_override.py`와
    `tests/test_beads_protocol.py`가 그것을 잡는다.
- 그래서 프로젝트 레이어 파일을 **손으로 고치지 않는다.** 정본을 고친 뒤
  `uv run --no-sync python tools/sync_project_layer.py`를 돌린다 — 패키지 사본에
  프로젝트 레이어의 `verify:` 줄(과 바로 위 주석)만 접붙여 다시 쓴다. 문구를 두
  파일에 나란히 타이핑하는 것은 같은 결과를 내더라도 절차가 아니다 — 다음 사람이
  정본만 고치고 끝내는 순간 갈린다. `--check`가 테스트로 돌아 드리프트를 잡는다.
  (`claunch cflow add --project --force`는 패키지 바이트로 덮어 `verify:`를 잃고,
  `claunch cflow update`는 전역 레이어만 본다 — 프로젝트 레이어에는 이 스크립트다.)
- 정본을 바꾼 머지가 master에 오르면 리더가 `claunch cflow update --force`로 전역
  레이어도 맞춘다 — 프로젝트 레이어가 없는 워크플로(예: improv-mid)는 전역 사본이
  실제로 도는 파일이다.

## 일감의 정본은 beads다

- 세션이 한 회차를 쓸 일, 다른 세션·회차로 넘어갈 일은 이슈로 존재한다. 호출은
  항상 `claunch beads <br 인자>` — 워크트리에서도 저장소 루트의 보드 하나를 쓴다.
- 누가 만들고 누가 상태를 옮기는지는 워크플로 정본(improv-worker/leader/mid의
  intake에 있는 「beads(br) 규칙 — 공통」 절)이 말한다. 여기에 다시 적지 않는다 —
  두 자리에 적으면 갈린다.
- 데몬도 보드를 쓴다(`src/claude_launcher/daemon/beads.py`) — 에이전트가 자리에
  없는 세 순간만. (1) 생성: task를 가진 세션(new-session, 웹 폼, spawn)에 이슈를
  만들어 assignee로 배정하고 오프닝에 `issue: <id>`를 실어 준다. 요청이 기존
  이슈를 말하면(`--issue`/`issue` 필드/task의 `issue: <id>`, 위저드·웹 폼의
  Board 라디오) 새로 만들지 않고 `beads.adoption`이 소유권을 판정한다 — 비었거나
  이미 그 세션 것이거나 붙잡던 세션이 종료됐으면 assignee로 지정(take), 살아 있는
  세션이 붙잡고 있으면 assignee를 손대지 않고 참가만 시킨다(join: JOINED 코멘트
  + 오프닝에 "너는 assignee가 아니다" + 공유 메시가 있으면 보유 세션에 fyi).
  `--no-issue`(웹 폼의 No issue, 바디 `beads: false`)면 아무것도 만들지 않는다.
  생성 폼이 채우는 후보 목록은 `GET /api/beads/candidates`이고, 각 줄이 그
  판정(take인가 join인가, 누가 붙잡고 있는가)을 미리 싣는다. (2) kill:
  활성 이슈를 쥔 세션은 바로 죽이지 않고 정리 요청 블록을 타이핑한 뒤 그 턴이
  끝나기(또는 유예 `beads_winddown_grace`초)를 기다렸다가 종료한다 — 두 번째
  kill이나 `--force`는 즉시. (3) exit: 어떤 이유로든 프로세스가 사라지면 그
  세션이 in_progress로 쥐고 있던 이슈는 `SESSION ENDED` 코멘트와 함께 open으로
  돌아가고, 데몬이 만들었는데 손도 안 댄 자리표시 이슈는 닫힌다. 데몬 재기동은
  exit가 아니라서 아무것도 쓸지 않는다. 설정 키는 `daemon.beads_auto_issue` /
  `beads_winddown` / `beads_winddown_grace`(store.DAEMON_DEFAULTS). 웹 UI의
  Beads 탭과 세션 레일의 Beads 블록이 이 매치(링크·assignee·created_by·task의
  `issue:` 참조)를 그대로 보여 준다.

## 중간 파일은 세션별 경로에 쓴다 — `/tmp`는 이 머신 전역이다

- Git Bash의 `/tmp`는 이 저장소 전용도, 세션 전용도 아니다. `cygpath -w /tmp`가
  `C:/Users/<user>/AppData/Local/Temp`를 내고 `$TEMP`·`$TMP`도 같은 곳을 가리킨다
  — **머신 전역 한 디렉터리**다. 이 머신에서는 세션이 스무 개 넘게 동시에 도는
  일이 흔하고, 서로 다른 워크트리에 서 있어도 `/tmp`는 하나다.
- 그래서 두 세션이 같은 파일 이름을 쓰면 **말없이 덮인다.** 에러도 경고도 없고,
  읽은 쪽은 남의 내용을 자기 것으로 읽는다. 잃는 것이 파일이 아니라 **근거**라서
  비싸다: 덮인 값 위에 낸 판정은 그럴듯하게 틀리고, 원인을 엉뚱한 데서 찾는다.
- 실제로 일어났다(2026-08-26, mesh-0824). 한 세션이 `/tmp/mine.txt`에 자기 변경
  파일 목록을 써 두고 다음 명령에서 읽었는데 내용이 남의 8줄로 바뀌어 있었고,
  그 위에서 낸 첫 교집합이 「자기가 만진 적도 없는 워크플로 yaml」이었다. 다른
  세션이 라운드 15에서 쓴 `/tmp/old.txt`·`/tmp/new.txt`는 지금도 그 자리에
  남아 있고, 무관한 세션에서 그대로 읽힌다 — 그 측정은 결과적으로 맞았지만
  덮이지 않은 것이 운이었을 뿐 방어는 아니었다.
- 규칙:
  - `/tmp/<고정이름>`을 쓰지 않는다. 하네스가 세션마다 주는 스크래치패드
    디렉터리가 있으면 거기에 쓴다.
  - 굳이 `/tmp`를 써야 하면 파일 이름에 세션을 넣는다: `/tmp/$CLAUNCH_SESSION-...`.
  - 자기 워크트리 안에 임시 파일을 쏟지 않는다. 충돌은 피하지만 트리를 더럽혀
    미커밋 변경을 근거로 삼는 판정(preflight·스윕 대조군)을 오염시킨다.
  - **이미 `/tmp`를 왕복시킨 측정이 있으면 다시 잰다.** 값이 맞았는지가 아니라
    근거가 섰는지가 문제다.
