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

## QA와 연결 (claunch-78dpm)

쓰레드와 세션 QA(`qa_*` 도구, `src/claude_launcher/qa/store.py`)는 별개의 기능이고 저장소도 따로다.
둘을 잇는 연결은 양쪽에 하나씩 있다. 둘 다 만들 때 정해지고 바뀌지 않는다.

| 연결 | 저장 위치 | 만드는 방법 |
|---|---|---|
| 쓰레드 → 다루는 질문 | `threads.qa` | 사용자: QA 카드의 「쓰레드로 논의」. 에이전트: `thread_open(qa=…)`. API `POST /api/threads {qa}` |
| 질문 → 출처 쓰레드 | `questions.thread` | 에이전트: `qa_ask(thread=…)`. 사용자는 쓰레드의 「QA로 정리 요청」으로 요청한다(사용자는 질문을 만들 수 없다) |

연결은 같은 세션 안에서만 만든다. 다른 세션의 질문·쓰레드, 없는 id는 라우트가 거절한다(400·404).
질문이 삭제되면 쓰레드 쪽 연결은 남고 상태가 `deleted`로 표시된다.

목록 응답(`GET /api/threads`, `GET /api/qa`, 쓰레드 읽기·열기·쓰기 응답)의 각 행에 `links`가 붙는다
(`daemon/talk_links.py`). 각 항목은 `{id, title, status, rel}`이고 `rel`은 다음 중 하나다.

| 기준 | rel | 뜻 |
|---|---|---|
| 쓰레드 | `subject` | 이 쓰레드가 다루는 질문 |
| 쓰레드 | `filed` | 이 쓰레드에서 만들어진 질문 |
| 질문 | `discussion` | 이 질문을 다루려고 연 쓰레드 |
| 질문 | `source` | 이 질문이 나온 쓰레드 |

### 흐름 1: 질문에서 쓰레드로

1. 세션이 `qa_ask`로 질문을 올린다.
2. 사용자가 답하기 전에 논의가 필요하면 QA 카드의 「쓰레드로 논의」를 누른다. 쓰레드 창에 새 쓰레드 양식이
   열리고, 제목은 `Q: <질문 제목>`, 연결 질문은 그 질문으로 채워진다.
3. 첫 메시지와 함께 열면 전달 블록에 `This thread is about your QA question qa-… (상태): "제목"` 줄이 붙는다.
   이후 그 쓰레드의 모든 사용자 메시지에도 같은 줄이 붙는다.
4. 논의로 질문이 바뀌면 에이전트가 `qa_update`로 고친다. 사용자는 QA 카드에서 답한다.
5. 답 전달 블록(`[claunch qa]`)에 그 질문의 쓰레드 목록이 붙는다. 에이전트는 결과를 쓰레드에도 남긴다.

### 흐름 2: 쓰레드에서 질문으로

1. 쓰레드에서 대화하다 사용자 결정이 필요한 지점이 나온다.
2. 에이전트가 스스로 `qa_ask(thread=…)`로 올리거나, 사용자가 쓰레드의 「QA로 정리 요청」을 누른다.
   그 요청은 쓰레드 메시지(`ask_qa: true`)로 전달되고, 전달 블록에 `qa_ask(..., thread="th-…")`로 올린 뒤
   쓰레드에 질문 id를 답하라는 지시가 붙는다.
3. 질문 카드에는 출처 쓰레드 링크가, 쓰레드 머리에는 만들어진 질문 링크가 나타난다.

### 화면: thread | qa | both

터미널 헤더의 `🗪 threads`와 `? qa` 버튼은 같은 대화 창(`talk-pane.js`)을 연다. 창 위쪽의 서브 탭으로
`쓰레드`, `QA`, `둘 다`(2열: 왼쪽 쓰레드, 오른쪽 QA)를 고른다. 고른 탭은 세션별로 기억된다(`sessLayout.pane`
값 `threads`·`qa`·`both`).

- QA 카드의 쓰레드 링크를 누르면 그 쓰레드가 선택된다. `QA` 탭에 있었으면 `둘 다`로 바뀐다.
- 쓰레드 머리의 질문 링크를 누르면 그 질문 카드로 스크롤하고 강조한다. `쓰레드` 탭에 있었으면 `둘 다`로 바뀐다.
- 한쪽 탭에서 링크를 따라갈 때 `둘 다`로 바꾸는 이유: 링크의 출발점(질문 또는 쓰레드)이 화면에 남아 있어야
  둘을 비교하며 읽을 수 있다.

## 범위 밖

- 메시 연합: 쓰레드는 그 세션을 실행하는 데몬에만 저장된다.
- 모바일 레이아웃: 분할 영역은 넓은 화면에서만 열린다.
