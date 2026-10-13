# 세션 쓰레드

claunch-doj98. 한 세션과 여러 서브토픽 대화를 터미널과 분리해서 하는 기능이다. 세션의 터미널은 하나의
스크롤백이라서, 본 작업 중에 다른 주제를 물으면 두 대화가 한 화면에 섞인다. 쓰레드는 주제마다 따로 저장되는
대화이고, 사용자는 세션의 분할 패널에서, 에이전트는 `thread_*` MCP 도구로 같은 쓰레드에 쓴다.

## 구성

| 부분 | 위치 | 내용 |
|---|---|---|
| 저장소 | `src/claude_launcher/threads/store.py` | 데몬 SQLite `daemon_dir()/threads.sqlite3`. 표 `threads`(세션, 제목, open/closed, 연 쪽, 마지막 seq, 에이전트가 읽은 seq)와 `messages`(전역 seq, author `user`/`agent`, 본문) |
| HTTP | `src/claude_launcher/daemon/threads_api.py` | `/api/threads` 라우트. 사용자 메시지를 세션 터미널에 전달 |
| MCP | `src/claude_launcher/threads_mcp.py` | `thread_list` / `thread_read` / `thread_reply` / `thread_open` / `thread_close` |
| 웹 | `src/claude_launcher/web/static/threads-pane.js` | 터미널 헤더의 `🗪 threads` 버튼, 분할 영역(`#term-wf`)의 쓰레드 목록·대화·입력창 |

## 흐름

1. 사용자가 `🗪 threads` 패널에서 「+ 새 쓰레드」로 주제와 첫 메시지를 쓰거나, 기존 쓰레드에 메시지를 보낸다.
2. 데몬이 다음 블록을 세션에 전달한다(`queue_delivery`, 보류된 세션은 `wake_soon`으로 깨운다).

   ```
   [claunch thread th-1a2b3c "주제"] The user wrote in this thread:
     > 메시지
   Answer in the thread, not the terminal: thread_reply(thread_id="th-1a2b3c", text=...). ...
   ```

3. 에이전트는 `thread_reply`로 그 쓰레드에 답한다. 주제를 다시 이어갈 때는 `thread_read`로 전체 대화를 읽는다.
   에이전트가 먼저 주제를 열 때는 `thread_open`을 쓴다.
4. 패널은 3초마다 목록과 열린 쓰레드의 새 메시지를 읽는다. 에이전트의 마지막 메시지를 이 브라우저가 아직 보지
   않은 쓰레드에는 점(●)이 붙고, 헤더 버튼은 `has-news` 표시를 단다. 패널을 열면 표시가 지워진다.

## 규칙

- 사용자는 모든 세션의 쓰레드에 쓸 수 있다. 세션은 자기 쓰레드에만 쓸 수 있다(다른 세션 쓰레드는 403).
- 닫힌 쓰레드에 어느 쪽이든 메시지를 쓰면 다시 열린다.
- 세션 하나당 열린 쓰레드는 50개까지다(`MAX_OPEN_PER_SESSION`).
- 세션이 종료되어 전달할 수 없으면 메시지는 쓰레드에 남고, 응답의 `delivered`가 `false`다. 패널이 그 사실을 표시한다.
- 에이전트의 읽음 위치(`agent_read`)는 소유 세션이 `thread_read`로 읽거나 `thread_reply`로 답할 때 이동한다.
  `thread_list`의 `agent_unread`는 그 뒤의 사용자 메시지 수다. 사용자의 읽음 위치는 브라우저 localStorage에 있다.

## 범위 밖

- 메시 연합: 쓰레드는 그 세션을 실행하는 데몬에만 저장된다.
- 모바일 레이아웃: 분할 영역은 넓은 화면에서만 열린다.
