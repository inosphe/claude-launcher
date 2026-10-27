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
문구가 같은 창에 있으면 그 문구가 이긴다(아래의 판정 규칙).

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
`fatal` 등급 카드를 하나 띄운다. 카드는 고정 표시이고 ×가 없다 — 리더가
서 있는 동안 지울 수 없다는 것이 이 등급의 뜻이다. 클릭하면 그 세션을
연다. 데몬이 그 행을 더 이상 내놓지 않으면(창 만료, 복구, 또는 세션이
`paused`/`killed`/`archived`로 이동) 카드는 폴링이 스스로 내린다.
카드를 다시 띄우는 것은 카드가 말하는 값(kind·메시지·`until`·범주)이 바뀌었을
때뿐이라, 2초 폴링이 알림 기록을 같은 사건의 사본으로 채우지 않는다. 기록에는
그 사건이 한 줄 남고, 알림 창 설정에서 `leader faults (harness stopped)`를
숨기면 카드는 뜨지 않는다(사용자가 직접 끈 것이다).

카드를 실제로 그린 폴만 그 서명을 기억한다. 알림 기록 패널이 열려 있으면
`notify()`는 기록만 하고 카드를 그리지 않는데, 그때 서명을 기억해 두면 패널을
닫은 뒤에도 카드가 서지 않는다. 그래서 패널이 열려 있는 동안은 아무것도 기억하지
않고 넘기며(폴마다 기록을 쌓지 않는다), 패널을 닫은 뒤 첫 폴에 카드가 선다.

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
- 복구를 문구로 알 수 없어서, 조기 해제는 하지 않는다. 만료는 window 또는
  메시지가 적은 리셋 시각이 정한다.
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
- `awaiting_input`의 기록은 화면에서 프롬프트가 사라진 뒤에도 window(15분)
  동안 남는다. 검출기는 화면만 읽으므로 "사람이 키를 눌렀다"를 별도로 알지
  못한다. 조기 해제를 넣지 않은 것은 의도다 — 근거가 되는 관측이 아직 없다.
- 창이 늘어나지 않으므로 실제로 이어지는 fault는 창이 끝난 뒤 새 run으로 다시
  선다. 그 사이 배지는 스캔 한 번(최대 1초)만큼 사라졌다 나타날 수 있고, 그
  재관측은 `strikes`로 남는다. 화면이 멈춰 스캔이 돌지 않으면 재관측도 없다.
- `awaiting_input`의 패턴은 2026-10-25에 관측한 푸터 문구 그대로다. 하네스가
  이 문구를 바꾸면(낱말 교체, 다른 키 이름, 푸터 제거) 그 화면은 더 이상
  검출되지 않는다. 테스트는 수집한 문구와 패턴이 어긋나는 것만 잡으므로 이
  변화를 잡지 못한다 — 패턴과 `sample`이 함께 낡기 때문이다. 새 문구를 본
  세션이 카탈로그에 그 줄을 더하고 `sample`에 넣는 것이 유일한 갱신 경로다.
