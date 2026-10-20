# mega thread

claunch-v5nhp. 여러 세션의 쓰레드에 **한 번의 입력**으로 말하는 기능이다. 사용자는 mega thread 하나에
자기 입력창을 두고, 데몬이 그 메시지가 어느 `(세션, 쓰레드)`의 것인지 Jev로 판정해 전달한다. 선택되지 않은
멤버는 그 메시지를 받지 않고, 나중에 그 대화를 참조할 수도 없다.

이 문서는 회차 1(백엔드)의 구현을 설명한다. 웹 원탁 UI(`web/static/megathread-page.js`)는 회차 2다.

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
  `chosen`은 실제로 전달한 쓰레드 id 목록이다.
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
| `logit ≥ threshold`인 멤버가 1명 | `auto` | 그 쓰레드에만 전달 |
| 2명 이상 | `choose` | 후보를 돌려주고 사용자가 다중 선택. 아무 데도 전달하지 않음 |
| 0명 | `none` | logit 상위 5명(`TOP_N`)을 후보로 돌려줌 |
| 모든 호출 실패 | `error` | 503 없이 200 + `errors`, 전달 없음 |
| 예산 초과 | — | 429, Jev를 부르기 전에 거절 |

`choose` 뒤 사용자는 `{"text": ..., "choose": [쓰레드 id, ...]}`로 다시 보낸다. 이 경로는 Jev를 다시
부르지 않고(`manual`) 고른 쓰레드에만 쓴다.

## 전달

선택된 쓰레드마다 `threads.post(user, thread, text)` → 소유 세션과 게스트에게 `[claunch thread …]` 블록이
타이핑된다. 선택되지 않은 멤버에는 행도, 알림도, 타이핑도 없다. 그래서 그 세션은 이 대화를 나중에도
참조할 수 없다 — "인지하지 않는다"를 저장하지 않는 것으로 보장한다.

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
- `tests/test_megathread_route.py` — 확률·log-odds, 판정 3분기, state 조립(메시지 포함), 모델 전달,
  한 멤버 실패 격리, 정책 기본값.
- `tests/test_megathread_api.py` — 입장 시 쓰레드 생성, 소유권 거절, 단독 라우팅이 **한 멤버에게만**
  전달되는지, 선택 후 전달, 예산 429, 설정 조회·수정.
- 회귀: `tests/test_threads_store.py`, `tests/test_threads_api.py`, `tests/test_threads_mcp.py`,
  `tests/test_qa_nudge.py`.

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
- 예산은 라우팅 단위로만 잰다. Jev가 응답에 `usage.input_tokens`를 주지 않으면 글자 수 추정으로 대신한다.
- 반올림된 확률(0.95/0.05 같은 값)이 자주 나온다. 임계값이 그 사이에 있으면 판정이 거칠다.

## 다음 회차

- 웹 원탁 UI: 사용자 노드와 멤버 노드를 SVG 링으로 배치하고, 라우팅 결과의 logit을 선 굵기로 보여 준다.
  `svg.mesh-ring`(`app.js:45036`)의 배치 방식을 재사용한다.
- 설정 카드에 예산과 기능별 모델 선택을 붙인다. 엔드포인트(`/api/megathreads/settings`,
  `/api/qa-nudge`의 `jev_models`)는 회차 1에 이미 있다.
