# 세션 fault

세션 fault는 하네스(에이전트 CLI)가 **작업이 아니라 계정·인증·서비스 상태, 또는
사람의 키 입력 때문에** 더 진행할 수 없게 된 상태다. 에이전트가 다르게 일해서
풀 수 없고, 사람이 조치해야 한다. 일반 작업 실패(테스트 실패, 도구 오류)는
fault가 아니다.

예: 주간 사용 한도 소진, 로그인 거부, API 5xx/overloaded, DNS·연결 실패, 정책상
응답 거부, 모달 프롬프트가 키 입력을 기다림.

무엇이 풀어 주는지는 kind마다 다르고, 그 차이는 `resolved_by`가 적는다(아래
「등급」).

## 왜 필요한가

세션이 멈춘 것을 사람이 `capture-pane`으로 눈으로 확인해야 했던 사고가 있다
(claunch-u8wjx.2, 2026-09-28: reviewer가 주간 한도에 걸린 채 리뷰가 멈췄다).
`claunch-pczoc`(2026-10-23)은 그 판별을 자동화한다.

## 어디서 읽는가

`daemon/session_fault.py`의 `Detector`가 세션 화면의 **아래쪽 16행**을 1초에 한
번 읽는다. 출력 스트림 전체가 아니라 화면 아래쪽을 읽는 이유는, fault 메시지가
세션이 멈출 때 마지막으로 그려지는 반면 대화 본문에 인용된 같은 문구는 위로
스크롤되어 사라지기 때문이다.

세 가지 행은 매칭 전에 뺀다.

- diff 행: `+ text`, `- text`, `  12 + text`. fault 문구를 인용한 문서를 쓰는
  세션이 자기 자신을 fault로 만들지 않게 한다.
- 하네스 도구 호출 헤더: `⏺ Bash(rg "You've hit...")` 같은 행. 명령줄에 문구가
  들어간 경우다.
- 그려진 구분선: `──────`처럼 상자나 안내를 감싸는 가로줄. 낱말이 없고, 기록되는
  메시지는 매칭된 행 아래를 길이 제한까지 이어 붙이므로 아래에 그어진 줄이
  메시지 끝에 붙었다(`pi`, s1736, 2026-10-25: `Error: Retry failed after 3
  attempts: Request timed out.` 뒤에 `─` 140자).

이 판별은 휴리스틱이다. 거짓 양성이 생기면 그 kind의 window가 끝날 때 사라진다.

## 종류(kind)

`src/claude_launcher/session_faults.yaml`의 `kinds:`가 정의한다.

| kind | label | 기본 window | `resolved_by` | 사람이 할 일 |
|---|---|---|---|---|
| `usage_limit` | usage limit | 24시간 | `account` | 리셋을 기다리거나 플랜을 올린다 |
| `account_suspended` | account suspended | 24시간 | `account` | 프로바이더의 결제·청구 상태를 확인한다 |
| `rate_limit` | rate limit | 15분 | `provider` | rate window가 지나기를 기다린다 |
| `overloaded` | provider overloaded | 5분 | `provider` | 몇 분 기다린다. 계속되면 프로바이더가 열화된 상태다 |
| `auth` | login | 6시간 | `account` | 하네스 로그인을 다시 한다 |
| `api_status` | API error | 30분 | `provider` | 프로바이더 상태를 확인하고 나중에 재시도한다 |
| `network` | network | 15분 | `provider` | 연결과 DNS를 확인한다 |
| `policy_blocked` | policy block | 1시간 | `policy` | 표현을 바꾸거나 모델을 바꾼다 |
| `awaiting_input` | waiting for input | 15분 | `input` | 그 세션의 터미널에서 프롬프트에 답한다 |

메시지가 리셋 시각을 적으면(`resets Oct 26, 9am (Asia/Seoul)`) window 대신 그
시각이 만료 시각이 된다. `zoneinfo`로 시간대를 해석하고, 해석할 수 없으면
window를 쓴다. 상대 표기(`resets in 3h`)도 읽는다.

`API Error: <숫자>` 꼴은 코드 전용 패턴이 없어도 `api_status`로 잡고, 읽은
코드를 fault의 `code` 필드에 싣는다(`API Error: 503` → `code: 503`). 이 일반
폴백은 `common:`에 있어 하네스와 무관하게 적용되고(2026-10-24 이전에는
`claude` 목록에만 있어 다른 하네스는 상태 코드를 읽지 못했다), `fallback: true`가
붙어 있어 같은 창에 원인을 말한 문구가 있으면 진다. 특정
코드가 고유한 의미를 가지면 그 코드 전용 패턴으로 별도 kind로 읽는다(401 →
`auth`, 412 → `account_suspended`, 429 → `usage_limit`/`rate_limit`, 529 →
`overloaded`). 코드로 읽는 판정도 `common:`에 있어야 그 판정이 다른 하네스에서도
선다 — 401과 429가 `claude` 목록에만 있던 동안 다른 하네스의 같은 문구는 fault로
서지 않았다. 한도를 적은 429는 rate window가 아니라 소진된 플랜이므로, 한도
문구가 같은 창에 있으면 그 문구가 이긴다(아래의 판정 규칙). `opencode` 경로 뒤의
프로바이더가 쓰는 `Request rejected (429) · Go usage limit exceeded`도 `common:`의
`\busage limit exceeded\b` 항목으로 `usage_limit`(24시간 창, 자동 pause 대상)으로
읽는다(claunch-16j5v). 이 항목 전에는 그 줄이 맨 429 항목에만 매칭되어
`rate_limit`(15분)으로 읽혔다.

