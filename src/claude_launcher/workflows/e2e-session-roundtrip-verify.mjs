// e2e-session-roundtrip 워크플로의 verify 명령 — 보고서가 아니라 서버 실측으로 단계 통과를 판정한다.
// 상태 파일(.claunch/tmp/e2e-roundtrip.json)은 드라이버가 각 단계에서 채운다:
//   { base, restore_provider, worker, doc_id, cell_ids: [], job_ids: [] }
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

if (mode === 'preflight') {
  const s = state();
  if (!s.base) fail('base(노트북 서버 URL) 미기록');
  if (!s.restore_provider) fail('restore_provider(원복 대상 프로바이더) 미기록 — 스냅샷 없이 라이브를 만지지 않는다');
  const p = await j(`${s.base}/api/llm-provider`);
  if (!p.providers.some((x) => x.kind === 'session')) fail('카탈로그에 kind:session 프로바이더가 없다');
} else if (mode === 'roundtrip') {
  const s = state();
  if (!s.worker) fail('worker(세션 이름) 미기록');
  if (!Array.isArray(s.job_ids) || s.job_ids.length < 2) fail('job_ids 2개 이상 필요 — 연속 잡 처리까지가 검증 범위다');
  for (const id of s.job_ids) {
    const job = await j(`${s.base}/api/jobs/${id}`);
    if (job.status !== 'done') fail(`잡 #${id} status=${job.status}${job.error ? ` (${job.error})` : ''}`);
    if (job.model !== `session:${s.worker}`) fail(`잡 #${id} model=${job.model} — 지정 레인(session:${s.worker}) 불일치`);
  }
  for (const cid of s.cell_ids ?? []) {
    const c = await j(`${s.base}/api/cells/${cid}`);
    if (!c.latest_proposal) fail(`셀 #${cid}에 제안이 없다 — post_result가 리비전으로 이어지지 않았다`);
  }
} else if (mode === 'cleanup') {
  const s = state();
  const r = await fetch(`${s.base}/api/documents/${s.doc_id}`).catch((e) => fail(`서버 연결 실패: ${e.message}`));
  if (r.status !== 404) fail(`검증 문서 #${s.doc_id} 잔존 (HTTP ${r.status}) — 삭제하고 다시 verify`);
  const p = await j(`${s.base}/api/llm-provider`);
  if (p.current !== s.restore_provider) fail(`프로바이더 미원복: ${p.current} ≠ ${s.restore_provider}`);
} else {
  fail('mode는 preflight|roundtrip|cleanup 중 하나');
}
console.log(`[e2e-verify:${mode}] ok`);
