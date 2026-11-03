# PR 탭 (#/pr) — 기록된 PR 목록과 페이지 내 머지

대시보드의 PR 탭. 세션이 기록한 PR을 프로젝트별·날짜별로 보여 주고, 행을 누르면
그 PR의 **실시간 상태**와 **머지** 버튼이 열린다.

이슈: `claunch-ijkge`(탭·기록), `claunch-ijkge.3`(릴레이 접두사·페이지 내 머지).

## 1. 무엇을 읽는가

| 자리 | 읽는 것 | 네트워크 |
|---|---|---|
| 목록 | `GET /api/prs` — 세션이 기록한 값(PR 위저드의 `prs.json`, v2 런의 `prs` 컬렉션) | 없음 |
| 상세 | `GET /api/prs/<host>/<owner>/<name>/<number>` — 데몬이 `gh pr view`를 실행 | 데몬 → forge |
| 머지 | `POST …/merge` — 데몬이 `gh pr merge`를 실행 | 데몬 → forge |

목록은 기록값만 읽으므로 `gh`가 없거나 로그인되지 않아도 뜬다. 상태·제목은
**기록 시점 값**이고, 화면이 그 사실을 목록 옆에 적는다.

## 2. 브라우저는 forge에 닿지 않는다

PR 탭은 릴레이 터널(`https://relay.inosphe.work/t/<backend>/`) 아래에서도 열린다.
브라우저는 그 터널 밖으로 나갈 수 없고, 대개 `github.ncsoft.com`(내부망)에도 닿지
못한다. 그래서 forge 접근과 `gh` 실행은 **데몬**이 맡고, 브라우저는 같은 출처의
데몬 API만 호출한다.

**모든 요청은 `url()`(`static/app.js:31`)을 거친다.** 이 헬퍼가 현재 페이지의
기저 경로(`/` 또는 `/t/<name>/`)를 붙인다. 절대경로 `fetch("/api/prs")`는 터널
밖으로 나가 릴레이가 404를 돌려준다 — 이것이 `claunch-ijkge.3`이 고친 결함이다.
`tests/web/prpage_check.js`가 이 규칙을 소스 수준에서, 그리고 실제 요청 경로에서
두 번 단언한다.

## 3. 엔드포인트

`<host>/<owner>/<name>`은 기록의 `repo`(`github.ncsoft.com/RnIAgentDev/claunch`)를
**네 개의 경로 조각**으로 나눈 것이다. aiohttp는 디코드된 경로로 라우팅하므로,
`owner/name`을 `%2F`로 인코딩한 한 조각으로 보내면 매치 시점에는 구분자다.

### `GET /api/prs/<host>/<owner>/<name>/<number>`

```json
{"ok": true,
 "pr": {"number": 805, "state": "OPEN", "draft": false,
        "base": "master", "head": "s6357-x", "mergeable": "MERGEABLE",
        "merge_state": "CLEAN", "review": "",
        "checks": {"total": 3, "passing": 3, "failing": 0, "pending": 0, "state": "passing"}},
 "verdict": {"can_merge": true, "kind": "clean", "reason": "머지할 수 있습니다."},
 "conflicts": null,
 "recorded": {"session": "s6357", "url": "...", "issues": ["claunch-ijkge.3"]},
 "error": null}
```

`verdict.kind`가 머지 버튼을 그리는 유일한 입력이다.

| kind | 뜻 |
|---|---|
| `clean` | 머지할 수 있다 — 버튼이 열린다 |
| `conflict` | 기준선과 충돌한다 — 버튼을 닫고 충돌 파일을 보여 준다 |
| `behind` | 기준선이 앞선다 — rebase가 필요하다 |
| `checks-pending` / `checks-failing` | 체크가 도는 중 / 실패가 있다 |
| `review-required` / `changes-requested` / `blocked` | 보호 규칙·리뷰가 막는다 |
| `draft` / `merged` / `closed` | 이미 끝났거나 초안이다 |
| `unknown` | GitHub이 아직 계산하지 못했다 |

