const {chromium} = require('../.tools/browser/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  });
  try {
    for (const width of [390, 1440]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      const errors = [];page.on('pageerror', error => errors.push(String(error)));
      const weights = {hp_change: 42.5, hp_risk: 30, enemy_hp_removed: 60, enemy_kills: 8,
        next_threat: 30, strength_potential: 10, self_damage_scaling: 10, block_stock: 5,
        hand_energy_opportunity: 2, potion_cost: 8};
      const model = {kind:'linear_preference', feature_version:'combat-preference-2',
        observation_mode:'hand_and_stage', trained:false, weights};
      const catalog = {features:{features:Object.keys(weights).map(key=>({key,label:key,definition:key}))},
        models:[{id:'active',name:'当前实战模型',source:'active',model},
          {id:'trained:test',name:'训练模型',source:'trained',model:{...model,weights:{...weights,hp_change:45}}}],
        snapshots:[{id:'sample_1',floor:2,turn:1,context:{act:1,encounter_id:'SEAPUNK'}}],
        jobs:[],active_job:null};
      let saved = null, started = null;
      await page.route('**/api/comparison/catalog', route=>route.fulfill({json:catalog}));
      await page.route('**/api/comparison/model', route=>{
        const body=JSON.parse(route.request().postData());saved=body;
        const row={id:'saved:test',name:body.name,source:'saved',model:{...model,weights:body.weights}};
        catalog.models.push(row);route.fulfill({json:row});
      });
      await page.route('**/api/comparison/model/rename', route=>{
        const body=JSON.parse(route.request().postData());
        const row=catalog.models.find(item=>item.id===body.id);row.name=body.name;
        route.fulfill({json:{id:body.id,name:body.name}});
      });
      await page.route('**/api/comparison/start', route=>{
        started=JSON.parse(route.request().postData());catalog.jobs=[{id:'job_1',status:'completed',completed:1,total:1}];
        route.fulfill({json:{id:'job_1',status:'starting',completed:0,total:1,counts:{},pairs:[]}});
      });
      await page.route('**/api/comparison/job?*', route=>route.fulfill({json:{id:'job_1',status:'completed',
        completed:1,total:1,counts:{resource_tradeoff:1},pairs:[{snapshot_id:'sample_1',status:'VALID',
          context:{floor:2,encounter_id:'SEAPUNK'},comparison:{category:'resource_tradeoff',hp_delta:5,potion_delta:-1},first_divergence:2}]}}));
      const pair = {snapshot_id:'sample_1',status:'VALID',
        comparison:{category:'resource_tradeoff',hp_delta:5,potion_delta:-1},first_divergence:2,
        a:{terminal:'combat_reward',outcome:{hp:50,block:0,potions:[],enemies:[]},actions:[{action:'play_card',after:{hp:50,block:0,potions:[],enemies:[]}}]},
        b:{terminal:'combat_reward',outcome:{hp:45,block:0,potions:['A'],enemies:[]},actions:[{action:'end_turn',after:{hp:45,block:0,potions:['A'],enemies:[]}}]}};
      await page.route('**/api/comparison/pair?*', route=>route.fulfill({json:pair}));
      await page.goto(process.env.DASHBOARD_URL || 'http://127.0.0.1:8879/human-capture');
      await page.waitForSelector('#cmp-weights input[type=number]');
      assert.equal(await page.locator('#cmp-weights input[type=number]').count(),10);
      await page.locator('#cmp-model-name').fill('测试力量');
      await page.locator('#cmp-weights input[type=number]').first().fill('55');
      await page.locator('#cmp-save-model').click();
      await page.waitForFunction(()=>document.querySelector('#cmp-model-b').value==='saved:test');
      assert.equal(saved.weights.hp_change,55);
      await page.locator('#cmp-model-name').fill('保血版本');
      await page.locator('#cmp-rename-model').click();
      await page.waitForFunction(()=>document.querySelector('#cmp-editor-source').selectedOptions[0].textContent==='保血版本');
      await page.locator('#cmp-start').click();
      await page.waitForFunction(()=>document.querySelector('#cmp-summary').textContent.includes('资源取舍'));
      assert.deepEqual(started.snapshots,['sample_1']);
      assert.equal(await page.locator('.comparison-scatter circle').count(),1);
      await page.locator('.comparison-scatter circle').click();
      await page.waitForFunction(()=>document.querySelector('#cmp-detail').textContent.includes('首次动作分叉'));
      assert.equal(errors.length,0,errors.join('\n'));
      const widthInfo=await page.evaluate(()=>({body:document.body.scrollWidth,viewport:innerWidth}));
      assert.ok(widthInfo.body<=widthInfo.viewport+1,JSON.stringify(widthInfo));
      if(process.env.COMPARISON_SCREENSHOT_DIR){fs.mkdirSync(process.env.COMPARISON_SCREENSHOT_DIR,{recursive:true});
        await page.locator('.comparison-area').screenshot({path:process.env.COMPARISON_SCREENSHOT_DIR+'/comparison-'+width+'.png'});}
      await page.close();
    }
    console.log('comparison UI passed');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
