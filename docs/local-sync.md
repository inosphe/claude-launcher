# 로컬 동기화 담당자와 Sync A/B

Sync의 실행 모드는 두 개의 cflow v2로 구분한다.

| 선택 | 생성 시 시작하는 workflow | 역할 | 완료 결과 |
|---|---|---|---|
| A — Delegate to agent | reposync-merge-push-v2 | sync-integrator | 검증·독립 검토·사용자 승인 후 통합/게시 결과 |
| B — Agent on failure | reposync-conflict-v2 | worker | 실패 원인과 격리 worktree의 검증된 복구 후보 |

B는 명령이 실제로 실패했을 때 생성한다. 승인 거절·오래된 계획은 실행
실패로 취급하지 않는다. B는 원래 branch를 갱신하거나 push하지 않는다.
B의 후보를 통합하려면 A에서 현재 ref를 다시 검사하고 별도 승인을 받는다.
A의 cflow 승인도 기존 reposync publish 승인을 대신하지 않는다. 두 workflow는
부모의 기본 workflow를 상속하지 않고 이름·버전·계획 입력을 명시한다.

설계 근거는 s150의 claunch-unvrm 코멘트 15309·15310이다. 당시 일반 worker는
workflow 없이 충돌을 해결한 뒤 22개 커밋을 재작성하고 로컬 master 갱신과
승인 후 push까지 수행했다. 이번에는 후보 작성과 통합·게시의 권한을 나누며,
후보 SHA가 달라지면 이전 승인과 테스트를 재사용하지 않는다.

## 저장소별 담당 범위

`local-sync-manager`는 통합 결정을 하지 않는다. 명시적인 PR-only 정책 아래
한 daemon의 한 canonical git common-dir를 맡는다. 같은 clone의 worktree와
workspace 별칭은 요청 DB를 공유하고, 같은 remote의 다른 clone은 분리된다.
mesh는 요청 문맥이다. 요청은 requester, mesh, board, issue와 target을 보존한다.
메시 전체의 `exclusive` role이 아니며 일반 root 자동 연결에도 참여하지 않는다.

정책과 요청·receipt·manager epoch는 Git common-dir의 SQLite DB에 보존된다.
담당자가 종료되어도 정책은 유지된다. reposync sync/push/branch 수신 및 PR
publish adapter는 보호 ref에 대한 직접 쓰기를 거절한다. 정책을 읽지 못한
경우도 쓰기를 허용하지 않는다. raw shell Git은 이 경계를 사용하지 않으므로
원격 branch protection도 설정해야 한다.

## 사용자 결정과 운용

Workspaces의 Sync 패널에서 Local synchronization manager를 열어 PR-only
정책을 활성화한다. 원격·보호 branch·authority mesh·통합 담당 handle 및 공유
authority revision을 명시한다. 각 호스트는 같은 authority 계약을 등록해야 한다.
이 로컬 DB는 여러 daemon 사이에서 authority를 자동 선출하거나 합의하지 않는다.
자동 PR merge는 제공하지 않는다. provider의 head/base 원자성 보장을 확인하지
못한 경우 지정 authority가 설정을 해소하기 전까지 workflow에서 보류한다.

MCP `repo_sync`의 `local_status`는 정책·담당자·요청을 반환한다. `local_request`에
다음 필드를 준다(requester는 호출 세션으로 채운다):

```
id, mesh, board, issue, remote, ref, required_sha,
policy_revision, authority_revision
```

id는 재전송용 멱등 키다. 같은 id의 다른 payload는 거절한다. required_sha는
전체 commit SHA이며 ref는 `refs/heads/…`이다. 생성 권한·profile·연결 범위를
확정하지 못하는 경우 daemon은 자동으로 일반 worker를 승격하지 않고 구체적인
해결 요청을 반환한다. 대시보드에서 전용 세션을 생성하거나 기존 명시적 담당자를
지정하고, 요청을 보류·취소할 수 있다. 생성은 정상 세션 생성 폼의 profile·quota·
mesh 검사를 따른다. daemon의 별도 무승인 자동 생성은 제공하지 않는다.

전용 세션의 role은 local-sync-manager, workflow는 local-sync-v2이며 시작 입력은
해결 요청에 표시된 request_id와 epoch다. 요청 mesh에 연결한 뒤 대시보드에서
Bind manager로 지정한다. workflow는 요청 처리 후 대기 단계로 돌아가 다음 요청을
받는다. 대기 중 polling하지 않으며, bind·재개·checkout 승인도 담당자에게 알림을
보낸다. 요청 결과는 workflow verifier가 DB의 receipt와 대조한다. 담당자는 `local_apply`에 request_id와 epoch를 보내며
세션 identity는 MCP가 채운다. 알려지지 않은 role/workflow나 다른 저장소의
세션을 지정할 수 없다. 다른 mesh의 요청에 연결이 없으면 담당자를 바꾸지 않고
그 연결의 해소를 요청한다. 세션 생성·지정은 게시나 PR merge를 승인하지 않는다.

요청 target ref만 고유한 임시 ref로 fetch하며, 삭제된 target이나 오래된 tracking
ref를 완료 근거로 쓰지 않는다. fetch는 요청/정책 DB 잠금 밖에서 수행한다. 이후 정책 revision·authority·epoch·
현재 old SHA를 다시 검사하고, 요청 SHA가 fetched target에 포함되는 경우에만
로컬 FF를 수행한다. 대상이 다른 checkout에 체크아웃되어 있으면 owner 조정을
요청한다. 대시보드에서 owner 조정을 확인한 뒤 정확한 old/new SHA의 로컬 FF를
승인하거나, owner가 다른 branch로 이동한 뒤 같은 요청을 다시 실행할 수 있다.
checkout 승인은 canonical 경로·worktree git-dir·directory identity와 epoch·정책
revision·old/new SHA에 묶이고 30분 후 만료된다. 승인 전후 대상 checkout이
바뀌면 새 계획과 owner 승인을 요구한다.
원격이 다시 이동하거나 checkout이 dirty이면 이전 승인은 쓰기를 허용하지 않는다.
분기한 local tip은 `refs/claunch/preserved/<old SHA>`로 보존하고 복구 결정을
요청한다. reset --hard나 무조건 force push는 경로에 없다.

원격 PR 착지와 host별 동기화 receipt는 별개다. 네트워크 장애는 완료로
보고하지 않는다. complete receipt를 재조회해 같은 요청을 다시 실행하지 않는다.
보류·취소 및 이전 epoch의 결과는 쓰기를 허용하지 않는다. 활성 cflow는 시작 시
정의를 snapshot하므로 기존 세션에는 새 정의가 소급 적용되지 않는다.