`BEHIND`는 머지하지 않는다. 기준선 최신화를 요구하는 저장소(이 플릿의 흔한 설정)는
그 상태에서 머지를 거절하므로, 가끔 되는 버튼보다 rebase를 권하는 문장이 낫다.

**충돌 파일**은 `verdict.kind == "conflict"`일 때만 계산한다. `git merge-tree
--write-tree --name-only`를 이 데몬이 가진 그 저장소의 로컬 체크아웃에서 돌린다.

```json
{"conflicts": {"computed": false, "files": [], "why": "이 데몬에 그 저장소의 로컬 체크아웃이 없어 …"}}
```

`computed: false`는 "지금 목록이 비어 있다"가 아니라 **"재지 못했다"**이다. 빈
배열만 보고 "충돌 없음"으로 읽지 않는다.

**읽기 실패는 예외가 아니라 답이다.** `ok: false` + `error.kind`(`no-gh`,
`network`, `not-found`, `bad-output`, `other`) + `error.raw`(gh의 원문). 목록의
기록값은 그대로 남는다.

### `POST /api/prs/<host>/<owner>/<name>/<number>/merge`

본문 `{"method": "auto"|"merge"|"squash"|"rebase", "confirm": true}`.

`auto`는 저장소가 허용하는 방식(`gh repo view --json mergeCommitAllowed,
squashMergeAllowed, rebaseMergeAllowed`)을 읽어 merge → squash → rebase 순으로
고른다. 읽지 못하면 **추측하지 않고 거절한다**(데몬에는 터미널이 없어 gh의 대화형
선택을 쓸 수 없다).

거절은 HTTP 409(저장소 상태가 막음) · 403(권한) · 404 · 502(네트워크·기타) ·
503(gh 없음)로 나가고, 본문에 `error.kind`·`error.message`·`error.raw`가 있다.
성공하면 `~/.claude-launcher/daemon/pr-merges.jsonl`에 한 줄이 남는다(누가·언제·
어느 PR을·어떤 방식으로).

### `POST …/rebase-request`

본문 `{"note": "<선택>", "session": "<선택>"}`. 기록된 세션에 메시로 요청을
보낸다(보낸이는 운영자, `external`). 그 세션이 이 데몬이 아는 어느 메시에도 속해
있지 않으면 `delivered: false`와 사유, 그리고 보낼 문구(`text`)를 돌려준다 —
전달되지 않은 요청을 전달됐다고 보고하지 않는다.

## 4. 안전 경계 — 머지는 사람만

- 머지 엔드포인트는 **대시보드 쿠키로 인증된 요청만** 받는다. bearer 토큰(셸·
  에이전트 세션·CLI)으로 온 요청은 gh를 실행하기 전에 403으로 거절한다
  (`daemon/api.py`, `h_pr_merge`). AGENTS.md의 「워커는 master를 머지하지
  않는다」가 이 경계다.
- **MCP 도구로 노출하지 않는다.** 어떤 에이전트 도구도 이 경로를 부르지 않는다.
- 화면은 **2단계 확인**이다 — 「머지…」를 누르면 방식 선택과 「정말 머지」가
  나타나고, 그때 `confirm: true`가 실려 나간다.
- 릴레이로 들어온 요청도 대시보드의 쿠키로 인증된다(`daemon/relay_uplink.py`:
  "the daemon's own Bearer/cookie auth still applies"). 터널이 인증 방식을
  바꾸지 않는다는 것이 이 경계의 전제이고, 착지 뒤 실환경에서 확인할 항목이다.

## 5. 테스트

| 파일 | 무엇을 잡는가 |
|---|---|
| `tests/web/prpage_check.js` | 터널 접두사(소스 + 실제 요청 경로), repo→경로 조각, 판정 입력, 라우팅 |
| `tests/test_prs_api.py` | 판정 행렬, 체크 집계, gh 실패 분류, 머지 방식 해석, 충돌 파일 계산, 라우트의 인증·확인·상태 코드, 감사 기록, rebase 요청 전달 |
