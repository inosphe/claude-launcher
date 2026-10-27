# mega thread

claunch-v5nhp. 여러 세션의 쓰레드에 **한 번의 입력**으로 말하는 기능이다. 사용자는 mega thread 하나에
자기 입력창을 두고, 데몬이 그 메시지가 어느 `(세션, 쓰레드)`의 것인지 Jev로 판정해 전달한다. 선택되지 않은
멤버는 그 메시지를 받지 않고, 나중에 그 대화를 참조할 수도 없다.

회차 1이 백엔드(저장소·라우팅·API)를, 회차 2가 웹 원탁 UI(`web/static/megathread-page.js`)를 맡았다.

## 용어

- **mega thread**: 여러 `(세션, 쓰레드)` 멤버를 묶은 클러스터. id는 `mt-<hex>`.
- **멤버**: `(세션, 쓰레드)` 쌍. 키는 쓰레드 id이므로 한 세션이 자기 쓰레드 둘로 한 클러스터에 들 수 있다.
- **라우팅**: 사용자 입력을 멤버별 Jev Choice(`mine` / `not_mine`)로 채점해 전달 대상을 정하는 일.

## 구성

| 부분 | 위치 | 내용 |
|---|---|---|
| 저장소 | `src/claude_launcher/megathread/store.py` | SQLite `daemon_dir()/megathread.sqlite3`. 표 `mega`, `members`, `routes` |
| 라우팅 | `src/claude_launcher/daemon/megathread_route.py` | 멤버별 state 조립, Jev 호출, log-odds 판정 |
| HTTP | `src/claude_launcher/daemon/megathread_api.py` | `/api/megathreads` 라우트, 설정 엔드포인트 |
| Jev | `src/claude_launcher/daemon/jev.py` | `system_one(..., model=)`과 `model_for(cfg, feature)` |
| 전달 | `src/claude_launcher/daemon/threads_api.py` | `app["threads_post_user"]` — 쓰레드에 사용자 메시지를 쓰고 멘션 규칙(`docs/session-threads.md` 「멘션」)에 따라 세션·게스트에 타이핑하는 기존 경로 |
| 설정 | `src/claude_launcher/store.py` | `megathread_*`, `jev_model_*` 기본값 |

## 데이터 모델

```
mega(id, title, status, created_at, updated_at)
members(mega, session, thread, added_by, added_at)      PK (mega, thread)
routes(seq, mega, body, at, verdict, threshold, candidates, chosen,
       delivered, tokens, model, error, kind)
```

- `routes`가 회계 원장이다. `tokens`는 그 라우팅이 쓴 Jev 입력 토큰의 합이고, 예산은 이 표의 최근 24시간
  합으로 잰다. 별도 사용량 표를 두지 않은 이유는 원장이 둘이면 갈리기 때문이다.
- `candidates`는 멤버별 `{session, thread, p_mine, p_not_mine, logit, choice, confidence, error}`,
  `chosen`은 전달을 시도한 쓰레드 id 목록이다.
- `delivered`는 그 라우팅의 메시지가 **적어도 한 곳에 실제로 타이핑됐다**는 뜻이다. 쓰레드 행은
  종료된 세션에도 남으므로, 행이 생겼다는 것과 전달됐다는 것은 다르다. 종료된 세션에는 타이핑 대신
  쓰레드 알림 큐에 들어가므로(아래 「전달」) 그 응답 행은 `queued: true`, `hold: "exited"`이고
  원장의 `delivered`는 `false`다. 멘션이 소유 세션이 아닌 게스트만 지목한 메시지는 그 게스트가 받은 것으로
  센다 — 응답 행의 `guests`가 타자된 게스트 이름이고, 게스트가 아직 받을 수 없으면 `guests_hold`에 이유가
  실린다(claunch-p9wes.1.1). 한 대상이 종료돼 그 몫만 큐에 들어가고 다른 대상은 실제로 받았으면 그 행은
  `delivered: true`, `queued: false`다 — `queued`는 어느 터미널에도 타자되지 않았을 때만 참이다.
