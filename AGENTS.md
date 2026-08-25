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
  만들어 assignee로 배정하고 오프닝에 `issue: <id>`를 실어 준다; 요청이 이미
  `issue: <id>`를 말하면 새로 만들지 않고 그 이슈의 assignee만 옮긴다. (2) kill:
  활성 이슈를 쥔 세션은 바로 죽이지 않고 정리 요청 블록을 타이핑한 뒤 그 턴이
  끝나기(또는 유예 `beads_winddown_grace`초)를 기다렸다가 종료한다 — 두 번째
  kill이나 `--force`는 즉시. (3) exit: 어떤 이유로든 프로세스가 사라지면 그
  세션이 in_progress로 쥐고 있던 이슈는 `SESSION ENDED` 코멘트와 함께 open으로
  돌아가고, 데몬이 만들었는데 손도 안 댄 자리표시 이슈는 닫힌다. 데몬 재기동은
  exit가 아니라서 아무것도 쓸지 않는다. 설정 키는 `daemon.beads_auto_issue` /
  `beads_winddown` / `beads_winddown_grace`(store.DAEMON_DEFAULTS). 웹 UI의
  Beads 탭과 세션 레일의 Beads 블록이 이 매치(링크·assignee·created_by·task의
  `issue:` 참조)를 그대로 보여 준다.
