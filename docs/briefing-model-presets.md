# Briefing model 프리셋 — 여러 백엔드를 저장하고 골라 쓴다

이슈 `claunch-wug8b`. Settings ▸ **Briefing model** 카드(스크린샷의 그 카드)에
이름 붙은 프리셋을 여러 개 저장하고, 프리셋 목록에서 하나를 고르는 것만으로
세션 브리핑을 쓰는 백엔드와 모델을 바꾼다.

## 왜

`~/.claude-launcher/.claunch.yaml`의 `llm:` 블록은 설정 하나가 곧 백엔드 하나였다. 다른
백엔드로 옮기려면 프로필·모델·엔드포인트·키를 카드에 다시 입력하거나 파일을
손으로 고쳐야 했고, 되돌아오려면 같은 값을 다시 입력해야 했다. 프리셋은 그
네 값을 이름 하나 뒤에 저장한다.

## 저장 구조

`llm:` 블록의 최상위 두 키가 늘었다. 나머지 키(`max_tokens`, `params`)와
`profile`/`model`/`endpoint`/`api_key`는 그대로다.

```yaml
llm:
  preset: cheap               # 지금 쓰는 프리셋 id. 비어 있으면 아래 필드가 정본
  presets:                    # 저장된 백엔드 목록 (최대 20개)
    - id: cheap               # 소문자·숫자·하이픈, 32자 이하
      endpoint: https://cheap.example/v1/chat/completions
      model: glm-small
      api_key: <key>
    - id: big
      profile: ds4            # 프로필을 적으면 그 프로필의 endpoint/key를 쓴다
      model: large
  max_tokens: 10000           # 블록의 값. 프리셋은 이 값을 갖지 않는다
```

해석 규칙은 `daemon/briefing.py:llm_config()` 하나에 있다.

| 상황 | 브리핑 호출이 쓰는 값 |
| --- | --- |
| `preset:`이 저장된 프리셋을 가리킨다 | 그 프리셋의 네 필드. 블록 자체의 `profile`/`model`/`endpoint`/`api_key`는 읽지 않는다 |
| `preset:`이 비어 있다 | 블록 자체의 네 필드 (예전과 같은 동작) |
| `preset:`이 없는 이름을 가리킨다 | 블록 자체의 필드로 되돌아간다. 기능이 꺼지지 않는다. 카드가 그 이름과 사유를 `error` 줄로 알린다 |
| 프리셋이 `profile`을 적었다 | 그 프로필의 `endpoints.openai`와 key. 프리셋의 `endpoint`/`api_key`는 쓰지 않는다 |
| 프리셋에 `model`이 없다 | 프로필의 `models.default` |

`max_tokens`와 `params`는 블록의 것이다. 프리셋을 바꿔도 호출이 쓸 수 있는
토큰 예산은 변하지 않는다 — 백엔드를 바꾸는 일과 지출 한도를 바꾸는 일은
다른 일이다.

## provider의 요청 헤더 규칙 (OpenCode Go)

프로필을 적은 프리셋은 그 프로필의 provider를 통해 나간다. provider가
`session_header`나 `anthropic_auth_header`를 선언했으면 그 규칙이 **이 호출에도**
적용된다. 실제로는 harness 트래픽만 shim을 거치므로 규칙이 shim 안에만 있으면
데몬이 직접 부르는 브리핑 호출은 거절된다 — OpenCode Go에서
`400 MissingSessionID`로 재현됐다(2026-10-25, `claunch-acy2f`).

- 규칙의 정본은 `src/claude_launcher/routing.py`의 `apply_session_header`와
  `apply_anthropic_auth`이고, shim과 `daemon/briefing.py`가 같은 함수를 부른다.
- `daemon/briefing.py:briefing_request_headers()`가 호출마다 헤더를 만든다.
  설정을 매번 다시 읽으므로 `~/.claude-launcher/.claunch.yaml`(또는 Settings 카드)의 편집이
  다음 브리핑에 바로 반영된다.
