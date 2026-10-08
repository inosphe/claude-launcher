/* Real stylesheet, modules and sendKeyLine against deterministic API fixtures. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require('playwright');
const staticDir = path.join(__dirname, '../../src/claude_launcher/web/static');
const appSource = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
function fn(name) {
  const start = appSource.search(new RegExp('(?:async )?function ' + name + '\\('));
  assert(start >= 0);
  return appSource.slice(start, appSource.indexOf('\n}', start) + 2);
}
const index = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
const formStart = index.indexOf('<form id="term-input"');
const form = index.slice(formStart, index.indexOf('</form>', formStart) + 7).replace('class="hidden"', '');
const fixture = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/style.css">
<div id="layout"><aside id="sidebar"><header><h1>claunch</h1><p>Workspace</p></header></aside><main id="main">
<div id="glossary-view"></div><div id="terminal-fixture" class="hidden" style="flex:1;padding:24px">Session · s1</div>${form}</main></div>
<script>globalThis.api = (url, opts) => fetch(url, opts);</script>
<script src="/glossary-page.js"></script><script src="/glossary-composer.js"></script>
<script>
let currentName = 's1', sessJournalBox = null, sessionEnded = false; const sessionsCache = [];
const $ = id => document.getElementById(id);
const currentScoreFeedback = () => 'none', setScoreFeedbackChoice = () => {}, renderScoreGoal = () => {};
const termInputQueueNote = () => '';
const isAltV = () => false, isCtrlJ = () => false;
${fn('autogrowTermInput')}
${fn('termInputNote')}
${fn('sendKeyLine')}
${fn('refreshTermInput')}
${fn('onTermInputKeydown')}
$('term-input').onsubmit = ev => { ev.preventDefault(); sendKeyLine($('term-input-field'), $('term-input-send'), $('term-input-note')); };
$('term-input-field').addEventListener('keydown', onTermInputKeydown);
GlossaryComposer.mount({field: $('term-input-field'), session: () => currentName, resize: autogrowTermInput});
GlossaryPage.open();
</script>`;
const entries = [
  {id:'explain', term:'자세히 · 구체적 설명', definition:'일의 목적과 동기, 실행 방법을 구체적으로 설명합니다.', aliases:['자세히', '상세히'], source:'설명과 보고를 요청할 때', reference:'[[term:#explain]]'},
  {id:'research', term:'자세히 · 추가 탐색', definition:'현재 모르는 일에 대해 브라우저 검색을 포함해 추가 탐색합니다.', aliases:['자세히'], source:'근거가 부족하거나 추가 조사가 필요할 때', reference:'[[term:#research]]'},
  {id:'model', term:'기반 모델', definition:'추가 학습을 시작할 때 사용하는 모델입니다.', aliases:['base model', '기반모델'], source:'모델 비교 실험', reference:'[[term:#model]]'},
];
let terms = structuredClone(entries), sent = [], staleResolve, composeResolve, sendResolve;
function analyze(text) {
  const references = [...text.matchAll(/\[\[term:([^\]]+)\]\]/g)].map(m => {
    const row = terms.find(t => '#' + t.id === m[1]);
    return {literal:m[0], start:Array.from(text.slice(0,m.index)).length, end:Array.from(text.slice(0,m.index+m[0].length)).length,
      status:row ? 'resolved':'ambiguous', ...(row ? {term:row}:{candidates:terms.slice(0,2)})};
  });
  const mentions = [...text.matchAll(/자세히/g)].filter(m => !text.slice(0,m.index).endsWith('[[term:')).map(m => ({
    literal:m[0],start:Array.from(text.slice(0,m.index)).length,end:Array.from(text.slice(0,m.index+3)).length,candidates:terms.slice(0,2)}));
  return {text,references,mentions,workspace:'claunch'};
}
const server = http.createServer((req,res) => {
  const file = req.url === '/' ? null : path.join(staticDir, req.url.slice(1));
  if (!file) { res.setHeader('Content-Type','text/html; charset=utf-8'); res.end(fixture); }
  else {res.setHeader('Content-Type',file.endsWith('.css')?'text/css':'application/javascript');res.end(fs.readFileSync(file));}
});
(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true, ...(process.env.CLAUNCH_CHROMIUM ? {executablePath:process.env.CLAUNCH_CHROMIUM} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.route('**/api/**', async route => {
      const req = route.request(), u = new URL(req.url()), body = req.postDataJSON();
      let data = {};
      if (u.pathname === '/api/workspaces') data = {workspaces:[{name:'claunch'},{name:'other'}]};
      else if (u.pathname.endsWith('/keys')) {sent.push(body); if (body.keys?.[0] === 'newer send') await new Promise(resolve => {sendResolve=resolve;}); data={ok:true};}
      else if (/\/sessions\/[^/]+\/glossary/.test(u.pathname)) {
        if (req.method() === 'GET') data = {terms:terms.slice(0,2),workspace:'claunch'};
        else {
          if (body.text === 'stale 자세히') await new Promise(resolve => {staleResolve = resolve;});
          if (body.action === 'compose' && body.text.endsWith(' slow')) await new Promise(resolve => {composeResolve = resolve;});
          data = analyze(body.text);
          if (body.action === 'compose') {
            data.ready = data.references.every(r => r.status === 'resolved') && !body.text.endsWith('[[term:');
            data.error = '용어의 의미를 선택하거나 참조 표기를 완성하세요.';
            data.delivery = body.text + '\n\n[Workspace glossary — selected meanings]\n' + JSON.stringify(data.references.map(r => r.term));
          }
        }
      } else if (u.pathname.endsWith('/suggestions')) data = {suggestions:[{...entries[0],id:'p1',status:'pending',proposed_by:'agent',created_at:'2026-10-08T00:00:00Z'}]};
      else if (u.pathname.endsWith('/resolve')) data = analyze(body.text);
      else if (req.method() === 'GET') data = {terms:u.searchParams.get('q') === 'none' ? [] : terms};
      else if (req.method() === 'PUT') {terms = terms.map(t => t.id === body.id ? {...t,...body}:t); data={};}
      await route.fulfill({json:data});
    });
    await page.goto('http://127.0.0.1:' + server.address().port);
    await page.locator('.glossary-card').first().waitFor();
    await page.locator('#term-input').evaluate(n => n.classList.add('hidden'));
    const screenshot = async name => {
      if (process.env.CLAUNCH_SCREENSHOTS) await page.screenshot({path:path.join(process.env.CLAUNCH_SCREENSHOTS,name+'.png')});
    };
    await screenshot('glossary-desktop');
    await page.getByRole('button',{name:'편집',exact:true}).first().click();
    const editor = page.locator('.glossary-editor');
    await editor.waitFor();
    assert(await page.locator('#glossary-term').evaluate(n => n === document.activeElement));
    await page.locator('#glossary-definition').fill('수정된 정의');
    await page.getByRole('button',{name:'저장',exact:true}).click();
    await page.getByText('수정된 정의',{exact:true}).waitFor();
    await page.getByRole('button',{name:'제안 검토',exact:true}).click();
    await page.getByRole('button',{name:'수락',exact:true}).waitFor();
    await screenshot('glossary-review');
    await page.getByRole('button',{name:'등록된 용어',exact:true}).click();
    await page.setViewportSize({width:390,height:844});
    await screenshot('glossary-mobile');
    assert(await page.locator('#glossary-view').evaluate(n => n.scrollWidth <= n.clientWidth), 'mobile page overflow');
    await page.getByRole('button',{name:'용어 추가',exact:true}).click();
    await page.locator('#glossary-term').fill('새 용어');
    assert(await page.locator('.glossary-editor').evaluate(n => n.getBoundingClientRect().right <= innerWidth));
    await screenshot('glossary-editor-mobile');
    // Session composer: normal text -> detected phrase -> selected ID -> real sendKeyLine.
    await page.setViewportSize({width:1280,height:900});
    await page.evaluate(() => {document.getElementById('glossary-view').classList.add('hidden');document.getElementById('term-input').classList.remove('hidden');document.getElementById('terminal-fixture').classList.remove('hidden');});
    const field = page.locator('#term-input-field');
    await field.fill('😀 자세히 설명해주세요');
    await page.getByRole('button',{name:'자세히 · 의미 2개'}).click();
    await screenshot('composer-meanings');
    await page.locator('.gc-choice').filter({hasText:'추가 탐색'}).click();
    assert.equal(await field.inputValue(),'😀 [[term:#research]] 설명해주세요');
    await page.locator('.gc-resolved').waitFor();
    await field.press('Enter');
    await page.waitForFunction(() => document.getElementById('term-input-field').value === '');
    assert(sent[0].paste.includes(entries[1].definition));
    assert(sent[0].paste.startsWith('😀 [[term:#research]] 설명해주세요'));
    assert(!sent[0].paste.includes('수정된 정의'));
    // Unresolved explicit name stays in the input; no transmission.
    await field.fill('[[term:자세히]] 해줘');
    await field.press('Enter');
    await page.locator('#term-input-note').filter({hasText:'의미를 선택'}).waitFor();
    assert.equal(sent.length,1); assert.equal(await field.inputValue(),'[[term:자세히]] 해줘');
    // Incomplete explicit search and Korean IME enter do not accidentally send.
    await field.fill('[[term:자세');
    await page.locator('.gc-choice').first().waitFor();
    await field.dispatchEvent('keydown',{key:'Enter',isComposing:true,keyCode:229});
    assert.equal(sent.length,1);
    await field.press('ArrowDown');
    assert(await page.locator('.gc-choice').first().evaluate(n => n === document.activeElement));
    await page.keyboard.press('Enter');
    assert.equal(await field.inputValue(),'[[term:#explain]]');
    // A response for the previous session must not appear or replace the draft.
    await field.fill('stale 자세히');
    await page.waitForTimeout(250);
    await page.evaluate(() => {currentName='s2';refreshTermInput();document.getElementById('term-input-field').value='new draft';});
    staleResolve(); await page.waitForTimeout(100);
    assert(await page.locator('.glossary-composer').evaluate(n => n.hidden));
    assert.equal(await field.inputValue(),'new draft');
    // Changing sessions during compose must not submit the old draft to either target.
    await field.fill('[[term:#research]] slow');
    await field.press('Enter'); await page.waitForTimeout(100);
    await page.evaluate(() => {currentName='s3';refreshTermInput();const f=document.getElementById('term-input-field');f.value='third draft';f.disabled=false;document.getElementById('term-input-send').disabled=false;});
    composeResolve(); await page.waitForTimeout(100);
    assert.equal(sent.length,1); assert.equal(await field.inputValue(),'third draft');
    await field.fill('ordinary message'); await field.press('Enter');
    await page.waitForFunction(() => document.getElementById('term-input-field').value === '');
    assert.deepEqual(sent[1].keys,['ordinary message','Enter']);
    // Away and back to the same name: an old prepare cannot send or unlock
    // a newer send on the same textarea, nor clear its replacement draft.
    composeResolve = null; sendResolve = null;
    const before = sent.length;
    await field.fill('[[term:#research]] slow'); await field.press('Enter');
    await page.waitForTimeout(100);
    await page.evaluate(() => {currentName='away';refreshTermInput();currentName='s3';refreshTermInput();});
    await field.fill('newer send'); await field.press('Enter');
    await page.waitForTimeout(100);
    composeResolve(); await page.waitForTimeout(100);
    assert.equal(sent.length,before+1);
    assert(await field.isDisabled()); assert.equal(await field.inputValue(),'newer send');
    sendResolve(); await page.waitForFunction(() => document.getElementById('term-input-field').value === '');
    await page.setViewportSize({width:390,height:844});
    await field.fill('자세히'); await page.getByRole('button',{name:'자세히 · 의미 2개'}).click();
    await screenshot('composer-mobile');
    assert(await page.locator('.glossary-composer').evaluate(n => n.scrollWidth <= n.clientWidth));
    assert.deepEqual(errors,[]);
    console.log('glossary browser: layout, edit, review, mobile, detect, select, send, ambiguity, IME, stale session passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>server.close());
