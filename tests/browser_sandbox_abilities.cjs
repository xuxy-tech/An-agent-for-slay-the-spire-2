// Uses a real v4 scorer response supplied as argv[2]; no live game required.
const {chromium} = require('../.tools/browser/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/sandbox/state') return route.fulfill({json: fixture});
      if (url.pathname === '/api/sandbox/catalog') return route.fulfill({json: {
        scenarios: [], cards: [], whitelist: [], encounters: [], draft_available: false,
      }});
      const files = {'/': 'sandbox.html', '/assets/sandbox.js': 'sandbox.js',
        '/assets/sandbox.css': 'sandbox.css'};
      const file = files[url.pathname];
      if (!file) return route.fulfill({body: '', contentType: 'application/javascript'});
      return route.fulfill({path: path.join(__dirname, '../ui', file),
        contentType: file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.goto('http://ability.test/');
    await page.locator('[data-mode="compare"]').click();
    await page.waitForSelector('#plan-comparison .plan-summary');
    const text = await page.locator('#plan-comparison').innerText();
    assert.ok(text.includes('生命 -1 · 伤害 0'), text);
    await page.locator('#plan-comparison summary').first().click();
    const expanded = await page.locator('#plan-comparison').innerText();
    assert.ok(expanded.includes(`能力预计贡献 ${fixture.plans[0].score.ability_score.toFixed(2)}`));
    assert.ok(!/NaN|undefined/.test(expanded));
    assert.equal(await page.locator('#error').textContent(), '');
    assert.deepEqual(errors, []);
    console.log('v4 sandbox actual outcomes and ability contribution: PASS');
  } finally { await browser.close(); }
})().catch(e => {console.error(e);process.exitCode = 1;});
