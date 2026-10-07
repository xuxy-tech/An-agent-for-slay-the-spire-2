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
      let historyRequests = 0, detailRequests = 0;
      await page.route('**/api/comparison/models', route => route.fulfill({json: {
        models: [{id: 'trained:b', name: 'B 版策略'}],
      }}));
      await page.route('**/api/status', route => route.fulfill({json: {
        runtime_version: 'history-20261002-2', mode: 'idle', client: {connected: false},
        config: {}, run_plan: {mode: 'single', target: 0, completed: 0, state: 'idle'},
      }}));
      await page.route('**/api/sessions?*', route => {
        historyRequests++;
        const limit = Number(new URL(route.request().url()).searchParams.get('limit'));
        if (limit) detailRequests++;
        route.fulfill({json: {sessions: limit ? [{id: 'live_dashboard/old', created_at: 1,
          finished_at: 2, status: 'DEFEAT', floor: 8, deck_size: 12,
          scoring_model_id: 'trained:b'}] : [], total: 1,
          next_offset: limit ? 1 : 0, has_more: !limit, loading: false,
          summary: {requested: 10, actual: 1,
            counts: {victory: 0, defeat: 1, stopped: 0, error: 0}, errors: [],
            defeat: {count: 1, average_floor: 8, act1_passed: 0, act2_passed: 0, top: []}}}});
      });
      await page.goto(process.env.DASHBOARD_URL || 'http://127.0.0.1:8765/');
      const toggle = page.locator('#history-toggle');
      assert.equal(historyRequests, 0);
      await toggle.click();
      await page.waitForFunction(() => document.querySelector('#history-summary-status')?.textContent.includes('实际纳入'));
      assert.equal(detailRequests, 0);
      await page.locator('#history-details-toggle').click();
      await page.waitForSelector('.history-delete');
      assert.match(await page.locator('#run-history').innerText(), /B 版策略/);
      assert.equal(detailRequests, 1);
      assert.equal(await page.locator('#history-body').isVisible(), true);
      await toggle.click();
      assert.equal(await page.locator('#history-body').isVisible(), false);
      await page.reload();
      await page.waitForFunction(() => document.querySelector('#history-toggle')?.getAttribute('aria-expanded') === 'false');
      assert.equal(await page.locator('#history-body').isVisible(), false);
      assert.equal(detailRequests, 1);
      await toggle.click();
      assert.equal(await page.locator('#history-body').isVisible(), true);
      assert.equal(await page.locator('#history-details-body').isVisible(), false);
      await page.locator('#history-details-toggle').click();
      await page.waitForSelector('.history-delete');
      assert.ok(await page.locator('.history-delete').count());
      const count = await page.locator('.history-delete').count();
      page.once('dialog', dialog => dialog.dismiss());
      await page.locator('.history-delete').first().click();
      assert.equal(await page.locator('.history-delete').count(), count);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.close();
    }
  } finally {
    await browser.close();
  }
  console.log('dashboard history browser checks passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