`529 Overloaded`는 프로바이더가 요청을 거절하는 동안 하네스가 스스로 재시도하는
상태다(`API Error: 529 Overloaded — retrying in 30s (attempt 3/10)`). 창을
`api_status`(30분)보다 짧은 5분으로 두는 이유는, 한 번의 재시도로 풀리는 경우가
많고 5분이 지나도 남아 있으면 그때는 프로바이더 열화로 읽어야 하기 때문이다.

`Error: Request timed out.`과 `Error: Retry failed after 3 attempts: Request
timed out.`은 `common:`에 있다 — 문구가 프로바이더 클라이언트의 것이지 한
하네스의 것이 아니다(`claude`도 찍고 pi도 찍는다, s1736 관측). 두 줄 모두
`network`로 읽는다. 나뉘는 것은 kind가 아니라 **기록되는 메시지**다: 그 줄로
끝나는 화면은 위의 시도 줄들 대신 소진 줄(`Retry failed after N attempts`)을
기록한다 — 소진 줄이 가장 아래 행이라서다. 그 화면이 클라이언트가 멈춘 상태이고
사람이 툴팁에서 읽어야 하는 줄이기 때문이다. 재시도가 소진됐다는 사실에 별도
kind나 다른 창을 주지는 않았다 — 관측이 한 건이고, 창의 근거가 될 재시도
간격을 재지 못했다.

### 창은 한 번 관측(run)에 하나다

fault 기록의 창(`until`)은 그 kind를 **처음 본 시각**에서 시작하고 늘어나지
않는다. 창 안에서 같은 kind가 다시 매칭되면 `last_seen`과 메시지만 갱신하고
`since`·`until`·`reset`은 그대로 둔다. 창이 지난 뒤 같은 kind가 다시 매칭되면
그것은 새 run이고, 재관측 횟수(`strikes`)가 1 오른다. kind가 바뀌면 횟수는 1로
돌아간다.

창이 끝나기 전에 run이 닫히는 경우가 하나 있다: **화면이 바뀌었는데 스캔 창에
그 fault 문구가 더 이상 없을 때**다. 스캔은 화면이 바뀐 때만 돌고(`session.py`의
지문 비교), 멈춰 있는 세션은 자기 fault 줄을 스캔 창 밖으로 밀어 낼 새 출력이
없다. 그러므로 바뀐 화면에서 메시지가 사라졌다는 것은 그 정지가 화면상
끝났다는 뜻이다 — 재시도가 성공해 작업이 이어졌거나, 사람이 프롬프트에 답했거나,
로그인을 다시 했다. 이때 run은 창을 채우지 않고 그 자리에서 닫힌다(`cleared`).
이 규칙이 들어오기 전에는 창이 끝날 때까지 배지가 남았고, 실측에서 그 갭이
드러났다(claunch-4ghrr, 2026-10-30: s6081·s6082가 429를 몇 분 만에 재시도
성공으로 넘기고 오류 줄 아래에서 작업을 이어 가는 동안 배지가 15분 창의
나머지를 채웠다). 조기 해제된 run이 다시 매칭되면 창 만료와 같은 규칙이다 —
새 run이고 `strikes`가 1 오른다. 빈 읽기(스캔 창에 읽을 행이 없는 순간)는
근거가 아니므로 조기 해제하지 않는다.

`reset`을 함께 보존하는 이유는 한 행이 한 번의 관측만 말하게 하기 위해서다.
`reset`은 `until`을 계산한 근거 문구이므로 둘이 다른 관측에서 오면 툴팁이
`resets: Oct 25, 2pm`과 `until Oct 26 09:00`을 나란히 그린다. 주간 한도 줄과
5시간 한도 줄이 같은 화면에 있으면 둘 다 `usage_limit`이라 창 안 재매칭이
일어나고, 그때 `reset`만 새 값으로 갈아 끼우던 것이 그 어긋남이었다
(claunch-5ldt8 리뷰).

메시지가 적은 리셋 시각이 이미 지나 있으면(`reset_deadline`은 6시간까지 과거를
읽는다) `until`이 첫 스캔부터 `since`보다 앞선다. 그런 기록에는 끝날 창이 없으므로
`now < until` 대신 **계속되는 run**으로 다룬다 — 그렇게 하지 않으면 매 스캔이 새
run이 되어 `strikes`가 관측이 아니라 스캔 수(화면이 바뀌는 동안 1초에 1씩)를
센다. 판정은 **이전 기록과 지금 관측 양쪽**을 본다: 이어 가는 것은 지금 관측도
과거 리셋을 적은 경우뿐이고, 지금 화면이 미래의 리셋을 적으면 그 시각으로 새
창이 열린다. 이전 기록만 보면 한 번 과거 리셋을 그린 세션이 이후 미래 리셋을
그려도 창이 과거에 묶인 채 `active`가 서지 않는다(claunch-5ldt8 리뷰의 회귀).
끝난 창이 없는 기록은 `strikes`를 올리지 않는다 — 끝난 것이 없기 때문이다.

이 규칙 전에는 매 스캔이 `until = now + window`로 창을 밀었다. 기록되는
메시지는 매칭된 행 아래를 이어 붙인 발췌라(`_message_from`), 세션이 자기
분석에서 그 문구를 인용하고 산문이 스크롤되면 발췌가 매초 달라졌다. 발췌가
다르면 새 기록으로 서서 창이 다시 열렸고, 상황이 풀린 세션의 배지가 15분마다
새로 섰다(s1812, 2026-10-25: `seen 15:13:38 → until 15:28:38` 다음에
`since 15:19:11 → until 15:34:11`).

