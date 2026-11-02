/* Real-browser keyboard, modal, source, and settings regression. No live daemon. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.CLAUNCH_PLAYWRIGHT || 'playwright');
const {safeListen} = require('./safe_listen.cjs');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
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
const searches = [];
const now = Date.parse('2026-09-18T10:00:00Z');
const cfg = {source:'custom', profile:'', profiles:[{name:'ds4', base_url:'https://api.fireworks.ai/inference/v1', has_key:true}], base_url:'http://omlx/v1', api_key_set:true, embedding_model:'embed', rerank_model:'rank', candidates:40, rerank_top:12, batch:16, timeout:120, watch_interval:30, verify_tls:true,
  // claunch-pxxn4: each connection path keeps its own model ids, so switching
  // the radio restores that path's pair instead of showing the other one's.
  sources:{custom:{base_url:'http://omlx/v1', embedding_model:'embed', rerank_model:'rank'},
           profile:{profile:'', embedding_model:'fw-embed', rerank_model:''}},
  // The card is preset-only (claunch-rag-preset-always-hn5bc): the picker
  // addresses one preset and the save writes it back and makes it active.
  presets:[{id:'omlx', source:'custom', base_url:'http://omlx/v1', embedding_model:'embed', rerank_model:'rank'},
           {id:'fw', source:'profile', profile:'ds4', embedding_model:'fw-embed', db:'shared'}],
  preset:'omlx', preset_max:20};
// The collections on disk as GET api/rag/dbs reports them: one named DB spread
// over two corpora, one per other preset, one nobody reads, and one built
// before presets existed (claunch-r4796).
let dbs = [
  {name:'sessions.embed', corpus:'sessions', identity:'embed', dims:2560, model:'embed', model_key:'embed', presets:['omlx'], in_use:true, bytes:1048576},
  {name:'all.embed', corpus:'all', identity:'embed', dims:2560, model:'embed', model_key:'embed', presets:['omlx'], in_use:true, bytes:2048},
  {name:'sessions.shared', corpus:'sessions', identity:'shared', dims:768, model:'fw-embed', model_key:'fw-embed', presets:['fw'], in_use:false, bytes:4096},
  {name:'sessions.old', corpus:'sessions', identity:'old', dims:768, model:'x', model_key:'x', presets:[], in_use:false, bytes:10},
  {name:'sessions', corpus:'sessions', identity:'', dims:768, model:'Legacy M', model_key:'', presets:[], in_use:false, bytes:10}];
const server = http.createServer(async (req,res) => {
  if (req.url === '/') { res.setHeader('Content-Type','text/html; charset=utf-8'); res.end(`<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/style.css"></head><body>
    <aside style="width:260px">${rail}</aside><input id="editor"><div class="xterm"><textarea id="terminal"></textarea></div><div id="settings"></div>
    <script>function api(path,options){return fetch('/'+path,options)}</script><script src="/search-anything.js"></script></body></html>`); return; }
  if (req.url === '/search-anything.js' || req.url === '/style.css') {
    res.setHeader('Content-Type',req.url.endsWith('.js')?'application/javascript':'text/css'); res.end(fs.readFileSync(path.join(root,req.url.slice(1)))); return;
  }
  if(req.url.startsWith('/api/search/stream?')) {
    searches.push(new URL(req.url, 'http://local').searchParams);
    // The daemon's answer is two server-sent events: the vector ranking at
    // once, then the reranker's order (or an error) when it comes back.
    res.setHeader('Content-Type','text/event-stream; charset=utf-8');
    const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    const q=new URL(req.url,'http://local').searchParams.get('q');
    if(q==='mode-race') {
      const mode = new URL(req.url,'http://local').searchParams.get('mode');
      if(mode==='general') await new Promise(resolve=>setTimeout(resolve,250));
      send('ranked',{results:[{id:'race',kind:'beads',title:mode,href:'#/beads/race'}],index:{indexed:1,total:1},warnings:[],rerank_pending:false});
      res.end(); return;
    }
    if(q==='grouped') {
      send('ranked',{results:[{id:'group',kind:'beads',title:'grouped issue',excerpt:'best passage',
        href:'#/beads/x',source_url:'api/beads/x',matches:[
          {kind:'comment',excerpt:'additional evidence'},
          {kind:'comment',excerpt:'<img src=x onerror=alert(1)>'}]}],
        index:{indexed:3,total:3},warnings:[],rerank_pending:false});
      res.end(); return;
    }
    if(q==='rerank-offer') {
      // The reranker reverses the ranked order. The page must keep the ranked
      // list until the reader applies the new one.
      const first={id:'r1',title:'first',kind:'checks',excerpt:'',sessions:[],href:'#/a'};
      const second={id:'r2',title:'second',kind:'checks',excerpt:'',sessions:[],href:'#/b'};
      send('ranked',{results:[first,second],index:{indexed:2,total:2},warnings:[],rerank_pending:true});
      await new Promise(resolve=>setTimeout(resolve,200));
      send('reranked',{results:[second,first],index:{indexed:2,total:2},warnings:[],reranked:true,rerank_pending:false});
      res.end(); return;
    }
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
    send('ranked',{results,index:{indexed:1,total:1},warnings:[],rerank_pending:true});
    send('error',{results,index:{indexed:1,total:1},warnings:['rerank unavailable'],error:'rerank unavailable',rerank_pending:false});
    res.end(); return;
  }
  res.setHeader('Content-Type','application/json');
  if (req.method === 'PUT' || req.method === 'POST') { let text=''; for await(const part of req) text+=part; writes.push({url:req.url,body:JSON.parse(text)}); }
  if(req.url==='/api/rag/settings' && req.method === 'PUT') {
    // The daemon answers a save with the settings as they now stand, so the
    // card's next save starts from them. Echo the rows that were written.
    const written = writes.at(-1).body.sources || {};
    for (const [name, row] of Object.entries(written)) {
      cfg.sources[name] = {...cfg.sources[name], ...row};
      for (const [key, value] of Object.entries(row)) if (key !== 'profile' && key !== 'api_key') cfg[key] = value;
    }
    cfg.source = writes.at(-1).body.source;
    cfg.profile = cfg.sources.profile.profile || '';
    const body = writes.at(-1).body;
    if (Array.isArray(body.presets)) cfg.presets = body.presets.map(({api_key, renamed_from, ...row}) => row);
    if (typeof body.preset === 'string') cfg.preset = body.preset;
    res.end(JSON.stringify(cfg));return;
  }
  if(req.url==='/api/rag/status') {res.end(JSON.stringify({indexes:[{kind:'sessions',pending:3,total:10}],active:{preset:cfg.preset},backfill_paused:false}));return;}
  if(req.url==='/api/rag/dbs') {res.end(JSON.stringify({dbs}));return;}
  if(req.url==='/api/rag/dbs/rename') {
    const {from, to} = writes.at(-1).body;
    dbs = dbs.map(row => row.identity === from ? {...row, identity: to, name: `${row.corpus}.${to}`} : row);
    const moved = cfg.presets.filter(p => (p.db || p.embedding_model) === from).map(p => p.id);
    for (const p of cfg.presets) if (moved.includes(p.id)) p.db = to;
    res.end(JSON.stringify({renamed: from, db: to, presets: moved, dbs}));return;
  }
  if(req.url==='/api/rag/settings') {res.end(JSON.stringify(cfg));return;}
  if(req.url==='/api/rag/test') {res.end(JSON.stringify({ok:true,dimensions:2560,rerank:true}));return;}
  res.end(JSON.stringify({text:'원문 기록',session:'s1'}));
});
(async()=>{
  await safeListen(server);
  const browser=await chromium.launch({headless:true,executablePath:process.env.CLAUNCH_CHROMIUM});
  try {
    const page=await browser.newPage(); const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.clock.install({time: now});
    // The rail's search line starts folded (claunch-p6xb3): the box is not drawn
    // and Search anything sits right of the filter icon, on the header's line.
    // app.js is not loaded here, so nothing opens it until the class is set.
    const row = page.locator('#session-search-row');
    const toggleBox = await page.locator('#session-search-toggle').boundingBox();
    let buttonBox = await page.locator('#search-anything-open').boundingBox();
    assert.equal(await page.locator('#session-search').boundingBox(), null);
    assert.equal(buttonBox.y + buttonBox.height / 2, toggleBox.y + toggleBox.height / 2); assert.ok(buttonBox.x >= toggleBox.x + toggleBox.width); assert.equal(buttonBox.width, 28);
    // Opened (what syncSessionSearchOpen sets): the box takes the header's place
    // and Search anything stays right of it on the same line.
    await row.evaluate(n => n.classList.add('searching'));
    const filterBox = await page.locator('#session-search').boundingBox(); buttonBox = await page.locator('#search-anything-open').boundingBox();
    assert.equal(buttonBox.y, filterBox.y); assert.ok(buttonBox.x >= filterBox.x + filterBox.width); assert.equal(buttonBox.width, 28);
    await row.evaluate(n => n.classList.remove('searching'));
    await page.locator('#editor').focus();await page.keyboard.type('/');assert.equal(await page.locator('dialog').evaluate(n=>n.open),false);
    await page.locator('#terminal').focus();await page.keyboard.type('/');assert.equal(await page.locator('dialog').evaluate(n=>n.open),false);
    await page.locator('#search-anything-open').focus();await page.keyboard.press('/');assert.equal(await page.locator('dialog').evaluate(n=>n.open),true);
    assert.equal(await page.locator('dialog input[type=search]').evaluate(n=>n===document.activeElement),true);
    assert.equal(await page.locator('dialog h2').evaluate(n=>getComputedStyle(n).fontSize),'14px');
    assert.equal(await page.locator('dialog').evaluate(n=>n.getBoundingClientRect().width),640);
    if (process.env.CLAUNCH_SCREENSHOT) await page.screenshot({path:process.env.CLAUNCH_SCREENSHOT.replace('.png','-desktop.png')});
    await page.locator('dialog input[type=search]').fill('needle');await page.keyboard.press('Enter');
    await page.getByRole('link',{name:'needle',exact:true}).waitFor();assert.equal(await page.locator('dialog img').count(),0);
    const time = page.locator('dialog time');
    assert.match(await time.textContent(), /\(5분 전\)$/);
    assert.ok(await time.evaluate(n => n.textContent.startsWith(new Date(n.dateTime).toLocaleString())));
    await page.clock.fastForward(60000);
    assert.match(await time.textContent(), /\(6분 전\)$/);
    assert.match(await page.locator('dialog [role=status]').textContent(),/Rerank 사용 불가/);
    const generalMode = page.getByRole('radio', {name:'일반', exact:true});
    const activeMode = page.getByRole('radio', {name:'활성 세션 + Beads', exact:true});
    assert.equal(await generalMode.isChecked(), true);
    assert.equal(searches.at(-1).get('mode'), 'general');
    let searched = page.waitForResponse(r => r.url().includes('mode=active'));
    await activeMode.check(); await searched;
    await page.getByRole('link',{name:'needle',exact:true}).waitFor();
    assert.equal(searches.at(-1).get('mode'), 'active');
    assert.equal(searches.at(-1).get('q'), 'needle');
    assert.equal(await generalMode.isChecked(), false);
    searched = page.waitForResponse(r => r.url().includes('mode=general'));
    await generalMode.check(); await searched;
    await page.getByRole('link',{name:'needle',exact:true}).waitFor();
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
    await page.locator('dialog input[type=search]').fill('only-session');await page.keyboard.press('Enter');
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
    await page.locator('dialog input[type=search]').fill('mixed');await page.keyboard.press('Enter');
    await page.locator('dialog .search-anything-record').first().waitFor();
    assert.match(await page.locator('dialog [role=status]').textContent(), /3개 결과 · 세션 1 · 그 외 2/);
    assert.deepEqual(await kindChips.evaluateAll(nodes=>nodes.map(n=>n.dataset.kind)),['','session','checks','beads']);
    assert.equal(await heads.nth(1).evaluate(n=>n.childNodes[0].textContent),'checks · beads');
    assert.equal(await heads.nth(1).evaluate(n=>n.childNodes[0].textContent),
      (await kindChips.evaluateAll(nodes=>nodes.map(n=>n.dataset.kind))).filter(kind=>kind&&kind!=='session').join(' · '));
    // The reranker's order is offered, not applied: the ranked list stays on
    // screen after the second answer arrives, and changes only on the click.
    await page.locator('dialog input[type=search]').fill('rerank-offer');await page.keyboard.press('Enter');
    await page.getByRole('link',{name:'first',exact:true}).waitFor();
    const apply = page.locator('dialog .search-anything-apply');
    await apply.waitFor({state:'visible'});
    const titles = () => page.locator('dialog .search-anything-record > a').allTextContents();
    assert.deepEqual(await titles(),['first','second']);
    assert.match(await page.locator('dialog [role=status]').textContent(), /정렬 개선 가능/);
    await apply.click();
    assert.deepEqual(await titles(),['second','first']);
    assert.equal(await apply.isHidden(),true);
    assert.match(await page.locator('dialog [role=status]').textContent(), /정렬 개선 적용됨/);
    await page.locator('dialog input[type=search]').fill('needle');await page.keyboard.press('Enter');
    await page.getByRole('link',{name:'needle',exact:true}).waitFor();
    assert.equal(await apply.isHidden(),true);
    await page.getByText('원문 보기',{exact:true}).click();await page.waitForFunction(()=>document.querySelector('dialog pre').textContent.includes('원문 기록'));
    await page.locator('dialog input[type=search]').fill('task-source');await page.keyboard.press('Enter');
    await page.getByRole('link',{name:'task-source',exact:true}).waitFor();
    await page.getByText('원문 보기',{exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('dialog pre').textContent==='원문 기록');
    // Switching scope during an outstanding response must not restore the
    // old scope's rows, even when that response completes later.
    await page.locator('dialog input[type=search]').fill('mode-race');
    const oldRequest = page.waitForRequest(r=>r.url().includes('q=mode-race'));
    await page.keyboard.press('Enter'); await oldRequest;
    await activeMode.check();
    await page.getByRole('link',{name:'active',exact:true}).waitFor();
    await new Promise(resolve=>setTimeout(resolve,350));
    assert.equal(await page.getByRole('link',{name:'general',exact:true}).count(),0);
    await page.keyboard.press('Escape');assert.equal(await page.locator('dialog').evaluate(n=>n.open),false);
    assert.equal(await page.locator('#search-anything-open').evaluate(n=>n===document.activeElement),true);
    await page.locator('#search-anything-open').click();await page.locator('dialog input[type=search]').fill('slow');await page.keyboard.press('Enter');
    assert.match(await time.textContent(), /\(6분 전\)$/);
    await page.locator('dialog input[type=search]').fill('newest');await page.keyboard.press('Enter');await page.getByRole('link',{name:'newest',exact:true}).waitFor();
    await page.waitForTimeout(350);assert.equal(await page.getByRole('link',{name:'slow',exact:true}).count(),0);
    for (const query of ['invalid-time', 'no-time']) {
      await page.locator('dialog input[type=search]').fill(query); await page.keyboard.press('Enter');
      await page.getByRole('link',{name:query,exact:true}).waitFor();
      if (query === 'invalid-time') assert.equal(await time.textContent(), 'invalid');
      else assert.equal(await time.count(), 0);
    }
    await page.keyboard.press('Escape');
    await page.evaluate(()=>document.querySelector('#settings').append(SearchAnything.settingsCard()));
    const card = page.locator('.search-settings');
    const embed = page.locator('input[name=embedding_model]'), dbField = page.locator('input[name=db]');
    await embed.waitFor();
    // The active preset loads first and is marked active; the summary names it.
    assert.equal(await page.locator('select[name=preset]').inputValue(),'omlx');
    assert.equal(await embed.inputValue(),'embed');
    assert.equal(await card.locator('.rag-badge-on').textContent(),'활성');
    await card.locator('.rag-summary-value').filter({hasText:'omlx'}).waitFor();
    await card.locator('.rag-preset-state').filter({hasText:'남은 문서 3 / 10건'}).waitFor();
    // No stray class names leak into the text (the old card printed
    // "rag-preset-db" and "rag-preset-rename" as labels).
    assert.equal(/rag-preset-|rag-db-/.test(await card.innerText()),false);
    await embed.fill('replacement');
    await page.evaluate(()=>{document.querySelector('#settings').replaceChildren();document.querySelector('#settings').append(SearchAnything.settingsCard());});
    assert.equal(await embed.inputValue(),'replacement');
    assert.equal(await page.locator('input[name=dimensions]').count(),0);
    // An empty DB field says the name the model gives it.
    await card.getByText('저장되는 DB: replacement (Embedding 모델 이름에서 만든 이름)').waitFor();
    // The DB list folds a DB's corpora into one row; a legacy collection is its own row.
    const dbRows = card.locator('.rag-db-entry');
    await dbRows.first().waitFor();
    assert.deepEqual(await card.locator('.rag-db-use').allTextContents(),['embed','shared','old','sessions (이전 형식)']);
    assert.equal(await dbRows.filter({hasText:'shared'}).locator('.rag-db-delete').isDisabled(),true);
    assert.equal(await dbRows.filter({hasText:'old'}).locator('.rag-db-delete').isDisabled(),false);
    // The connection test reports the model's width and marks DBs of another width.
    await card.getByRole('button',{name:'연결 테스트',exact:true}).click();
    await card.getByText(/이 모델의 차원 2560/).waitFor();
    assert.equal(await dbRows.filter({hasText:'shared'}).getByText('차원 불일치').count(),1);
    assert.equal(await dbRows.nth(0).getByText('차원 불일치').count(),0);
    // Clicking a DB name fills the field; the legacy row fills its model's slug.
    await card.getByRole('button',{name:'shared',exact:true}).click();
    assert.equal(await dbField.inputValue(),'shared');
    await card.getByText(/DB 이름 칸에 shared을 채웠습니다.*2560차원이라 저장이 거절됩니다/).waitFor();
    assert.equal(await card.getByRole('button',{name:'shared',exact:true}).getAttribute('aria-pressed'),'true');
    await card.getByRole('button',{name:'sessions (이전 형식)',exact:true}).click();
    assert.equal(await dbField.inputValue(),'legacy-m');
    await dbField.fill('My DB');
    await card.getByText('저장되는 DB: my-db (입력한 이름을 변환함)').waitFor();
    await card.getByRole('button',{name:'omlx 저장하고 활성화',exact:true}).click();
    await card.getByText(/프리셋 omlx을 저장했고 활성 프리셋이 됐습니다/).waitFor();
    // claunch-pxxn4: the save carries both rows, the picked one taking what is
    // on screen; the selected preset's row is replaced and stays active.
    let body = writes.at(-1).body;
    assert.equal(writes.at(-1).url,'/api/rag/settings');
    assert.equal(body.embedding_model,undefined);
    assert.equal(body.sources.custom.embedding_model,'replacement');
    assert.equal(body.sources.custom.api_key,undefined);
    assert.equal(body.source,'custom');
    assert.equal(body.preset,'omlx');
    assert.deepEqual(body.presets.map(p=>[p.id,p.embedding_model,p.db]),[['omlx','replacement','My DB'],['fw','fw-embed','shared']]);
    // claunch-dkrqf: the profile path hides the typed endpoint.
    assert.equal(await page.locator('select[name=profile]').isVisible(),false);
    await page.locator('input[name=rag-source][value=profile]').check();
    assert.equal(await page.locator('input[name=base_url]').isVisible(),false);
    await page.locator('select[name=profile]').selectOption('ds4');
    await card.getByRole('button',{name:'omlx 저장하고 활성화',exact:true}).click();
    await page.waitForFunction(n=>document.querySelector('.rag-status').textContent.includes('저장했고'),null);
    body = writes.at(-1).body;
    assert.equal(body.source,'profile');
    assert.equal(body.sources.profile.profile,'ds4');
    assert.equal(body.presets[0].source,'profile');
    assert.equal(body.presets[0].profile,'ds4');
    await page.locator('input[name=rag-source][value=custom]').check();
    assert.equal(await page.locator('input[name=base_url]').isVisible(),true);
    // Picking another preset only loads it: no write, and it reads as not active.
    const writesBefore = writes.length;
    await page.locator('select[name=preset]').selectOption('fw');
    assert.equal(writes.length, writesBefore);
    assert.equal(await card.locator('.rag-badge').first().textContent(),'비활성');
    assert.equal(await embed.inputValue(),'fw-embed');
    assert.equal(await dbField.inputValue(),'shared');
    await card.getByRole('button',{name:'fw 저장하고 활성화',exact:true}).waitFor();
    // Renaming a DB: the slug shows as it is typed, a taken name is refused
    // before the request, and the field follows when it held the old name.
    await dbRows.filter({hasText:'shared'}).getByRole('button',{name:'이름 변경'}).click();
    const renameRow = card.locator('.rag-db-rename-row');
    await renameRow.locator('input').fill('Old');
    await renameRow.getByText('DB old이 이미 있습니다.').waitFor();
    assert.equal(await renameRow.getByRole('button',{name:'이름 변경'}).isDisabled(),true);
    await renameRow.locator('input').fill('Shared Two');
    await renameRow.getByText('저장되는 이름: shared-two').waitFor();
    await renameRow.getByRole('button',{name:'이름 변경'}).click();
    await card.getByText(/DB shared의 이름을 shared-two으로 바꿨습니다.*\(fw\)/).waitFor();
    assert.deepEqual(writes.at(-1),{url:'/api/rag/dbs/rename',body:{from:'shared',to:'shared-two'}});
    assert.equal(await dbField.inputValue(),'shared-two');
    await card.getByRole('button',{name:'shared-two',exact:true}).waitFor();
    // Deleting a DB nobody reads takes the whole DB by name.
    page.once('dialog', dialog => dialog.accept());
    await dbRows.filter({hasText:'old'}).locator('.rag-db-delete').click();
    await page.waitForFunction(()=>document.querySelector('.rag-status').textContent.includes('삭제'),null);
    assert.deepEqual(writes.at(-1),{url:'/api/rag/dbs/delete',body:{db:'old'}});
    // Renaming a preset: the rule shows before the request; the active pointer stays.
    const presetBox = card.locator('fieldset.rag-section').first();
    await presetBox.getByRole('button',{name:'이름 변경',exact:true}).click();
    const presetRename = presetBox.locator('.rag-subform').first();
    await presetRename.locator('input').fill('Bad Name');
    await presetRename.getByText(/영문 소문자·숫자로 시작하고/).first().waitFor();
    await presetRename.locator('input').fill('omlx');
    await presetRename.getByText('프리셋 omlx이 이미 있습니다. 다른 이름을 쓰세요.').waitFor();
    await presetRename.locator('input').fill('fw2');
    await presetRename.getByRole('button',{name:'이름 변경',exact:true}).click();
    await card.getByText(/프리셋 fw의 이름을 fw2으로 바꿨습니다.*활성 프리셋은 omlx 그대로입니다/).waitFor();
    body = writes.at(-1).body;
    assert.equal(body.preset,'omlx');
    assert.deepEqual(body.presets.map(p=>[p.id,p.renamed_from]),[['omlx',undefined],['fw2','fw']]);
    if (process.env.CLAUNCH_SETTINGS_SHOT) {
      await page.setViewportSize({width:900,height:2400});
      await page.screenshot({path:process.env.CLAUNCH_SETTINGS_SHOT, clip: await card.boundingBox()});
    }
    // At phone width the card does not scroll sideways; the DB table scrolls in its own box.
    await page.setViewportSize({width:390,height:844});
    assert.ok(await card.evaluate(n=>n.scrollWidth<=n.clientWidth+1));
    if (process.env.CLAUNCH_SETTINGS_SHOT) {
      await page.setViewportSize({width:390,height:3200});
      await page.screenshot({path:process.env.CLAUNCH_SETTINGS_SHOT.replace(/\.png$/,'-mobile.png'), clip: await card.boundingBox()});
    }
    await page.setViewportSize({width:1280,height:720});
    await page.locator('#search-anything-open').click();
    await page.locator('dialog input[type=search]').fill('grouped');await page.keyboard.press('Enter');
    await page.locator('dialog .search-anything-matches').waitFor();
    assert.equal(await page.locator('dialog .search-anything-result').count(),1);
    assert.equal(await page.locator('dialog .search-anything-matches summary').textContent(),'관련 구절 2개');
    await page.locator('dialog .search-anything-matches summary').click();
    assert.deepEqual(await page.locator('dialog .search-anything-matches p').allTextContents(),
      ['additional evidence','<img src=x onerror=alert(1)>']);
    assert.equal(await page.locator('dialog .search-anything-matches img').count(),0);
    await page.locator('dialog .search-anything-filter[data-kind="comment"]').click();
    assert.equal(await page.locator('dialog .search-anything-result').count(),1);
    assert.equal(await page.locator('dialog .search-anything-filter[data-kind="comment"] .search-anything-filter-count').textContent(),'1');
    await page.keyboard.press('Escape');
    await page.setViewportSize({width:390,height:844});await page.locator('#search-anything-open').click();
    assert.ok(await page.locator('dialog').evaluate(n=>n.getBoundingClientRect().width<=innerWidth));
    if (process.env.CLAUNCH_SCREENSHOT) await page.screenshot({path:process.env.CLAUNCH_SCREENSHOT});
    assert.deepEqual(errors,[]);console.log('search anything browser: passed');
  } finally {await browser.close();server.close();}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
