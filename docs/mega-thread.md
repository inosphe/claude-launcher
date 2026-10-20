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
| 전달 | `src/claude_launcher/daemon/threads_api.py` | `app["threads_post_user"]` — 쓰레드에 사용자 메시지를 쓰고 세션·게스트에 타이핑하는 기존 경로 |
| 설정 | `src/claude_launcher/store.py` | `megathread_*`, `jev_model_*` 기본값 |

## 데이터 모델

```
mega(id, title, status, created_at, updated_at)
members(mega, session, thread, added_by, added_at)      PK (mega, thread)
routes(seq, mega, body, at, verdict, threshold, candidates, chosen,
       delivered, tokens, model, error)
```

- `routes`가 회계 원장이다. `tokens`는 그 라우팅이 쓴 Jev 입력 토큰의 합이고, 예산은 이 표의 최근 24시간
  합으로 잰다. 별도 사용량 표를 두지 않은 이유는 원장이 둘이면 갈리기 때문이다.
- `candidates`는 멤버별 `{session, thread, p_mine, p_not_mine, logit, choice, confidence, error}`,
  `chosen`은 전달을 시도한 쓰레드 id 목록이다.
- `delivered`는 그 라우팅의 메시지가 **적어도 한 곳에 실제로 타이핑됐다**는 뜻이다. 쓰레드 행은
  종료된 세션에도 남으므로, 행이 생겼다는 것과 전달됐다는 것은 다르다.
- `verdict`: `auto`(한 멤버가 임계값 통과) / `choose`(둘 이상, 사용자 선택 대기) / `manual`(사용자가
  직접 고름) / `none`(아무도 통과 못 함) / `error`(모든 호출 실패).

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
| 클러스터가 `closed` | — | 409, 채점하지 않음 |

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

선택된 쓰레드마다 `threads.post(user, thread, text)` → 소유 세션과 게스트에게 `[claunch thread …]` 블록이
타이핑된다. 선택되지 않은 멤버에는 행도, 알림도, 타이핑도 없다. 그래서 그 세션은 이 대화를 나중에도
참조할 수 없다 — "인지하지 않는다"를 저장하지 않는 것으로 보장한다.

멤버 자리는 세션이 종료돼도 남는다. 멤버십은 사용자가 만든 배치이고 세션의 생존 상태가 아니며, 같은
이름·같은 쓰레드로 재개한 세션이 자기 자리를 잃으면 클러스터가 조용히 비어 보인다. 세션이 없으면
`member_view`가 `status: "gone"`으로 보여 주고, 그 쓰레드로의 전달은 쓰레드 경로가 그대로 처리한다.
자리를 지우는 일은 사용자가 `DELETE /{id}/members/{thread}`로 한다.

전달이 끝난 뒤 `routes` 기록이 실패하면 응답은 여전히 200이고 `ledger_error`에 그 이유가 실린다.
이미 사용자가 받은 메시지를 500으로 되돌려 보고하지 않기 위해서다.

## 설정

| 키 | 기본값 | 뜻 |
|---|---|---|
| `megathread_threshold_ratio` | `9.0` | `mine`이 `not_mine`보다 몇 배 높아야 단독 라우팅인가 |
| `megathread_jev_budget_tokens` | `10000000` | 최근 24시간 입력 토큰 상한. 0이면 무제한 |
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
| `GET` | `/api/megathreads/{id}` | 멤버별 세션 상태·쓰레드 제목·안 읽은 수 |
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
  전달되는지, 비선택 쓰레드에 메시지 행이 없는지, 선택 후 전달과 중복 id, `none`/`error`에 전달 0건,
  예산 경계와 동시 라우팅 직렬화, `closed` 409, NaN 거절, 설정 조회·수정, 원장 실패가 전달을 500으로
  바꾸지 않음.
- `tests/web/megathread_check.js` — 링 배치·선 굵기·판정별 행과 문구·전송 본문·SVG 노드 수, 그리고
  페이지가 주소·레일·`VIEWS`·라우터에 배선됐는지.
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

1. **왼쪽**: 클러스터 목록과 `새 mega thread`(제목 + 세션 체크박스). 만든 클러스터는 `#/megathreads/<id>`로
   열린다.
2. **가운데**: 원탁 SVG. 사용자 노드가 중심이고, 멤버 `(세션, 쓰레드)`가 링에 놓인다. 마지막 라우팅의
   logit이 중심에서 각 노드로 가는 선의 굵기다. 임계값을 넘은 노드는 초록, 0 이상은 노랑, 그 아래는 회색이며
   종료된 세션은 흐리게 그린다. 노드를 누르면 그 세션의 터미널로 간다.
3. **오른쪽**: 최근 라우팅 20건 — 입력, 판정, 사용 토큰, 멤버별 logit, 전달한 곳의 수.

입력은 아래 입력창에서 한다. `Ctrl/Cmd+Enter`로 보낸다.

- `auto`면 데몬이 이미 전달했다. 노드가 초록으로 서고 전달 결과(`전달됨` / `대기 중` / `hold`)가 아래 줄에 뜬다.
- `choose`/`none`이면 후보 카드가 열린다. 각 후보에 `p(mine)`과 `logit`이 있고, 체크박스로 여러 곳을 고른 뒤
  `선택한 곳으로 보내기`를 누르면 `{text, choose: [...]}`가 같은 엔드포인트로 간다. 이 경로는 Jev를 다시
  부르지 않는다.
- 오류(키 없음, 예산 초과, Jev 실패)는 숨기지 않고 그대로 표시한다.

페이지가 열려 있는 동안 5초마다 목록·멤버·로그를 다시 읽는다. 라우터가 다른 페이지로 옮길 때
`MegaThreadPage.stop()`이 폴링을 멈춘다.

### 좌하단 nav

데스크톱에서 `#rail-nav`는 첫 행만 보이고 hover하면 펼쳐진다. 펼침 상한은 `--rail-nav-rows`(24) × 행
높이이고, 그 수는 링크 수(현재 21) 이상이어야 한다. 링크를 더 넣으면 이 값을 같이 올린다.

### 아직 하지 않은 것

- 모바일 레이아웃: 900px 이하에서 세 열이 한 열로 접히기만 한다.
- 실제 데몬 화면 확인: 정적 파일은 데몬이 서빙하므로 새 페이지를 보려면 데몬 재기동이 필요하다. 재기동은
  운영자 명령이다(AGENTS.md). 이번 회차의 증거는 `tests/web/megathread_check.js`와 DOM 수준 검사다.