- `verdict`: `auto`(한 멤버가 임계값 통과) / `choose`(둘 이상, 사용자 선택 대기) / `manual`(사용자가
  직접 고름) / `none`(아무도 통과 못 함) / `error`(모든 호출 실패).
- `kind`: `route`(클러스터로 간 메시지) / `draft`(생성 폼의 주제 미리보기). `draft` 행은 클러스터가
  없으므로 `mega`가 빈 문자열이고, 두 예산이 이 열로 갈린다. 스키마 2에서 더해졌고, 그 전에 쓰인
  파일은 열을 얻으면서 기존 행이 전부 `route`로 남는다.

## 라우팅

### state

멤버 한 명당 문서 하나를 만든다.

```json
{
  "message": "<사용자 입력>",
  "session": {"name": "...", "status": "idle", "cwd": "...", "task": "...", "issue": "...", "run": {...}},
  "briefing": "<데몬이 이미 만든 브리핑, 또는 트랜스크립트 꼬리>",
  "thread": {"title": "...", "status": "open", "messages": [{"author": "user|agent", "text": "..."}]}
}
```

- `briefing`은 **새 LLM 호출을 하지 않는다.** 대시보드가 만든 캐시(`briefing.cached_result`)를 쓰고,
  없으면 트랜스크립트 꼬리(`briefing.tail_events`)를 쓴다. 메시지마다 멤버 수만큼 요약 호출이 붙으면
  싼 판정이 비싸진다.
- `thread.messages`는 마지막 12개(`THREAD_MESSAGES`), 메시지당 1000자, 문서 전체 `megathread_max_chars`
  (기본 12000자)로 자른다.
- 잘라도 **꼬리를 남긴다**. 쓰레드의 현재 주제는 마지막 줄에 있다.

### 질문과 확률

```
questions = {"route": {"type": "choice",
                       "instructions": "…이 메시지가 이 쓰레드의 것인가…",
                       "criteria": {"mine": "…", "not_mine": "…"}}}
```

Jev의 `ChoiceAnswer`는 `choice`, `confidence`, `probabilities`를 준다. 확률 분포가 없으면(구형 응답)
`choice`와 `confidence`로 대체한다. 0과 1은 `EPS`로 잘라 로그가 무한이 되지 않게 한다.

```
logit = ln p(mine) - ln p(not_mine)
threshold = ln(megathread_threshold_ratio)      # 기본 ratio 9 = ln 9 ≈ 2.197 (90% 대 10%)
```

### 판정

| 조건 | 결과 | 동작 |
|---|---|---|
| `logit ≥ threshold`인 멤버가 1명, 모든 호출 성공 | `auto` | 그 쓰레드에만 전달 |
| 통과 1명 + 호출 실패가 1명 이상 | `choose` | 통과 멤버를 후보로 돌려주고 사용자가 고른다. 아무 데도 전달하지 않음 |
| 통과 2명 이상 | `choose` | 후보를 돌려주고 사용자가 다중 선택. 아무 데도 전달하지 않음 |
| 0명 | `none` | logit 상위 5명(`TOP_N`)을 후보로 돌려줌 |
| 모든 호출 실패 | `error` | 503 없이 200 + `errors`, 전달 없음 |
| 예산 초과 | — | 429, Jev를 부르기 전에 거절 |
| 클러스터가 `closed` | — | 409, 채점하지 않음. `POST /{id}/status`로 다시 열면 라우팅이 재개된다 |

호출이 실패한 멤버는 채점되지 않으므로 그 멤버가 정답이었을 수 있다. 그래서 실패가 하나라도 있으면
`auto`로 내리지 않는다 — 남은 한 멤버에게 조용히 전달하면 사용자는 자기가 어디에 말했는지 모른 채
다른 세션의 답을 받는다. 실패한 멤버는 `errors`에 실리고 후보에는 서지 않는다.