- `session_header`에 보내는 값은 **`claunch-briefing`** 이다
  (`briefing.BRIEFING_SESSION_ID`). 세션 이름을 쓰지 않는다: 브리핑은 세션의
  한 턴이 아니라 데몬의 요약 호출이고, 사용자가 상류에서 그것을 구분해
  보이게 해 달라고 요청했다(2026-10-25). 데몬의 다른 두 호출자도 같은 방식으로
  자기 표시를 쓴다 — 관찰은 `claunch-observer`(`observer.OBSERVER_SESSION_ID`),
  의미 검색은 `claunch-rag`(`rag.RAG_SESSION_ID`, `docs/rag-search.md`).
- `endpoint`/`api_key`를 직접 적은 프리셋은 어느 provider 항목에도 속하지
  않으므로 규칙이 적용되지 않는다.
- 값이 잘못된 경우(헤더 이름 규칙 위반) 브리핑 호출은
  `BriefingError`로 실패하며 메시지에 설정 키가 들어간다. shim처럼 헤더를
  건너뛰고 트래픽을 계속 보내는 선택지는 여기 없다 — 상류가 어차피 거절한다.
- `GET /api/briefing/llm`의 `resolved.provider`와 프리셋 행의
  `resolved.provider`가 어느 provider가 규칙을 정하는지 알려 준다.

## API

`GET /api/briefing/llm`의 응답에 세 키가 늘었다.

- `preset` — 지금 켜진 프리셋 id (없으면 `""`)
- `presets` — 저장된 프리셋 목록. 각 행은 `id`, `profile`, `model`, `endpoint`,
  `api_key_set`, `resolved`(호출이 실제로 쓸 endpoint·model·키 유무), `error`
  (프로필이 해석되지 않을 때의 사유). **키 값 자체는 응답에 실리지 않는다.**
- `max_presets`, `preset_name_re` — 편집기가 이름을 검사할 때 쓰는 상한과 정규식

`PUT /api/briefing/llm`은 `presets`(목록 전체 교체)와 `preset`(포인터)를 받는다.

- `preset`은 **같은 요청이 저장하는 목록**에 대해 검사한다. 카드가 "프리셋을
  추가하고 고른다"를 한 번의 저장으로 보낼 수 있게 하기 위해서다.
- 프리셋 행의 `api_key`는 블록의 키와 같은 규칙을 따른다: 값이 있으면 저장,
  `null`이면 제거, **빈 문자열이거나 필드 자체가 없으면 저장된 키를 유지**한다.
  카드의 비밀번호 상자는 매 렌더마다 비어 오므로, 빈 값을 제거로 읽으면 다른
  필드를 저장할 때마다 모든 프리셋의 키가 지워진다.
- 이름을 바꾸는 행은 자기 `previous_id`(저장돼 있던 이름)를 함께 보낸다. 키는
  저장 당시의 이름 아래에 있으므로, 새 이름만 보면 승계할 것을 찾지 못해
  자격증명이 사라진다. 유지 규칙은 `previous_id` → 새 `id` 순서로 찾는다
  (`api.py:_llm_block_update`).
- 저장할 수 없는 값은 400으로 거절한다: 이름 규칙 위반, 중복 id, `profile`도
  `endpoint`도 없는 행, 상한 초과, 저장된 목록에 없는 `preset`.

`POST /api/briefing/llm/test`는 카드의 **Test connection**이 부르는 경로다.
저장하지 않은 편집값을 그대로 받아 최소 호출을 한 번 보내고 결과를 돌려준다.

- 본문은 `profile`, `model`, `endpoint`, `api_key`, `preset` 다섯 필드만 받는다.
  `presets`·`max_tokens`는 400으로 거절한다 — 시험 대상은 백엔드 하나이고,
  조용히 버린 필드가 있으면 요청한 것과 다른 것을 보고하게 된다.
- `preset`은 저장된 행을 가리키며, **보내지 않은 필드의 출처**다. 저장된 목록에
  없는 이름은 블록으로 되돌아간다(`llm_config()`와 같은 규칙).
