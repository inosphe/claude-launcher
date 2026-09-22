/* Real-browser keyboard, modal, source, and settings regression. No live daemon. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.CLAUNCH_PLAYWRIGHT || 'playwright');
const root = path.join(__dirname, '../src/claude_launcher/web/static');
/* The rail row's markup, taken whole. The row holds nested divs (the search box
   and its clear button), so slicing to the first `</div>` cut it short and the
   fixture served a page without the button this test drives — silently, until
   the day the row gained its first nested div. Balanced counting, and a miss
   throws rather than serving a page with no rail. */
function railMarkup(html) {
  const start = html.indexOf('<div id="session-search-row"');
  if (start < 0) throw Error('no #session-search-row in index.html');
  const tag = /<\/?div\b[^>]*>/g;
  tag.lastIndex = start;
  let depth = 0, match;
  while ((match = tag.exec(html))) {
    depth += match[0].startsWith('</') ? -1 : 1;
    if (depth === 0) return html.slice(start, match.index + match[0].length);
  }
  throw Error('the #session-search-row div is not closed in index.html');
}
const rail = railMarkup(fs.readFileSync(path.join(root, 'index.html'), 'utf8'));
const writes = [];
const now = Date.parse('2026-09-18T10:00:00Z');
const cfg = {base_url:'http://omlx/v1', api_key_set:true, embedding_model:'embed', rerank_model:'rank', candidates:40, rerank_top:12, batch:16, timeout:120, watch_interval:30, verify_tls:true};
const server = http.createServer(async (req,res) => {
  if (req.url === '/') { res.setHeader('Content-Type','text/html; charset=utf-8'); res.end(`<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/style.css"></head><body>
    <aside style="width:260px">${rail}</aside><input id="editor"><div class="xterm"><textarea id="terminal"></textarea></div><div id="settings"></div>
    <script>function api(path,options){return fetch('/'+path,options)}</script><script src="/search-anything.js"></script></body></html>`); return; }
  if (req.url === '/search-anything.js' || req.url === '/style.css') {
    res.setHeader('Content-Type',req.url.endsWith('.js')?'application/javascript':'text/css'); res.end(fs.readFileSync(path.join(root,req.url.slice(1)))); return;
  }
  res.setHeader('Content-Type','application/json');
  if (req.method === 'PUT' || req.method === 'POST') { let text=''; for await(const part of req) text+=part; writes.push({url:req.url,body:JSON.parse(text)}); }
  if(req.url.startsWith('/api/search?')) {
    const q=new URL(req.url,'http://local').searchParams.get('q');
    if(q==='slow') await new Promise(resolve=>setTimeout(resolve,250));
    const at = q === 'invalid-time' ? 'invalid' : q === 'no-time' ? '' : new Date(now - 5 * 60000).toISOString();
    // A session and a record about sessions, as the daemon sends them: the
    // states come down on the row that *is* the session and on each chip a
    // record carries, and a session row has no `at` (the unified corpus does
    // not stamp one), which is what leaves the record's time the only one.
    const session={id:'s1',kind:'session',name:'s1',title:'s1',status:'idle',excerpt:'세션 작업 내용',
      sessions:[{name:'s1',status:'idle'}],href:'#/s/s1'};
    // An opening task result reads its record as prose; every other kind stays JSON.
    const kind = q === 'task-source' ? 'opening-task' : 'checks';
    const record={id:'r1',title:q,kind,at,excerpt:'<img src=x onerror=alert(1)>',
      sessions:[{name:'s1',status:'idle'},{name:'s2',status:'busy'},{name:'s3',status:'exited',paused:true}],
      href:kind==='opening-task'?'#/s/s1':'#/observer/session/s1',source_url:'api/search/records/s1/e1'};
    // A second record of another kind, so an answer can be asked for that holds
    // several — the second list is named by the kinds it carries, and this is
    // what tells that name apart from the one-kind case.
    const board={id:'r2',title:q,kind:'beads',at,excerpt:'보드 항목',sessions:[{name:'s2',status:'busy'}],href:'#/beads/claunch-b1'};
    const results = q === 'only-session' ? [session] : q === 'mixed' ? [session, record, board] : [session, record];
    res.end(JSON.stringify({results,index:{indexed:1,total:1},warnings:['rerank unavailable']})); return;
  }
  if(req.url==='/api/rag/settings') {res.end(JSON.stringify(cfg));return;}
  if(req.url==='/api/rag/test') {res.end(JSON.stringify({ok:true,dimensions:2560,rerank:true}));return;}
  res.end(JSON.stringify({text:'원문 기록',session:'s1'}));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:process.env.CLAUNCH_CHROMIUM});
  try {
    const page=await browser.newPage(); const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.clock.install({time: now});
    const filterBox = await page.locator('#session-search').boundingBox(), buttonBox = await page.locator('#search-anything-open').boundingBox();
    assert.equal(buttonBox.y, filterBox.y); assert.ok(buttonBox.x >= filterBox.x + filterBox.width); assert.equal(buttonBox.width, 28);
    await page.locator('#editor').focus();await page.keyboard.type('/');assert.equal(await page.locator('dialog').evaluate(n=>n.open),false);
    await page.locator('#terminal').focus();await page.keyboard.type('/');assert.equal(await page.locator('dialog').evaluate(n=>n.open),false);
    await page.locator('#search-anything-open').focus();await page.keyboard.press('/');assert.equal(await page.locator('dialog').evaluate(n=>n.open),true);
    assert.equal(await page.locator('dialog input').evaluate(n=>n===document.activeElement),true);
    assert.equal(await page.locator('dialog h2').evaluate(n=>getComputedStyle(n).fontSize),'14px');
    assert.equal(await page.locator('dialog').evaluate(n=>n.getBoundingClientRect().width),640);
    if (process.env.CLAUNCH_SCREENSHOT) await page.screenshot({path:process.env.CLAUNCH_SCREENSHOT.replace('.png','-desktop.png')});
    await page.locator('dialog input').fill('needle');await page.keyboard.press('Enter');
    await page.getByRole('link',{name:'needle',exact:true}).waitFor();assert.equal(await page.locator('dialog img').count(),0);
    const time = page.locator('dialog time');
    assert.match(await time.textContent(), /\(5분 전\)$/);
    assert.ok(await time.evaluate(n => n.textContent.startsWith(new Date(n.dateTime).toLocaleString())));
    await page.clock.fastForward(60000);
    assert.match(await time.textContent(), /\(6분 전\)$/);
    assert.match(await page.locator('dialog [role=status]').textContent(),/Rerank 사용 불가/);
    // The two classes are two lists, each headed by what it holds and how
    // many of them there were; the answer's own counts are in the notice. The
    // first list is named for the one thing it holds; the second is named by
    // the kinds it carries, so a reader learns what is inside without opening it.
    assert.match(await page.locator('dialog [role=status]').textContent(), /2개 결과 · 세션 1 · 그 외 1/);
    const heads = page.locator('dialog .search-anything-group h3');
    assert.deepEqual(await heads.evaluateAll(nodes=>nodes.map(n=>n.childNodes[0].textContent)),['세션','checks']);
    assert.deepEqual(await page.locator('dialog .search-anything-group-count').allTextContents(),['1','1']);
    // The session's own row: it is the link, and its state rides in the chip.
    const sessionChip = page.locator('dialog .search-anything-session .beads-sess');
    assert.equal(await sessionChip.getAttribute('class'),'beads-sess idle');
    assert.equal(await sessionChip.getAttribute('href'),'#/s/s1');
    assert.equal(await sessionChip.locator('.beads-sess-state').textContent(),'idle');
    // A record keeps its source on the head line, and each session it names
    // carries that session's own state — including `paused`, which is a
    // status the fleet reports as `exited` and a word only this marker holds.
    const recordRows = page.locator('dialog .search-anything-record');
    assert.equal(await recordRows.count(),1);
    assert.equal(await recordRows.locator('.search-anything-kind').textContent(),'checks');
    const chips = recordRows.locator('.beads-sess');
    assert.deepEqual(await chips.evaluateAll(nodes=>nodes.map(n=>n.className)),['beads-sess idle','beads-sess busy','beads-sess paused']);
    assert.deepEqual(await chips.locator('.beads-sess-state').allTextContents(),['idle','busy','paused']);
    assert.equal(await chips.nth(2).getAttribute('title'),'s3 (paused)');
    // The kind filter: 전체 plus one chip per kind this answer holds, each
    // carrying its own count; picking one narrows the two lists to that kind
    // and 전체 puts them back.
    const kindChips = page.locator('dialog .search-anything-filter');
    assert.deepEqual(await kindChips.evaluateAll(nodes=>nodes.map(n=>n.dataset.kind)),['','session','checks']);
    assert.deepEqual(await kindChips.locator('.search-anything-filter-count').allTextContents(),['2','1','1']);
    assert.deepEqual(await kindChips.evaluateAll(nodes=>nodes.map(n=>n.getAttribute('aria-pressed'))),['true','false','false']);
    await page.locator('dialog .search-anything-filter[data-kind="checks"]').click();
    assert.equal(await page.locator('dialog .search-anything-session').count(),0);
    assert.equal(await page.locator('dialog .search-anything-record').count(),1);
    assert.equal(await page.locator('dialog .search-anything-filter[data-kind="checks"]').getAttribute('aria-pressed'),'true');
    assert.match(await page.locator('dialog [role=status]').textContent(), /· 표시 1$/);
    await page.locator('dialog .search-anything-filter[data-kind="session"]').click();
    assert.equal(await page.locator('dialog .search-anything-session').count(),1);
    assert.equal(await page.locator('dialog .search-anything-record').count(),0);
    assert.deepEqual(await heads.evaluateAll(nodes=>nodes.map(n=>n.childNodes[0].textContent)),['세션']);
    assert.match(await page.locator('dialog [role=status]').textContent(), /· 표시 1$/);
    // The filter narrows what arrived and does not re-ask the daemon, so the
    // counts on the chips stay the answer's counts.
    assert.deepEqual(await page.locator('dialog .search-anything-filter-count').allTextContents(),['2','1','1']);
    // An answer holding one class draws one list: no heading for a list that
    // has no rows, and the notice still says what the answer held. A filter
    // whose kind is not in the new answer is dropped with it — the chips are
    // the answer's, so a pressed chip with nothing under it cannot survive.
    await page.locator('dialog input').fill('only-session');await page.keyboard.press('Enter');
    await page.locator('dialog .search-anything-session').waitFor();
    assert.equal(await page.locator('dialog .search-anything-record').count(),0);
    assert.deepEqual(await heads.evaluateAll(nodes=>nodes.map(n=>n.childNodes[0].textContent)),['세션']);
    assert.match(await page.locator('dialog [role=status]').textContent(), /1개 결과 · 세션 1 · 그 외 0/);
    assert.deepEqual(await kindChips.evaluateAll(nodes=>nodes.map(n=>n.dataset.kind)),['','session']);
    assert.deepEqual(await kindChips.evaluateAll(nodes=>nodes.map(n=>n.getAttribute('aria-pressed'))),['true','false']);
    assert.ok(!/· 표시/.test(await page.locator('dialog [role=status]').textContent()));
    // The second list is named by the kinds it holds, in the order the chips
    // count them, and the two are read from one count — so an answer holding
    // several kinds says all of them, and says exactly what the chips say.
    await page.locator('dialog input').fill('mixed');await page.keyboard.press('Enter');
    await page.locator('dialog .search-anything-record').first().waitFor();
    assert.match(await page.locator('dialog [role=status]').textContent(), /3개 결과 · 세션 1 · 그 외 2/);
    assert.deepEqual(await kindChips.evaluateAll(nodes=>nodes.map(n=>n.dataset.kind)),['','session','checks','beads']);
    assert.equal(await heads.nth(1).evaluate(n=>n.childNodes[0].textContent),'checks · beads');
    assert.equal(await heads.nth(1).evaluate(n=>n.childNodes[0].textContent),
      (await kindChips.evaluateAll(nodes=>nodes.map(n=>n.dataset.kind))).filter(kind=>kind&&kind!=='session').join(' · '));
    await page.locator('dialog input').fill('needle');await page.keyboard.press('Enter');
    await page.getByRole('link',{name:'needle',exact:true}).waitFor();
    await page.getByText('원문 보기',{exact:true}).click();await page.waitForFunction(()=>document.querySelector('dialog pre').textContent.includes('원문 기록'));
    await page.locator('dialog input').fill('task-source');await page.keyboard.press('Enter');
    await page.getByRole('link',{name:'task-source',exact:true}).waitFor();
    await page.getByText('원문 보기',{exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('dialog pre').textContent==='원문 기록');
    await page.keyboard.press('Escape');assert.equal(await page.locator('dialog').evaluate(n=>n.open),false);
    assert.equal(await page.locator('#search-anything-open').evaluate(n=>n===document.activeElement),true);
    await page.locator('#search-anything-open').click();await page.locator('dialog input').fill('slow');await page.keyboard.press('Enter');
    assert.match(await time.textContent(), /\(6분 전\)$/);
    await page.locator('dialog input').fill('newest');await page.keyboard.press('Enter');await page.getByRole('link',{name:'newest',exact:true}).waitFor();
    await page.waitForTimeout(350);assert.equal(await page.getByRole('link',{name:'slow',exact:true}).count(),0);
    for (const query of ['invalid-time', 'no-time']) {
      await page.locator('dialog input').fill(query); await page.keyboard.press('Enter');
      await page.getByRole('link',{name:query,exact:true}).waitFor();
      if (query === 'invalid-time') assert.equal(await time.textContent(), 'invalid');
      else assert.equal(await time.count(), 0);
    }
    await page.keyboard.press('Escape');
    await page.evaluate(()=>document.querySelector('#settings').append(SearchAnything.settingsCard()));
    await page.locator('input[name=embedding_model]').waitFor();await page.locator('input[name=embedding_model]').fill('replacement');
    assert.equal(await page.locator('input[name=embedding_model]').inputValue(),'replacement');
    await page.evaluate(()=>{document.querySelector('#settings').replaceChildren();document.querySelector('#settings').append(SearchAnything.settingsCard());});
    assert.equal(await page.locator('input[name=embedding_model]').inputValue(),'replacement');
    assert.equal(await page.locator('input[name=dimensions]').count(),0);
    await page.getByRole('button',{name:'연결 테스트',exact:true}).click();await page.getByText(/실제 차원 2560/).waitFor();
    await page.getByRole('button',{name:'저장',exact:true}).click();await page.getByText(/저장됨 · 검색 색인/).waitFor();
    assert.equal(writes.at(-1).body.embedding_model,'replacement');assert.equal(writes.at(-1).body.api_key,'');
    await page.setViewportSize({width:390,height:844});await page.locator('#search-anything-open').click();
    assert.ok(await page.locator('dialog').evaluate(n=>n.getBoundingClientRect().width<=innerWidth));
    if (process.env.CLAUNCH_SCREENSHOT) await page.screenshot({path:process.env.CLAUNCH_SCREENSHOT});
    assert.deepEqual(errors,[]);console.log('search anything browser: passed');
  } finally {await browser.close();server.close();}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
