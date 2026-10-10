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
   sessMeshes=()=>[{mesh:'team',role:'leader'}]; sessCflowRun=()=>({workflow:'leader-flow'});
   meshCache=[{name:'team',members:[{handle:'lead',session:'lead',role:'leader',roles:['leader'],local:true}]},
     {name:'other',members:[]}];
   spawnRecall=()=>({});
   const f=document.getElementById('new-session');
   f.profile.add(new Option('work','work')); f.harness.add(new Option('claude','claude'));
   f.mesh.add(new Option('none','')); f.role.add(new Option('none',''));
   f.workflow.add(new Option('none','')); f.cwd.add(new Option('repo','F:/repo'));
   window.posts=[];
   api=async(url,opts)=>{ if(opts?.method==='POST') {posts.push({url,body:JSON.parse(opts.body)});return {ok:false,status:400,json:async()=>({error:'test refusal'})};}
     if(url.includes('/children')) return {ok:true,json:async()=>({child_cflow:'worker-flow'})};
     if(url.includes('/roles')) return {ok:true,json:async()=>({default:'free-role',roles:[{name:'leader',exclusive:true},{name:'worker'},{name:'free-role'}]})};
     if(url.includes('/cflow/workflows')) return {ok:true,json:async()=>({workflows:[{name:'leader-flow'},{name:'worker-flow'},{name:'side',kind:'subflow'}]})};
     return {ok:true,json:async()=>({})}; };
   showView('new');
   return openNewSession();
  });
  assert.equal(await page.locator('#modal-overlay').isVisible(), false);
  // Measure the shared form in both hosts. A saved narrow dialog must respond
  // to its own width, and sticky actions must remain clickable above the
  // scrolled fields (including the floating Operator control).
  const rect = selector => page.locator(selector).boundingBox();
  async function checkLayout(host, columns) {
   const where=await rect('#new-where'), identity=await rect('#new-identity');
   const form=await rect('#new-session'), task=await rect('#new-task');
   if(columns===2) {
    assert.ok(Math.abs(where.y-identity.y)<1, `${host}: settings share a row`);
    assert.ok(identity.x>where.x+where.width, `${host}: settings use two columns`);
   } else {
    assert.ok(identity.y>=where.y+where.height, `${host}: settings stack`);
    assert.ok(Math.abs(identity.x-where.x)<1, `${host}: columns align`);
   }
   assert.ok(Math.abs(task.width-form.width)<1, `${host}: task uses full width`);
   const scroller=host==='page'?'#new-view':'#modal-body';
   for(const fraction of [0,0.5,1]) {
    await page.locator(scroller).evaluate((el,f)=>{el.scrollTop=(el.scrollHeight-el.clientHeight)*f;},fraction);
    const geometry=await page.locator(scroller).evaluate(el=>({w:el.clientWidth,scroll:el.scrollWidth}));
    assert.ok(geometry.scroll<=geometry.w+1, `${host}: no horizontal overflow`);
    for(const button of await page.locator('#new-session-actions button').all()) {
     const clickable=await button.evaluate(el=>{
      const r=el.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
      return y>=0 && y<innerHeight && el.contains(document.elementFromPoint(x,y));
     });
     assert.ok(clickable, `${host}: actions reachable at scroll ${fraction}`);
    }
   }
   await page.locator(scroller).evaluate(el=>{el.scrollTop=0;});
  }
  for(const width of [1440,768,390,320]) {
   await page.setViewportSize({width,height:900});
   await checkLayout('page',width===1440?2:1);
  }
  await page.setViewportSize({width:1440,height:900});
  await page.locator('#new-session [name=name]').focus();
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(()=>document.activeElement.name),'profile');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(()=>document.activeElement.name),'harness');
  await page.evaluate(()=>openSpawnModal('lead'));
  await checkLayout('modal',2);
  await page.locator('.modal-box').evaluate(el=>{el.style.width='420px';});
  await checkLayout('modal',1);
  for(const width of [390,320]) {
   await page.setViewportSize({width,height:900});
   await checkLayout('modal',1);
  }
  await page.setViewportSize({width:390,height:600});
  await checkLayout('modal',1);
  await page.evaluate(()=>sessionModalClose());
  await page.setViewportSize({width:1280,height:900});
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
  // claunch-qrvee: the fork box is sized by its rows, not by the size the
  // New/Spawn box was last dragged to. A remembered 640x380 used to open the
  // fork at that size, scrolling its six rows inside a slit.
  await page.evaluate(()=>{ try{localStorage.setItem(SPAWN_SIZE_KEY,JSON.stringify({w:640,h:380}));}catch{} });
  await page.evaluate(()=>openQuickForkModal('lead'));
  assert.equal(await page.locator('#modal-overlay').evaluate(el=>el.classList.contains('fork-open')),true);
  const forkBox=await page.locator('#modal-overlay .modal-box').evaluate(el=>({
    w:el.getBoundingClientRect().width, inline:[el.style.width,el.style.height]}));
  assert.deepEqual(forkBox.inline,['',''],'the remembered spawn size is not applied to the fork');
  assert.ok(forkBox.w>640,'the fork box is wider than the remembered spawn size');
  const forkScroll=await page.locator('#modal-body').evaluate(el=>[el.scrollHeight,el.clientHeight]);
  assert.ok(forkScroll[0]<=forkScroll[1]+1,`the fork fits without scrolling (${forkScroll})`);
  const [placeBox,meshBox]=[await rect('[name=quick_fork_place]'),await rect('[name=quick_fork_mesh]')];
  assert.ok(Math.abs(placeBox.y-meshBox.y)<1 && meshBox.x>placeBox.x+placeBox.width,
    'Placement and Mesh share a row');
  assert.equal(await page.locator('#new-tabs').isVisible(),false);
  assert.equal(await page.locator('#new-quick-fork').isVisible(),true);
  assert.ok(Math.abs((await rect('#new-quick-fork')).width-(await rect('#new-session')).width)<1,
    'Fork keeps the full form width when the other sections are hidden');
  for(const id of ['new-where','new-identity','new-runs-on','new-onboard',
   'new-runtime','new-worktree','new-task','new-beads']) {
   assert.equal(await page.locator('#'+id).isVisible(),false, `Fork hides ${id}`);
  }
  assert.equal(await page.locator('[name=task]').isEnabled(),false);
  await page.locator('[name=quick_fork_task]').fill('fork task');
  // claunch-9vpeq: four separate rows, each preselecting the inherited
  // answer as recommended. The workflow inherited is the PAIR the origin's
  // run declares (worker-flow), not the origin's own (leader-flow).
  await page.waitForFunction(()=>document.querySelector('[name=quick_fork_workflow]').value==='.');
  await page.waitForFunction(()=>document.querySelector('[name=quick_fork_role]').options.length===3);
  const forkRows=await page.evaluate(()=>Object.fromEntries(['place','mesh','role','workflow'].map(k=>{
    const s=document.querySelector(`[name=quick_fork_${k}]`);
    return [k,{value:s.value,options:[...s.options].map(o=>o.textContent+(o.disabled?' [disabled]':''))}];
  })));
  assert.deepEqual(forkRows.place.options,['As child of lead (recommended)','As sibling — a root session, like lead']);
  assert.deepEqual(forkRows.mesh,{value:'.',options:['Inherit — team (recommended)','No mesh — a scratch copy','other']});
  assert.deepEqual(forkRows.workflow,{value:'.',options:[
    "Inherit — worker-flow (recommended, the pair lead's run declares)",'No workflow','leader-flow']});
  // The origin is the mesh's live leader, so the copy cannot be one.
  assert.deepEqual(forkRows.role,{value:'worker',options:['leader — held by lead [disabled]','worker','free-role']});
  assert.match(await page.locator('#new-quick-fork-role-note').textContent(),/Role change: the conversation was held as leader/);
  // Validation: a held exclusive role is refused before any post.
  const before=await page.evaluate(()=>posts.length);
  await page.evaluate(()=>{document.querySelector('[name=quick_fork_role]').value='leader';});
  await page.getByRole('button',{name:'Fork',exact:true}).click();
  await page.getByText(/leader is already held by lead in team/).waitFor();
  assert.equal(await page.evaluate(()=>posts.length),before);
  await page.locator('[name=quick_fork_role]').selectOption('worker');
  await page.locator('[name=quick_fork_place]').selectOption('sibling');
  // No mesh hides the Role row and sends neither mesh nor role.
  await page.locator('[name=quick_fork_mesh]').selectOption('-');
  assert.equal(await page.locator('#new-quick-fork-role-row').isVisible(),false);
  await page.locator('[name=quick_fork_mesh]').selectOption('.');
  await page.waitForFunction(()=>document.querySelector('[name=quick_fork_role]').value==='worker');
  await page.getByRole('button',{name:'Fork',exact:true}).click();
  await page.getByText('test refusal',{exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>posts.at(-1)),{url:'/api/sessions/lead/quick-fork',
    body:{task:'fork task',mesh:'.',workflow:'.',role:'worker',sibling:true}});
  assert.equal(await page.locator('[name=quick_fork_task]').inputValue(),'fork task');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal(await page.locator('[name=task]').inputValue(),'page draft');
  assert.equal(await page.locator('[name=task]').isEnabled(),true);
  assert.deepEqual(await page.evaluate(()=>[JSON.parse(localStorage.getItem(SPAWN_SIZE_KEY)),
    document.getElementById('modal-overlay').classList.contains('fork-open')]),
    [{w:640,h:380},false],'closing the fork leaves the spawn size as it was');
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
  // Prompter contexts are packs; prompt buttons append real editable task text.
  await page.evaluate(() => {
    api = async (url, opts) => {
      if (opts?.method === 'POST') {
        posts.push({url, body: JSON.parse(opts.body)});
        return {ok:false, status:400, json:async()=>({error:'test refusal'})};
      }
      const kind = url === '/api/prompter/contexts' ? 'context' : 'prompt';
      return {ok:true, json:async()=> url.startsWith('/api/prompter/') ? {
        enabled:true, connected:true, url:'https://p.example',
        [kind+'s']:[{id:1, name:kind, title:kind, body:kind+' BODY'}],
      } : {}};
    };
    window.taskInputs = [];
    document.getElementById('new-session').addEventListener('input', e => {
      if (e.target.name === 'task') taskInputs.push(e.target.value);
    });
  });
  for (const mode of ['new', 'spawn']) {
    await page.evaluate(async mode => {
      showView('new');
      if (mode === 'new') await openNewSession(); else await openSpawnModal('lead');
      const f = document.getElementById('new-session');
      f.elements.namedItem('task').value = 'existing';
      f.elements.namedItem('name').value = 'prompter-'+mode;
      f.elements.namedItem('beads').value = 'none';
      f.mesh.value = ''; f.workflow.value = '';
      f.worktree_mode.value = '';
      await Promise.all([refreshNewPrompterOffer(), refreshNewTaskPrompter()]);
    }, mode);
    await page.locator('#new-task-prompter button').click();
    assert.equal(await page.locator('#new-task textarea').inputValue(), 'existing\n\nprompt BODY');
    assert.equal(await page.evaluate(()=>taskInputs.at(-1)), 'existing\n\nprompt BODY');
    assert.equal(await page.evaluate(()=>document.activeElement.name), 'task');
    assert.equal(await page.evaluate(()=>readNewFormDraft()?.task), undefined,
      'per-launch task stays out of the persistent settings draft');
    await page.locator('#new-task textarea').fill('edited prompt BODY');
    await page.locator('[name=prompter_context_1]').check();
    const before = await page.evaluate(()=>posts.length);
    await page.evaluate(()=>document.getElementById('new-session').dispatchEvent(
      new Event('submit', {bubbles:true,cancelable:true})));
    await page.waitForFunction(n=>posts.length>n, before);
    const post = await page.evaluate(()=>posts.at(-1));
    assert.equal(post.url, mode === 'new' ? '/api/sessions' : '/api/sessions/lead/children');
    assert.equal(post.body.task, 'edited prompt BODY');
    assert.deepEqual(post.body.prompter_contexts, {url:'https://p.example',ids:[1]});
    assert.ok(!('prompter_prompts' in post.body));
    if (mode === 'spawn') {
      await page.evaluate(()=>sessionModalClose());
      assert.equal(await page.locator('#new-task textarea').inputValue(), 'edited prompt BODY',
        'the New form task draft returns after closing Spawn');
    }
  }
  assert.deepEqual(errors,[]);
  console.log('sessionform browser: page tabs, Spawn-only dialog, Fork fields/payload/error and draft restoration passed');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
