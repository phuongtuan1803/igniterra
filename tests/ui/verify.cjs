/* Run against fixture_server.py only. Browser dependency lives outside the project. */
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || '/tmp/igniterra-browser/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = 'http://127.0.0.1:8765';
const evidence = path.resolve(__dirname, '../../docs/ux/evidence');
fs.mkdirSync(evidence, {recursive:true});
const results = {viewports:[],checks:[],pageErrors:[],safety:'Temporary data, fake GPU/model providers, loopback runtime; external browser requests blocked.'};
const check = (name, value) => { assert(value, name); results.checks.push(name); console.log('PASS '+name); };
let browser;
(async()=>{
 browser=await chromium.launch({executablePath:process.env.CHROME_PATH || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 await context.route('**/*', route => {const url=new URL(route.request().url()); return ['127.0.0.1','localhost'].includes(url.hostname) ? route.continue() : route.abort();});
 const page=await context.newPage(); page.on('pageerror',e=>results.pageErrors.push(e.message));
 let accept=true; page.on('dialog',dialog=>accept?dialog.accept():dialog.dismiss());
 const meta=await (await context.request.get(base+'/__fixture')).json();
 let runURL;
 assert(meta.keyframes && meta.workflow, 'Must use isolated fixture server');
 const goto=async url=>{await page.goto(base+url); await page.waitForLoadState('networkidle');};
 const snap=async name=>page.screenshot({path:path.join(evidence,name+'.png'),fullPage:true});
 const routes=[['tasks','/'],['storyboard',`/tasks/${meta.keyframes}`],['review',`/tasks/${meta.final}`],['task-detail',`/tasks/${meta.keyframes}/detail`],['gpu','/gpu'],['offers','/gpu?tab=offers&search=1'],['rental',`/gpu/instances/${meta.ready}`],['comfyui','/comfyui/workflows'],['workflow',`/comfyui/workflows/${meta.workflow}`],['workflow-blocked',`/comfyui/workflows/${meta.pending}`],['monitor','/monitor?refresh=off'],['series','/series'],['series-bible','/series/browser-series'],['settings','/settings']];
 for(const width of [1440,768,390]){
  await page.setViewportSize({width,height:width===768?1024:1000});
  for(const [name,url] of routes){
   await goto(url);
   const dimensions=await page.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth,main:Math.round(document.querySelector('main').getBoundingClientRect().width),missingLabels:[...document.querySelectorAll('input:not([type=hidden]),textarea,select')].filter(e=>!e.labels?.length&&!e.getAttribute('aria-label')).map(e=>e.name),duplicateIds:[...document.querySelectorAll('[id]')].map(e=>e.id).filter((id,i,ids)=>ids.indexOf(id)!==i)}));
   results.viewports.push({width,name,...dimensions});
   check(`${width}px ${name}: no document overflow`,dimensions.document<=width);
   check(`${width}px ${name}: meaningful content width`,dimensions.main>width*.65);
   check(`${width}px ${name}: form names accessible`,dimensions.missingLabels.length===0);
   check(`${width}px ${name}: unique field IDs`,dimensions.duplicateIds.length===0);
   await snap(`${width}-${name}`);
  }
  await goto('/__reference/igniterra-prototype.html');
  for(const name of ['tasks','storyboard','gpu','rental','comfyui','workflow','monitor','series','settings']){
   await page.evaluate(name=>{
    const click=selector=>document.querySelector(selector)?.click();
    if(name==='storyboard')click('[data-action="open-default-storyboard"]');
    else if(name==='rental'){click('[data-action="nav"][data-id="gpu"]');click('[data-action="open-rental"]');}
    else if(name==='workflow'){click('[data-action="nav"][data-id="comfyui"]');click('[data-action="open-workflow"]');}
    else click(`[data-action="nav"][data-id="${name}"]`);
   },name);
   await snap(`${width}-reference-${name}`);
  }
 }
 await page.setViewportSize({width:1440,height:1000});
 await goto('/');
 for(const href of ['/series','/gpu','/comfyui/workflows','/monitor','/settings','/']){
  await page.locator(`.app-sidebar .nav-item[href="${href}"]`).click();await page.waitForURL(base+href);
  check(`Desktop navigation ${href}`,await page.locator(`.app-sidebar .nav-item[href="${href}"]`).getAttribute('aria-current')==='page');
 }
 await goto('/');
 await page.keyboard.press('Tab'); check('Keyboard skip link visible',await page.locator('.skip-link').evaluate(e=>e.getBoundingClientRect().top>=0));
 await page.locator('[data-open-dialog="new-task"]').click(); check('Task create dialog opens',await page.locator('#new-task').evaluate(e=>e.open));
 await snap('1440-create-task');
 await page.keyboard.press('Escape'); check('Dialog Escape restores opener focus',await page.locator('[data-open-dialog="new-task"]').evaluate(e=>e===document.activeElement));
 await page.locator('[data-open-dialog="new-task"]').click();
 await page.locator('#brief_json').fill('{broken'); accept=false;
 await page.locator('#new-task button[type=submit]').click();
 check('Paid task confirmation cancellation keeps dialog open',await page.locator('#new-task').evaluate(e=>e.open));
 accept=true;await page.locator('#new-task button[type=submit]').click();await page.waitForURL(/err=/);
 check('Invalid Brief shows backend error',await page.locator('[role=alert]').isVisible()); await snap('1440-task-error');
 await page.locator('[data-open-dialog="new-task"]').click();await page.locator('#brief_file').selectOption('brief-01.json');await page.locator('#new-task button[type=submit]').click();await page.waitForURL(/\/tasks\/t_\d+/);
 check('Create task uses real route and stored ID',/\/tasks\/t_\d+/.test(page.url()));
 await goto('/'); await page.locator('#task-search').fill(meta.keyframes); await page.locator('.task-search button').click();
 check('Task search filters actual task rows',await page.locator('.production-table tbody tr').count()===1);
 await page.locator('#task-search').fill('no-such-task');await page.locator('.task-search button').click();
 check('Task search empty state',await page.getByText('Không tìm thấy task phù hợp.').isVisible());await snap('1440-task-search-empty');
 await goto(`/?status=failed`);check('Status filter shows actual failed task',await page.locator('.production-table').innerText().then(t=>t.includes(meta.failed)&&!t.includes(meta.keyframes)));
 await goto(`/tasks/${meta.keyframes}`);await page.locator(`.scene[href$="scene=s02"]`).click();
 check('Scene selection uses production query route',new URL(page.url()).searchParams.get('scene')==='s02');
 check('Unchanged scene save disabled',await page.locator('[data-save]').isDisabled());
 await page.locator('[name=visual]').fill('Robot scene s02, browser verification edit');await page.locator('[data-save]').click();await page.waitForURL(/msg=/);
 check('Scene save creates revision',await page.getByText('ScenePlan rev 2',{exact:false}).first().isVisible());
 await context.request.post(base+'/__fixture/tick',{data:{task:meta.keyframes}});await goto(`/tasks/${meta.keyframes}?scene=s02`);
 const imageURL=await page.locator('.selected-preview').getAttribute('src');const image=await (await context.request.get(base+imageURL)).body();
 await page.locator('input[type=file][name=file]').setInputFiles({name:'browser.png',mimeType:'image/png',buffer:image});
 await page.locator('[name=source]').fill('browser fake fixture');await page.locator('[name=rights]').fill('owned');await page.getByRole('button',{name:'Upload và chọn'}).click();await page.waitForURL(/msg=/);
 check('Upload adds real user candidate',await page.locator('.cand.sel').innerText().then(t=>t.includes('up_s02_1')));
 await context.request.post(base+'/__fixture/tick',{data:{task:meta.keyframes}});await goto(`/tasks/${meta.keyframes}?scene=s02`);
 await page.locator('form[action$="/select"] button').first().click();await page.waitForURL(/msg=/);
 check('Candidate selection posts production form',await page.locator('.flash').innerText().then(t=>t.includes('đã chọn')));
 await context.request.post(base+'/__fixture/tick',{data:{task:meta.keyframes}});await goto(`/tasks/${meta.keyframes}`);
 await page.locator('form[action$="/gate"] [value=approve]').click();await page.waitForURL(/msg=/);
 check('Keyframe approval requeues task',await page.locator('#status').getAttribute('data-task-status')==='pending');
 await goto(`/tasks/${meta.final}`);await page.locator('form[action$="/gate"] [value=approve]').click();await page.waitForURL(/err=/);
 check('Final gate refuses missing review',await page.locator('[role=alert]').innerText().then(t=>t.includes('phiếu')));
 for(let i=1;i<=9;i++)await page.locator(`[name=C0${i}_result]`).selectOption('pass');
 await page.locator('[name=C03_ts]').fill('00:02');await page.locator('[name=active_minutes]').fill('5');await page.locator('#review button').click();await page.waitForURL(/msg=/);
 check('C01–C09 review stored as accepted',await page.locator('.flash').innerText().then(t=>t.includes('accepted=True')));
 await page.locator('form[action$="/gate"] [value=approve]').click();await page.waitForURL(/msg=/);
 check('Final gate marks task completed',await page.locator('#status').getAttribute('data-task-status')==='completed');
 await goto('/gpu');await page.getByRole('tab',{name:'Tìm GPU'}).click();
 await page.route('**/gpu/offers?*',async route=>{await new Promise(resolve=>setTimeout(resolve,350));await route.continue()});
 await page.locator('#find-offer button[type=submit]').click();check('Offer search loading indicator visible',await page.locator('#offer-loading').isVisible());
 await page.locator('.offer-rent').first().waitFor();await page.unroute('**/gpu/offers?*');
 const rent=page.locator('.offer-rent').first();await rent.locator('button').click();check('GPU rent requires explicit checkbox',await rent.locator('[name=confirm]').evaluate(e=>!e.checkValidity()));
 await rent.locator('[name=confirm]').check();await rent.locator('button').click();await page.waitForURL(/\/gpu\/instances\//);
 const rentalID=new URL(page.url()).pathname.split('/').at(-1);check('GPU rent reaches provisioning, no simulated ready',await page.locator('.status').innerText().then(t=>t.includes('provisioning')));await snap('1440-gpu-provisioning');
 await page.getByRole('button',{name:'Kiểm provisioning'}).click();await page.waitForURL(/msg=/);
 check('Real reconcile validates fake health and smoke',await page.locator('.status').innerText().then(t=>t.includes('ready')));
 const stop=page.locator('form[action$="/stop"]');await stop.locator('[name=confirm]').check();await stop.locator('button').click();await page.waitForURL(/msg=/);
 check('Stop keeps storage cost warning',await page.locator('.status').innerText().then(t=>t.includes('stopped'))&&await page.locator('main').innerText().then(t=>t.includes('disk')));
 await context.request.post(base+'/__fixture/tick',{data:{destroy_unknown:true}});
 const destroy=page.locator('form[action$="/destroy"]');await destroy.locator('[name=confirm]').check();await destroy.locator('button').click();await page.waitForURL(/msg=/);
 check('Unknown destroy remains unresolved',await page.locator('.status').innerText().then(t=>t.includes('destroy_unknown')));
 check('Unknown destroy has no resend control',await page.locator('form[action$="/destroy"]').count()===0);await snap('1440-gpu-unknown');
 await page.getByRole('button',{name:'Reconcile',exact:true}).click();await page.waitForURL(/msg=/);
 check('Reconcile keeps record when fake instance remains',await page.locator('.status').innerText().then(t=>t.includes('destroy_unknown')));
 // A separate known stop/destroy verifies the terminal lifecycle without resending an unknown operation.
 await goto('/gpu?tab=offers&search=1');const rent2=page.locator('.offer-rent').first();await rent2.locator('[name=confirm]').check();await rent2.locator('button').click();await page.waitForURL(/\/gpu\/instances\//);
 const destroy2=page.locator('form[action$="/destroy"]');await destroy2.locator('[name=confirm]').check();await destroy2.locator('button').click();await page.waitForURL(/msg=/);
 check('Destroy confirmed only after provider disappearance',await page.locator('.status').innerText().then(t=>t.includes('destroyed')));
 await goto('/comfyui/workflows');await page.locator('[data-open-dialog="import-workflow"]').click();await page.locator('#import-workflow [name=name]').fill('Browser imported workflow');
 const prompt={"1":{"class_type":"EmptyImage","inputs":{"width":64,"height":64,"batch_size":1,"color":0}},"2":{"class_type":"SaveImage","inputs":{"images":["1",0],"filename_prefix":"browser"}}};
 await page.locator('#import-workflow [name=file]').setInputFiles({name:'browser-workflow.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(prompt))});await page.locator('#import-workflow button[type=submit]').click();await page.waitForURL(/\/comfyui\/workflows\/wf_/);
 check('Imported workflow blocked pending real dependency review',await page.locator('main').innerText().then(t=>t.includes('needs_review'))&&await page.locator('form[action$="/runs"]').count()===0);
 await page.locator('form[action="/comfyui/workflows"] [name=file]').setInputFiles({name:'browser-workflow.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({...prompt,"3":{"class_type":"SaveImage","inputs":{"images":["1",0],"filename_prefix":"revision2"}}}))});
 await page.locator('form[action="/comfyui/workflows"] button').click();await page.waitForURL(/revision=2/);check('Workflow revision import creates revision 2',new URL(page.url()).searchParams.get('revision')==='2');
 const importedWorkflow=new URL(page.url()).pathname.split('/').at(-1);
 await context.request.post(base+'/__fixture/tick',{data:{approve_workflow:importedWorkflow}});
 await goto(`/comfyui/workflows/${importedWorkflow}?revision=2`);
 check('Imported revision approved through existing manifest CLI',await page.locator('.status').innerText().then(t=>t.includes('approved')));
 const runForm=page.locator('form[action$="/runs"]');await runForm.locator('[name=confirm]').check();await runForm.locator('button').click();await page.waitForURL(/\/comfyui\/runs\//);
 runURL=new URL(page.url()).pathname;check('Approved workflow starts a real stored run',await page.locator('.status').innerText().then(t=>/queued|running/.test(t)));await snap('1440-run-running');
 await goto(`/gpu/instances/${meta.ready}`);
 check('Destroy warns about run output before verification',await page.locator('[name=accept_unverified]').count()===1);
 const guardedDestroy=page.locator('form[action$="/destroy"]');await guardedDestroy.locator('[name=confirm]').check();await guardedDestroy.locator('button').click();await page.waitForURL(/err=/);
 check('Destroy refuses unverified output without separate acceptance',await page.locator('.flash.err[role=alert]').isVisible());
 const before=await (await context.request.get(base+'/__fixture')).json();await goto('/monitor?refresh=off');const after=await (await context.request.get(base+'/__fixture')).json();check('Monitor GET performs no provider calls or submissions',before.provider_calls===after.provider_calls&&before.posts===after.posts);
 check('Monitor links run and rental',await page.locator(`a[href="${runURL}"]`).count()>0&&await page.locator(`a[href="/gpu/instances/${meta.ready}"]`).count()>0);
 await context.request.post(base+'/__fixture/tick',{data:{finish_runs:true,fail_download:true}});await goto(runURL);
 for(let attempt=0;attempt<4&&await page.locator('form[action$="/refresh"]').count();attempt++){
  await Promise.all([page.waitForNavigation({waitUntil:'networkidle'}),page.locator('form[action$="/refresh"] button').click()]);
 }
 check('Failed output download stays output_failed',await page.locator('.status').innerText().then(t=>t.includes('output_failed')));
 await snap('1440-run-output-failed');
 await context.request.post(base+'/__fixture/tick',{data:{restore_download:true}});
 await page.locator('form[action$="/retry-download"] button').click();await page.waitForURL(/msg=/);
 check('Output succeeds only after verified download',await page.locator('.status').innerText().then(t=>t.includes('succeeded'))&&await page.locator('main').innerText().then(t=>t.includes('verified')));
 for(const width of [1440,768,390]){await page.setViewportSize({width,height:1000});await snap(`${width}-run-output`);check(`${width}px run output no overflow`,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
 const csrf=await context.request.post(base+`/gpu/instances/${meta.ready}/destroy`,{form:{confirm:'on'}});check('CSRF guard rejects tokenless destructive request',csrf.status()===403);
 await page.setViewportSize({width:390,height:844});await goto('/');await page.locator('[data-menu-toggle]').click();check('Mobile drawer opens and focuses close control',await page.locator('[data-menu-toggle]').getAttribute('aria-expanded')==='true'&&await page.locator('.nav-close').evaluate(e=>e===document.activeElement));
 await snap('390-navigation-open');await page.keyboard.press('Shift+Tab');check('Mobile drawer traps focus',await page.locator('.app-sidebar a').last().evaluate(e=>e===document.activeElement));await page.keyboard.press('Escape');check('Mobile drawer Escape restores menu focus',await page.locator('[data-menu-toggle]').evaluate(e=>e===document.activeElement));
 await page.locator('.mobile-nav a[href="/gpu"]').click();await page.waitForURL(base+'/gpu');check('Mobile bottom navigation works',await page.locator('.mobile-nav a[aria-current=page]').innerText().then(t=>t==='GPU'));
 await goto('/gpu?tab=offers');await page.route('**/gpu/offers?*',route=>route.abort());await page.locator('#find-offer button').click();await page.locator('#offers [role=alert]').waitFor();check('Offer network error has visible retry guidance',await page.locator('#offers [role=alert]').isVisible());await snap('390-offer-network-error');await page.unroute('**/gpu/offers?*');
 await goto('/gpu?tab=offers&search=1&min_gpu_ram_gb=999');check('Offer empty state',await page.getByText('Không có offer phù hợp.').isVisible());await snap('390-offer-empty');
 // Check graceful navigation and creation entry without the enhancement script.
 const nojs=await browser.newContext({javaScriptEnabled:false,viewport:{width:390,height:844}});const plain=await nojs.newPage();await plain.goto(base+'/');check('Navigation works without JavaScript',await plain.locator('.app-sidebar').isVisible());await plain.locator('[data-open-dialog="new-task"]').click();check('Create form available without JavaScript',await plain.locator('#brief_file').isVisible());await nojs.close();
 check('No browser JavaScript exceptions',results.pageErrors.length===0);
 results.checks=[...new Set(results.checks)];results.result='passed';fs.writeFileSync(path.join(evidence,'browser-results.json'),JSON.stringify(results,null,2));
 console.log(JSON.stringify({result:results.result,checks:results.checks.length,viewports:results.viewports.length}));
 await browser.close();
})().catch(async error=>{results.result='failed';results.error=error.stack;fs.writeFileSync(path.join(evidence,'browser-results.json'),JSON.stringify(results,null,2));console.error(error);await browser?.close();process.exit(1)});