`strikes`는 세션 정보·`/api/session-faults`·MCP `session_faults`의 각 행에
실린다. 배지 툴팁은 2회 이상일 때 `re-observed: N×` 줄을 그린다. 화면이
바뀌지 않으면 스캔이 돌지 않으므로(`session.py`의 지문 비교), 멈춘 화면에서는
창이 지나면 그대로 만료된다.

### 등급: `resolved_by`

kind가 진단이라면 `resolved_by`는 **무엇이 이 정지를 풀어 주는가**다. 값 넷.

| `resolved_by` | 뜻 |
|---|---|
| `input` | 그 터미널에서 사람이 키를 누른다 |
| `account` | 사람이 플랜·결제·로그인을 고친다 |
| `provider` | 프로바이더 상태가 회복되거나 시간이 지난다 |
| `policy` | 세션이 표현이나 모델을 바꾼다 |

이 축이 없으면 "기다리면 풀리는 정지"와 "아무도 보지 않으면 영원히 서 있는 정지"가 같은 무게로 읽힌다. `policy_blocked`를 `account`에 넣지 않은 이유도 같다: 그 둘의 조치는 서로 다른 사람·대상에게 있다.

### `awaiting_input`: 사용자 입력 대기

하네스가 키 입력을 기다려 멈춘 상태다. 계정도 서비스도 정상이고, 기다려도 풀리지 않는다 — 그 터미널 앞의 사람만 푼다. 정지의 성격(다른 누구도 세션을 움직일 수 없다)이 위의 fault들과 같아 같은 목록에서 읽고, `resolved_by: input`으로 가른다.

관측된 화면 둘(2026-10-25, `claude`):

```
  Teach auto mode about your environment?
  ❯ 1. Yes
    2. Not now
    3. Don't show again

  Enter to confirm · Esc to cancel
```

```
  We're changing auto mode to no longer charge for classifier requests in Claude Code.
  …

  Enter to continue · Esc to cancel
```

패턴은 두 푸터의 공통 부분(`Enter to confirm|continue … Esc to cancel`)을 잡는다. 두 낱말 사이의 구분자는 화면폭에 따라 달라지므로 패턴이 그것에 의존하지 않는다. 푸터는 `harnesses.claude`에 있다 — 관측이 이 하네스 하나뿐이다.

이 kind에는 자동 동작이 없다. 검출은 기록과 표시까지이고, 키를 대신 보내거나 세션을 끝내지 않는다. 키를 기다리는 화면은 이 둘 외에도 있다(도구 승인 프롬프트가 같은 푸터를 그린다). 반대로 이 목록에 넣지 않는 정지가 있다: 리더·상위의 답을 기다리는 mesh ask, compaction, 작업 실패 재시도 루프 — 셋 다 키 입력을 기다리지 않고 사람이 풀 일도 없다.

### API 응답을 기다리는 줄: `network`

관측된 화면 하나(2026-10-27, `s3568`, `claude`):

```
* Waiting for API response · will retry in 1m 24s · check your network
```

클라이언트가 다음 시도를 세고 있는 상태다. kind는 `network`, 항목은 `fallback: true`, 목록은 `common:`이다.

- `api_status`가 아니라 `network`인 이유: 이 줄은 상태 코드를 받은 뒤의 판독이 아니다. 응답이 오지 않아 전송 계층을 의심한다고 스스로 적는다. `api_status`는 제공자가 코드로 답했을 때의 판독이다.
- `fallback`인 이유: 이 줄은 클라이언트가 아직 기다린다는 것만 말하고 왜인지는 말하지 않는다. 같은 창의 `You've hit your weekly limit`이나 `safeguards flagged`가 위아래 어디에 있든 그쪽이 이긴다. 표시가 없으면 이 줄이 더 아래에 있을 때 플랜 소진을 15분짜리 network fault로 읽는다.
- `common:`인 이유: 관측이 `claude` 하나뿐이지만, 위의 플랜 소진 줄과 같은 규칙을 쓴다 — 나중에 추가된 하네스도 자기 목록 없이 읽는다(claunch-pczoc.2.1).
- 툴팁은 그 줄을 통째로 남긴다. 재시도까지 남은 시간(`will retry in 1m 24s`)은 kind가 담지 않는 값이다.

폴백끼리 같은 창에 서면 위치가 가른다. 맨 상태 코드(`API Error: 500`) 아래에 이 줄이 있으면 `network`, 위에 있으면 그 코드의 kind(`api_status`)다 — 둘 다 원인을 말하지 않는 판독이라 규칙이 정한 순서대로 아래 행이 이긴다.

### 상태 코드 없는 API 오류 재시도 줄: `api_status`

관측된 화면 하나(2026-11-06, `s6183`, `claude`, claunch-al4lg):

```
✻ API error · Retrying in 0s · attempt 1/10
```

API 오류 뒤 클라이언트가 재시도하는 동안 그리는 스피너 줄이다. 상태 코드가 없어 `API Error: <숫자>` 항목으로는 읽히지 않는다. 원인 미상. kind는 `api_status`, 항목은 `fallback: true`, 목록은 `common:`이다.

- `api_status`인 이유: 줄이 API가 오류로 답했다고 적는다. 전송 계층을 의심하는 문구는 없다.
- `fallback`인 이유: 원인을 말하지 않는다. 같은 창에 원인 문구가 있으면 그쪽이 이긴다.
- 패턴은 `API error` 바로 뒤의 `·`를 요구한다. `API Error: 529 Overloaded — retrying in 30s`는 이 항목에 걸리지 않고 `overloaded`로 남는다.

## 판정 규칙

한 스캔 창(아래 16행)에 fault 문구가 둘 이상 있으면 어느 것이 kind를 정하는지가
문제다. 규칙은 세 축이다.

