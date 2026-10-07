const {chromium} = require('../.tools/browser/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const initial = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  const browser = await chromium.launch({executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
  try {
    for(const width of [390,1440]) {
      const catalog = structuredClone(initial);
      delete catalog.models[0].model.ability_config; // Existing v4 models have implicit defaults.
      const page = await browser.newPage({viewport:{width,height:1000}});
      const errors=[];let saved=null;
      page.on('pageerror', e=>errors.push(String(e)));
      await page.route('**/*', async route=>{
        const url=new URL(route.request().url());
        if(url.pathname==='/api/comparison/catalog')return route.fulfill({json:catalog});
        if(url.pathname==='/api/comparison/model'){
          saved=JSON.parse(route.request().postData());
          const row={id:'saved:test',name:saved.name,source:'saved',model:{...catalog.models[0].model,weights:saved.weights,ability_config:saved.ability_config}};
          catalog.models.push(row);return route.fulfill({json:row});
        }
        const basename=path.basename(url.pathname);
        if(url.pathname==='/human-capture')return route.fulfill({path:path.join(__dirname,'../ui/human_capture.html'),contentType:'text/html'});
        if(basename==='combat_comparison.js'||basename.endsWith('.css')){
          const file=path.join(__dirname,'../ui',basename);
          if(fs.existsSync(file))return route.fulfill({path:file,contentType:basename.endsWith('.js')?'application/javascript':'text/css'});
        }
        return route.fulfill({body:'',contentType:'application/javascript'});
      });
      await page.goto('http://ability.test/human-capture');
      const realization=page.locator('#cmp-ability-realization'), discount=page.locator('#cmp-ability-discount');
      await realization.waitFor();
      assert.equal(await realization.inputValue(),'0.35');
      assert.equal(await discount.inputValue(),'0.85');
      assert.equal(await page.locator('#cmp-ability-settings input[type=number]').count(),2);
      assert.equal(await page.locator('#cmp-ability-max_horizon').isVisible(),false);
      await realization.fill('0.55');await discount.fill('0.92');
      await page.locator('#cmp-ability-advanced summary').click();
      await page.locator('#cmp-ability-max_horizon').fill('4');
      await page.locator('#cmp-ability-damage_per_turn').fill('24');
      await page.locator('#cmp-random').click();
      assert.equal(await realization.inputValue(),'0.55');
      assert.equal(await page.locator('#cmp-ability-max_horizon').inputValue(),'4');
      await page.locator('#cmp-model-name').fill('Manual ability settings');
      await page.locator('#cmp-save-model').click();
      await page.waitForFunction(()=>document.querySelector('#cmp-model-b').value==='saved:test');
      const expected={realization:0.55,discount:0.92,max_horizon:4,damage_per_turn:24};
      assert.deepEqual(saved.ability_config,expected);
      assert.equal(Object.keys(saved.weights).length,7);
      await page.reload();
      await page.locator('#cmp-editor-source').selectOption('saved:test');
      assert.equal(await realization.inputValue(),'0.55');assert.equal(await discount.inputValue(),'0.92');
      await page.locator('#cmp-ability-advanced summary').click();
      assert.equal(await page.locator('#cmp-ability-max_horizon').inputValue(),'4');
      assert.equal(await page.locator('#cmp-ability-damage_per_turn').inputValue(),'24');
      await discount.fill('1.1');saved=null;
      await page.locator('#cmp-save-model').click();
      await page.waitForFunction(()=>!document.querySelector('#cmp-error').hidden);
      assert.equal(saved,null);assert.deepEqual(errors,[]);
      const overflow=await page.evaluate(()=>document.body.scrollWidth-innerWidth);
      assert.ok(overflow<=1,`horizontal overflow ${overflow}`);
      await page.close();
    }
    console.log('ability settings: defaults, normal/advanced, save/reload, manual-only, validation, mobile/desktop PASS');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
