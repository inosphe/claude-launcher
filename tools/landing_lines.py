"""착지 보고의 (a)~(e) 다섯 줄을 스윕 출력에서 기계로 뽑는다.

사용:
    python tools/landing_lines.py <out.txt> <err.txt> <tree-oid> <tool-ref>
        [<judged-tree 의 changed_tests.py 사본>] [<후보 도구 blob>] [<사슬 tip 도구 blob>]

이 스크립트는 판단을 넣지 않는다. 출력에 있는 줄만 옮기고, 없는 줄은 **왜 없는지를
갈라서** 적는다 -- 그 갈래가 이 도구의 전부다:

* 도구가 그 규칙을 갖고 있는데 이번에 낼 것이 없었다   -> 「줄 없음」
* 도구 판에 그 규칙 자체가 없다                        -> 「측정 안 됨」 + 그 판의 blob
* 출력 파일이 없거나 판정 줄이 없다                    -> **줄을 하나도 안 낸다.** rc 1

마지막 갈래가 이 도구가 존재하는 이유다. 빈 입력에서 다섯 줄을 「없음」으로 채우면
훑고 비어 있는 것과 아예 못 읽은 것이 같은 문장이 되고, 읽는 사람은 그것을 커버리지의
증거로 읽는다. 그래서 근거가 없으면 보고를 만들지 않고 그 사실을 stderr 로 낸다.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: relations_tried() 가 부르는 이름. 그 함수를 가진 판을 판정할 때만 쓴다.
RELATIONS = (
    "same-named test (tests/test_<stem>.py)",
    "direct import by a test",
    "a test naming '<needle>'",
    "EXPLICIT_GUARDS(비-src 경로)",
)

#: 도구 소스에서 규칙의 존재를 읽을 때 쓰는 표식과 그 규칙의 이름.
RULES = (
    ("동명 테스트", "def select"),
    ("직접 임포트(2b)", "def importers"),
    ("이름 언급(3a)", "def mentioning"),
    ("명시 가드(3b)", "EXPLICIT_GUARDS = "),
)

STOP_PREFIXES = ("WARNING:", "NOTE:", "$ ", "  Check by hand", "  Direct imports only")


class NoEvidence(Exception):
    """근거가 없다 -- 다섯 줄을 만들지 않고 그 사실을 낸다."""


def read(path: str, label: str) -> str:
    """없는 파일은 근거가 아니다 -- 조용히 빈 문자열로 대체하지 않는다."""
    f = Path(path)
    if not f.is_file():
        raise NoEvidence(
            f"근거 없음: {label} 파일이 없다 ({path}). "
            f"스윕을 '--list > out.txt 2> err.txt' 형태로 다시 받아라."
        )
    return f.read_text(encoding="utf-8", errors="replace")


def block(text: str, head: str, stop_prefixes=STOP_PREFIXES):
    """``head`` 로 시작하는 줄과 그 아래 들여쓴 본문을 돌려준다."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith(head):
            body = []
            for more in lines[i + 1:]:
                if more.startswith("  ") and not any(more.startswith(s) for s in stop_prefixes):
                    body.append(more.strip())
                else:
                    break
            return line, body
    return None, []


