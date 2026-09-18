/* Real-browser keyboard, modal, source, and settings regression. No live daemon. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.CLAUNCH_PLAYWRIGHT || 'playwright');
const root = path.join(__dirname, '../src/claude_launcher/web/static');
const rail = fs.readFileSync(path.join(root, 'index.html'), 'utf8').match(/<div id="session-search-row"[\s\S]*?<\/div>/)[0];
const writes = [];
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
    res.end(JSON.stringify({results:[{id:'r1',title:q,kind:'checks',excerpt:'<img src=x onerror=alert(1)>',sessions:[{name:'s1'}],href:'#/observer/session/s1',source_url:'api/search/records/s1/e1'}],index:{indexed:1,total:1},warnings:['rerank unavailable']})); return;
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
    assert.match(await page.locator('dialog [role=status]').textContent(),/Rerank 사용 불가/);
    assert.equal(await page.getByRole('link',{name:'s1',exact:true}).getAttribute('href'),'#/s/s1');
    await page.getByText('원문 보기',{exact:true}).click();await page.waitForFunction(()=>document.querySelector('dialog pre').textContent.includes('원문 기록'));
    await page.keyboard.press('Escape');assert.equal(await page.locator('dialog').evaluate(n=>n.open),false);
    assert.equal(await page.locator('#search-anything-open').evaluate(n=>n===document.activeElement),true);
    await page.locator('#search-anything-open').click();await page.locator('dialog input').fill('slow');await page.keyboard.press('Enter');
    await page.locator('dialog input').fill('newest');await page.keyboard.press('Enter');await page.getByRole('link',{name:'newest',exact:true}).waitFor();
    await page.waitForTimeout(350);assert.equal(await page.getByRole('link',{name:'slow',exact:true}).count(),0);
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
