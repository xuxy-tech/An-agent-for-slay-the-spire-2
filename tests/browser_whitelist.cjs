const {chromium}=require('../.tools/browser/node_modules/playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}); const base='http://127.0.0.1:8877';
 try {
  await page.goto(base+'/');
  await page.waitForSelector('#profile-toggle');
  await page.locator('#profile-toggle').click();
  assert.equal(await page.locator('#profile-body').isVisible(),false);
  await page.reload();assert.equal(await page.locator('#profile-body').isVisible(),false);
  await page.locator('#profile-toggle').click();assert.equal(await page.locator('#profile-body').isVisible(),true);
  await page.screenshot({path:'logs/sandbox_qa/profile-fold.png',fullPage:true});
  await page.goto(base+'/sandbox');
  await page.waitForFunction(async()=>{const s=await(await fetch('/api/sandbox/state')).json();return s.active&&!s.busy;},null,{timeout:60000});
  const catalog=await(await page.request.get(base+'/api/sandbox/catalog')).json();
  assert.ok(catalog.excluded_scenarios>0);
  const allowed=new Set(catalog.whitelist.map(c=>c.id));
  const state=await(await page.request.get(base+'/api/sandbox/state')).json();
  assert.ok(state.hand.every(c=>allowed.has(c.id)));
  await page.locator('#new-scene').click();await page.locator('#scene-form summary').click();
  assert.equal(await page.locator('[data-add="STRIKE_IRONCLAD"]').count(),0);
  await page.locator('#close-scene').click();
  await page.screenshot({path:'logs/sandbox_qa/whitelist.png',fullPage:true});
  console.log(JSON.stringify({passed:true,excluded:catalog.excluded_scenarios,cards:state.hand.length}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