- **폴백은 원인을 말한 문구에 진다.** 항목에 `fallback: true`가 붙으면 그 항목은
  원인을 말하지 않는 판독이다 — 맨 상태 코드(`API Error: 401`·`412`·`429`·`529`,
  `API Error: <숫자>`), 클라이언트가 포기했다는 줄(`Error: Request timed out.`,
  `Error: Retry failed after N attempts`), 그리고 응답을 기다리는 중이라는 줄
  (`* Waiting for API response … check your network`)이 그렇다. 창 안에 폴백이 아닌
  매칭이 하나라도 있으면 폴백은 후보에서 빠진다. 그래서 `You've hit your weekly limit`이
  `Request timed out`과 같은 화면에 있어도 플랜 소진으로 읽힌다 — 두 줄의 위아래
  순서와 무관하다(claunch-pczoc.2.1.1).
- **남은 후보 중에서는 가장 아래 행이 이긴다.** 화면은 위에서 아래로 그려지므로
  더 아래 행이 더 나중에 그려진 줄이고, 세션이 멈춘 줄이다. 한 패턴이 여러 번
  매칭되면 마지막 매칭을 쓴다.
- **같은 자리에서 시작하는 두 항목은 목록 순서가 가른다.** 위치로는 나뉘지 않는
  유일한 경우다. `common:`이 하네스 목록보다 먼저 오는 것도 이 동률 처리
  때문이다.

`resolved_by`는 이 판정에 참여하지 않는다. 세 축은 창 안의 패턴과 그 행 위치만
읽고, 등급은 이긴 kind에서 사후에 읽는 값(`Kind.resolved_by`)이다. 항목에 등급을
더하거나 바꿔도 어느 줄이 이기는지는 달라지지 않는다.

이 규칙 전에는 목록 순서가 유일한 판정 기준이었다(claunch-pczoc.2.1까지). 그래서
`common:`에 문구를 넣으면 그 하네스가 같은 창에 함께 그린 더 구체적인 문구를
이겼고, 그것을 피하려면 그 문구를 손으로 `common:` 위로 옮겨야 했다 — 실제로
두 타임아웃 줄을 `common:`에 넣은 뒤 플랜 소진 문구·로그인 안내·정책 거부·한도
없는 429를 그 위로 옮겼다. 옮기지 않았으면 그 화면들은 `usage_limit`·`auth`·
`policy_blocked`·`rate_limit` 대신 `network`(15분)로 읽혔다(claunch-pczoc.2.1의
리뷰에서 지적, 2026-10-24 실측). claunch-pczoc.2.1.1이 그 판단을 데이터가 아니라
항목의 `fallback` 표시로 옮겼다.

## 어디에 나타나는가

- 세션 정보(`Session.info()`, `/api/sessions` 행): `fault` 필드. 활성/만료
  모두 `active`로 구분해 싣는다.
- 세션 레일: 활성 fault가 있으면 빨간 `!` pill과 툴팁(메시지·리셋·할 일)을
  그린다. 상태 점의 툴팁도 fault를 먼저 말한다.
- `GET /api/session-faults`: 함대 조회. `session`, `active`(`active` 기본,
  `inactive`, `all`), `kind`, `harness`, `limit` 질의를 받는다.
- MCP 도구 `session_faults`: 같은 조회의 읽기 전용 래퍼. 에이전트가 피어의
  한도 소진을 `capture-pane` 없이 확인하는 경로다.

## fault가 바꾸는 데몬 동작

- 반복 재촉: fault가 활성이거나 보류(`held`) 중인 세션에는 주기적으로 반복되는
  재촉을 보내지 않는다(claunch-ijot0, claunch-16j5v). 모든 kind가 대상이고, 판정은
  `session_fault.holds_notices` 하나다(활성 판정은 `session_fault.is_active`).
  대상은 cflow v2 재촉(renudge, `Cflow2Clock._driver_faulted`), 세션 리마인더
  (`SessionReminderService.session_faulted` — cflow·role·prompt pack 블록),
  stall ping(`StallPingClock`, 기본 꺼짐)이다.
  fault 상태의 하네스는 입력마다 같은 fault 문구로 답하므로, 재촉은 화면만
  채운다(s29: 주간 한도 상태에서 10분마다 8시간 동안 입력됨). 바쁜 driver와
  같은 방식으로 건너뛰어 간격을 쓰지 않으므로, fault 창이 끝난 뒤 첫 pass에서
  한 번 재촉한다. 런이 실제로 움직인 알림(wait 발화, 위임 완료)은 그대로 보낸다.
- 반복 보류(claunch-16j5v): 창이 끝난 직후의 재촉이 같은 fault를 다시 띄우면, 그
  재촉은 화면만 채운 것이다(s6252, 2026-11-01: `Go usage limit exceeded`가
  15분짜리 `rate_limit`으로 읽혀 17:53·18:08·18:24에 재촉과 429가 반복됨, strikes 9).
  그래서 run마다 `repeats`와 `hold_until`을 기록한다.
  - `repeats`: 직전 run의 보류가 끝난 뒤 kind의 창 하나 안에 다시 관측된 연쇄의
    길이. 첫 관측은 1이고, 창 하나보다 늦게 돌아온 관측은 1로 다시 시작한다.
    `strikes`는 간격과 무관하게 모든 재관측을 센다. `cleared` 여부는 연쇄를 끊지
    않는다 — 입력된 재촉이 fault 줄을 화면 밖으로 밀어 run을 닫기 때문이다.
  - `hold_until`: `since + min(window × 2^(repeats−1), NOTICE_HOLD_CAP)`. 상한은
    6시간이다(15분 창: 15·30·60·120·240·360분). 화면이 바뀌지 않은 채 끝난 정지도
    상한 안에 재촉을 한 번 받는다. 하네스가 리셋 시각을 적었으면(`until_stated`)
    `hold_until`은 `until`과 같다.
  - `held`: 창이 끝났고(`now >= until`) `hold_until` 전이며 `cleared`가 없음.
    화면이 fault 줄을 지나 움직이면 창과 같이 보류도 그 자리에서 끝난다. 배지·
    레일은 `held`를 표시하지 않는다(`active`만 표시).
