# shim 모델 해석, provider 중간 교체, tiered model preset 설계

상태: 설계 (구현 미착수). 이슈 `claunch-cvife`.

## 1. 배경

세 가지 요청을 다룬다.

1. profile의 provider를 세션 실행 중에 교체 (예: fireworks ds4 → deepseek official).
2. harness는 `small`/`default`/`large` 같은 역할 이름(과 claude 별칭 `haiku`/`opus`/`sonnet`/`fable`)을 그대로 쓰고, shim proxy가 요청 시점에 실제 모델로 변환.
3. improv-worker-v2의 모델 라우팅에 쓸 tiered preset 개념. 설정 페이지에서 관리. 사용처 연결은 후속 작업으로 다루고, 이 문서는 개념과 기반만 정의한다.

현재 구조에서 이미 확인된 사실:

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| provider·profile·모델 정의 위치 | `~/.claunch.yaml`의 `providers:`/`profiles:` 섹션 | `store.py` 스키마, `providers.py:66-74` |
| 모델 역할 어휘 | `MODEL_ROLES = (default, small, large, xlarge, subagent)`, 폴백 규칙 `ROLE_FALLBACKS = {xlarge: large, subagent: small}` | `provider_spec.py:118-120` |
| role → 백엔드 모델 표 | `providers.<name>.models`와 프로파일 오버레이 `profiles.<name>.models` | `provider_spec.py:152-158`, `254-260` |
| claude 하네스의 모델 결정 시점 | 런칭 시점에 `ANTHROPIC_*_MODEL` env로 구워짐. 이후 변경 경로 없음 | `translators.py:111-167`, `runner.py:189-304` |
| claude 별칭 → env 대응 | `CLAUDE_ALIAS_VARS`(haiku/sonnet/opus/fable → `ANTHROPIC_DEFAULT_*_MODEL`) | `provider_spec.py:178-215` |
| shim 구조 | (upstream, spec)지문당 하나의 루프백 프록시. upstream은 스폰 시 고정. body 수정은 `provider:` 핀 병합만 | `routing.py:207-238`, `routing_shim.py:143-178` |
| shim 핫 리로드 선례 | `session_affinity`를 shim이 요청마다 `store.load`(100ms TTL)로 읽음 | `routing_shim.py:105-129`, `store.py:103` |
| 세션 식별 헤더 | `X-Claunch-Session` 헤더가 metering 귀속용으로 이미 실림 | `metering.py:59`, `121-134` |
| improv-worker-v2 라우팅 변수 | `daemon.delegate_profile` + `daemon.delegate_model`(MODEL_ROLES 단어 허용). 위임 시점에 라이브 읽기 | `store.py:583-587`, `api.py:4234-4296` |
| provider 핀 기록 명령 | `claunch set-provider PROFILE NAME`이 `profiles.<name>.provider`를 기록 | `cli.py:739-776`, `providers.py:391-403` |

## 2. 공통 설계: shim을 해석 지점으로

세 요청의 공통 분모는 하나다. 지금은 런칭 시점에 env와 shim upstream으로 구워지는 값들(provider, 모델)을, 요청 시점에 live config로 읽는 값으로 옮기는 것. `session_affinity`가 이미 이 방식으로 동작하므로 선례는 저장소 안에 있다.

### 2.1 route 키와 지문 기준의 변경

- profile이 고정 upstream을 갖는 대신, **route 키**를 갖는다. 최소 변경으로 route 키는 profile의 provider 이름 그대로 쓴다.
- shim은 스폰 시 해석된 upstream 대신 route 키를 입력받고, 요청 시점에 `store.load`로 `providers.<route>`를 읽어 upstream endpoint와 api key를 결정한다.
- 지문(fingerprint) 입력이 (해석된 upstream, spec)에서 (route 키, spec)로 바뀐다(`routing.py:207-218`). provider 교체는 config 파일 쓰기 하나이며, 살아있는 세션에 TTL(100ms) 안에 적용된다.
- metering 기록 파일도 지문 단위로 묶이므로 route 단위로 묶인다. 기록의 `model` 필드는 upstream 응답의 model 값이므로(`metering.py:351-354`) 해석 후 id가 남는다.

