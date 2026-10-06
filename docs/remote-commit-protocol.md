# remote 편성의 커밋 공유 확인 프로토콜

이슈: claunch-yq80p.7. 도구: `tools/commit_check.py`. 테스트: `tests/test_commit_check.py`.

리더와 워커가 서로 다른 머신에 있으면 두 세션은 객체 저장소를 공유하지 않는다.
메시지에 적힌 커밋을 상대가 가지고 있는지, 브랜치 이름이 두 머신에서 같은 커밋을
가리키는지는 가정할 수 없다. 그래서 커밋을 주고받는 모든 자리에서 보내는 쪽과
받는 쪽이 각각 확인한다.

## 1. 참조 형식

머신을 넘는 커밋 참조는 항상 다음 쌍으로 쓴다.

```
<전체 40자 SHA> @ <remote>/<branch>
```

짧은 해시, 로컬 브랜치 이름, `HEAD`·`@{upstream}`·`x~1` 같은 상대 참조는 쓰지 않는다.
대상 자리: 착지 마커, REBASE/REMEASURE 요청, 컷, 머지 통지, landing 컬렉션 레코드의
`tip`·`ref` 필드.

## 2. 보내는 쪽

push 뒤, 메시지를 보내기 전에 원격이 그 SHA를 그 ref에 가지고 있는지 확인한다.

```
uv run --no-sync python tools/commit_check.py --remote <r> --ref <branch> --sha <sha> --pushed
```

`git ls-remote <r> refs/heads/<branch>`의 SHA가 보낼 SHA와 같으면 종료 코드 0이다.
다르면 메시지를 보내지 않는다.

## 3. 받는 쪽

그 참조로 판정·작업하기 전에 확인한다.

```
uv run --no-sync python tools/commit_check.py --remote <r> --ref <branch> --sha <sha> --exact
uv run --no-sync python tools/commit_check.py --remote <r> --ref <branch> --sha <sha> --contains
```

도구는 `git fetch <r> +refs/heads/<branch>:refs/remotes/<r>/<branch>` 뒤에
(a) `git cat-file -e <sha>^{commit}` 성공, (b) `--exact`이면 `<r>/<branch>`가 그 SHA와
같음, `--contains`이면 그 SHA가 `<r>/<branch>`의 조상(또는 같음)인지를 본다.
정확 일치를 요구하는 자리(리뷰·측정 대상 tip)는 `--exact`, 포함을 요구하는
자리(이미 다음 커밋이 올라간 base)는 `--contains`를 쓴다.

실패하면 작업하지 않고 도구가 stdout 첫 줄에 낸 답을 보낸 쪽에 그대로 보낸다.

```
NOT FOUND <sha> @ <r>/<branch> (fetched <그 ref의 현재 SHA 또는 ->)
```

가장 가까운 커밋을 추측해서 쓰지 않는다.

## 4. 답에 SHA를 다시 적는다

확인한 쪽은 답(ack·판정·머지 통지)에 자기가 본 SHA를 다시 적는다. 도구가 성공 시
내는 줄이 그 형식이다.

```
OK <sha> @ <r>/<branch> (<mode>)
```

보낸 SHA와 답에 적힌 SHA가 같아야 그 교환이 성립한다.

## 5. 머지 통지

리더의 머지 통지는 머지 커밋 SHA와 원격 base ref를 `<merge sha> @ <r>/<base>`로 적는다.
워커는 다음으로 확인한 뒤에 착지로 판정한다.

```
uv run --no-sync python tools/commit_check.py --remote <r> --ref <base> --sha <merge sha> --contains --parent-of <내 tip 전체 SHA>
```

`--parent-of`는 그 머지 커밋의 부모 목록에 워커의 tip이 있는지를 본다.

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 확인됨. stdout: `OK ...` |
| 1 | SHA는 이 클론에 있으나 ref가 다른 곳을 가리킴(또는 `--parent-of`의 부모가 없음). stdout: `NOT FOUND ...` |
| 2 | fetch 뒤에도 SHA가 없음(`--pushed`에서는 원격에 그 ref가 없음). stdout: `NOT FOUND ...` |
| 3 | fetch 또는 ls-remote 실패. stderr에 git 오류 |
| 4 | 인자가 머신 간 참조 형식이 아님(짧은 해시, 상대 ref, 모드 누락). argparse 기본 코드 2와 겹치지 않도록 따로 둔다 |

## 범위

v2 워크플로 정의의 wait probe와 작업 `verify:`가 이 도구를 부르는 배선은
claunch-yq80p.4 / .5 / .6에서 한다.