- 자동 pause: `usage_limit`이 활성인 idle 세션은 데몬이 pause한다
  (`daemon/fault_pause.py`, 30초 주기, `ended_by`는 `daemon`/`usage-limit`).
  pause된 세션은 입력을 받지 않고, 리더면 아래 카드도 내려간다. 바쁜 세션은
  pause하지 않는다(화면 판정의 오탐 대비). 리셋 전에 사람이 resume한 세션은
  같은 한도(`until`이 같음)로 다시 pause하지 않는다. 그 기억은 세션 디렉터리의
  `fault-pause.json`에 `pause_key`(판독의 `until`, 하네스가 리셋 시각을 적지
  않았으면 빈 문자열)로 함께 남아 데몬 재기동을 넘긴다 — 메모리 맵만 있던 동안은
  재기동 뒤 첫 pass가 사람이 되돌린 결정을 다시 pause로 덮었다(claunch-ijot0.1).
  기록이 `until`을 추정해 채운 경우 그 값은 신원이 아니므로, 옛 기록은 하네스가
  시각을 적었을 때만 `until`로 맞춘다. 설정 키
  `daemon.fault_auto_pause`(기본 켬)로 끈다. 리셋 뒤 resume은 사람이 한다.
- 재개 후 유예 (claunch-x9agn): 세션이 pause·hold·kill에서 다시 실행되거나
  (respawn·wake·resume) 키 변경·borrow·redefine으로 재시작되면, 데몬은 그 시각을
  `Session.resumed_at`에 남기고 그 뒤 `daemon.fault_resume_grace`초(기본 30, 0이면
  끔) 동안 그 세션을 자동 pause하지도 자동 키 전환하지도 않는다. 재개한 대화가
  이전 실행이 멈춘 한도 문구를 화면에 다시 그리면, 그 문구가 새 키·프로필이
  응답하기 전에 세션을 다시 pause했다. 유예 중에는 세션 상단 timer line에
  `fault`/`grace` chip(⛨ `usage-limit grace`)이 남은 시간을 보인다. chip은 세션이
  active `usage_limit` 판독을 내는 동안에만 보이고, 한도가 해소되면 유예 시간이
  남아 있어도 사라진다(claunch-xn9ir). 유예가 끝난
  뒤의 pass에서 한도 문구가 아직 화면 하단에 있으면 이전처럼 pause한다(pass가
  30초 주기라 재개부터 최대 약 60초). 데몬 재기동 뒤의 자동 복원은 유예 대상이
  아니다(`resumed_at`은 메모리에만 있다).
- 자동 키 전환 (claunch-d2t79): 키 풀을 가진 프로필의 세션이 `usage_limit`을
  맞으면, 그 세션이 스스로 켜 둔 경우(`SessionDef.auto_key_switch`, 기본 꺼짐)
  pause 전에 다른 키로 갈아탄다(`daemon/key_switch.py`). 같은 pass에서 이 전환이
  먼저 돌고, 살아난 세션은 fault 판독이 새 화면이라 pause 대상에서 빠진다 —
  clock을 둘로 나누면 전환이 재시작하려는 세션을 pause가 먼저 잡는 창이 생긴다.
  - 켜는 자리는 셋이다. (1) 키 모달 — 세션 카드의 키 칩이나 detail panel의
    Restart settings에서 연 「Change key…」 모달 안의 체크박스이고, 값을 쓰는
    라우트는 `POST /api/sessions/<name>/auto-key-switch`다. Restart settings 줄은
    지금 키와 이 값의 상태를 함께 보여 준다. (2) new session 폼과 spawn child
    폼의 체크박스 — 체크되었을 때만 `auto_key_switch: true`를 바디에 싣고,
    생성 라우트와 `spawn.check()`가 각각 정의에 옮긴다. 요청에 값이 없으면
    자식은 꺼짐으로 시작한다(상속하지 않는다). (3) Settings 페이지의
    `fault_key_switch_retries` — 세션별 스위치가 아니라 재시도 횟수다.
  - 고르는 키는 `usage min` 하나로 고정한다(`profile_keys.switch_candidate`).
    후보는 풀의 키 가운데 (1) 어느 윈도도 한도에 닿지 않았고 (2) 지금 이 세션이
    쓰는 키가 아닌 것이다. 순서는 윈도별 사용률의 **최댓값**이 작은 순이고,
    동률이면 5시간 → 1주 → 1개월 순으로 낮은 쪽이다(`usage.tie_break_key`).
    사용량을 읽을 수 없는 키는 건너뛴다 — 읽히지 않은 키에는 비교할 최솟값이 없다.
  - 전환은 `SessionManager.set_key`(정의에 핀을 쓰고 재시작)로 하고, 프로세스가
    입력을 받을 수 있게 되면 `continue`를 한 줄 넣는다(`key_switch.CONTINUE`).
  - 한 fault당 시도 횟수는 설정 키 `daemon.fault_key_switch_retries`(기본 1, 0이면
    전 기능 끔)로 제한한다. 시도 횟수는 세션 디렉터리의 `key-switch.json`에
    fault의 리셋 시각과 함께 남으므로 데몬을 재기동해도 이어진다 — pause 쪽의
    같은 문제(claunch-ijot0.1)도 이제 `fault-pause.json`의 `pause_key`로 같은
    방식으로 넘긴다(위 「자동 pause」 절). 횟수를 다 쓰거나 남은 키가 없으면 지금까지처럼
    pause한다. 갈 곳이 없다는 판정은 10분간 유지해, 며칠짜리 한도 동안 30초마다
    provider에 키마다 묻지 않는다(`key_switch.RECONSULT`).