`choose` 뒤 사용자는 `{"text": ..., "choose": [쓰레드 id, ...]}`로 다시 보낸다. 이 경로는 Jev를 다시
부르지 않고(`manual`) 고른 쓰레드에만 쓴다. 같은 id를 두 번 보내면 한 번으로 줄인다.

### 예산과 직렬화

예산 점검(최근 24시간 합 읽기) → Jev 호출 → `routes` 기록이 클러스터별 `asyncio.Lock` 안에서 한 번에
하나씩 일어난다. 잠금이 없으면 동시에 들어온 두 POST가 같은 사용량을 읽고, 둘 중 하나만 들어갈 상한을
둘 다 통과한다. 잠금 단위가 클러스터이므로 큰 클러스터의 라우팅이 다른 클러스터를 막지 않는다.

호출이 실패한 멤버도 토큰을 기록한다(`usage.input_tokens`가 없으면 글자 수 추정). 기록하지 않으면
과금된 호출이 원장 밖에 남아 상한이 실제 지출보다 높게 잡힌다.

## 전달

선택된 쓰레드마다 `threads.post(user, thread, text)` → 멘션 규칙에 따라 소유 세션(멘션이 없으면 소유 세션 하나,
있으면 이름이 적힌 세션)에 `[claunch thread …]` 블록이 타이핑된다. 선택되지 않은 멤버에는 행도, 알림도,
타이핑도 없다. 그래서 그 세션은 이 대화를 나중에도 참조할 수 없다 — "인지하지 않는다"를 저장하지 않는 것으로
보장한다. 게스트에게 타이핑되는 조건은 `docs/session-threads.md`의 「멘션」 절과 같다(claunch-p9wes.1.1).

멤버 자리는 세션이 종료돼도 남는다. 멤버십은 사용자가 만든 배치이고 세션의 생존 상태가 아니며, 같은
이름·같은 쓰레드로 재개한 세션이 자기 자리를 잃으면 클러스터가 조용히 비어 보인다. 세션이 없으면
`member_view`가 `status: "gone"`으로 보여 주고, 그 쓰레드로의 전달은 쓰레드 경로가 그대로 처리한다.
종료된 세션에는 블록이 곧바로 타이핑되지 않고 세션별 알림 큐에 쌓였다가, 그 세션이 respawn되거나
데몬 restore로 다시 launch될 때 타이핑된다(`daemon/thread_notices.py`, claunch-sgxfh). 그래서 라우팅
응답과 원장은 그 건을 `delivered`가 아니라 `queued`로 적는다.
자리를 지우는 일은 사용자가 `DELETE /{id}/members/{thread}`로 한다.

전달이 끝난 뒤 `routes` 기록이 실패하면 응답은 여전히 200이고 `ledger_error`에 그 이유가 실린다.
이미 사용자가 받은 메시지를 500으로 되돌려 보고하지 않기 위해서다.

## 설정

| 키 | 기본값 | 뜻 |
|---|---|---|
| `megathread_threshold_ratio` | `9.0` | `mine`이 `not_mine`보다 몇 배 높아야 단독 라우팅인가 |
| `megathread_jev_budget_tokens` | `10000000` | 최근 24시간 입력 토큰 상한. 0이면 무제한 |
| `megathread_draft_budget_tokens` | `2000000` | 같지만 생성 폼의 주제 미리보기 몫. 0이면 무제한 |
| `megathread_max_chars` | `12000` | 멤버 한 명의 state 길이 상한 |
| `megathread_concurrency` | `8` | 동시에 보내는 Jev 호출 수 |
| `jev_model` | `jev-latest` | 모든 기능의 기본 모델 |
| `jev_model_megathread` | `jev-latest` | mega thread만의 모델 |
| `jev_model_qa_nudge` | (빈 값) | QA nudge만의 모델, 비면 `jev_model` |

- 예산 기본값 10M 토큰은 $0.042/MTok 기준 약 $0.42/일이다. 멤버 10명짜리 라우팅이 약 30k 토큰이므로
  하루 330회 정도이고, 정상 사용에서는 닿지 않는다.
