const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  await page.addInitScript(() => { window.setInterval = () => 0; });
  await page.route('http://session.test/**', async route => {
   const url=new URL(route.request().url());
   if(url.pathname.startsWith('/api/')) return route.fulfill({json:{}});
   const name=url.pathname==='/'?'index.html':url.pathname.replace('/static/','');
   let content=fs.readFileSync(path.join(root,name));
   if(name==='app.js') content=content.toString().replace(/boot\(\);\s*$/, '');
   await route.fulfill({body:content,contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
  });
  await page.goto('http://session.test/');
  await page.evaluate(() => {
   sessionsCache=[{name:'lead',status:'idle',harness:'claude',conversation_id:'c1',cwd:'F:/repo'}];
   for(const name of ['refreshWorkspaces','refreshNewWorktree','refreshWorkflowChoices','refreshRoles',
    'refreshSpawnPolicy','refreshSessionConnect','syncSpawnMode','syncOnboardPickers','refreshIssueChoices',
    'syncForkAvailability','refreshSessKids']) window[name]=()=>{};
   refreshSessions=async()=>{};
   sessMeshes=()=>[{mesh:'team'}]; sessCflowRun=()=>({workflow:'worker'});
   spawnRecall=()=>({});
   const f=document.getElementById('new-session');
   f.profile.add(new Option('work','work')); f.harness.add(new Option('claude','claude'));
   f.mesh.add(new Option('none','')); f.role.add(new Option('none',''));
   f.workflow.add(new Option('none','')); f.cwd.add(new Option('repo','F:/repo'));
   window.posts=[];
   api=async(url,opts)=>{ if(opts?.method==='POST') {posts.push({url,body:JSON.parse(opts.body)});return {ok:false,status:400,json:async()=>({error:'test refusal'})};}return {ok:true,json:async()=>({})}; };
   showView('new');
   return openNewSession();
  });
  assert.equal(await page.locator('#modal-overlay').isVisible(), false);
  await page.locator('[name=task]').fill('page draft');
  await page.locator('#new-tab-spawn').click();
  assert.equal(await page.locator('#modal-overlay').isVisible(), false);
  assert.equal(await page.locator('#new-parent-row').isVisible(), true);
  await page.getByRole('button',{name:'Spawn child',exact:true}).last().click();
  await page.getByText('Choose a parent session.',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>posts.length),0);
  await page.locator('[name=parent]').selectOption('lead');
  await page.locator('#new-tab-new').click();
  await page.locator('#new-tab-spawn').click();
  assert.equal(await page.locator('[name=parent]').inputValue(),'lead');
  assert.equal(await page.locator('[name=task]').inputValue(),'page draft');
  assert.deepEqual(await page.evaluate(()=>sessionFormPayload('fork',
    {task:'fork',mesh:'.',workflow:'.',cwd:'other',profile:'other',worktree:true,beads:false})),
    {task:'fork',mesh:'.',workflow:'.'});
  assert.deepEqual(await page.evaluate(()=>sessionFormPayload('spawn',
    {workspace:'registered-repo',cwd:'unregistered-path',task:'child',quick_fork_task:'hidden'})),
    {workspace:'registered-repo',cwd:'unregistered-path',task:'child'});
  await page.evaluate(()=>openSpawnModal('lead'));
  assert.equal(await page.locator('#modal-overlay').isVisible(),true);
  assert.equal(await page.locator('#new-tab-new').isVisible(),false);
  assert.equal(await page.locator('#new-tabs').isVisible(),false);
  await page.locator('[name=task]').fill('modal draft');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal(await page.locator('[name=task]').inputValue(),'page draft');
  assert.equal(await page.locator('#new-tab-spawn').getAttribute('aria-selected'),'true');
  await page.evaluate(()=>openQuickForkModal('lead'));
  assert.equal(await page.locator('#new-tabs').isVisible(),false);
  assert.equal(await page.locator('#new-quick-fork').isVisible(),true);
  assert.equal(await page.locator('#new-identity').isVisible(),false);
  assert.equal(await page.locator('[name=task]').isEnabled(),false);
  await page.locator('[name=quick_fork_task]').fill('fork task');
  await page.locator('[name=quick_fork_carry]').selectOption('inherit');
  await page.getByRole('button',{name:'Fork',exact:true}).click();
  await page.getByText('test refusal',{exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>posts.at(-1)),{url:'/api/sessions/lead/quick-fork',body:{task:'fork task',mesh:'.',workflow:'.'}});
  assert.equal(await page.locator('[name=quick_fork_task]').inputValue(),'fork task');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal(await page.locator('[name=task]').inputValue(),'page draft');
  assert.equal(await page.locator('[name=task]').isEnabled(),true);
  await page.evaluate(()=>{
    route=()=>{};
    api=(url,opts)=>{
      posts.push({url,body:JSON.parse(opts.body)});
      return new Promise(resolve=>{window.finishFork=()=>resolve({ok:true,json:async()=>({session:{name:'copy'}})});});
    };
    return openQuickForkModal('lead');
  });
  await page.getByRole('button',{name:'Fork',exact:true}).click();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#modal-overlay').isVisible(),true);
  await page.evaluate(()=>{
    document.getElementById('new-session').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
    setSessionModalTab('new');
    openSpawnModal('lead');
  });
  assert.equal(await page.evaluate(()=>posts.length),2);
  assert.equal(await page.evaluate(()=>sessionFormView().mode),'fork');
  await page.evaluate(()=>finishFork());
  await page.waitForFunction(()=>location.hash==='#/s/copy');
  assert.equal(await page.locator('#modal-overlay').isVisible(),false);
  assert.equal(await page.locator('[name=task]').inputValue(),'page draft');
  assert.deepEqual(errors,[]);
  console.log('sessionform browser: page tabs, Spawn-only dialog, Fork fields/payload/error and draft restoration passed');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
