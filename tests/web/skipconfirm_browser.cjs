/* "Back to the form" really does come back (claunch-9mwba).

   The skip-permissions confirm is raised from #new-session's own submit, and
   that form is a node inside the shared dialog's body. showModal used to
   close the session modal before asking -- so the box under the question was
   gone, and sessionModalClose had already put every row back to what it held
   when the box opened. "Back to the form" then answered into nothing: the
   window itself closed.

   The check drives the real page (no boot) through the user's own path --
   "Spawn same setup", whose New session tab is the top-level form -- and
   holds three things:

   - the question is asked with the form still alive: it is parked out of the
     dialog body for the length of the question, and the dialog title is the
     question's;
   - "Back to the form" puts the form back in the dialog, under its own
     title, with what was typed, and sends nothing;
   - the explicit start still sends the create, without the skip args.
   The Spawn child tab is measured too: its permission row is inherited
   (spawn policy), so no question is asked there and the press goes straight
   out -- the fix must not turn that submit into a question. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../src/claude_launcher/web/static');
const QUESTION = 'Start without skip permissions?';
const TYPED = 'form typing that must survive';

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
   if(name==='app.js') content=content.toString().replace(/^boot\(\);$/m, '');
   await route.fulfill({body:content,contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
  });
  await page.goto('http://session.test/');
  await page.evaluate(async () => {
   sessionsCache=[{name:'lead',status:'idle',harness:'claude',conversation_id:'c1',cwd:'F:/repo',args:[]}];
   harnessDetails={claude:{skip_permissions_args:['--dangerously-skip-permissions']}};
   profileDetails={'work:claude':{profile:'work',harness:'claude'},
                   'work':{profile:'work',harness:'claude'}};
   for(const name of ['refreshWorkspaces','refreshNewWorktree','refreshWorkflowChoices','refreshRoles',
    'refreshSpawnPolicy','refreshSessionConnect','syncOnboardPickers','refreshIssueChoices',
    'syncForkAvailability','refreshSessKids']) window[name]=()=>{};
   refreshSessions=async()=>{};
   sessMeshes=()=>[]; sessCflowRun=()=>null;
   spawnRecall=()=>({});
   window.posts=[];
   api=async(url,opts)=>{
     if(opts?.method==='POST'&&url==='/api/batch') return {ok:true,json:async()=>({answers:{},errors:{},statuses:{}})};
     if(opts?.method==='POST'){ posts.push({url,body:JSON.parse(opts.body)}); return {ok:false,status:400,json:async()=>({error:'test refusal'})}; }
     return {ok:true,json:async()=>({})};
   };
   const f=document.getElementById('new-session');
   if (![...f.profile.options].some(o=>o.value==='work:claude'))
     f.profile.add(new Option('work claude','work:claude'));
   showView('new');
   // "Spawn same setup": the child tab and the New session tab beside it.
   await openSessionModal({tab:'spawn', parent:'lead', modes:['spawn','new'],
                           seed:null, newSeed:{cwd:'F:/repo'}});
   setSessionModalTab('new');
   f.profile.value='work:claude';
   f.profile.dispatchEvent(new Event('change'));
   f.task.dispatchEvent(new Event('input', {bubbles:true}));
  });
  const state = await page.evaluate(() => {
   const f=document.getElementById('new-session');
   return {mode: sessionFormView().mode, parent: f.parent.value,
           box: f.skip_permissions.checked, shut: f.skip_permissions.disabled,
           asks: newSkipPermissionsAnswer(f, null)};
  });
  assert.deepEqual(state, {mode:'new', parent:'', box:false, shut:false, asks:false},
    'the New session tab stands as the top-level form, with the row answerable');

  await page.evaluate((typed) => {
   const f=document.getElementById('new-session');
   f.skip_permissions.checked=false;
   f.task.value=typed;
   f.task.dispatchEvent(new Event('input', {bubbles:true}));
  }, TYPED);
  const submit = () => page.evaluate(()=>document.getElementById('new-session')
    .dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
  await submit();
  await page.waitForFunction(q=>document.getElementById('modal-title').textContent===q, QUESTION);
  assert.equal(await page.locator('#modal-overlay').isVisible(), true,
    'the question is up');
  assert.equal(await page.evaluate(()=>document.getElementById('new-view')
    .contains(document.getElementById('new-session'))), true,
    'the form is parked out of the dialog body while it is asked');

  await page.getByRole('button',{name:'Back to the form',exact:true}).click();
  await page.waitForFunction(()=>document.getElementById('modal-body')
    .contains(document.getElementById('new-session')));
  assert.equal(await page.locator('#modal-overlay').isVisible(), true,
    '"Back to the form" leaves the window open');
  assert.equal(await page.evaluate(()=>sessionModal!==null), true,
    '...with the session modal still the one holding it');
  assert.equal(await page.locator('#new-session [name=task]').inputValue(), TYPED,
    '...and what was typed is still there');
  assert.equal(await page.evaluate(()=>document.getElementById('modal-title').textContent),
    'New session', '...under its own title, not the question’s');
  assert.equal(await page.evaluate(()=>posts.length), 0,
    '...and nothing was sent');
  // The answer's own buttons are the question's, not the form's: a bar left
  // holding them offers a press that only settles an already-settled promise
  // (claunch-9mwba review).
  assert.equal(await page.locator('#modal-actions button').count(), 0,
    '...and the question’s buttons are gone with it');

  // The explicit start still launches, without the skip args.
  await submit();
  await page.waitForFunction(q=>document.getElementById('modal-title').textContent===q, QUESTION);
  await page.getByRole('button',{name:'Start without skip permissions',exact:true}).click();
  await page.waitForFunction(()=>posts.length>0);
  const post = await page.evaluate(()=>posts.at(-1));
  assert.equal(post.url, '/api/sessions');
  assert.equal(post.body.task, TYPED);
  assert.ok(!(post.body.args||[]).includes('--dangerously-skip-permissions'),
    'the create carries no skip-permissions args');

  // The Spawn child tab: its row is inherited, so the same unticked box must
  // go straight out as before -- no question, nothing parked.
  const childState = await page.evaluate(async () => {
   const f=document.getElementById('new-session');
   setSessionModalTab('spawn');
   f.task.value='child task';
   f.task.dispatchEvent(new Event('input', {bubbles:true}));
   posts.length=0;
   f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
   await new Promise((r)=>setTimeout(r,50));
   return {mode: sessionFormView().mode, shut: f.skip_permissions.disabled,
           title: document.getElementById('modal-title').textContent,
           posts: posts.length};
  });
  assert.equal(childState.mode, 'spawn');
  assert.equal(childState.shut, true, 'a child inherits the permission row');
  assert.notEqual(childState.title, QUESTION, 'no question is asked for a child');
  assert.equal(childState.posts, 1, 'the child create went straight out');

  assert.deepEqual(errors, []);
  console.log('skip confirm browser: Back to the form keeps the form, the start launches, a child never asks');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
