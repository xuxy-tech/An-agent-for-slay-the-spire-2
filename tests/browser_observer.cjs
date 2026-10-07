const { chromium } = require('../.tools/browser/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const output = path.resolve('logs/observer_qa');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    const base = process.env.OBSERVER_URL || 'http://127.0.0.1:8876';
    const sessions = await (await page.request.get(base + '/api/sessions')).json();
    assert.ok(sessions.sessions.length > 0);
    const oldId = sessions.sessions.find(s => s.id.includes('continuous')).id;
    const real = await (await page.request.get(base + '/api/session?id=' + encodeURIComponent(oldId))).json();
    assert.equal(real.status, 'PASS');
    assert.equal((await page.request.get(base + '/api/session?id=../../README.md')).status(), 404);
    assert.equal((await page.request.get(base + '/api/download?id=' + encodeURIComponent(oldId))).status(), 200);
    const template = real.combats[0].actions.find(a => a.root_candidates?.length);
    const before = {screen:'COMBAT',turn:2,run:{current_hp:64,max_hp:80,gold:120,floor:3}};
    const report = {
      schema_version:2,status:'RUNNING',identity:{run_id:'QA-OBSERVATION',character:'IRONCLAD'},
      config:{draft_policy:'heuristic',depth:5,search_budget_ms:1200},completed_combat_count:1,
      parity_checkpoints:[{status:'PASS'}],
      actions:[
        {sequence:1,client_action:'play_card',client_params:{card_index:1},status:'completed',verification:'COVERED_FIELDS_MATCH',decision_telemetry:{...template,decision_ms:1234.5},action_wall_ms:45.2,visible_delay_ms:700,settle_ms:55,shadow_ms:2,compare_ms:1,client_before:before,client_after:{...before,run:{...before.run,current_hp:62}}},
        {sequence:2,client_action:'end_turn',client_params:{},status:'pending',verification:'PENDING',decision_telemetry:{policy:'bounded_combat_search',reused_plan:true,decision_ms:0.8},client_before:before}
      ]
    };
    const status={mode:'paused',message:'等待执行许可',worker_running:true,preparing:false,session_id:'qa',worker:{phase:'awaiting_permission'},client:{connected:true,screen:'COMBAT',floor:3,turn:2,hp:64,max_hp:80},capabilities:{active_run_return:false}};
    const commands=[];
    await page.route('**/api/**',async route=>{
      const url=new URL(route.request().url());let body;
      if(url.pathname==='/api/status')body=status;
      else if(url.pathname==='/api/sessions')body={sessions:[{id:'history',status:'PASS'}]};
      else if(url.pathname==='/api/session')body=url.searchParams.get('id')==='history'?real:report;
      else if(url.pathname==='/api/command'){
        const command=route.request().postDataJSON().command;commands.push(command);
        if(command==='step'){report.actions[1].status='completed';report.actions[1].verification='COVERED_FIELDS_MATCH';report.actions.push({...report.actions[1],sequence:3,status:'pending',verification:'PENDING'});}
        if(command==='resume')status.mode='running';
        if(command==='pause')status.mode='paused';
        body=status;
      }else return route.continue();
      await route.fulfill({json:body});
    });
    await page.goto(base);
    await page.waitForFunction(()=>document.querySelectorAll('#timeline tr').length===2);
    assert.ok(await page.locator('#step').isEnabled());
    await page.locator('#timeline tr').first().click();
    assert.ok(await page.locator('.candidate').count()>0);
    await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
    const pixels=await page.locator('#search-outcome-pie').evaluate(canvas=>{const a=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;let colored=0;for(let i=0;i<a.length;i+=4)if(a[i+3]>0)colored++;return colored;});
    assert.ok(pixels>100);
    await page.locator('#step').click();
    await page.waitForFunction(()=>document.querySelectorAll('#timeline tr').length===3);
    assert.deepEqual(commands,['step']);
    await page.locator('#resume').click();
    await page.waitForFunction(()=>!document.getElementById('pause').disabled);
    await page.locator('#pause').click();
    await page.locator('#sessions').selectOption('history');
    await page.waitForFunction(()=>document.querySelectorAll('#timeline tr').length===29);
    assert.ok(await page.locator('#step').isEnabled());
    assert.ok(await page.locator('#start').isDisabled());
    await page.locator('#live').click();
    await page.waitForFunction(()=>document.querySelectorAll('#timeline tr').length===3);
    const failed={...report,status:'FAIL',error:'Checkpoint mismatch',parity_checkpoints:[{status:'FAIL'}],actions:[{...report.actions[0],verification:'FAIL',checkpoint:{differences:[{path:'player.energy',client:3,headless:2}]}}]};
    await page.locator('#file').setInputFiles({name:'failed.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(failed))});
    await page.waitForFunction(()=>document.getElementById('differences').textContent.includes('player.energy'));
    assert.ok(await page.locator('#start').isDisabled());
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    assert.equal(await page.locator('svg.lucide').count()>10,true);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:'ok',real_history_actions:29,commands,canvas_pixels:pixels,screenshots:output}));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