def report(out, err, tree, toolref, tool_src=None, cand_blob=None, tip_blob=None):
    """(a)~(e) 줄을 만들어 돌려준다. 근거가 없으면 ``NoEvidence``."""
    has_hop = ("reached_indirectly" in tool_src) if tool_src is not None else None
    has_named = ("def relations_tried" in tool_src) if tool_src is not None else None
    lines = []

    if has_named is False:
        present = [name for name, needle in RULES if needle in tool_src]
        lines.append(
            "(a) 훑은 관계   : " + " / ".join(present)
            + f"  [{toolref} 소스에서 셈. 이 판에는 relations_tried 가 없어 도구가 관계를 이름으로 안 낸다]"
        )
    elif has_named is True:
        lines.append(
            "(a) 훑은 관계   : " + " / ".join(RELATIONS)
            + f"  [{toolref} 의 relations_tried 에서 읽음]"
        )
    else:
        lines.append(
            "(a) 훑은 관계   : " + " / ".join(RELATIONS)
            + "  [**도구 사본을 안 줘서 안 쟀다** — 5번째 인자로 판정 대상 트리의"
            " changed_tests.py 를 주면 그 판에서 읽는다]"
        )
    if has_hop is not None:
        lines.append(
            "                  한 홉 규칙(2b 한 칸 밖 보고): "
            + ("있다" if has_hop else "**이 판의 도구에 없다**")
            + f"  [{toolref} 소스에서 읽음]"
        )
    lines.append(
        "                  안 훑는 관계: 실행(parametrize 표·하네스 목록·서브프로세스 호출)"
        " — 이 도구에 그 규칙이 없다"
    )

    sel_head = next((l for l in out.split("\n") if "test module(s) selected" in l), None)
    if sel_head is None:
        raise NoEvidence(
            "근거 없음: stdout 에 선택 줄('N test module(s) selected')이 없다. "
            "도구가 판정을 못 냈거나 다른 스트림으로 갔다. "
            f"stdout {len(out.encode('utf-8'))}바이트 / stderr {len(err.encode('utf-8'))}바이트"
            " — 두 파일을 직접 읽어라."
        )
    lines.append(f"(b) 선택       : {sel_head}")

    warn_head, warn_body = block(out, "WARNING: ")
    warn_err, warn_err_body = block(err, "WARNING: ")
    if warn_head or warn_err:
        src = "stdout" if warn_head else "stderr"
        lines.append(f"(c) 못 맞춘 경로: [{src}] {warn_head or warn_err}")
        lines.extend(f"                  {b}" for b in (warn_body or warn_err_body))
    else:
        clean = next((l for l in out.splitlines() if "map to at least one test module" in l), None)
        if clean:
            lines.append(f"(c) 못 맞춘 경로: [stdout] {clean}")
        else:
            lines.append(
                "(c) 못 맞춘 경로: **블록 없음** — 그 판의 도구는 비었을 때 아무 줄도 안 낸다"
                "(조건부 출력). 트리 " + tree[:12]
            )

    note_head, note_body = block(out, "NOTE: ")
    if note_head:
        lines.append(f"(d) 한 홉 밖    : [stdout] {note_head}")
        lines.extend(f"                  {b}" for b in note_body)
    else:
        clean = next((l for l in out.splitlines() if "one import hop outside this selection" in l), None)
        if clean:
            lines.append(f"(d) 한 홉 밖    : [stdout] {clean}")
        elif has_hop is False:
            if not cand_blob:
                raise NoEvidence(
                    "근거 없음: (d) 를 「측정 안 됨」으로 적으려면 읽은 도구 판을 blob 으로"
                    " 넘겨야 한다(6번째 인자). 판 이름 없는 「측정 안 됨」은 요구 미준수다"
                    " — 심사자가 반증할 자리가 없다."
                )
            lines.append(
                "(d) 한 홉 밖    : **측정 안 됨** — 이 판의 도구에 그 규칙 자체가 없다"
                "(없어서 못 낸 것이지 훑고 비어 있는 것이 아니다). 도구 "
                + toolref + ", blob " + cand_blob[:12]
            )
            if tip_blob:
                same = cand_blob[:12] == tip_blob[:12]
                lines.append(
                    "                  후보 판 블롭 %s — 사슬 tip 과 %s. 그 축을 덮는 고침은"
                    " 아직 사슬에 없다." % (cand_blob[:12], "같다" if same else "다르다")
                )
        elif has_hop is True:
            lines.append(
                "(d) 한 홉 밖    : **줄 없음** — 규칙은 있는데 출력에 그 줄이 없다"
                "(조건부 출력 판). 트리 " + tree[:12]
            )
        else:
            lines.append(
                "(d) 한 홉 밖    : **줄 없음** — 그 판의 도구에 이 규칙이 있는지 안 쟀다"
                "(5번째 인자로 도구 사본을 주면 판정한다)"
            )

    lines.append(
        f"(e) 읽은 곳     : stdout {len(out.encode('utf-8'))}바이트 /"
        f" stderr {len(err.encode('utf-8'))}바이트 — 두 파일 다 읽었다."
        f" 트리 {tree}, 도구 {toolref}"
    )
    return lines


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 4:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    out_p, err_p, tree, toolref = argv[:4]
    tool_src_p = argv[4] if len(argv) > 4 else None
    cand_blob = argv[5] if len(argv) > 5 else None
    tip_blob = argv[6] if len(argv) > 6 else None
    try:
        out = read(out_p, "stdout")
        err = read(err_p, "stderr")
        tool_src = None
        if tool_src_p and Path(tool_src_p).is_file():
            tool_src = Path(tool_src_p).read_text(encoding="utf-8", errors="replace")
        for line in report(out, err, tree, toolref, tool_src, cand_blob, tip_blob):
            print(line)
    except NoEvidence as why:
        print(why, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
