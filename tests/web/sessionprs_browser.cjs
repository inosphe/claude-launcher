const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
(async()=>{
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  await page.route('http://pr.test/**', async route=>{
   const u = new URL(route.request().url());
   if(u.pathname.startsWith('/api/')) return route.fulfill({json:{}});
   const file = u.pathname==='/'?'index.html':u.pathname.replace('/static/','');
   let body = fs.readFileSync(path.join(root,file));
   if(file==='app.js') body=body.toString().replace(/^boot\(\);$/m, '');
   return route.fulfill({body,contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
  });
  await page.goto('http://pr.test/');
  await page.evaluate(async()=>{
   const pr={url:'https://ghe.example.com/team/claunch/pull/1234', number:1234,repo:'ghe.example.com/team/claunch',title:'Session PR labels',branch:'s175-session-pr-label',base:'master',status:'open',issues:['claunch-ohuw5']};
   const session={name:'s175-a-very-long-session-name',status:'idle',harness:'codex',profile:'codex',cwd:'/repo',role:'specialist-reviewer',prs:[pr],rail_commits:{commits:[{short:'1111111a',subject:'one',state:'merged',pr:1234},{short:'2222222b',subject:'two',state:'pushed',pr:1234},{short:'3333333c',subject:'three',state:'local'}],more:0}};
   meshCache=[{name:'claunch-long-mesh-name',members:[{session:session.name,handle:'long-worker-handle',role:'specialist-reviewer',local:true}]}];
   window.api=async()=>({ok:true,json:async()=>({sessions:[session],llm_configured:false})});
   await refreshSessions();
   const panel=document.createElement('div');
   panel.style='position:fixed;left:600px;top:40px;width:400px;padding:20px;background:#1b1e24';
   panel.appendChild(sessionPrPanel(session));
   document.body.appendChild(panel);
  });
  // The PR is a frame on the card's commit line, around the commits it
  // carries; the name line no longer carries a PR badge.
  assert.equal(await page.locator('#session-list .rail-pr').count(),0);
  const label=page.locator('#session-list .rail-prgroup-label');
  assert.equal(await label.textContent(),'#1234');
  assert.equal(await page.locator('#session-list .rail-prgroup .rail-commit').count(),2);
  assert.deepEqual(await page.locator('#session-list .rail-commit-mark').allTextContents(),['✓','↑','○']);
  const geometry=await page.locator('#session-list .rail-commits').evaluate(line=>{
   const li=line.closest('li');
   const frame=line.querySelector('.rail-prgroup');
   const style=getComputedStyle(frame);
   return {line:line.getBoundingClientRect().toJSON(),li:li.getBoundingClientRect().toJSON(),
     border:style.borderTopWidth+' '+style.borderTopStyle};
  });
  assert(geometry.line.right<=geometry.li.right+1,JSON.stringify(geometry));
  assert.equal(geometry.border,'1px solid');
  const link=page.locator('.sess-pr-link');
  assert.equal(await link.getAttribute('href'),'https://ghe.example.com/team/claunch/pull/1234');
  assert(await link.isVisible());
  console.log('Browser PR frame, commit chips and detail link: passed',geometry);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