### 2.2 자격증명 주입

- 현재 shim은 인증 헤더를 그대로 통과시킨다(`routing_shim.py`의 `_forward`). provider 교체가 생기면 대상 provider의 현재 api key를 shim이 주입해야 한다. 루프백 전용 리스너(`routing.py:324-348`)이므로 도달 가능한 프로세스는 이 머신 안의 세션 뿐이다.
- incoming `Authorization`/`x-api-key`는 폐기하고, live config에서 읽은 route의 키로 교체한다. 헤더 형식 차이(Authorization 대 x-api-key)는 provider 설정에서 결정한다.

### 2.3 모델 alias 해석

- shim이 body의 `model` 필드를 검사해 alias 어휘에 속하면 `providers.<route>.models` 표로 해석한다. 해석 단계는 `merge_body`(`routing.py:221-238`) 옆에 둔다.
- alias 어휘는 둘로 정한다.
  - 역할 이름: `MODEL_ROLES` 5단어.
  - claude 별칭: `CLAUDE_ALIAS_VARS`에서 파생되는 `haiku`/`sonnet`/`opus`/`fable`.
- 표 키로 역할 이름만 허용하던 `models:`에 claude 별칭도 키로 허용한다. 예:

  ```yaml
  providers:
    ds4:
      models: {default: ds4, small: glm-flash, large: ds4, opus: ds4, haiku: glm-flash}
  ```

- 해석 규칙: 표에 있는 alias면 표 값으로 교체, 없는 값은 그대로 통과. 이 규칙은 `merge_body`의 "body가 이미 값을 가지면 건드리지 않음" 원칙과 같은 계열이다. 통과 기준을 택하면 기존 동작과의 호환성이 유지된다. 표에도 없는 alias 단어가 왔을 때 거절(400)할지 통과할지는 미결정이며, 권고는 통과와 로그 한 줄이다(7절).

### 2.4 하네스 쪽 변경 (dynamic 모드)

- claude 하네스의 `ANTHROPIC_*_MODEL` env 값이 구체 id 대신 alias 단어를 담는 모드를 둔다. route 설정의 `dynamic: true` 같은 플래그로 선택한다.
- 이 모드는 shim 경유 route에만 적용한다. shim 경유가 아닌 경로(OAuth 경로, metering 꺼진 provider)는 기존처럼 런칭 시점에 구체 id를 구운다.
- 이 동작이 성립하려면 Claude Code가 `ANTHROPIC_DEFAULT_OPUS_MODEL=opus`처럼 env 값을 요청의 model 필드에 그대로 실어 본다는 전제가 필요하다. 확인되지 않았으므로 7절의 미검증 항목에 두고, 통제 시험으로 확인한다. 성립하지 않으면 shim 표의 키에 claude 기본 id(`claude-opus-5` 등)도 포함하는 대체안을 쓴다.
- 프로파일 오버레이 `profiles.<name>.models`의 라이브 해석: shim이 이미 받는 `X-Claunch-Session` 헤더(`metering.py:59`)로 세션 → 프로파일을 찾아 오버레이를 적용할 수 있다. 다단계 해석(route 표 → 프로파일 오버레이)의 규칙은 구현 단계에서 확정한다.

### 2.5 지원 범위

- 이 설계가 다루는 것은 커스텀 provider(fireworks, deepseek official 등) 경유 세션이다. OAuth 경로(`default`/`claude` provider)는 shim을 거치지 않으므로 대상에서 제외된다. 제약으로 유지한다.

## 3. 기능 1 상세: provider 중간 교체

