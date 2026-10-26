# Workspace 지식 기록 (Knowledge)

이슈와 세션 기록에는 작업의 이유가 간접적으로만 남는다. 세션이 「이 저장소가 왜 이렇게
하는가」를 알려면 코멘트를 거슬러 읽어야 했다. Knowledge는 그 이유를 명시적인 기록으로
남기고 검색하는 저장소다(claunch-269q1, 초안).

## 기록의 종류

| kind | 뜻 |
|---|---|
| `vision` | 기능·제품이 향하는 방향 |
| `need` | 사용자가 말한 요구와 그 배경 |
| `decision` | 정한 방향과 그 이유(버린 대안 포함) |
| `principle` | 앞으로 지킬 작업 규칙 |
| `lesson` | 시도했다가 틀린 것, 예상과 달랐던 측정 |

필드: `title`, `body`(markdown), `tags`, `refs`(이슈 id·세션·경로), `source`, `author`,
`status`(`active`/`superseded`), `supersedes`(대체한 기록 id).

## 저장 위치 두 곳, 색인 하나

- **파일 기록** — 저장소의 `.claunch/knowledge/*.md`. 파일 하나가 기록 하나이고 YAML
  front matter(`kind`, `title`, `tags`, `refs`, `date`, `author`, `status`)와 본문으로
  쓴다. git이 추적하므로 PR 리뷰를 거쳐 바뀐다. id는 `md:<파일 이름>`이고 웹·API에서는
  읽기만 한다. 이 저장소의 시드 기록(지금까지의 결정을 소급한 것)이 여기에 있다.
- **세션 기록** — 워크스페이스별 SQLite(`<런처 홈>/knowledge/<키>.sqlite3`, 용어집과 같은
  키). 웹 페이지와 MCP `knowledge_record`가 바로 쓴다. git 워크트리 밖이므로 회차가 트리를
  정리한 뒤에 써도 트리가 더러워지지 않는다.

검색은 두 출처를 합쳐 어휘 기반으로 한다: 질의를 공백으로 나눈 낱말마다 제목(3점)·태그와
refs(2점)·본문(1점)의 부분 문자열로 맞추고, 맞은 낱말 수, 점수, 최신 순으로 정렬한다.
점수는 확률이 아니다. 임베딩 검색(`rag` 서비스)과 Jev 연동은 후속이다.

## 화면·API·MCP

- 대시보드 **Knowledge** 탭(`#/knowledge`, Hive 옆): 검색, 종류·상태 필터, 세션 기록의
  추가·편집·삭제, Markdown 내보내기.
- `GET /api/knowledge?workspace=&q=&kind=&status=&limit=`, `POST /api/knowledge`,
  `PUT|DELETE /api/knowledge/<id>`(파일 기록은 409), `GET /api/knowledge/export`
  (`text/markdown`).
- MCP `knowledge_search {query, kind?, limit?, workspace?}`, `knowledge_record {kind, title,
  body, tags?, refs?, source?, supersedes?, workspace?}`. `workspace`를 생략하면 세션의
  등록 위치에서 찾는다. 기존 stdio MCP 프로세스와 데몬은 새 코드를 다시 읽어야 도구와
  API가 보인다.

## improv-worker-v2의 두 자리

| 노드 | 위치 | 시작 입력 | 기본값 |
|---|---|---|---|
| `knowledge-recall` | `category-tag` 뒤, 코딩 전 | `knowledge_recall` | `false` |
| `knowledge-record` | `category-refresh` 뒤, 회차 보고 전 | `knowledge_record` | `true` |

- 두 입력 모두 `set_by: [user]`라 런 페이지에서 런 도중에 바꿀 수 있고 다음 회차부터
  적용된다. 참조의 기본을 끈 것은 참조한 런과 참조하지 않은 런을 비교(A/B)하기 위해서다.
- `knowledge_record`는 `!= false`로 읽으므로 이 입력이 없던 런(null)도 기록한다.
- 두 노드 모두 회차를 막지 않는다. 도구를 쓸 수 없으면 사유를 보고하고 넘어간다.
- 구현: `src/claude_launcher/knowledge.py`, `daemon/knowledge_api.py`, `knowledge_mcp.py`,
  `web/static/knowledge-page.js`. 테스트: `tests/test_knowledge*.py`,
  `tests/test_cflow2_knowledge_nodes.py`, `tests/web/knowledgepage_check.js`.
