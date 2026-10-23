# 세션 보고 타이머

claunch-lvrcp. 에이전트는 자기 턴이 끝나면 다음 턴을 스스로 시작할 수 없으므로 "n초마다 보고"를
스스로 지킬 수 없다. 이 기능은 그 주기를 데몬에 맡긴다. 에이전트가 `timer` MCP 도구로
(간격, 횟수, 메세지)를 주면 데몬의 시계가 그 간격마다 메세지를 세션 터미널에 넣는다.

## 구성

| 부분 | 위치 | 내용 |
|---|---|---|
| 원장 | `src/claude_launcher/daemon/agent_timers.py` | 세션별 `<세션 디렉터리>/timers.json`. 타이머마다 `id`, `every`, `times`, `fires`, `message`, `created_at`, `next_due` |
| 시계 | 같은 파일의 `TimerClock` | 데몬 기동 시 `daemon/__main__.py`가 시작한다. 기본 15초마다 원장을 훑어 만기가 된 타이머를 발사한다 |
| 전달 | `TimerClock._deliver` | `delivery_wake("timer")` 안에서 `Session.queue_delivery`로 블록을 넣는다 |
| 상태 회계 | `resume_after_pause`, heartbeat `<데몬 디렉터리>/agent-timers.json` | 데몬이 살아 있던 마지막 시각을 기록하고, 다음 기동에서 그 사이의 다운타임만큼 모든 `next_due`를 민다 |
| 타이머 띠 | `daemon/session_timers.py`의 `agent` provider | `GET /api/sessions/<name>/timers`에 `source: "agent", kind: "timer"`로 실린다. 웹 UI의 `#term-timers`가 그대로 그린다 |
| MCP | `src/claude_launcher/timer_mcp.py` | 도구 `timer`. `POST /api/sessions/<name>/timers`로 넘긴다 |
| HTTP | `src/claude_launcher/daemon/api.py`의 `h_session_timer_add` | 본문 `{every, times, message}`를 검증하고 원장에 쓴다 |

## 흐름

1. 에이전트가 `timer(every=600, times=6, message="지금까지 진행 상황을 5줄로 보고해라")`를 부른다.
2. 데몬이 원장에 타이머를 쓰고 `next_due = 지금 + 600`으로 둔다. 첫 발사는 한 간격 뒤다.
3. 시계의 패스가 `next_due`가 지난 타이머를 찾으면 `fires`를 올리고, 다음 만기를 `지금 + every`로
   옮기며, 세션에 다음 블록을 넣는다.

   ```
   ---
   # claunch timer: scheduled message -- machine-generated, not typed by the user
   timer: t-1a2b3c4d (fire 2/6, every 600s)
   message: 지금까지 진행 상황을 5줄로 보고해라
   protocol: ... 다음 발사는 한 간격 뒤다.
   ---
   ```

4. `fires`가 `times`에 닿으면 원장에서 지운다. 마지막 블록은 `(last)`로 그 사실을 말한다.

## 세션 상태와 수명

| 세션 상태 | 타이머 | 발사 시 |
|---|---|---|
| 실행 중(턴 진행·idle) | 계속 | 터미널에 블록을 넣는다 |
| hold (유휴로 프로세스 종료) | 계속 | `queue_delivery`의 wake 경로가 세션을 되살려 블록을 넣는다 |
| pause (일시 중지) | 계속 | 시계가 `respawn_async`로 세션을 되살린 뒤 블록을 넣는다. `resumed_by_human`은 False로 둔다 — 데몬의 시계이지 사람이 아니다 |
| kill·크래시·아카이브 | 원장 삭제 | 발사 없음 |
| 데몬 정지·재기동 | 유지, 다운타임은 세지 않음 | 재기동 뒤 남은 시간이 지나면 발사 |

- 다운타임 회계의 기준점은 heartbeat다. 기록 시점은 세 곳이다: 타이머가 있을 때의 15초 패스,
  부팅(`resume_after_pause`), 그리고 타이머를 만들 때(`add`). 개별 타이머가 미는 양은 자기
  수명(생성 뒤로 지난 시간)을 넘지 않는다 — 기준점이 오래된 값이어도 데몬이 켜져 있던 시간을
  다운타임으로 세지 않는다.
- hold는 exit hook 자체를 부르지 않고(`manager._session_exited`), pause는 `paused_at`이 선 채로
  부른다. `agent_timers.session_ended`는 그 둘을 보고 원장을 남긴다. 나머지 종료는 세션의 진짜
  끝이므로 지운다.
- 데몬 재기동은 세션의 종료가 아니고(`shutting_down`이면 exit hook이 돌지 않는다), `redefine`으로
  세션을 다시 만들 때도 hook이 보류된다. 두 경우 모두 타이머가 살아남는다.

## 규칙

- 간격은 10초에서 30일, 횟수는 1에서 1000, 메세지는 4000자까지다. 세션 하나에 살아 있는 타이머는
  20개까지다(`MIN_EVERY`, `MAX_EVERY`, `MAX_TIMES`, `MAX_MESSAGE`, `MAX_ACTIVE`).
- 시계가 한 간격보다 오래 막혀 있었으면 밀린 발사를 몰아서 하지 않는다. 다음 만기는 `지금 + every`다.
  보고 타이머의 일은 주기를 지키는 것이지 밀린 보고를 배달하는 것이 아니다.
- 취소·목록 도구는 없다. 타이머는 `times`로 유한하고, 세션을 kill하면 함께 끝난다. 필요해지면
  후속 이슈로 다룬다.