- pause는 beads 이슈를 open으로 돌리지 않는다. hold와 같이 in_progress로
  남고, pause된 기록을 kill·archive·remove할 때 `SESSION ENDED` 정리가 돈다
  (`SessionManager.unpause`/`settle_pause`, `beads.Board.session_settled`).
  데몬 재기동도 pause된 기록을 정리하지 않는다.
- pause된 세션의 fault: pause는 프로세스를 종료시키므로 화면을 읽던 fault
  기록도 함께 사라진다. 그래서 pause한 세션은 리셋까지 며칠 동안 `fault: null`로
  보였고, 대시보드에는 데몬이 멈춘 세션과 그냥 죽은 세션의 구분이 없었다
  (claunch-9djny.1). 이제 `DeadSession.fault()`가 세션 디렉터리의
  `fault-pause.json`을 읽어 같은 모양의 뷰를 답하고 `source`를
  `pause-record`로 낸다. 조건은 넷이고 모두 참일 때만 답한다 —
  `ended_by.reason`이 `usage-limit`, `paused_at`이 있음, `archived_at`이 없음,
  기록의 `paused_at`이 세션의 값과 같음(이름을 다시 쓴 나중 세션이 옛 파일을
  읽지 않게). `active`는 살아 있는 판독과 같은 식(`now < until`)이고, `until`을
  읽을 수 없는 기록은 kind의 창(usage_limit 86400초)을 pause 시각에 더해 상한으로
  쓴다. unpause는 `paused_at`을 지우므로 그 fault도 함께 사라진다. 세션 레일은
  이 행에 `!` pill을 세우고, 툴팁이 pause 이유·리셋 시각·할 일과 「리셋이
  자동으로 재개하지 않는다」를 적는다.
- 리셋 알림: 자동 pause는 그 fault(`kind`, `until`, `until_stated` 등)를
  세션 디렉터리의 `fault-pause.json`에 남긴다. 데몬 재기동 뒤의 기록은 화면을
  잃으므로 이 파일이 근거다. `GET /api/fault-resets`는 이 시계가 pause했고 아직
  pause 상태인 세션을 두 목록으로 나눠 돌려준다 — `pending`(리셋 전)과
  `resets`(리셋 시각이 지남), 행마다 `due`가 붙는다. 대시보드는 세션마다 카드
  하나를 처음부터 띄우고(`usage limit pause: <세션>`, 출처 `fault`, ×로 닫힘),
  리셋이 지나면 같은 카드의 문구만 「재개 결정 필요」(`usage limit reset: ...`)로
  바뀐다 — 키는 세션이고 서명은 `session + until`이라 카드가 두 장이 되지 않는다.
  카드는 하네스가 화면에 리셋 시각을 적었을 때(`until_stated`)만 뜬다 —
  시각이 없어 kind의 창으로 추정한 `until`은 계획이 돌아오는 시각과 무관하고,
  하네스마다 시각을 적는지가 다르다. resume·kill·archive하면 카드가 내려간다.

## 리더 fault 알림 (fatal)

리더 역할 세션이 멈추면 그 세션 하나의 문제가 아니다. 착지 요청·통합·심사가
그 세션을 거치므로 프로젝트 전체가 서 있고, 레일의 작은 `!` pill 하나로는
다른 세션을 보던 운영자가 알아채지 못한다. 그래서 이 경우만 대시보드의 알림
창에 fatal 등급 카드를 띄운다(claunch-9djny).

판정은 `daemon/leader_fault.py`가 한다. 세 가지를 함께 읽는다.

- 역할: 이 데몬이 아는 메시에서 그 세션이 `leader`를 쥐고 있다
  (`Member.roles` — 대표 역할이거나 subrole).
- 상태: 세션 범주가 `running` 또는 `hold`다. `hold`는 프로세스는 멈췄지만
  대화·런·일이 남아 있어 프로젝트가 여전히 그 세션을 기다리는 상태다
  (유휴 hold가 fault 기록을 지우지는 않는다 — 데몬이 그 세션 객체를 계속
  들고 있는 동안 `fault()`가 답한다). `paused`·`killed`·`failed`·`delayed`·
  `archived`는 사람이 일부러 멈춘 것이므로 알리지 않는다 — 운영이 서 있는
  상태가 아니다.
- fault: 세션의 fault 기록이 활성(`active`)이다. 창이 지난 기록은 알리지
  않는다.

`GET /api/leader-faults`가 그 목록을 `{"alerts": [...], "count": N}`으로
내놓는다. 행은 `session`, `handle`, `meshes`, `category`, `fault`(게시하는
필드는 `FAULT_FIELDS`로 고정)다. 같은 세션이 여러 메시에서 리더면 한 행에
`meshes`로 모인다. 다른 머신의 멤버는 이 데몬이 세션을 모르므로 행이 서지
않는다 — fault는 이 머신의 화면에서 읽은 값이다.

