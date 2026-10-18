---
title: 테스트 격리 — 실제 홈과 다른 인스턴스에 쓰지 않는다
description: 이 저장소의 모든 세션이 받는 지침. 테스트와 재현이 실제 런처 홈, 설정 파일, 다른 데몬 인스턴스에 프로필·데몬 상태를 만들지 않게 한다.
delivery: always
---
## 테스트 격리: 실제 홈과 다른 인스턴스에 쓰지 않는다

이 저장소의 테스트가 실제 런처 홈(`~/.claude-launcher`)과 설정 파일
(`~/.claunch.yaml`)에 프로필·데몬 상태를 만든 사고가 반복됐다
(claunch-49bod, claunch-edy0w, claunch-gziru). 남은 산출물은
`profiles/play`, `profiles/work`, `profiles/codex2`, `daemons/{a,alpha,b,inst,other,x}`
같은 디렉터리였고, 테스트는 그 상태에서도 통과했다. 아래 규칙을 테스트를 쓰고
돌리고 재현하는 모든 작업에 적용한다.

### 격리의 정본: `tests/conftest.py`
- autouse fixture `home`이 테스트마다 `CLAUDE_LAUNCHER_HOME`,
  `CLAUDE_LAUNCHER_SYNC_FILE`, `CLAUDE_LAUNCHER_SEED`, `CLAUDE_CONFIG_DIR` 등을
  `tmp_path` 아래로 돌리고, 데몬 인스턴스 변수(`CLAUNCH_INSTANCE_BASE`,
  `CLAUNCH_INSTANCE`, `CLAUNCH_DAEMON`, `CLAUNCH_INSTANCE_APPLIED`,
  `CLAUNCH_INSTANCE_PREV`, `CLAUNCH_DAEMON_PORT`)를 지운다.
- `CLAUNCH_INSTANCE_BASE`가 남아 있으면 `paths.base_home()`이 런처 홈보다 그
  값을 먼저 쓰므로, 홈을 격리해도 `daemons/<name>`이 실제 홈에 생긴다
  (claunch-llmi1).
- 같은 fixture가 실제 홈 목록(기본 런처 홈, 사용자 홈, pytest 시작 시점의
  `CLAUDE_LAUNCHER_HOME`·`CLAUNCH_INSTANCE_BASE`·설정 파일 디렉터리)을
  `CLAUNCH_TEST_REAL_HOMES`(`config.TEST_REAL_HOMES_ENV`)로 내보낸다. 이 값이
  있는 동안 런처 홈이나 설정 파일이 그 경로로 해석되면 `config.RealHomeInTest`가 난다
  (claunch-gziru, `tests/test_real_home_guard.py`). 이 오류는 격리가 풀렸다는
  신호이므로 변수를 지우거나 예외를 삼켜 통과시키지 않고, 격리가 풀린 원인을
  고친다. 이 가드는 런처 홈과 설정 파일 경로의 해석만 검사하므로, 아래 규칙은 가드와 별도로 지킨다.

### 규칙
1. 테스트 파일에 `home`이라는 이름의 fixture를 새로 만들지 않는다. 같은 이름의
   로컬 fixture는 conftest의 autouse `home`을 대체하여 격리가 적용되지 않는다
   (claunch-iw5i8.7, claunch-edy0w의 `profiles/codex2`). 다른 홈이 필요하면
   conftest의 `home`을 인자로 받아 그 아래에 만든다.
2. 테스트 안에서 `CLAUDE_LAUNCHER_HOME`, `CLAUDE_LAUNCHER_SYNC_FILE`,
   `CLAUNCH_INSTANCE_BASE`, `CLAUNCH_DAEMON`을 지우거나 실제 경로
   (`~/.claude-launcher`, `~/.claunch*.yaml`, `~/.claude-launcher/daemons/<name>`)로
   두지 않는다. 기본값 동작을 시험할 때는 `Path.home`(또는 `HOME`)을 `tmp_path`로
   monkeypatch한 뒤 변수를 지운다.
3. 서브프로세스 env는 호출 시점의 `os.environ`을 복사해 만든다. 빈 dict나 import
   시점에 저장한 사본으로 만들면 격리 변수와 가드 변수가 자식 프로세스에 전달되지
   않는다. `CLAUNCH_INSTANCE_APPLIED`/`PREV`처럼 conftest가 지운 변수를 자식에게
   다시 넣지 않는다 — `cli.main`이 그 값으로 홈을 되돌려 테스트 임시 홈 대신
   실제 홈이 쓰인 사례가 있다(claunch-gziru, `profiles/play`·`work`).
4. 실제 `~/.claude-launcher`, `~/.claunch.yaml`, `daemons/<name>`에 쓰는 테스트를
   만들지 않는다. 결과를 실제 홈에서 확인해야 통과하는 테스트도 같은 경우다.
5. 데몬·CLI 재현은 실제 홈에서 하지 않는다. `CLAUDE_LAUNCHER_HOME`과
   `CLAUDE_LAUNCHER_SYNC_FILE`을 임시 디렉터리(`$CLAUNCH_SCRATCH` 아래)로 둔
   셸에서 하고, `CLAUNCH_INSTANCE_BASE`·`CLAUNCH_DAEMON`은 비운다. 데몬 재기동·정지는
   이 규칙과 관계없이 사용자의 명령이다(AGENTS.md).
6. 실제 홈에 남은 산출물을 발견하면 사용자 승인 없이 지우지 않는다. 경로·생성
   시각·추정 출처를 이슈 코멘트로 남기고 사용자에게 알린다.
7. 테스트는 대상 파일만 실행한다. 전체 스윕은 지연 스윕 러너의 몫이고, 그 러너는
   옛 커밋의 conftest로도 반복 실행되므로 격리의 빈틈은 수정 뒤에도 재현된다
   (claunch-gziru: 같은 sha 잡의 재시도 499~670회).