- 사용자 조작은 기존 명령을 그대로 쓴다: `claunch set-provider <profile> <provider>`(`cli.py:739-776`). 지금은 이후 실행에만 적용되고, 이 설계가 적용되면 dynamic route를 쓰는 살아있는 세션에도 TTL 안에 적용된다.
- `profiles.<name>.models` 핀이 다른 백엔드로 옮겨갈 때의 문제("fireworks에 deepseek 모델 id를 묻는 형태")는 기존 `overlay_models=False` 기계(`providers.py:164-183`)가 런칭 시점에 막는다. shim 해석에서는 이 문제가 자연 해소된다. role과 claude 별칭은 route에 묶이고, 매 요청 그 시점의 route 표로 풀리기 때문이다.
- 적용 여부 표시: 세션이 dynamic route를 쓰는지는 세션 정의의 env(`ANTHROPIC_BASE_URL`이 shim인지)로 판별 가능하다. UI 표시 여부는 구현 단계 결정.

## 4. 기능 2 상세: alias 변환

- 변환 표는 새 파일을 만들지 않고 기존 `providers.<name>.models`를 확장한다(2.3절). 사용자 예시(`small→glm-flash`, `large→ds4`, `haiku→glm-flash`, `opus→ds4`)는 이 표로 표현된다.
- 기본값 규칙: 표에 없는 역할은 `ROLE_FALLBACKS`를 따라 `default`로 풀린다. claude 별칭이 표에 없으면 claude 하네스의 기본 id(`claude-haiku-4-5-20251001` 등)로 푼다. 이 경우 사실상 변환은 일어나지 않는다.
- harness 쪽 어휘는 그대로 유지된다. `harnesses.yaml`의 claude 모델 목록(`haiku, sonnet, opus, fable`)과 `--model=<alias>` 경로(`daemon/harness.py:1164-1167`)에 손을 대지 않고, 변환 지점은 shim 하나로 모인다.
- spawn 프리셋(`spawn.py:_resolve_model_preset`)과의 관계: spawn 경로는 런칭 시점에 alias 단어를 `--model`에 붙이는 데서 끝난다. 이후 결정이 shim으로 옮겨오므로, 프리셋 해석의 후반부(프로파일 표 조회)는 dynamic 모드에서 생략되거나 shim과 동일한 표를 읽게 된다. 어느 쪽이든 사용자에게 보이는 어휘는 같다.

## 5. 기능 3 상세: tiered model preset (개념 정의)

### 5.1 스키마

`~/.claunch.yaml` 최상위 키로 둔다. `daemon:` 블록에 넣지 않는 이유: 이 값은 세션 라우팅 정의로 데몬 설정보다 상위에 있고, shim과 워크플로 양쪽이 라이브로 읽어야 하기 때문이다. `relays:` 블록(`store.py:775-817`)과 같은 위상이다.

```yaml
model_tiers:
  version: 1
  tiers:
    - id: economy            # 티어 id. 사용자 지정, 고유. 소문자 영숫자와 하이픈
      entries:               # (profile, model, harness) 튜플 목록. 순서가 우선순위
        - profile: glm-flash # 필수. 존재하는 프로파일
          model: small       # 선택. MODEL_ROLES 단어 또는 모델명
          harness: claude    # 선택. 생략 시 프로파일 기본 하네스
    - id: standard
      entries:
        - profile: ds4
          model: large
    - id: heavy
      entries:
        - profile: ds4-1m
  active:                    # 티어별 활성 entry. 사용처 연결은 후속 작업
    standard: 0              # 티어 id -> entries 인덱스
```

- 티어 수는 사용자가 정한다(요청 기준 3개 정도). 편집기 복잡도를 이유로 상한 5 정도를 권고하며, 확정은 구현 단계.
- entry 수 제한은 두지 않는다. n개를 관리한다는 요청이 그대로이고, 조합의 유효성은 검증 규칙으로 검사한다.

### 5.2 validation (가능한 경우의 수)

