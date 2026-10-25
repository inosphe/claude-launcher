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

## 판정 규칙

한 스캔 창(아래 16행)에 fault 문구가 둘 이상 있으면 어느 것이 kind를 정하는지가
문제다. 규칙은 세 축이다.

- **폴백은 원인을 말한 문구에 진다.** 항목에 `fallback: true`가 붙으면 그 항목은
  원인을 말하지 않는 판독이다 — 맨 상태 코드(`API Error: 401`·`412`·`429`·`529`,
  `API Error: <숫자>`)와 클라이언트가 포기했다는 줄(`Error: Request timed out.`,
  `Error: Retry failed after N attempts`)이 그렇다. 창 안에 폴백이 아닌 매칭이
  하나라도 있으면 폴백은 후보에서 빠진다. 그래서 `You've hit your weekly limit`이
  `Request timed out`과 같은 화면에 있어도 플랜 소진으로 읽힌다 — 두 줄의 위아래
  순서와 무관하다(claunch-pczoc.2.1.1).
- **남은 후보 중에서는 가장 아래 행이 이긴다.** 화면은 위에서 아래로 그려지므로
  더 아래 행이 더 나중에 그려진 줄이고, 세션이 멈춘 줄이다. 한 패턴이 여러 번
  매칭되면 마지막 매칭을 쓴다.
- **같은 자리에서 시작하는 두 항목은 목록 순서가 가른다.** 위치로는 나뉘지 않는
  유일한 경우다. `common:`이 하네스 목록보다 먼저 오는 것도 이 동률 처리
  때문이다.

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
- `awaiting_input`의 기록은 화면에서 프롬프트가 사라진 뒤에도 window(15분)
  동안 남는다. 검출기는 화면만 읽으므로 "사람이 키를 눌렀다"를 별도로 알지
  못한다. 조기 해제를 넣지 않은 것은 의도다 — 근거가 되는 관측이 아직 없다.