- 기능별 모델은 `jev.model_for(cfg, "<feature>")`가 고른다. `jev-latest`가 `jev-1.14.0`으로 넘어가는
  시점을 기다리지 않고 `jev_model_megathread`에 `jev-1.14.0`을 직접 넣을 수 있다.
- 설정은 `claunch daemon config <키> <값>` 또는 `GET`/`PUT /api/megathreads/settings`로 바꾼다.
  Jev 키는 여전히 `jev.key` 파일이나 `jev_api_key_env` 환경변수에서 읽는다.

## HTTP

| 메서드 | 경로 | 내용 |
|---|---|---|
| `GET` | `/api/megathreads?closed=` | 클러스터 목록 |
| `POST` | `/api/megathreads` | `{title, members?: [{session, thread?}]}` 생성 |
| `POST` | `/api/megathreads/draft` | `{topic, sessions?}` 주제를 세션들에 채점해 추천 목록을 돌려준다. 아무것도 만들지 않는다 |
| `GET` | `/api/megathreads/{id}` | 멤버별 세션 상태·쓰레드 제목·안 읽은 수 |
| `POST` | `/api/megathreads/{id}/status` | `{status: open\|closed}` 닫기·다시 열기. 다른 값은 400, 없는 클러스터는 404 |
| `POST` | `/api/megathreads/{id}/members` | `{session, thread?}` 입장. 쓰레드가 없으면 그 세션의 최근 열린 쓰레드, 그것도 없으면 새로 연다 |
| `DELETE` | `/api/megathreads/{id}/members/{thread}` | 나가기 |
| `POST` | `/api/megathreads/{id}/route` | `{text}` 또는 `{text, choose: [...]}` |
| `GET` | `/api/megathreads/{id}/routes?limit=` | 라우팅 로그 |
| `GET`/`PUT` | `/api/megathreads/settings` | 정책과 예산 사용량 |

## 테스트

- `tests/test_megathread_store.py` — 클러스터·멤버·라우팅 로그·예산 합계.
- `tests/test_megathread_route.py` — 확률·log-odds, 판정 3분기와 `>=` 경계, 실패가 있으면 `auto`로
  내리지 않음, state 조립(메시지 포함), 모델 전달, 실패 멤버의 토큰 계상, 삭제된 쓰레드가 다른 멤버를
  깨뜨리지 않음, 정책 기본값과 NaN 폴백.
- `tests/test_megathread_api.py` — 입장 시 쓰레드 생성, 소유권 거절, 단독 라우팅이 **한 멤버에게만**
  (그 밖에 주제 미리보기: 추천 목록, 아무것도 만들지 않음, 예산 분리, 잘못된 입력 거절)
  전달되는지, 비선택 쓰레드에 메시지 행이 없는지, 선택 후 전달과 중복 id, `none`/`error`에 전달 0건,
  예산 경계와 동시 라우팅 직렬화, `closed` 409, 닫기·다시 열기와 잘못된 status 400, NaN 거절,
  설정 조회·수정, 원장 실패가 전달을 500으로 바꾸지 않음.
- `tests/web/megathread_check.js` — 링 배치·선 굵기·판정별 행과 문구·전송 본문·SVG 노드 수, 생성 패널의
  모델(수명주기 7구간, 정렬 4종, 부모-자식 위계, 프로젝트·메시·역할·상태 필터, 행의 role/cflow/브리핑 조인, 점수 구간과 두 테마의 대비),
  그리고 페이지가 주소·레일·`VIEWS`·라우터에 배선됐는지.
- 회귀: `tests/test_threads_store.py`, `tests/test_threads_api.py`, `tests/test_threads_mcp.py`,
  `tests/test_qa_nudge.py`, `tests/test_web_topology.py`.

## 실측 (2026-10-21, jev-latest)

두 멤버(s1 "log format", s2 "deploy")로 실제 엔드포인트에 물었다.