| 필드 | 규칙 | 재사용 대상 |
| --- | --- | --- |
| `profile` | `profile_mod.list_all()`에 존재해야 함 | delegate-setting 핸들러의 프로파일 검증(`api.py:4255-4296`) |
| `harness` | 해당 프로파일이 허용하는 하네스(`harness_policy`)여야 함 | `input_options.Catalog`(`input_options.py:93-153`) |
| `model` | `Catalog.models(profile, harness)` — MODEL_ROLES 단어 + 프로바이더 선언 모델 + 하네스 선택지 | claunch-iq1xc의 거절 규칙 |
| entry 전체 | 동일 티어 안 (profile, harness, model) 조합의 중복 금지 | 새 규칙 |
| `id` | 소문자 영숫자와 하이픈, 보드 전체에서 고유 | relay id 정규식(`daemon/relay_settings.py`)과 같은 형태 |

검증을 통과하지 못하는 조합은 400으로 거절하고, 어느 필드가 무엇 때문에 거절되는지 메시지에 적는다. 조용히 버리는 방식은 쓰지 않는다.

### 5.3 기본값 규칙

- `model` 생략 → `default` 역할 → 그 프로파일의 default 모델.
- `harness` 생략 → 프로파일의 기본 하네스(`profiles.<name>.harness`, 없으면 하네스 정의 기본).

### 5.4 저장과 편집

- 저장은 `~/.claunch.yaml`의 최상위 `model_tiers:` 블록. `store.update(mutator)`로 원자적 갱신.
- 정제(sanitize)는 `daemon/status_checks.py`의 `_clean_presets` 패턴을 따른다: 읽기와 쓰기 양쪽에서 같은 정제 함수를 지나게 하고, uuid id를 부여하고, 파일 쓰기는 `atomic.replace`로 한다.
- API: `GET /api/model-tiers`(정제 후 상태), `PUT /api/model-tiers`(전체 교체, 본문 검증 후 저장, 저장된 상태 반환). 행 단위 엔드포인트는 두지 않는다. 목록 전체가 작고 편집 빈도가 낮기 때문이다.
- 설정 페이지: General 탭에 카드 하나 추가(`app.js:31003-31030`의 카드 목록). UI 패턴은 delegate 카드(`app.js:32795-32863`, select + 즉시 저장)와 status-checks 카드(`app.js:31549-31611`, 행 추가·편집·삭제)를 조합한다.

### 5.5 해석 함수 (개념만 정의)

```python
def resolve_tier_entry(tier_id: str) -> Optional[TierEntry]:
    """티어 id -> 활성 entry -> 기본값 규칙 적용 -> (profile, harness, model)."""
```

- 입력은 `store.load()`의 결과만 쓴다. 호출자가 부를 때마다 최신값이므로, 3.2의 "같은 티어 안 실시간 변경" 기반은 이 함수를 shim의 요청 처리와 워크플로의 위임 단계에서 부르는 데서 나온다.
- 사용처 연결(improv-worker-v2의 `route-delegate` 단계, spawn 인자, 활성 entry 전환 UI)은 후속 작업. 이 문서의 범위 밖.

### 5.6 MODEL_ROLES와의 관계와 명칭

- `MODEL_ROLES`는 단일 프로파일 안의 역할 어휘다. `model_tiers`는 여러 프로파일을 묶는 상위 개념이고, tier entry의 `model` 필드는 `MODEL_ROLES` 단어를 값으로 쓸 수 있다.
- `spawn.py`에서 "model preset"은 이미 `MODEL_ROLES`를 가리킨다(`spawn.py:576-629`). 혼선을 막기 위해 새 개념의 명칭은 "model tier"로 하고 설정 키는 `model_tiers`로 한다.

## 6. 변경 지점 (구현 시)

