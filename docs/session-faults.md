# 세션 fault

세션 fault는 하네스(에이전트 CLI)가 **작업이 아니라 계정·인증·서비스 상태 때문에**
더 진행할 수 없게 된 상태다. 에이전트가 다르게 일해서 풀 수 없고, 사람이 조치해야
한다. 일반 작업 실패(테스트 실패, 도구 오류)는 fault가 아니다.

예: 주간 사용 한도 소진, 로그인 거부, API 5xx/overloaded, DNS·연결 실패, 정책상
응답 거부.

## 왜 필요한가

세션이 멈춘 것을 사람이 `capture-pane`으로 눈으로 확인해야 했던 사고가 있다
(claunch-u8wjx.2, 2026-09-28: reviewer가 주간 한도에 걸린 채 리뷰가 멈췄다).
`claunch-pczoc`(2026-10-23)은 그 판별을 자동화한다.

## 어디서 읽는가

`daemon/session_fault.py`의 `Detector`가 세션 화면의 **아래쪽 16행**을 1초에 한
번 읽는다. 출력 스트림 전체가 아니라 화면 아래쪽을 읽는 이유는, fault 메시지가
세션이 멈출 때 마지막으로 그려지는 반면 대화 본문에 인용된 같은 문구는 위로
스크롤되어 사라지기 때문이다.

두 가지 행은 매칭 전에 뺀다.

- diff 행: `+ text`, `- text`, `  12 + text`. fault 문구를 인용한 문서를 쓰는
  세션이 자기 자신을 fault로 만들지 않게 한다.
- 하네스 도구 호출 헤더: `⏺ Bash(rg "You've hit...")` 같은 행. 명령줄에 문구가
  들어간 경우다.

이 판별은 휴리스틱이다. 거짓 양성이 생기면 그 kind의 window가 끝날 때 사라진다.

## 종류(kind)

`src/claude_launcher/session_faults.yaml`의 `kinds:`가 정의한다.

| kind | label | 기본 window | 사람이 할 일 |
|---|---|---|---|
| `usage_limit` | usage limit | 24시간 | 리셋을 기다리거나 플랜을 올린다 |
| `account_suspended` | account suspended | 24시간 | 프로바이더의 결제·청구 상태를 확인한다 |
| `rate_limit` | rate limit | 15분 | rate window가 지나기를 기다린다 |
| `overloaded` | provider overloaded | 5분 | 몇 분 기다린다. 계속되면 프로바이더가 열화된 상태다 |
| `auth` | login | 6시간 | 하네스 로그인을 다시 한다 |
| `api_status` | API error | 30분 | 프로바이더 상태를 확인하고 나중에 재시도한다 |
| `network` | network | 15분 | 연결과 DNS를 확인한다 |
| `policy_blocked` | policy block | 1시간 | 표현을 바꾸거나 모델을 바꾼다 |

메시지가 리셋 시각을 적으면(`resets Oct 26, 9am (Asia/Seoul)`) window 대신 그
시각이 만료 시각이 된다. `zoneinfo`로 시간대를 해석하고, 해석할 수 없으면
window를 쓴다. 상대 표기(`resets in 3h`)도 읽는다.

`API Error: <숫자>` 꼴은 코드 전용 패턴이 없어도 `api_status`로 잡고, 읽은
코드를 fault의 `code` 필드에 싣는다(`API Error: 503` → `code: 503`). 이 일반
폴백은 `common:`의 마지막 항목이라 하네스와 무관하게 적용된다(2026-10-24 이전에는
`claude` 목록에만 있어 다른 하네스는 상태 코드를 읽지 못했다). 특정 코드가
고유한 의미를 가지면 그 코드 전용 패턴을 폴백 앞에 두어 별도 kind로 읽는다
(401 → `auth`, 412 → `account_suspended`, 429 → `usage_limit`/`rate_limit`,
529 → `overloaded`). `common:`이 하네스 목록보다 먼저 평가되므로, 코드로 읽는
판정도 `common:`에 있어야 그 하네스가 다른 하네스에서도 선다 — 401과 429가
`claude` 목록에만 있던 동안 다른 하네스의 같은 문구는 fault로 서지 않았다.
429를 코드로 읽는 항목은 한도 문구(`Your quota will reset…`)보다 뒤에 둔다:
한도를 적은 429는 rate window가 아니라 소진된 플랜이다.

`529 Overloaded`는 프로바이더가 요청을 거절하는 동안 하네스가 스스로 재시도하는
상태다(`API Error: 529 Overloaded — retrying in 30s (attempt 3/10)`). 창을
`api_status`(30분)보다 짧은 5분으로 두는 이유는, 한 번의 재시도로 풀리는 경우가
많고 5분이 지나도 남아 있으면 그때는 프로바이더 열화로 읽어야 하기 때문이다.

`Error: Request timed out.`과 `Error: Retry failed after 3 attempts: Request
timed out.`은 `common:`에 있다 — 문구가 프로바이더 클라이언트의 것이지 한
하네스의 것이 아니다(`claude`도 찍고 pi도 찍는다, s1736 관측). 두 줄 모두
`network`로 읽는다. 나뉘는 것은 kind가 아니라 **기록되는 메시지**다: 소진
문구(`Retry failed after N attempts`)를 목록 앞에 두어, 그 줄로 끝나는 화면은
위의 시도 줄들 대신 소진 줄을 기록한다. 그 화면이 클라이언트가 멈춘 상태이고
사람이 툴팁에서 읽어야 하는 줄이기 때문이다. 재시도가 소진됐다는 사실에 별도
kind나 다른 창을 주지는 않았다 — 관측이 한 건이고, 창의 근거가 될 재시도
간격을 재지 못했다.

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

   - 목록 안의 순서가 우선순위다. 구체적인 문구를 일반 상태 코드 fallback보다
     앞에 둔다. `common:`이 하네스 목록보다 먼저 평가된다 — 프로바이더의 한도
     문구가 하네스의 일반 429 fallback을 이겨야 하기 때문이다.
   - `pattern`은 `re.IGNORECASE`로 컴파일된다. 줄바꿈은 스캔 창에서 공백으로
     합쳐지므로 감싸진 문장은 공백 기준으로 쓴다.
   - `sample`은 테스트가 실제 메시지와 패턴이 어긋나는 것을 잡는 근거다.
3. `uv run --no-sync pytest tests/test_session_fault.py -q`로 확인한다.

하네스 항목이 비어 있는 것(`codex`, `kimi`, `agent`, `devin`)은 아직 이 머신에서
그 하네스가 낸 fault 문구를 관측하지 못했다는 뜻이다. `common`만 적용된다.

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
