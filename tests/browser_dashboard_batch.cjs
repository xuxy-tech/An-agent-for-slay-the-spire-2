const {chromium} = require('../.tools/browser/node_modules/playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
  });
  try {
    for (const width of [390, 1440]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      page.on('pageerror', error => console.error('page error:', error));
      const commands = [];
      const status = {
        runtime_version: 'strategy-snapshot-20261003-1', mode: 'running', worker_running: true,
        active_scoring_model_id: 'trained:combat_preference_v4_20260921',
        session_id: 'live_dashboard/current', client: {connected: true, run_id: 'CURRENT', floor: 19,
          hp: 42, max_hp: 80, gold: 135, turn: 3, screen: 'COMBAT',
          interaction: {kind: 'combat_action', scene: 'COMBAT', stage: 'ready'}},
        worker: {phase: 'combat_decision'}, run_plan: {mode: 'single', target: 0, completed: 0, state: 'idle'},
        config: {depth: 12, search_ms: 20000, worker_mode: 'adaptive', max_workers: 2,
          delay_ms: 200, stall_seconds: 900},
      };
      const history = [{id: 'live_dashboard/old', created_at: 100, finished_at: 101, status: 'DEFEAT', floor: 17,
        deck_size: 13, seed: 'SEED'}, ...Array.from({length: 44}, (_, i) => ({
          id: `live_dashboard/older-${i}`, created_at: 90-i, finished_at: 91-i,
          status: 'DEFEAT', floor: 8, deck_size: 12,
        }))];
      await page.route('**/api/status', route => route.fulfill({json: status}));
      await page.route('**/api/comparison/models', route => route.fulfill({json:{models:[
        {id:'active',name:'当前实战模型',source:'active'},
        {id:'trained:combat_preference_v4_20260921',name:'保血版本 B',source:'trained'}]}}));
      let historyRequests = 0, detailRequests = 0;
      await page.route('**/api/sessions?*', route => {historyRequests++;
        const url = new URL(route.request().url()), offset = Number(url.searchParams.get('offset')),
          limit = Number(url.searchParams.get('limit'));
        if (limit) detailRequests++;
        route.fulfill({json: {
          sessions: history.slice(offset, offset+limit), total: history.length,
          next_offset: offset+Math.min(limit,history.length-offset), has_more: offset+limit < history.length,
          loading: false, summary: {requested: 10, actual: 10,
            counts: {victory: 0, defeat: 10, stopped: 0, error: 0}, errors: [],
            defeat: {count: 10, average_floor: 8.9, act1_passed: 0, act2_passed: 0, top: history.slice(0, 3)}},
        }});
      });
      await page.route('**/api/session?id=*', route => route.fulfill({json: {
        status: 'DEFEAT', identity: {run_id: 'OLD', character: 'IRONCLAD'}, actions: [],
        terminal_client: {run: {floor: 17, current_hp: 1, max_hp: 80, gold: 0}},
      }}));
      await page.route('**/api/command', async route => {
        const body = JSON.parse(route.request().postData());
        commands.push(body);
        if (body.command === 'set_run_plan') {
          status.run_plan = {mode: body.options.run_mode, target: body.options.batch_target,
            completed: 0, state: 'running'};
          status.auto_start = true;
        }
        await route.fulfill({json: status});
      });
      await page.goto(process.env.DASHBOARD_URL || 'http://127.0.0.1:8879/');
      await page.waitForTimeout(1200);
      assert.equal(historyRequests, 0);
      await page.locator('#history-toggle').click();
      await page.waitForFunction(() => document.querySelector('#history-summary-status')?.textContent.includes('实际纳入'));
      assert.equal(detailRequests, 0);
      await page.locator('#history-details-toggle').click();
      await page.waitForSelector('#run-history tr');
      assert.equal(await page.locator('#history-breakdown').innerText().then(v => v.includes('种子 SEED')), true);
      assert.equal(await page.locator('#run-history tr').count(), 20);
      await page.locator('#history-scroll').evaluate(el => {el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll'));});
      await page.waitForFunction(() => document.querySelectorAll('#run-history tr').length === 40);
      await page.locator('#history-scroll').evaluate(el => {el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll'));});
      await page.waitForFunction(() => document.querySelectorAll('#run-history tr').length === 45);
      await page.locator('#run-history tr').first().click();
      await page.waitForFunction(() => document.querySelector('#view-context')?.textContent.includes('历史记录'));
      assert.equal(await page.locator('#sessions').inputValue(), 'live_dashboard/old');
      assert.equal(await page.locator('#position').innerText(), '19 / 3');
      assert.equal(await page.locator('#hp').innerText(), '42 / 80');
      assert.equal(await page.locator('#gold').innerText(), '135');
      assert.match(await page.locator('#active-strategy').innerText(),/保血版本 B/);
      assert.equal(await page.locator('#pause').isDisabled(), false);
      await page.locator('#pause').click();
      assert.equal(commands.at(-1).command, 'pause');
      assert.equal(commands.at(-1).options.expected_session_id, 'live_dashboard/current');
      await page.locator('#run-mode').selectOption('batch');
      await page.locator('#batch-target').fill('4');
      await page.locator('#apply-plan').click();
      assert.equal(commands.at(-1).command, 'set_run_plan');
      assert.equal(commands.at(-1).options.batch_target, 4);
      status.mode='idle';status.worker_running=false;status.run_plan={mode:'single',target:0,completed:0,state:'idle'};
      await page.reload();
      await page.locator('#scoring-model').selectOption('trained:combat_preference_v4_20260921');
      await page.locator('.run-settings summary').click();
      await page.locator('#save-config').click();
      assert.equal(commands.at(-1).options.scoring_model_id,'trained:combat_preference_v4_20260921');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.DASHBOARD_SCREENSHOT_DIR) {
        await page.screenshot({path: `${process.env.DASHBOARD_SCREENSHOT_DIR}/batch-${width}.png`, fullPage: true});
      }
      await page.close();
    }
  } finally {
    await browser.close();
  }
  console.log('dashboard batch browser checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
