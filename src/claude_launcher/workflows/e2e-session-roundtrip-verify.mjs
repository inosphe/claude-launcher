// e2e-session-roundtrip 워크플로의 verify 명령 — 보고서가 아니라 서버 재실측으로 단계 통과를 판정한다.
// 프로젝트 특화 사실(엔드포인트·필드 의미)은 여기 하드코딩하지 않는다: 드라이버가 상태 파일에
// 재실측 가능한 프로브(URL + 기대)를 기록하고, 이 스크립트는 verify 시점에 그 프로브를 다시 쏘아
// 기대와 대조한다. 더 강한 프로젝트 전용 판정이 필요하면 같은 이름의 스크립트를 프로젝트
// 레이어(.claunch/workflows/)에 두면 그쪽이 우선한다.
//
// 상태 파일(.claunch/tmp/e2e-roundtrip.json) 계약 — 드라이버가 각 단계에서 채운다:
// {
//   service: { url },                          // 대상 서비스 — preflight가 도달성을 실측
//   snapshot: { probe: { url, path }, value }, // 원복 대상 공유 상태 — preflight/cleanup이 재실측 대조
//   worker,                                    // 워커 세션 이름
//   job_probes: [ { url, expect: [ { path, equals } | { path, exists: true } ] } ],
//                                              // 잡별 증거 — 완료 상태·레인 지정이 드러나는 필드
//   artifact_probes: [ { url, gone_status } ], // cleanup: 부재(기본 404)여야 하는 검증 아티팩트
//   worker_cleanup                             // cleanup: "killed" 또는 인계 대상 명시
// }
import fs from 'node:fs';

const mode = process.argv[2];
const STATE = '.claunch/tmp/e2e-roundtrip.json';
const fail = (m) => { console.error(`[e2e-verify:${mode}] ${m}`); process.exit(1); };
const state = () => {
  try { return JSON.parse(fs.readFileSync(STATE, 'utf8')); }
  catch { return fail(`상태 파일 없음/파손: ${STATE} — 단계 지시대로 기록해야 verify가 성립한다`); }
};
const j = async (url) => {
  const r = await fetch(url).catch((e) => fail(`${url} 연결 실패: ${e.message}`));
  if (!r.ok) fail(`${url} → HTTP ${r.status}`);
  return r.json();
};
const dig = (obj, path) => String(path).split('.').reduce((a, k) => (a == null ? a : a[k]), obj);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const replaySnapshot = async (s, why) => {
  const p = s.snapshot?.probe;
  if (!p?.url || p.path === undefined || s.snapshot?.value === undefined)
    fail('snapshot(probe.url·probe.path·value) 미기록 — 스냅샷 없이 라이브를 만지지 않는다');
  const got = dig(await j(p.url), p.path);
  if (!same(got, s.snapshot.value))
    fail(`${why}: ${p.url} 의 ${p.path}=${JSON.stringify(got)} ≠ 기록값 ${JSON.stringify(s.snapshot.value)}`);
};

if (mode === 'preflight') {
  const s = state();
  if (!s.service?.url) fail('service.url(대상 서비스 URL) 미기록');
  const r = await fetch(s.service.url).catch((e) => fail(`${s.service.url} 연결 실패: ${e.message}`));
  if (r.status >= 500) fail(`${s.service.url} → HTTP ${r.status}`);
  // 스냅샷은 지금 서버 현재값과 일치해야 한다 — 라이브를 만진 뒤 찍은 스냅샷은 원복 기준이 못 된다.
  await replaySnapshot(s, '스냅샷이 서버 현재값과 다르다');
} else if (mode === 'roundtrip') {
  const s = state();
  if (!s.worker) fail('worker(세션 이름) 미기록');
  if (!Array.isArray(s.job_probes) || s.job_probes.length < 2)
    fail('job_probes 2개 이상 필요 — 연속 잡 처리까지가 검증 범위다');
  for (const probe of s.job_probes) {
    if (!probe?.url || !Array.isArray(probe.expect) || !probe.expect.length)
      fail('job_probe에는 url과 expect(완료·레인 지정이 드러나는 필드) 1개 이상이 필요하다');
    const body = await j(probe.url);
    for (const e of probe.expect) {
      const got = dig(body, e.path);
      if (e.exists) { if (got === undefined) fail(`${probe.url} 의 ${e.path} 부재`); }
      else if (!same(got, e.equals))
        fail(`${probe.url} 의 ${e.path}=${JSON.stringify(got)} ≠ 기대 ${JSON.stringify(e.equals)}`);
    }
  }
} else if (mode === 'cleanup') {
  const s = state();
  if (!s.worker_cleanup) fail('worker_cleanup 미기록 — kill 완료 또는 인계 대상 명시가 필요하다');
  if (!Array.isArray(s.artifact_probes) || !s.artifact_probes.length)
    fail('artifact_probes 미기록 — 만든 검증 아티팩트의 부재 프로브가 필요하다');
  for (const probe of s.artifact_probes) {
    const r = await fetch(probe.url).catch((e) => fail(`${probe.url} 연결 실패: ${e.message}`));
    const gone = probe.gone_status ?? 404;
    if (r.status !== gone)
      fail(`검증 아티팩트 잔존: ${probe.url} → HTTP ${r.status} (기대 ${gone}) — 삭제하고 다시 verify`);
  }
  await replaySnapshot(s, '공유 상태 미원복');
} else {
  fail('mode는 preflight|roundtrip|cleanup 중 하나');
}
console.log(`[e2e-verify:${mode}] ok`);
