const {chromium}=require('../.tools/browser/node_modules/playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 const base=process.env.SANDBOX_URL||'http://127.0.0.1:8877';const errors=[];
 page.on('pageerror',e=>errors.push(String(e)));
 async function waitJob(old,requestId){
  const deadline=Date.now()+60000;
  while(true){
   const s=await(await page.request.get(base+'/api/sandbox/state')).json();
   if(s.job?.status==='failed')throw new Error(s.job.error);
   if(s.active&&!s.busy&&s.revision>old&&s.job?.status==='completed'&&(!requestId||s.job.id===requestId))break;
   assert.ok(Date.now()<deadline,'Job timed out');await new Promise(resolve=>setTimeout(resolve,150));
  }
  await page.waitForFunction(()=>document.getElementById('job-status').textContent==='就绪');
 }
 async function command(name,options={}){
  const s=await(await page.request.get(base+'/api/sandbox/state')).json();
  const requestId=require('crypto').randomUUID();
  const response=await page.request.post(base+'/api/sandbox/command',{data:{command:name,options,revision:s.revision,request_id:requestId}});
  assert.equal(response.status(),202);await waitJob(s.revision,requestId);
 }
 try{
  await page.goto(base+'/sandbox');
  await page.waitForSelector('.collection-toolbar');
  const initial=await(await page.request.get(base+'/api/sandbox/state')).json();
  if(initial.busy)await waitJob(initial.revision);
  await command('generate',{seed:'browser-shared-score',stage:'mid',energy:4,enemy_hp:150,hand:['RUPTURE','HEMOKINESIS','SHRUG_IT_OFF']});
  let before=await(await page.request.get(base+'/api/sandbox/state')).json();
  await page.locator('#agent-hint').click();await waitJob(before.revision);
  await page.waitForSelector('#hint-result:not([hidden])');
  let after=await(await page.request.get(base+'/api/sandbox/state')).json();
  assert.deepEqual(after.hand,before.hand);assert.deepEqual(after.player,before.player);assert.equal(after.history.length,0);
  assert.equal(after.agent_hint.verification,'PASS');assert.equal(after.agent_hint.score_matches,true);
  assert.equal(after.agent_hint.scorer.weights_sha256,after.scorer.weights_sha256);
  assert.equal(await page.locator('#mode-badge').textContent(),'已看提示');
  assert.match(await page.locator('#active-scorer').textContent(),/combat-preference-4/);
  await page.locator('#hint-result summary').click();
  await page.screenshot({path:'logs/sandbox_qa/scoring-desktop.png',fullPage:true});
  const action=after.actions.find(a=>a.metadata?.card_id==='RUPTURE');await command('action',{action_id:action.id});
  await page.waitForFunction(()=>document.getElementById('hint-first').textContent.startsWith('此前局面建议'));
  before=await(await page.request.get(base+'/api/sandbox/state')).json();
  await page.locator('#agent-hint').click();await waitJob(before.revision);
  after=await(await page.request.get(base+'/api/sandbox/state')).json();
  assert.equal(after.agent_hint.prefix_length,1);assert.equal(after.agent_hint.verification,'PASS');
  assert.equal(after.history.length,1);
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'logs/sandbox_qa/scoring-mobile.png',fullPage:true});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await command('demonstrate',{confidence:'high',note:'automated QA, isolated store'});
  const exported=await(await page.request.get(base+'/api/sandbox/export')).json();
  assert.equal(exported.demonstrations.at(-1).assisted,true);
  assert.deepEqual(errors,[]);console.log(JSON.stringify({status:'passed',score:after.agent_hint.score,verification:after.agent_hint.verification}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
