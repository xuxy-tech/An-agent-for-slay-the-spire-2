const {chromium} = require('../.tools/browser/node_modules/playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  });
  try {
    for (const width of [390, 1440]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      const errors = [];
      page.on('pageerror', error => errors.push(String(error)));
      const base = {client_before: {screen: 'COMBAT', turn: 1, run: {floor: 19, current_hp: 42, gold: 135}},
        status: 'completed', verification: 'COVERED_FIELDS_MATCH'};
      const candidates = Array.from({length: 55}, (_, i) => ({action: {card_id: `CARD_${i}`}, score: i}));
      const actions = [
        {...base, sequence: 1, decision_telemetry: {decision_reason: 'highest_score',
          decision_audit: {ran_search: true, turn_space: {bounded_tree_complete: true}}}},
        {...base, sequence: 2, decision_telemetry: {decision_reason: 'highest_score', root_candidates: candidates,
          decision_audit: {ran_search: true, turn_space: {bounded_tree_complete: false, beam_width_pruned: 5}}}},
        {...base, sequence: 3, decision_telemetry: {fell_back: true, decision_reason: 'fallback'}},
        {...base, sequence: 4, decision_telemetry: {decision_audit: {ran_search: true, turn_space: {}}}},
        {...base, sequence: 5, decision_telemetry: {reused_plan: true}},
        ...Array.from({length: 55}, (_, i) => ({...base, sequence: 6+i,
          decision_telemetry: {decision_reason: 'forced_action'}})),
      ];
      const report = {status: 'RUNNING', identity: {run_id: 'QA', character: 'IRONCLAD'},
        completed_combat_count: 1, actions};
      const status = {runtime_version: 'history-20261002-2', mode: 'running', worker_running: true,
        session_id: 'live_dashboard/current', client: {connected: true, screen: 'COMBAT',
          run_id: 'QA', floor: 19, turn: 1, hp: 42, max_hp: 80, gold: 135},
        worker: {}, run_plan: {mode: 'single', target: 0, completed: 0, state: 'idle'}, config: {}};
      await page.route('**/api/status', route => route.fulfill({json: status}));
      await page.route('**/api/session?id=*', route => route.fulfill({json: report}));
      await page.goto(process.env.DASHBOARD_URL || 'http://127.0.0.1:8879/');
      await page.waitForFunction(() => document.querySelectorAll('#timeline tr').length === 60);
      const legend = await page.locator('#search-outcome-legend').innerText();
      for (const label of ['完全搜索 1 · 33.3%', '不完全搜索 1 · 33.3%', 'Fallback 1 · 33.3%'])
        assert.ok(legend.includes(label), label);
      assert.ok((await page.locator('#search-outcome-note').innerText()).includes('未判定 1 · 计划复用 1 · 强制动作 55'));
      await page.locator('#timeline tr').nth(1).click();
      assert.equal(await page.locator('#search-status').innerText(), '不完全搜索');
      assert.equal(await page.locator('#search-group').getAttribute('open'), null);
      await page.locator('#search-group summary').click();
      assert.ok((await page.locator('#search-quality').innerText()).includes('Beam 裁剪 5 条'));
      assert.equal(await page.locator('.candidate').count(), 55);
      if (width === 1440) {
        const layout = await page.evaluate(() => {
          const a = document.querySelector('.timeline').getBoundingClientRect();
          const b = document.querySelector('.inspector').getBoundingClientRect();
          const left = document.querySelector('.timeline .table-scroll');
          const right = document.querySelector('.inspector');
          return {equal: Math.abs(a.height-b.height)<2, leftScroll: left.scrollHeight>left.clientHeight,
            rightScroll: right.scrollHeight>right.clientHeight};
        });
        assert.deepEqual(layout, {equal: true, leftScroll: true, rightScroll: true});
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      if (process.env.DASHBOARD_SCREENSHOT_DIR)
        await page.screenshot({path: `${process.env.DASHBOARD_SCREENSHOT_DIR}/decisions-${width}.png`, fullPage: true});
      await page.close();
    }
  } finally {
    await browser.close();
  }
  console.log('dashboard decision layout checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
