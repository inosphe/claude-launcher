# Firefox 웹 세션 한글 입력 유실 조사

이슈: `claunch-70py6`, 조사일: 2026-09-29, 기준: `e2216ec3`.

## 결과

현재 제공되는 xterm.js 6.0.0의 조합 처리에서 문자 유실을 합성 이벤트로
재현했다. Firefox의 실제 OS IME 이벤트 기록은 확보하지 못했다.
사용자가 신고한 현상의 원인은 **원인 미상**이다.

확인된 라이브러리 결함은 조합 확정의 지연 전송이 남아 있을 때 일반
키의 `keydown`이 들어오는 경우다. `CompositionHelper.keydown`은
`_finalizeComposition(false)`를 호출한다. 이 함수는 공유 상태
`_isSendingComposition`을 false로 바꾸고 현재 조합 위치만 전송한다.
기존 지연 콜백들은 전송하지 않는다. 마지막 `compositionupdate`의
타이머도 아직 실행되지 않았다면 끝 위치가 이전 값으로 남아 빈 문자열이
전송될 수 있다. 이는 WebSocket 및 PTY에 도달하기 전의 손실이다.

## 재현

```powershell
node tools/diagnose_ime.cjs
node tools/diagnose_ime.cjs <다운로드한-xterm.js-경로>
```

이 진단 명령은 손실을 발견하면 종료 코드 1을 반환한다. CI 회귀 검사에는
등록하지 않았다. 실제 배포 번들에서 클래스를 추출하며, 타이머 큐만
제어한다. 브라우저를 실행하거나 실제 세션에 입력하지 않는다.

| 실험 설정 | 후속 키 | 기대값 | 결과 |
|---|---|---|---|
| 각 조합 사이 타이머 실행 | 없음 / Space / Enter / 마침표 | 가나 | 가나 (4건) |
| 조합 두 개 확정 후 타이머 실행 | 없음 | 가나 | 가나 (1건) |
| 조합 두 개 확정 후 일반 키, 이후 타이머 실행 | Space / Enter / 마침표 | 가나 | 빈 문자열 (3건) |

표는 조합 문자열의 전송만 측정한다. 일반 키 자체의 전송은 xterm의
다른 경로가 담당하므로 이 클래스 단위 측정에 포함하지 않았다.

기존 `tests/web/composition_check.js`는 통과한다. 이 검사는 종료 경계에서도
`keyCode: 229`를 사용하므로 일반 키가 지연 전송을 취소하는 경로를 검증하지
않는다. 주석의 입력 속도 표현만으로 실제 Firefox의 이벤트 순서를 추정할
수 없다.

## 배포와 관련 경로

- 로컬 데몬 `http://127.0.0.1:8378/static/vendor/xterm.js`: HTTP 200,
  `Cache-Control: no-cache`.
- 응답과 현재 체크아웃의 SHA-256이 동일하다:
  `8a957b831837d7dc1b30bd96df0acd903488426b99c84d1093fbcabe56f8ea03`.
  두 파일에서 동일한 3/8 손실을 재현했다. 사용자의 열린 탭 메모리에
  로드된 번들은 측정하지 않았다.
- `src/claude_launcher/web/static/vendor/VENDORED`: xterm 6.0.0.
  이전 수정 `claunch-m95r` / `41147625`가 현재 트리에 포함되어 있다.
- `app.js`의 `watchComposer`는 조합 이벤트를 감시해 typing 제어 프레임을
  전송한다. `sendInput`은 `onData`의 문자열을 UTF-8로 인코딩해 보낸다.
- `daemon/pty_backend.py`에는 writer별 증분 UTF-8 디코더가 있고,
  `daemon/session.py`의 `write_bytes`에는 쓰기 잠금이 있다.
  이는 이전 `claunch-pty-split-utf8-e6rjd` 이후의 처리다.
  실행 중인 Python 객체가 어느 버전인지 이번 조사에서는 확인하지 않았다.

## 검증

- `node tests/web/composition_check.js`: 통과.
- `node tests/web/typing_check.js`: 통과.
- `node tests/web/sendinput_check.js`: 통과.
- `tests/test_pty_input_utf8.py` 및 `tests/test_ws_typing.py`: 15 passed.
  메인 체크아웃의 Python 환경을 사용하되 pytest의 `pythonpath=src`로
  조사 워크트리를 검사했다. 임시 경로는 세션 scratch 아래 `pytest-ime-1`.
- 최초 Python 시도는 새 가상환경에 pytest가 없어 실행되지 않았다.
  다음 시도는 공용 임시 디렉터리 접근 거부로 setup 오류 15건이 발생했다.
  세션별 `--basetemp` 지정 후 위 15건을 다시 검사했다.
- `tests/web/link_check.js`는 존재하지 않아 실행되지 않았다.
  전체 테스트 스위트와 실제 Firefox OS IME 검사는 수행하지 않았다.

## 실제 입력 확인 방법

사용자의 운영체제, Firefox 버전, 입력 위치(터미널 직접 입력 또는 별도
입력란), 입력 전후 문자열이 필요하다. 터미널 직접 입력이라면 다음
기록으로 합성 재현과 실제 현상의 관계를 확인할 수 있다.

웹 세션의 개발자 콘솔에서 아래 코드를 실행한 뒤, 비밀 정보가 없는 짧은
한글 예시를 입력한다. `finishImeTrace()`를 호출하면 감시를 해제하고 기록을
반환한다. 기록에는 입력한 문자열이 포함되며 자동으로 외부에 전송하지 않는다.
실제 연결된 터미널의 입력은 평소처럼 세션으로 전달되므로, Enter 실험은
테스트용 세션에서 수행한다.

```javascript
const finishImeTrace = (() => {
  const t = term, ta = t.textarea, rows = [];
  const types = ['keydown', 'compositionstart', 'compositionupdate',
                 'compositionend', 'beforeinput', 'input'];
  const listener = e => rows.push({at: performance.now(), type: e.type,
    key: e.key, keyCode: e.keyCode, composing: e.isComposing,
    inputType: e.inputType, data: e.data, value: ta.value,
    start: ta.selectionStart, end: ta.selectionEnd});
  types.forEach(type => ta.addEventListener(type, listener));
  const sub = t.onData(data => rows.push({at: performance.now(),
                                        type: 'onData', data}));
  return () => {
    types.forEach(type => ta.removeEventListener(type, listener));
    sub.dispose();
    return {userAgent: navigator.userAgent, rows};
  };
})();
```

`compositionend`로 확정된 문자가 `onData`에 없으면 브라우저 입력 경로를
추가 조사한다. `onData`에는 있지만 화면에 없다면 전송 및 PTY 수신을
대조해야 한다. 별도 입력란은 xterm 조합 처리를 거치지 않으므로 별도로
측정해야 한다.

## 관련 upstream 기록

[xterm.js #6089](https://github.com/xtermjs/xterm.js/issues/6089)는 6.0.0에서
지연된 조합 전송들이 공유 boolean에 의해 취소되는 현상을 보고한다.
보고 환경은 Chrome/Linux이므로 Firefox 재현 근거로 사용하지 않았다.
이번 측정은 저장소 및 라이브 응답 번들을 직접 구동한 별도 실험이다.

수정 시에는 대기 중인 조합을 순서대로 보존하고 일반 키 처리와의 중복
전송을 막아야 한다. 받침 이동, Backspace, textarea 초기화도 함께 검증해야
한다. 이번 조사에서는 운영 코드나 vendored 번들을 변경하지 않았다.
