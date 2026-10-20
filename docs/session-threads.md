# 세션 쓰레드

claunch-doj98. 한 세션과 여러 서브토픽 대화를 터미널과 분리해서 하는 기능이다. 세션의 터미널은 하나의
스크롤백이라서, 본 작업 중에 다른 주제를 물으면 두 대화가 한 화면에 섞인다. 쓰레드는 주제마다 따로 저장되는
대화이고, 사용자는 세션의 분할 패널에서, 에이전트는 `thread_*` MCP 도구로 같은 쓰레드에 쓴다.

## 구성

| 부분 | 위치 | 내용 |
|---|---|---|
| 저장소 | `src/claude_launcher/threads/store.py` | 데몬 SQLite `daemon_dir()/threads.sqlite3`. 표 `threads`(세션, 제목, open/closed, 연 쪽, 마지막 seq, 에이전트가 읽은 seq)와 `messages`(전역 seq, author `user`/`agent`, 본문, 첨부 이미지) |
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

## 이미지 첨부 (claunch-shl2r)

사용자 메시지에 이미지를 붙일 수 있다. 텍스트 없이 이미지만 보내도 된다. 에이전트의 `thread_reply`에는 이미지가 없다.

| 단계 | 위치 | 내용 |
|---|---|---|
| 첨부 | `threads-pane.js` | 입력창에 붙여넣기(Ctrl+V), 대화 영역·새 쓰레드 폼에 끌어다 놓기, `🖼` 버튼(여러 장 선택). 보내기 전까지 입력창 위에 썸네일로 남고 `×`로 뺀다 |
| 업로드 | `POST /api/threads/images` | 이미지 한 장을 원본 바디로 올린다. 새 세션 오프닝 이미지와 같은 스테이징(`daemon/opening_images.py`, 24MiB, png/jpeg/gif/webp)이고 id를 돌려준다 |
| 게시 | `POST /api/threads`, `POST /api/threads/{id}/messages`의 `images` | 스테이징 id 목록(최대 10장). 데몬이 파일을 `daemon_dir()/thread-images/`로 옮기고 메시지의 `messages.images`(스키마 v3)에 `{name, type, bytes}`로 기록한다. 게시가 거절되면 파일을 스테이징으로 되돌린다 |
| 전달 | `daemon/threads_api.py` | 세션에 보내는 블록에 각 파일의 경로를 적는다. 하네스가 이미지를 받는 살아 있는 세션(`image_paste_keys` 또는 path 전송 선언)에는 블록보다 먼저 이미지를 입력창에 붙여 넣는다 — 'type for this session'의 이미지 붙여넣기와 같은 경로(`api._opening_image_step` → `_deliver_pasted_image`)다. 보류된 세션은 경로만 받는다 |
| 기록 | `GET /api/threads/{id}/images/{name}` | 그 쓰레드 메시지가 가진 이미지만 내준다. 패널의 말풍선과 쓰레드 페이지(`threads-history.js`)가 이 경로로 이미지를 다시 보여 준다. `thread_read`가 돌려주는 메시지의 `images`에는 파일 `path`가 있다 |

이미지 파일은 세션 디렉터리가 아닌 데몬 디렉터리에 둔다. 쓰레드 기록은 세션이 끝난 뒤에도 남기 때문이다.

## QA와 연결 (claunch-78dpm)

쓰레드와 세션 QA(`qa_*` 도구, `src/claude_launcher/qa/store.py`)는 별개의 기능이고 저장소도 따로다.
둘을 잇는 연결은 양쪽에 하나씩 있다. 둘 다 만들 때 정해지고 바뀌지 않는다.

| 연결 | 저장 위치 | 만드는 방법 |
|---|---|---|
| 쓰레드 → 다루는 질문 | `threads.qa` | 사용자: QA 카드의 「쓰레드로 논의」, 또는 제목에 질문 id(`qa-78302411: …`)를 넣어 연 쓰레드. 에이전트: `thread_open(qa=…)` 또는 같은 제목 규칙. API `POST /api/threads {qa}` |
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
2. 사용자가 답하기 전에, 또는 답한 뒤 이어서 논의가 필요하면 QA 카드의 「쓰레드로 논의」를 누른다(닫힌
   질문에도 버튼이 있다). 쓰레드 창에 새 쓰레드 양식이 열리고, 제목은 `<질문 id>: <질문 제목>`, 연결 질문은
   그 질문으로 채워진다.