대시보드는 이 목록을 폴링해(`app.js`의 `refreshLeaderFaults`) 세션마다
`fatal` 등급 카드를 하나 띄운다. 카드는 자동으로 사라지지 않고(타임아웃 없음)
다른 알림 카드와 같은 우상단 × 를 가진다 — 자리를 비운 읽는 사람에게도 남아
있고, 손으로 치울 수도 있다. × 로 내린 카드는 같은 값이 유지되는 동안 다시
서지 않고, 값이 바뀌면 다시 선다. 클릭하면 그 세션을 연다. 데몬이 그 행을 더
이상 내놓지 않으면(창 만료, 복구, 또는 세션이 `paused`/`killed`/`archived`로
이동) 카드는 폴링이 스스로 내린다 — 이 폴이 기억한 서명에 키가 없어도 화면에
남은 `leader-fault-` 카드는 다음 폴이 회수한다(claunch-8vl1s).
카드를 다시 띄우는 것은 카드가 말하는 값(kind·메시지·`until`·범주)이 바뀌었을
때뿐이라, 2초 폴링이 알림 기록을 같은 사건의 사본으로 채우지 않는다. 기록에는
그 사건이 한 줄 남고, 알림 창 설정에서 `leader faults (harness stopped)`를
숨기면 카드는 뜨지 않는다(사용자가 직접 끈 것이다).

카드를 실제로 그린 폴만 그 서명을 기억한다. 알림 기록 패널이 열려 있으면
`notify()`는 기록만 하고 카드를 그리지 않는데, 그때 서명을 기억해 두면 패널을
닫은 뒤에도 카드가 서지 않는다. 그래서 패널이 열려 있는 동안은 아무것도 기억하지
않고 넘기며(폴마다 기록을 쌓지 않는다), 패널을 닫은 뒤 첫 폴에 카드가 선다.

### 카드가 남는 조건 (claunch-8vl1s.1)

세션이 정상화된 뒤에도 카드가 화면에 남는 조건은 둘이고, 각각 다른 장치가
끊는다.

| 조건 | 무엇이 일어나는가 | 끊는 장치 |
|---|---|---|
| 목록은 읽히고 그 행만 사라졌다 | 다음 폴의 스윕이 화면에 남은 `leader-fault-` 카드를 회수한다 | 폴의 서명 맵 + `notices` 스윕 (claunch-8vl1s) |
| 폴 자체가 돌지 않는다 | 대시보드 틱이 기다리는 요청 하나가 응답 없이 매달리면 틱이 끝나지 않고, `pollTick`의 `polling`이 참으로 남아 이후 모든 틱이 즉시 반환한다. 카드에 타임아웃이 없으므로 화면에는 그 카드만 남는다 — 터미널 소켓은 폴과 별개라 화면은 계속 반응한다 | 요청 기한 `API_TIMEOUT_MS`(30초) + 틱 재무장 `POLL_STUCK_MS`(60초) |

두 번째 조건의 장치 둘은 함께 있어야 한다. 요청 기한은 매달린 요청이 반드시
settle하게 만들어 `budgeted()`의 슬롯을 반납시키고(`daemonHealth`가
claunch-fm3ud에서 쓴 것과 같은 모양), 재무장은 그래도 남은 매달림이 폴을 영구히
세우지 못하게 한다(`visiblePoll`이 페이지 폴에 쓰는 규칙, claunch-02w8s). 지금
틱이 매달려 있으면 다음 틱이 그대로 돌고, 재무장 사실은 링크 로그에
`poll.stuck` 한 줄로 남는다 — 같은 증상이 다시 보고되면 폴이 멈췄는지를 추측하지
않고 그 기록에서 읽는다.

목록을 읽지 못한 폴(라우트 오류, 401)은 스윕도 하지 않는다. 무엇이 살아 있는지
모르는 상태에서 카드를 지우면 실제로 멈춘 리더의 카드가 사라지기 때문이다.
라우트가 회복되면 다음 폴이 카드를 내린다.

## 메시지를 추가하는 절차

1. 실제 터미널에서 관측한 문구를 그대로 가져온다. 추측으로 쓰지 않는다.
2. `session_faults.yaml`의 `common:` 또는 해당 하네스 아래에 항목을 넣는다.

   ```yaml
   - kind: auth
     pattern: |-
       Please run /login
     sample: |-
       ●Please run /login · API Error: 401 Invalid bearer token
   ```

   - 원인을 말하지 않는 문구(맨 상태 코드, 클라이언트가 포기했다는 줄)에는
     `fallback: true`를 붙인다. 붙이지 않으면 그 문구가 같은 창에 함께 뜬 원인
     문구와 위치로 겨루게 되고, 더 아래에 있으면 이긴다.
   - 목록 순서는 같은 자리에서 시작하는 두 항목의 동률만 가른다. 지금 카탈로그에서
     실제로 그런 쌍은 `common:` 안에만 있다 — 맨 401·412·429·529 코드 항목이 일반
     `API Error: <숫자>` 항목과 같은 문자에서 시작하고, 위쪽 항목이 이겨 각자의
     kind로 읽힌다(`Overloaded`라는 낱말이 없는 `API Error: 529`도 그래서
     `overloaded`(5분)다). `common:`이 하네스 목록보다 먼저 오는 것은 앞으로 그런
     동률이 생겼을 때를 위한 관례다 — 두 목록에서 같은 문자에 걸리는 쌍(`claude`의
     `API Error: 401[^\n]{0,160}`와 `common`의 `API Error: 401\b`)은 한쪽이
     `fallback`이라 표시가 먼저 갈라, 지금은 이 순서가 판정을 바꾸는 자리가 없다.
   - kind를 새로 만들면 `resolved_by`도 정해야 한다(`input`·`account`·
     `provider`·`policy` 가운데 하나). 없거나 다른 값이면 카탈로그가
     `CatalogError`로 거절한다 — 등급 없는 kind는 배지에서 무엇을 해야
     하는지 말하지 못한다.
   - `pattern`은 `re.IGNORECASE`로 컴파일된다. 줄바꿈은 스캔 창에서 공백으로
     합쳐지므로 감싸진 문장은 공백 기준으로 쓴다.
   - `sample`은 테스트가 실제 메시지와 패턴이 어긋나는 것을 잡는 근거다.
