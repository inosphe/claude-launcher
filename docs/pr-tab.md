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

각 조각은 `[A-Za-z0-9._-]+`만 받는다. 그래서 PR URL의 호스트에 포트가 붙은
기록(`host:8443/owner/name`)은 이 경로로 표현되지 않고, 화면은 형식이 맞지
않는다고 말한다. 이 플릿의 호스트는 포트를 쓰지 않으므로 지금 문제가 되지
않는다.

### `GET /api/prs/<host>/<owner>/<name>/<number>`

```json
{"ok": true,
 "pr": {"number": 805, "state": "OPEN", "draft": false,
        "base": "master", "head": "s6357-x", "head_sha": "e9007201…",
        "mergeable": "MERGEABLE",
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

본문 `{"method": "auto"|"merge"|"squash"|"rebase", "confirm": true,
"match_head_commit": "<선택: 40자 SHA>"}`.

`auto`는 저장소가 허용하는 방식(`gh repo view --json mergeCommitAllowed,
squashMergeAllowed, rebaseMergeAllowed`)을 읽어 merge → squash → rebase 순으로
고른다. 읽지 못하면 **추측하지 않고 거절한다**(데몬에는 터미널이 없어 gh의 대화형
선택을 쓸 수 없다).

**머지 대상 고정(`match_head_commit`, claunch-ijkge.3.1).** 페이지는 상세에서 읽은
`pr.head_sha`를 그대로 되보낸다. 데몬은 gh를 부르기 전에 지금 head(`gh pr view
--json headRefOid`)와 비교하고, 다르면 `error.kind: "head-moved"`로 409를 내며
양쪽 SHA를 문장에 싣는다 — 판정을 받은 뒤 누가 커밋을 올렸다면 화면이 보여 준
커밋이 아니라 그때의 head가 머지되던 자리다. 지금 head를 읽지 못해도 고정하지
않고 머지하는 대신 거절한다. 통과하면 `gh pr merge … --match-head-commit <sha>`로
실행하므로 gh도 같은 커밋을 확인한다. 형식이 40자 SHA가 아니면 400이다. 값이
없으면(옛 페이지) 고정 없이 머지한다.

**정지 중 거절(`error.kind: "paused"`, claunch-0r2m2).** 지연 스윕 큐는 회귀
검증에 실패한 저장소를 정지시키고(`delayed_sweep.Queue.admit`), 그동안은 그
실패를 고치는 수리 이슈의 머지만 받는다. 데몬은 gh를 부르기 전에 같은 게이트에
물어보고(`pr_actions.admit_state`) 정지 중이면 409로 거절한다 — 응답의
`admit`에 큐의 사유(`issue`·`job`·`why`)가 들어 있다. 기록된 PR 행의 이슈
가운데 수리 이슈가 있으면 통과한다. 로컬 체크아웃이 없어 정지 상태를 읽지
못한 경우는 거절하지 않는다(`state: "unknown"`) — 확인되지 않은 것을 거절
사유로 쓰지 않는다.

거절은 HTTP 409(저장소 상태가 막음 · `head-moved` · `paused`) · 400(형식) ·
403(권한) · 404 · 502(네트워크·기타) · 503(gh 없음)로 나가고, 본문에
`error.kind`·`error.message`·`error.raw`가 있다.

**머지 뒤 스윕(`sweep`, claunch-ijkge.3.2).** 성공하면 데몬이 그 머지 커밋을
`delayed_sweep` 큐에 넣는다 — 리더 워크플로가 자기 머지마다 하는 것과 같은
잡이고, 같은 SHA·같은 이슈의 재-enqueue는 새 잡을 만들지 않는다(`Queue.enqueue`의
`UNIQUE(common, sha)`). 이슈 이름은 기록된 PR 행의 첫 이슈, 없으면
`pr-tab:<번호>`다. 넣지 못하면(로컬 체크아웃 없음 · 머지 커밋을 읽지 못함 ·
squash/rebase처럼 두 부모가 아닌 커밋 · 큐의 거절) **사유를 응답과 장부에 남긴다**.
응답은 `{"sweep": {"state": "queued"|"skipped", "job", "ref", "issue", "why"}}`다.

기준 브랜치가 저장소의 기본 브랜치가 아니면(릴리스·유지보수 브랜치로의 머지)
큐에 넣지 않고 그 사실을 사유로 남긴다(claunch-c9h9w) — 지연 스윕은 기본
브랜치의 트리를 검증하고, 다른 브랜치로의 머지는 다른 이력이다. 기준 브랜치나
기본 브랜치를 읽지 못한 경우도 같은 자리에 사유를 남긴다(스윕 대상인지 확인하지
못한 것을 "대상 아님"으로 읽지 않는다).

머지는 성공했거나 큐의 정지로 거절됐을 때 `~/.claude-launcher/daemon/pr-merges.jsonl`에
한 줄이 남는다(누가·언제·어느 PR을·어떤 방식으로). 성공한 줄에는 `sweep`이
따르고(큐에 넣었으면 잡 id·ref, 못 넣었으면 사유), 거절된 줄은 `merged: false`와
`refused`(`kind`·`issue`·`job`·`why`)를 싣는다 — "머지를 시도했고 아무것도
머지되지 않았다"를 나중에 요청을 다시 재생하지 않고 읽을 수 있다.

### `POST …/rebase-request`

본문 `{"note": "<선택>", "session": "<선택>"}`. 기록된 세션에 메시로 요청을
보낸다(보낸이는 운영자, `external`). 그 세션이 이 데몬이 아는 어느 메시에도 속해
있지 않으면 `delivered: false`와 사유, 그리고 보낼 문구(`text`)를 돌려준다 —
전달되지 않은 요청을 전달됐다고 보고하지 않는다.

**같은 요청을 보드에도 한 줄 남긴다.** 메시는 살아 있는 구성원에게 한 번만 닿고,
세션이 종료됐거나 다른 머신에 있으면 어디에도 남지 않는다. 그래서 기록 행이
이름으로 남긴 이슈(`issues`)마다 `REBASE REQUEST @ <host>/<owner>/<name>#<번호>:
<문구>`를 코멘트로 쓴다 — 그 이슈를 이어받는 다음 세션이 읽는 자리다. 줄 끝에
전달 상태를 적는다(`(메시로도 전달됨)` / `(메시 전달 실패 — <사유>)`). 응답의
`noted`는 실제로 쓴 이슈 id들이고, 못 쓴 이유는 `noted_why`에 있다(기록된 세션이
이 데몬에 없어 보드를 알 수 없음, 기록 행이 이슈를 남기지 않음, 일부 `br` 실패).

**인증**: 머지와 같은 쿠키 게이트다. `external`은 "운영자가 보냈다"는 뜻이고
토큰은 이 호스트의 세션이 읽을 수 있으므로, 토큰 요청이 통과하면 어떤 세션이든
운영자 이름으로 다른 세션 터미널에 문장을 넣을 수 있다.

## 4. 안전 경계 — 머지는 사람만

이 경계가 무엇을 막고 무엇을 막지 못하는지 그대로 적는다. AGENTS.md의 데몬
재기동 절과 같은 구조다: 「열려 있다는 사실이 권한이 있다는 뜻은 아니지만,
열려 있지 않다고 주장해서도 안 된다」.

**막는 것**

- **CLI·MCP 경로.** 머지 엔드포인트는 **대시보드 쿠키로 인증된 요청만** 받고,
  bearer 토큰으로 온 요청은 gh를 실행하기 전에 403으로 거절한다
  (`daemon/api.py`, `h_pr_merge`). 어떤 MCP 도구도 이 경로를 부르지 않는다.
  에이전트가 평소에 쓰는 길(CLI·MCP·`daemon_client`)은 전부 토큰 인증이다.
- **남의 이름으로 말하기.** rebase 요청도 같은 쿠키 게이트를 쓴다. 이 요청은
  `external=True`로 나가 "운영자가 보냈다"는 뜻이므로, 토큰만 가진 세션이 이
  경로를 쓰면 어떤 세션 터미널에도 운영자 발화를 넣을 수 있다(리뷰 s6357-rev2
  지적, `claunch-ijkge.3`).
- **한 번의 실수.** 화면은 2단계 확인이다 — 「머지…」를 누르면 방식 선택과
  「정말 머지」가 나타나고, 그때 `confirm: true`가 실려 나간다.

**막지 못하는 것 (실측으로 확인한 우회 경로)**

`POST /api/auth/session`은 본문의 토큰이 맞으면 쿠키 세션을 새로 발급한다
(`api.py:h_auth_session`). 이 라우트는 인증 미들웨어의 예외 목록에 있고
(`api.py:475`), 발급 과정에서 `_foreign_origin`도 보지 않는다. 그리고
`_foreign_origin`은 `Sec-Fetch-Site`도 `Origin`도 없는 요청을 통과시킨다.
토큰은 이 호스트의 셸과 에이전트 세션이 읽을 수 있는 값이다. 그러므로

```sh
curl -c jar -d '{"token":"<token>"}' http://127.0.0.1:<port>/api/auth/session
curl -b jar -X POST -d '{"confirm":true}' \
     http://127.0.0.1:<port>/api/prs/<host>/<owner>/<name>/<number>/merge