| 파일 | 변경 내용 |
| --- | --- |
| `src/claude_launcher/routing.py` | 지문 입력을 route 키 기준으로. shim 스폰 인자에 upstream 대신 route 키 전달 |
| `src/claude_launcher/routing_shim.py` | 요청 시점 upstream·api key 해석, model alias 해석 단계, 자격 주입 |
| `src/claude_launcher/provider_spec.py` | `models:` 키에 claude 별칭 허용(검증 규칙 확장) |
| `src/claude_launcher/translators.py` | dynamic 모드: `ANTHROPIC_*_MODEL`에 alias 단어 배치 |
| `src/claude_launcher/runner.py` | dynamic 모드 선택과 env 조립 순서 조정 |
| `src/claude_launcher/daemon/model_tiers.py`(신규) | `model_tiers:` 정제·검증·해석 함수 |
| `src/claude_launcher/store.py` | 최상위 `model_tiers:` 블록 읽기/쓰기 헬퍼 |
| `src/claude_launcher/daemon/api.py` | `GET/PUT /api/model-tiers`, route 적용 상태 조회 |
| `src/claude_launcher/web/static/app.js` | 설정 페이지 tier 편집 카드 |
| `src/claude_launcher/workflows/v2/improv-worker-v2.yaml` | tier 사용처 연결(후속 작업) |

## 7. 미검증 항목

- Claude Code가 `ANTHROPIC_DEFAULT_OPUS_MODEL` 같은 env 값을 요청의 model 필드에 그대로 실어 본다는 전제(2.4). 통제 시험 필요. 성립하지 않으면 shim 표 키에 claude 기본 id를 포함하는 대체안.
- shim 자격 주입 시 provider별 인증 헤더 형식 차이(Authorization 대 x-api-key)의 요청마다 전환. OpenAI 어댑터 경유(pi 하네스)와의 조합.
- dynamic 모드와 routing 핀(`providers.<name>.routing`, OpenRouter 병합)의 조합 규칙. 동적 upstream 아래서도 spec 병합을 유지할지.
- 프로파일 오버레이 `profiles.<name>.models`의 라이브 해석에서 세션 헤더 → 프로파일 대응이 실패하는 경우의 동작(세션 삭제 후의 요청 등).
- 티어 수 상한(5 권고)과 행 단위 API 필요 여부. 구현 단계 확정.
- alias 어휘가 표에도 없을 때 거절 대 통과의 최종 선택(2.3).

## 8. 검증 방법 (구현 시)

단위 시험:

1. alias 해석: 표에 있는 역할 이름·claude 별칭이 표 값으로 교체되고, 표에 없는 값은 통과한다.
2. route 해석: config의 provider 교체가 store TTL 안에 shim의 upstream 결정에 반영된다(시간 의존 시험은 mock clock으로).
3. 지문: 동일 route 키에 대해 provider 교체 전후 shim 프로세스가 하나로 유지된다.
4. `resolve_tier_entry`: 활성 포인터·기본값 규칙·폴백 순서를 각각 확인한다.
5. tier 저장: 정제가 잘못된 조합을 거절하고, 중복 entry와 존재하지 않는 프로파일을 거절한다.
6. API: PUT 본문의 무효한 조합이 400으로 돌아오고 메시지에 필드와 사유가 있다.

수동 확인(사용자 재기동 자리 포함):

7. dynamic 모드 프로파일로 세션을 띄우고, `claunch set-provider`로 교체한 뒤 같은 세션의 다음 요청이 새 provider에서 답한다.
8. `models:` 표의 `small` 값을 바꾼 뒤 같은 세션의 다음 요청이 새 모델로 기록된다(metering 기록의 model 확인).

## 9. 작업 분할

| 단계 | 내용 | 비고 |
| --- | --- | --- |
| 1 | shim 동적 라우트 + alias 해석 (기능 1·2 코어) | 이 문서 2~4절 |
| 2 | `model_tiers` 저장·검증·API·설정 카드 (기능 3 기반) | 이 문서 5절, 사용처 연결 없음 |
| 3 | improv-worker-v2 연결과 티어 내 실시간 전환 | 후속 논의 대상(3.2) |
