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
      let storageReads = 0;
      page.on('pageerror', error => errors.push(String(error)));
      page.on('request', request => {
        if (new URL(request.url()).pathname === '/api/storage') storageReads++;
      });
      await page.goto(process.env.DASHBOARD_URL || 'http://127.0.0.1:8766/');
      assert.equal(storageReads, 0);
      await page.locator('#storage-panel summary').click();
      await page.waitForFunction(() => document.querySelector('#storage-summary')?.textContent.includes('未归档'));
      assert.ok(storageReads >= 1);
      assert.equal(errors.length, 0, errors.join('\n'));
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      await page.close();
    }
  } finally {
    await browser.close();
  }
  console.log('dashboard storage browser checks passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