```

는 403에 걸리지 않는다. 같은 문이 rebase 요청에도 열려 있다(같은 쿠키로
`…/rebase-request`에 POST). 이는 쿠키를 쓰는 **모든 쓰기 라우트**가 공유하는
성질이지 어느 한 라우트의 구멍은 아니다.

그래서 에이전트에게는 **규칙으로** 금지한다: 워커는 master에 머지하지 않는다
(AGENTS.md). 규칙을 어길 마음이 있는 세션을 코드가 막지는 못한다.
헤더(`Sec-Fetch-Site`)를 요구하는 보강안이 검토됐고, 위협 모델을 정하는 일이라
사용자 결정으로 남겨 두었다.

- 릴레이로 들어온 요청도 대시보드의 쿠키로 인증된다(`daemon/relay_uplink.py`:
  "the daemon's own Bearer/cookie auth still applies"; `_foreign_origin`의
  docstring도 릴레이가 `Sec-Fetch-Site`를 바꾸지 않는다고 적는다). 착지 뒤
  실환경에서 확인할 항목이다.

## 5. 테스트

| 파일 | 무엇을 잡는가 |
|---|---|
| `tests/web/prpage_check.js` | 터널 접두사(소스 + 실제 요청 경로), repo→경로 조각, 판정 입력, 라우팅 |
| `tests/test_prs_api.py` | 판정 행렬, 체크 집계, gh 실패 분류, 머지 방식 해석, 충돌 파일 계산, 라우트의 인증·확인·상태 코드, 감사 기록, rebase 요청의 메시 전달과 보드 코멘트(전달 상태 문구·일부 실패 처리 포함), 두 라우트의 오프-루프 기록 읽기 |
