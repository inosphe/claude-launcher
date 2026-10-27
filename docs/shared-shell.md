# 세션 셸

세션마다 셸(PTY) 하나를 두고, 그 세션 아래 분할 패널(`❯ cli`)에 붙인다.
셸은 사용자 권한으로 돌고(`daemon/clipty.py`의 `SessionShell`), 그 세션의
에이전트는 출력을 읽고 입력을 제안만 한다. PTY에 실제로 들어가는 입력은
사용자가 그 세션의 패널에서 제안을 수락할 때뿐이다.

대시보드 CLI 탭(`#cli`)은 사용자 전용 터미널이다. `claunch-nk3ll`이 그
탭에 붙였던 에이전트 공유 기능은 이 구조로 옮겨졌고, 탭에는 에이전트가
닿는 라우트도 소켓 프레임도 없다(claunch-jg8pt).

## 구성

| 부분 | 위치 | 내용 |
|---|---|---|
| 셸 | `src/claude_launcher/daemon/clipty.py` | `ShellPty`(CLI 탭)와 그 위에 제안 큐를 얹은 `SessionShell`. 출력 링(256KB)과 절대 바이트 커서(`total`) |
| 레지스트리 | `src/claude_launcher/daemon/session_shells.py` | 세션 이름 → 셸. 첫 사용 때 생성, 세션 종료 훅(`manager.exit_hooks`)에서 정리, 데몬 종료에서 전부 정리 |
| HTTP | `src/claude_launcher/daemon/shell_api.py` | `/api/sessions/<name>/shell*` 라우트. 읽기, 제안 생성·목록·수락·거절·철회 |
| 소켓 | `src/claude_launcher/daemon/ws.py` | `/api/sessions/<name>/shell/ws`. CLI 탭의 `/api/cli/ws`와 같은 프레임 규약에 `suggestions` 프레임이 더해진다 |
| MCP | `src/claude_launcher/shell_mcp.py` | `shell_status` / `shell_read` / `shell_suggest` / `shell_suggestions` / `shell_withdraw` |
| CLI | `src/claude_launcher/cli_shell.py` | `claunch shell [--session NAME] status\|read\|suggest\|suggestions\|accept\|reject\|withdraw` |
| 웹 | `src/claude_launcher/web/static/cli-pane.js`, `app.js`, `index.html`, `style.css` | `SPLIT_PANES`의 `cli` 패널. 터미널 위의 제안 줄과 `run`/`dismiss` 버튼 |
| 프롬프트 팩 | `src/claude_launcher/prompt_packs/mcp-shell.md` | 에이전트에게 경계와 쓰임을 알린다 |

## 흐름

1. 에이전트가 `shell_suggest(text, why)`를 부른다. 데몬이 그 세션 셸의 큐에
   제안을 넣고 `$CLAUNCH_SESSION`을 제안자로 기록한다. 이 호출은 PTY에
   아무것도 쓰지 않는다.
2. 그 세션의 `cli` 패널이 접속 시 `init` 프레임의 `suggestions`, 이후
   `suggestions` 프레임으로 대기 제안을 받아 터미널 위에 그린다.
3. 사용자가 `run`을 누르면 `POST /api/sessions/<name>/shell/suggestions/{id}/accept`가
   본문에 `session` 없이 온다. 데몬이 제안을 `accepted`로 옮기고 텍스트와
   Enter(`\r`)를 PTY에 쓴다. `dismiss`는 `rejected`로만 옮긴다.
4. 에이전트는 `shell_read(offset)`으로 결과를 읽는다. 응답의 `offset`을 다음
   호출에 넘겨 이어 읽는다.

## 규칙

- **주소가 곧 경계다.** 셸은 세션 이름으로만 닿는다
  (`/api/sessions/<name>/shell*`). 다른 세션의 셸을 지정하는 MCP 인자는
  없고, 라우트는 본문 `session`이 경로의 이름과 다르면 403으로 거절한다.
  `shell_suggest`/`shell_withdraw`는 자기 세션 셸에만 쓴다.
- `claunch shell --session NAME`은 사람이 다른 세션의 셸을 들여다보고 그
  자리에서 수락·거절할 때 쓴다. 제안을 만드는 동사(`suggest`·`withdraw`)는
  `$CLAUNCH_SESSION`과 다른 세션을 지정하면 거절한다 — 에이전트가 플래그로
  남의 셸에 제안할 수 없다.
- 셸의 수명은 세션의 수명이다. 세션이 끝나면(`manager.exit_hooks`) 그
  셸을 종료하고 레지스트리에서 지운다. 세션 **재시작**은 셸을 유지한다:
  매니저가 재시작 동안 exit 훅을 미룬다(`manager._restart_exit_hooks`).
- 셸은 데몬 프로세스에서 스폰된다(`pty_backend.spawn_local`). CLI 탭의 셸과
  같이 데몬이 재기동되면 새 셸이 서고 출력 커서는 0부터 다시 시작한다.
  제안 큐도 데몬 메모리에만 있다. PTY 호스트(`daemon/ptyhost.py`,
  claunch-xnnct)를 넘어 세션 셸을 살아남게 하지 않는다 — 후속 프로세스가
  이어받을 기록이 없고, 이 이슈는 그 기록을 만들지 않기로 정했다
  (claunch-jg8pt).
- 출력 링은 256KB다. 읽는 쪽이 그보다 뒤처지면 응답의 `dropped`가 놓친
  바이트 수를 말하고, 커서는 `retained_from`에서 다시 시작한다. 조용히
  건너뛰지 않는다.
- 에이전트의 입력 도구는 없다. `POST .../accept`와 `.../reject`는 본문에
  `session`이 오면 403으로 거절한다. MCP 도구 목록에도 수락 도구가 없다.
- 제안 텍스트는 4,096자, `why`는 1,000자까지다. 셸마다 대기 10개, 전체 대기
  50개까지이며, 결정된 제안은 최근 200개만 남는다.
- 셸이 exited면 수락은 409로 거절한다. 사용자가 그 세션의 `cli` 패널에서
  셸을 재시작한 뒤에 수락해야 한다.
- 제안자 세션은 자기 대기 제안만 `shell_withdraw`로 철회할 수 있다. 다른
  세션의 제안은 403이다.
- 이 경계는 데몬의 다른 기능과 같은 신뢰 모델 위에 있다: 대시보드와 MCP
  도구가 같은 토큰을 쓰므로, 소유권·호출자 규칙은 도구 경로의 에이전트에
  대해서 강제된다. 셸의 권한 자체(사용자 권한)는 바뀌지 않는다.