| 입력 | s1 p(mine) / logit | s2 p(mine) / logit | 판정 |
|---|---|---|---|
| "The log line is too long — can you trim the timestamp?" | 0.99 / 4.60 | 0.31 / -0.80 | `auto` → s1 |
| "deploy to staging once the build is green" | 0.15 / -1.74 | 0.95 / 2.94 | `auto` → s2 |

라우팅 한 번에 약 1,100 입력 토큰, 약 $0.00005였다.

## 한계

- 브리핑은 캐시된 것만 쓴다. 브리핑이 없는 세션은 트랜스크립트 꼬리로 채점되므로 근거가 얇다.
- 판정 품질은 쓰레드 대화 길이에 좌우된다. 라우팅 로그의 logit 분포를 보고
  `megathread_threshold_ratio`를 조정한다.
- 예산의 사전 추정은 보수적이다(멤버당 `megathread_max_chars / 3`, 입력 길이 `/ 3`). 상한 근처에서는
  실제 지출보다 조금 일찍 거절한다. 사후 정산은 `usage.input_tokens`이고, 응답에 없거나 호출이
  실패하면 글자 수 추정이다.
- 클러스터별 잠금은 한 데몬 프로세스 안에서만 유효하다. 같은 `megathread.sqlite3`를 두 데몬이 쓰는
  구성은 지원하지 않는다.
- 반올림된 확률(0.95/0.05 같은 값)이 자주 나온다. 임계값이 그 사이에 있으면 판정이 거칠다.

## 화면 (회차 2)

`#/megathreads` — 좌측 사이드바 하단 `#rail-nav`의 `◍ Mega`로 들어간다.

| 부분 | 파일 | 내용 |
|---|---|---|
| 페이지 | `web/static/megathread-page.js` | `globalThis.MegaThreadPage = {open, stop, render}` |
| 배선 | `index.html`, `app.js` | nav 링크, `#megathreads-view`, `VIEWS`, `parseHash`, 라우터 case, 중앙 stop 목록 |
| 스타일 | `style.css` | `.mt-*` 규칙 |

세 열이다.

1. **왼쪽**: 클러스터 목록과 `＋ 새 mega thread`(생성 패널을 연다). 만든 클러스터는 `#/megathreads/<id>`로
   열린다. 생성 패널은 아래 「생성 패널」.
2. **가운데**: 원탁 SVG. 사용자 노드가 중심이고, 멤버 `(세션, 쓰레드)`가 링에 놓인다. 마지막 라우팅의
   logit이 중심에서 각 노드로 가는 선의 굵기다. 임계값을 넘은 노드는 초록, 0 이상은 노랑, 그 아래는 회색이며
   종료된 세션은 흐리게 그린다. 노드를 누르면 그 세션의 터미널로 간다.
3. **오른쪽**: 최근 라우팅 20건 — 입력, 판정, 사용 토큰, 멤버별 logit, 전달한 곳의 수.

머리의 `닫기` 버튼은 클러스터를 `closed`로 바꾸고(`POST /{id}/status`), 같은 버튼이 `다시 열기`로
바뀐다. 닫힌 클러스터는 왼쪽 목록에서 흐리게 서고, 입력창과 전송 버튼이 비활성되며, 라우팅은
409로 거절된다. 멤버·라우팅 로그·예산은 그대로 남는다.

입력은 아래 입력창에서 한다. `Ctrl/Cmd+Enter`로 보낸다.

- `auto`면 데몬이 이미 전달했다. 노드가 초록으로 서고 전달 결과(`전달됨` / `대기 중` / `hold`)가 아래 줄에 뜬다.
- `choose`/`none`이면 후보 카드가 열린다. 각 후보에 `p(mine)`과 `logit`이 있고, 체크박스로 여러 곳을 고른 뒤
  `선택한 곳으로 보내기`를 누르면 `{text, choose: [...]}`가 같은 엔드포인트로 간다. 이 경로는 Jev를 다시
  부르지 않는다.
- 오류(키 없음, 예산 초과, Jev 실패)는 숨기지 않고 그대로 표시한다.