3. `uv run --no-sync pytest tests/test_session_fault.py -q`로 확인한다.

하네스 항목이 비어 있는 것(`pi`, `codex`, `kimi`, `agent`, `devin`)은 그 하네스에
고유한 문구가 목록에 없다는 뜻이다 — 아직 관측하지 못했거나(대부분), 관측한
문구가 `common`으로 올라갔거나(pi의 로그인 안내). 그 하네스에도 `common`은
적용된다. `claude`의 목록에는 남은 두 항목(401, `Can't reach the API server`)만
있다.

## 한계

- 화면에 보이는 문구만 읽는다. 이미 스크롤되어 아래 16행 밖으로 나간 메시지는
  새로 감지되지 않는다.
- 데몬이 재시작되면 detector가 새로 만들어지므로 fault 기록은 사라진다.
  (compacting 감지와 같은 규칙이다.)
- 세션이 종료되면 그 기록(`DeadSession`)은 화면이 없어 fault를 답하지 않는다.
  fault는 살아 있는 세션의 판독이다.
- 조기 해제는 「바뀐 화면에 메시지가 없다」 하나만 읽는다. 복구가 화면 밖에서
  일어나 화면이 그대로 멈춰 있으면(예: 한도가 풀렸지만 세션에 아무 시도도
  없었다) 스캔이 돌지 않아 배지는 창까지 남는다. 반대 방향의 오류도 있다:
  터미널 크기 변경 같은 재그리기가 fault 줄을 스캔 창 밖으로 밀면 정지가
  이어지는데도 배지가 내려간다 — 그 경우 다음 실패 시도가 다시 매칭되어 새
  run으로 선다.
- fault 줄이 스캔 창 안에 남아 있는 채로 그 아래에서 작업이 이어지는 경우는
  읽지 못한다 — 그 줄이 밀려 나갈 때까지 배지가 남는다(s6082는 복구 후 약
  4분 뒤에 줄이 밀려 나갔다). 아래 행의 내용으로 복구를 읽는 판정은 하네스별
  휴리스틱이 필요해 이번에 넣지 않았다.
- `background_render: false`인 하네스(pi)는 아무도 붙어 있지 않을 때 화면을
  갱신하지 않는다. 그 구간의 fault는 뷰어가 붙어 화면이 그려질 때 읽힌다.
- `Error: Request timed out.`은 전송 계층의 실패라 `network`로 읽지만, 그
  줄이 한 번 찍힌 것과 재시도를 다 쓴 것(`Retry failed after N attempts`)은
  kind가 같아 창 길이로는 갈리지 않는다. 소진 쪽 창을 따로 둘 근거(재시도
  간격·시도 횟수의 관측)는 아직 없다.
- 한 화면에 원인을 말하는 문구가 둘 이상 있으면 가장 아래 행이 이긴다. 어느
  쪽이 세션을 실제로 멈춘 원인인지는 화면 순서로만 추정하므로, 나중에 그려진
  문구가 더 오래 남는 원인이면 그 판정은 틀린다(예: 아래쪽에 찍힌 `network`
  한 줄이 위쪽의 `usage_limit`을 이긴다). 폴백 표시는 이 추정에서 원인을 말하지
  않는 문구만 빼는 장치이고, 원인 문구끼리의 우열은 데이터로 정하지 않는다.
- 폴백끼리 같은 창에 서면 위치가 가른다. 맨 상태 코드(`API Error: 500`) 아래에
  `* Waiting for API response …`가 있으면 `network`(15분)로, 위에 있으면 그 코드의
  kind(`api_status`, 30분)로 읽힌다(2026-10-27 실측). 둘 다 원인을 말하지 않는
  판독이라 어느 쪽이 실제 원인인지는 이 규칙이 답하지 않는다.
- `awaiting_input`의 기록은 프롬프트가 사라진 뒤에는 조기 해제된다 — 답한
  프롬프트는 화면의 라이브 영역에서 사라지고, 그것이 「키를 눌렀다」를 읽는
  경로다(위 「창은 한 번 관측에 하나다」). 프롬프트가 화면에 남아 있는 한
  창(15분)까지 남는다.
- 창이 늘어나지 않으므로 실제로 이어지는 fault는 창이 끝난 뒤 새 run으로 다시
  선다. 그 사이 배지는 스캔 한 번(최대 1초)만큼 사라졌다 나타날 수 있고, 그
  재관측은 `strikes`로 남는다. 화면이 멈춰 스캔이 돌지 않으면 재관측도 없다.
- `awaiting_input`의 패턴은 2026-10-25에 관측한 푸터 문구 그대로다. 하네스가
  이 문구를 바꾸면(낱말 교체, 다른 키 이름, 푸터 제거) 그 화면은 더 이상
  검출되지 않는다. 테스트는 수집한 문구와 패턴이 어긋나는 것만 잡으므로 이
  변화를 잡지 못한다 — 패턴과 `sample`이 함께 낡기 때문이다. 새 문구를 본
  세션이 카탈로그에 그 줄을 더하고 `sample`에 넣는 것이 유일한 갱신 경로다.
