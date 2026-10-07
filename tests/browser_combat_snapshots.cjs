const {chromium} = require('../.tools/browser/node_modules/playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
  try {
    const page = await browser.newPage();
    let commands = 0;
    let historical = false;
    const row = {snapshot_id: 'fixture', created_at_utc: 1, floor: 20, turn: 3,
      status: 'VALIDATION_FAILED', artifact_dir: 'C:/captures/<fixture>', error: '<script>bad()</script>'};
    await page.route('http://snapshot.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/status') return route.fulfill({json: {history_only: false}});
      if (url.pathname === '/api/combat-snapshots') return route.fulfill({json: {
        snapshots: [row], history_only: historical,
        validation_set: {valid: 12, target: 48, stale: 2,
          acts: {'1': 8, '2': 4, '3': 0}, encounters: 10}}});
      if (url.pathname === '/api/command') {
        assert.equal(route.request().postDataJSON().command, 'capture_combat_snapshot');
        commands++;
        return route.fulfill({json: {snapshot: row}});
      }
      return route.fulfill({contentType: 'text/html', body: '<details id="combat-snapshots-panel"><summary>快照</summary><button id="snapshot-capture">采集</button><button id="snapshot-refresh">刷新</button><p id="snapshot-coverage"></p><p id="snapshot-message"></p><table><tbody id="snapshot-list"></tbody></table></details><select id="sessions"><option value="">当前</option><option value="history">历史</option></select>'});
    });
    await page.goto('http://snapshot.test/');
    await page.addScriptTag({path: path.resolve('ui/combat_snapshots.js')});
    await page.locator('summary').click();
    await page.waitForSelector('#snapshot-list tr');
    assert.match(await page.locator('#snapshot-coverage').innerText(), /12 \/ 48.*2 份待重验/);
    assert.match(await page.locator('#snapshot-coverage').innerText(), /第一幕 8\/24.*10 种遭遇/);
    assert.equal(await page.locator('#snapshot-list script').count(), 0);
    assert.match(await page.locator('#snapshot-list').innerText(), /<script>bad/);
    await page.locator('#snapshot-capture').click();
    await page.waitForFunction(() => document.querySelector('#snapshot-message').textContent.includes('恢复验证失败'));
    assert.equal(commands, 1);
    await page.selectOption('#sessions', 'history');
    await page.locator('#snapshot-capture').click();
    await page.waitForFunction(() => document.querySelector('#snapshot-message').textContent.includes('请先返回当前运行'));
    assert.equal(commands, 1);
    historical = true;
    await page.locator('#snapshot-refresh').click();
    await page.waitForFunction(() => document.querySelector('#snapshot-capture').hidden);
    console.log('PASS: capture command, failure display, literal text, history guard, read-only mode');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
