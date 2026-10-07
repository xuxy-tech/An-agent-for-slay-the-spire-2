const {chromium}=require('../.tools/browser/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1050}});
 const base=process.env.SANDBOX_URL||'http://127.0.0.1:8877';
 const errors=[];page.on('pageerror',e=>errors.push(String(e)));
 async function act(selector){
   const old=(await(await page.request.get(base+'/api/sandbox/state')).json()).revision;
   await page.locator(selector).click();
   await page.waitForFunction(async({base,old})=>{const s=await(await fetch(base+'/api/sandbox/state')).json();if(s.job?.status==='failed')throw new Error(s.job.error);return !s.busy&&s.revision>old;},{base,old},{timeout:60000});
   await page.waitForFunction(()=>document.getElementById('job-status').textContent==='就绪');
 }
 try{
  await page.goto(base+'/sandbox');
  await page.waitForSelector('.scene-item');
  if (await page.locator('#resume-draft').isVisible()) await page.locator('#resume-draft').click();
  await page.waitForFunction(()=>document.querySelectorAll('.card-play').length>0&&document.getElementById('job-status').textContent==='就绪',null,{timeout:30000});
  assert.equal(await page.locator('#comparison-panel').isVisible(),false);
  await page.locator('#batch-position').selectOption('mid');
  await act('#next-scene');
  let s=await(await page.request.get(base+'/api/sandbox/state')).json();
  assert.equal(s.turn_context.position,'mid');assert.ok(s.root_turn_history.length>0);assert.equal(s.history.length,0);
  await page.locator('#prelude-summary').click();
  fs.mkdirSync('logs/sandbox_qa',{recursive:true});
  await page.screenshot({path:'logs/sandbox_qa/collection-desktop.png',fullPage:true});
  await act('#end-turn');
  await act('#save-next');
  s=await(await page.request.get(base+'/api/sandbox/state')).json();
  assert.ok(s.collection.demonstrations>=1);assert.ok(s.last_submission.saved);
  assert.match(await page.locator('#submission-receipt').textContent(),/已保存/);
  await act('#skip-scene');
  s=await(await page.request.get(base+'/api/sandbox/state')).json();assert.ok(s.collection.skipped>=1);
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'logs/sandbox_qa/collection-mobile.png',fullPage:true});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  const guide=await page.request.get(base+'/sandbox/guide');assert.equal(guide.status(),200);assert.match(await guide.text(),/保存并下一题/);
  const data=await(await page.request.get(base+'/api/sandbox/export')).json();
  assert.ok(data.demonstrations.length>0);assert.equal(data.labels.length,0);assert.equal(data.plans.length,0);
  assert.ok(data.demonstrations[0].verified);assert.equal(data.demonstrations[0].annotation_kind,'decision_demonstration');
  assert.deepEqual(errors,[]);console.log(JSON.stringify({status:'passed',demonstrations:data.demonstrations.length,skips:data.skips.length}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