3. 첫 메시지와 함께 열면 전달 블록에 `This thread is about your QA question qa-… (상태): "제목"` 줄이 붙는다.
   이후 그 쓰레드의 모든 사용자 메시지에도 같은 줄이 붙는다.
4. 논의로 질문이 바뀌면 에이전트가 `qa_update`로 고친다. 사용자는 QA 카드에서 답한다.
5. 답 전달 블록(`[claunch qa]`)에 그 질문의 쓰레드 목록이 붙는다. 에이전트는 결과를 쓰레드에도 남긴다.

### 질문 id로 연결 (claunch-236ei)

쓰레드 창에서 직접 연 쓰레드도 질문 id로 연결된다. `qa` 없이 열린 쓰레드의 제목에 같은 세션 질문의 id
(`qa-` + 16진수 8자리)가 정확히 하나 있으면 데몬이 그 질문을 `threads.qa`로 기록한다. 제목에 해당하는 id가
없으면 첫 메시지를 같은 규칙으로 본다. 해당 id가 둘 이상이면 연결하지 않는다. 다른 세션의 질문 id와 없는
id는 무시한다(거절하지 않는다). 판정은 `talk_links.subject_in`이다.

사용자 메시지 본문에 같은 세션 질문의 id가 있으면(쓰레드가 다루는 질문은 제외) 전달 블록에 질문마다
`The user's message names your QA question qa-… (상태): "제목".` 줄이 붙는다(`talk_links.mentioned`).
에이전트는 그 id를 `qa_list`(닫힌 질문은 `closed=true`)로 읽고 답한다.

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
`둘 다`에서는 두 버튼이 모두 켜진 상태로 표시된다. 그 상태에서 버튼 하나를 누르면 그 버튼의 열만 닫히고 다른 쪽
단독 탭으로 바뀐다(`? qa` → `쓰레드`, `🗪 threads` → `QA`). 단독 탭에서 켜진 버튼을 누르면 분할 창이 닫힌다
(`app.js`의 `splitAfterPress`).
`둘 다`에서는 두 열 사이에 드래그 바(`.talk-split`)가 있다. 끌면 QA 열의 폭이 바뀌고(오른쪽 끝 고정, 나머지는
쓰레드 열), 쓰레드 열 안의 목록/본문 바(`.th-split`)와 합쳐 목록 | 본문 | QA의 3열을 각각 조절할 수 있다. 폭은
놓을 때 브라우저에 저장되고(`localStorage`의 `claunch_talkqaw`, 모든 세션 공통) 바를 두 번 누르면 지워져
두 열이 같은 폭으로 돌아간다. QA 열은 220px 이상, 쓰레드 열은 400px 이상을 유지한다. 둘을 함께 지킬 수 없을 만큼 좁으면 QA 열의 220px가 우선한다. 좁은 화면(820px 이하)에서는
바가 숨겨진다.

- QA 카드의 쓰레드 링크를 누르면 그 쓰레드가 선택된다. `QA` 탭에 있었으면 `둘 다`로 바뀐다.
- 쓰레드 머리의 질문 링크를 누르면 그 질문 카드로 스크롤하고 강조한다. `쓰레드` 탭에 있었으면 `둘 다`로 바뀐다.
- 한쪽 탭에서 링크를 따라갈 때 `둘 다`로 바꾸는 이유: 링크의 출발점(질문 또는 쓰레드)이 화면에 남아 있어야
  둘을 비교하며 읽을 수 있다.

## 범위 밖

- 메시 연합: 쓰레드는 그 세션을 실행하는 데몬에만 저장된다.
- 모바일 레이아웃: 분할 영역은 넓은 화면에서만 열린다.