페이지가 열려 있는 동안 5초마다 목록·멤버·로그를 다시 읽는다. 라우터가 다른 페이지로 옮길 때
`MegaThreadPage.stop()`이 폴링을 멈춘다.

### 생성 패널 (claunch-vz96l)

`＋ 새 mega thread`가 페이지 위에 패널(최대 980px × 86vh)을 연다. 예전에는 240px짜리 aside 안에
200px 목록이 있어 여섯 줄만 보였고, 이름만으로는 세션을 구분할 수 없었다. 패널은 세 부분이다.

1. **제목과 주제**: 제목은 필수다. 주제를 적고 `Jev로 멤버 추천`을 누르면 `POST /api/megathreads/draft`가
   지금 필터를 통과한 세션(최대 40개, `DRAFT_MAX_CANDIDATES`)을 채점한다. 응답의 `recommended`가 체크로
   들어오고, 임계값을 넘은 세션에는 `추천 p(mine)` 표시가 붙는다. 사용자는 그 체크를 직접 고친 뒤
   `만들기`를 누른다 — 미리보기는 아무것도 만들지 않는다. 실패하면 사유와 `재시도` 버튼이 남고, 폼은
   그대로 쓸 수 있다. 채점이 실패한 후보의 행은 `session`을 실어 돌아온다: 아직 열린 쓰레드가 없는
   세션은 `thread`가 빈 문자열이라, thread id만으로는 어느 세션의 실패인지 가릴 수 없다.
   - **미리보기의 상태 문서** (claunch-wlk6g): 라우팅과 같은 문서에서 세 가지가 다르다. 판정 대상은
     `message` 대신 `topic` 키에 들어간다. `session`에는 `draft_facts`가 더해진다: 등록 워크스페이스
     이름, git 파일에서 읽은 브랜치(프로세스를 띄우지 않는다), `parent`, 그 세션의 보드 이슈
     (`beads.match` 순서로 최대 4개, id·status·title만). 보드 목록은 레일이 쓰는 캐시
     (`issues_or_stale`)를 보드당 한 번 읽고, 보드를 못 읽으면 이슈만 빠진다. `briefing`에는
     `progress`가 더해진다. 판정 기준은 이미 한 일도 `mine`으로 센다(예전 기준은 "already finished
     here"를 `not_mine`에 넣었고, 실제 세션 40개 × 주제 4개 측정에서 맞는 세션이 logit 1.5~2.4에 머물러
     ln 9를 넘지 못했다). 메시지 라우팅의 문서와 기준은 바뀌지 않았다.
   - **임계값 슬라이더**: 응답은 추천 여부와 관계없이 채점된 모든 멤버의 `logit`을 싣는다. 첫
     미리보기가 오면 `추천 임계값` 슬라이더(p(mine) 50~99%, 처음 값은 데몬 임계값 ln 9 = 90%)가
     나타나고, 움직이면 받은 점수로 체크를 다시 계산한다 — 요청도 Jev 호출도 없다. 채점된 세션은
     임계값을 넘을 때만 체크되고(손으로 고친 체크도 슬라이더를 다시 움직이면 덮인다), 채점되지 않은
     세션의 체크는 그대로다. 정한 임계값은 다음 미리보기에도 유지된다.
   - **점수 색** (claunch-kzudj): 행의 점수 알약은 `scoreBand`가 정한 네 구간의 색으로 그린다 —
     `hit`(지금 임계값 이상, `추천` 표시와 같은 집합, 초록 채움), `warm`(임계값 미만이고 p ≥ 0.5, 주황),
     `mild`(p ≥ 0.2, 파랑), `cold`(그 아래, 회색). 슬라이더를 움직이면 색도 요청 없이 바뀐다. 라이트
     테마는 theme.js가 이 색들에서 파생하며, `megathread_check.js`가 두 테마에서 각 구간 글자의 명도
     대비가 4.5:1 이상인지 잰다.
