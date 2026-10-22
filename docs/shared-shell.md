# 공유 사용자 셸

대시보드 CLI 탭의 셸을 에이전트 세션과 공유한다. 셸은 사용자 권한으로 도는
하나의 지속 PTY(`daemon/clipty.py`의 `ShellPty`)이고, 에이전트는 그 출력을
읽기만 하며 입력은 제안만 한다. PTY에 실제로 들어가는 입력은 사용자가 CLI
탭에서 제안을 수락할 때뿐이다.

## 구성

| 부분 | 위치 | 내용 |
|---|---|---|
| 셸 | `src/claude_launcher/daemon/clipty.py` | `ShellPty` 하나. 출력 링(256KB)과 절대 바이트 커서(`total`), 대기·결정된 제안 큐 |
| HTTP | `src/claude_launcher/daemon/shell_api.py` | `/api/shell*` 라우트. 읽기, 제안 생성·목록·수락·거절·철회 |
| MCP | `src/claude_launcher/shell_mcp.py` | `shell_status` / `shell_read` / `shell_suggest` / `shell_suggestions` / `shell_withdraw` |
| CLI | `src/claude_launcher/cli_shell.py` | `claunch shell status\|read\|suggest\|suggestions\|accept\|reject\|withdraw` |
| 웹 | `src/claude_launcher/web/static/app.js`, `index.html`, `style.css` | CLI 탭 터미널 위의 제안 줄. `run`/`dismiss` 버튼 |
| 프롬프트 팩 | `src/claude_launcher/prompt_packs/mcp-shell.md` | 에이전트에게 경계와 쓰임을 알린다 |

## 흐름

1. 에이전트가 `shell_suggest(text, why)`를 부른다. 데몬이 제안을 큐에 넣고
   `$CLAUNCH_SESSION`을 제안자로 기록한다. 이 호출은 PTY에 아무것도 쓰지 않는다.
2. CLI 탭이 `suggestions` 프레임(또는 접속 시 `init` 프레임)으로 대기 제안을
   받아 터미널 위에 그린다.
3. 사용자가 `run`을 누르면 `POST /api/shell/suggestions/{id}/accept`가 본문에
   `session` 없이 온다. 데몬이 제안을 `accepted`로 옮기고 텍스트와 Enter(`\r`)를
   PTY에 쓴다. `dismiss`는 `rejected`로만 옮긴다.
4. 에이전트는 `shell_read(offset)`으로 결과를 읽는다. 응답의 `offset`을 다음
   호출에 넘겨 이어 읽는다.

## 규칙

- 셸은 데몬당 하나다(`id: cli`). 데몬이 재기동되면 새 셸이 서고 출력 커서는
  0부터 다시 시작한다. 제안 큐는 데몬 메모리에만 있으므로 재기동할 때 사라진다.
- 출력 링은 256KB다. 읽는 쪽이 그보다 뒤처지면 응답의 `dropped`가 놓친 바이트
  수를 말하고, 커서는 `retained_from`에서 다시 시작한다. 조용히 건너뛰지 않는다.
- 에이전트의 입력 도구는 없다. `POST .../accept`와 `.../reject`는 본문에
  `session`이 오면 403으로 거절한다. MCP 도구 목록에도 수락 도구가 없다.
- 제안 텍스트는 4,096자, `why`는 1,000자까지다. 세션당 대기 10개, 전체 대기
  50개까지이며, 결정된 제안은 최근 200개만 남는다.
- 셸이 exited면 수락은 409로 거절한다. 사용자가 CLI 탭 헤더에서 셸을 재시작한
  뒤에 수락해야 한다.
- 제안자 세션은 자기 대기 제안만 `shell_withdraw`로 철회할 수 있다. 다른
  세션의 제안은 403이다.
- 이 경계는 데몬의 다른 기능과 같은 신뢰 모델 위에 있다: 대시보드와 MCP
  도구가 같은 토큰을 쓰므로, 소유권·호출자 규칙은 도구 경로의 에이전트에
  대해서 강제된다. 셸의 권한 자체(사용자 권한)는 바뀌지 않는다.