- 빈 값의 뜻은 필드마다 다르다. `api_key`가 빈 문자열이면 그 행에 저장된 키를
  쓰고(`null`은 제거), `profile`·`model`·`endpoint`가 비면 빈 값 그대로다 —
  저장 경로에서도 빈 상자는 그 필드를 지우므로 두 경로가 같은 것을 뜻한다.
  카드의 비밀번호 상자는 비밀을 되받지 못해 늘 비어 오므로 이 구분이 필요하다:
  없으면 저장된 활성 프리셋의 키가 다른 프리셋의 endpoint로 나간다.
- 해석 규칙은 `llm_config()`와 같다: `profile`이 있으면 그 프로필의 endpoint와
  키를 쓰고 보낸 endpoint·키는 쓰지 않는다.
- 요청은 브리핑 호출과 같은 경로를 탄다: 같은 `briefing_request_headers()`,
  같은 본문 형태. 그래서 `session_header`를 요구하는 provider는 시험에서도
  같은 헤더를 받는다. 예산은 `params`를 머지한 **뒤에**
  `CONNECTION_MAX_TOKENS`(16)로 묶는다 — 블록의 `params.max_tokens`가 상한을
  넘어서지 못한다. 프롬프트는
  `CONNECTION_PROMPT`("ping")이다.
- 실패는 예외가 아니라 결과다. 연결 실패, 상류의 4xx/5xx, `choices[0]`가 없는
  본문, 설정 누락이 모두 `ok: false`와 `detail` 문장으로 온다. `detail`은
  응답 본문 일부(300자)만 담고 요청은 담지 않는다 — 키가 새지 않는다.
- 아무것도 저장하지 않는다. 파일은 시험 전후로 같다.

## 카드

`src/claude_launcher/web/static/app.js`의 `llmSettingsCard()`.

- 맨 위 **Preset** 선택 상자(`#llm-preset`)가 스위치다. 고르면 아래 네 필드가
  그 프리셋의 값으로 바뀌고, 그 상태에서 저장하면 그 프리셋이 저장된다.
- **Name**(`#llm-preset-name`)은 고른 프리셋의 이름을 바꾼다.
- **Add preset**은 지금 필드에 보이는 값으로 새 행을 만들고 즉시 저장한다.
  새 행을 고른 상태로 저장해야 그 이름이 목록에 서고, 포인터와 목록이 한
  파일에서 서로 맞는다.
- **Delete preset**은 고른 행을 목록에서 빼고 포인터를 비운다.
- **Remove stored key**는 지금 편집 중인 것(블록 또는 고른 프리셋)의 키만
  지운다. 다른 프리셋의 키는 그대로다.
- **Test connection**은 지금 필드에 보이는 값으로 최소 호출을 한 번 보낸다.
  저장하지 않은 편집 내용으로도 시험할 수 있고, 아무것도 저장하지 않는다.
  결과는 카드 아래 한 줄에 성공·실패와 `detail`로 남고, 시험한 endpoint와
  model을 함께 적어 그 뒤에 필드를 고쳤을 때 결과가 낡았다는 것을 알 수 있게
  한다. 결과는 `llmTestResult`에 있으므로 다음 폴링의 재렌더에서도 남는다.

## 시험

- `tests/test_briefing.py` — `llm_config` 해석(활성 프리셋 우선, 포인터 없음,
  없는 이름, `max_tokens` 미상속), 목록 정제와 중복 제거, 표시용 목록에 키가
  없는지, 제출 목록 검증, 그리고 `GET`/`PUT /api/briefing/llm`의 저장·전환·
  키 보존·거절·이름 변경 시 키 승계, 없는 이름을 가리키는 포인터의 `error` 보고.
- `tests/web/briefmodel_check.js` — 카드가 프리셋 목록을 그리고, 고른 프리셋의
  endpoint·model을 필드에 옮기고, 저장 본문에 목록과 포인터를 함께 싣고,
  삭제가 포인터를 비우고, 추가가 새 행을 저장하는지, **Test connection**이
  저장하지 않은 필드로 `/api/briefing/llm/test`를 부르고 결과를 카드에
  남기는지.
