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
   const session={name:'s175-a-very-long-session-name',status:'idle',harness:'codex',profile:'codex',cwd:'/repo',role:'specialist-reviewer',prs:[pr]};
   meshCache=[{name:'claunch-long-mesh-name',members:[{session:session.name,handle:'long-worker-handle',role:'specialist-reviewer',local:true}]}];
   window.api=async()=>({ok:true,json:async()=>({sessions:[session],llm_configured:false})});
   await refreshSessions();
   const panel=document.createElement('div');
   panel.style='position:fixed;left:600px;top:40px;width:400px;padding:20px;background:#1b1e24';
   panel.appendChild(sessionPrPanel(session));
   document.body.appendChild(panel);
  });
  const badge=page.locator('#session-list .rail-pr');
  assert.equal(await badge.textContent(),'PR #1234');
  const geometry=await badge.evaluate(el=>({badge:el.getBoundingClientRect().toJSON(),head:el.parentElement.getBoundingClientRect().toJSON()}));
  assert(geometry.badge.right<=geometry.head.right+1,JSON.stringify(geometry));
  const link=page.locator('.sess-pr-link');
  assert.equal(await link.getAttribute('href'),'https://ghe.example.com/team/claunch/pull/1234');
  assert(await link.isVisible());
  console.log('Browser PR label and detail link: passed',geometry);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