2. **정렬·필터**: 정렬은 활동/생성 × 최신/오래된 네 가지(`SORTS`), `부모-자식 위계` 체크박스는
   `parent`로 트리를 만들고 들여쓴다(부모가 목록에 없으면 루트). 필터는 프로젝트·메시·역할·수명주기
   (`running · hold · paused · killed · delayed · failed · archived`, `session.py::session_category`와 같은
   규칙)와 부분 일치 검색이다. 역할 필터(claunch-kzudj)는 다중 선택 콤보박스다. 버튼(`역할: 전체` 또는
   고른 역할 이름)을 누르면 역할마다 체크박스와 `역할 없음`, 찾기 입력칸이 열린다. 여러 역할을 고르면 그중
   하나에 해당하는 세션을 남기고, 아무것도 고르지 않으면 모든 역할이다. 값은 행의 meta 줄과 같다
   (`roleOf`: 레코드의 `role`, 없으면 메시 로스터의 role). `역할 없음`은 role이 없는 세션이다. Escape나
   바깥 클릭은 목록만 닫는다. 선택지는 거르기 전 전체 레코드에서 만든다. 모든 필터는 가진 레코드로
   목록만 다시 그리며 데몬에 다시 묻지 않는다. Jev 미리보기도 이 필터들이 남긴 세션만 채점한다(최대
   40개, 화면 순서). 메시·역할 필터는 메시 로스터를 읽으므로 미리보기 요청도 로스터를 넘긴다 — 넘기지
   않던 동안에는 메시나 역할로 거르면 후보가 0개가 되어 데몬이 "no sessions to score"로 거절했다.
   필터가 남긴 세션이 0개면 요청을 보내지 않고 패널에 그 사실을 적는다.
3. **목록**: 행마다 이름, role(메시 로스터), 메시 이름, cflow2 런(workflow · status · 현재 노드), 프로젝트,
   이슈·브랜치·note, 한 줄 브리핑. 값이 없는 줄은 그리지 않는다.

세션 목록은 `/api/sessions?view=rail`(레코드) + `/api/mesh?view=rail`(role·메시) + `/api/cflow2/rail`
(cflow2 런)을 세션 이름으로 조인해 만든다. 레일 자체의 `role`은 살아 있는 행에서 비어 있어
(claunch-cn8v6 실측 0/26) 로스터가 유일한 출처다. 두 조인은 실패해도 목록을 막지 않는다.
이 세 읽기는 페이지를 연 뒤 클러스터(`/api/megathreads`)를 먼저 그리고 나서 나간다. state 없는
`/api/sessions?view=rail`은 보관 기록까지 전부라 cold 응답이 1.5-2.3초였고(기록 2303개, claunch-wbs0i),
도착 전의 피커는 「세션 목록을 읽는 중…」을 보여 준다(claunch-wbs0i.2). 도착 전에 누른 `Jev로 멤버 추천`은
빈 후보로 나가지 않고 도착을 기다렸다가 보낸다(그동안 패널은 「세션 목록을 읽는 중… 도착하면 Jev에
보냅니다」). 패널에 포커스가 있는 동안 목록이 도착하면 패널을 다시 만들지 않고 목록·카운터와 프로젝트·
메시·역할 선택지만 그 자리에서 채운다.

### 좌하단 nav

데스크톱에서 `#rail-nav`는 첫 행만 보이고 hover하면 펼쳐진다. 펼침 상한은 `--rail-nav-rows`(24) × 행
높이이고, 그 수는 링크 수(현재 21) 이상이어야 한다. 링크를 더 넣으면 이 값을 같이 올린다.

### 아직 하지 않은 것

- 모바일 레이아웃: 900px 이하에서 세 열이 한 열로 접히기만 한다.
- 실제 데몬 화면 확인: 정적 파일은 데몬이 서빙하므로 새 페이지를 보려면 데몬 재기동이 필요하다. 재기동은
  운영자 명령이다(AGENTS.md). 이번 회차의 증거는 `tests/web/megathread_check.js`와 DOM 수준 검사다.
