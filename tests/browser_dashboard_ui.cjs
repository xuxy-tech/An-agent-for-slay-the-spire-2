const { chromium } = require('../.tools/browser/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const pathLib = require('node:path');

(async () => {
  const base = process.env.OBSERVER_URL || 'http://127.0.0.1:8766';
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  });
  try {
    for (const width of [390, 1440]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      for (const [path, current] of [['/', '实战观察'], ['/human-capture', '学习']]) {
        await page.goto(base + path);
        const links = page.locator('.workspace-nav a');
        assert.deepEqual(await links.allTextContents(), ['实战观察', '学习']);
        assert.equal(await page.locator('.workspace-nav a[aria-current="page"]').textContent(), current);
        assert.equal(await page.locator('a[href="/sandbox"]').count(), 0);
        const nav = await links.first().evaluate(el => {
          const style = getComputedStyle(el);
          return { display: style.display, fontSize: style.fontSize, border: style.borderStyle,
            height: el.getBoundingClientRect().height };
        });
        assert.ok(['flex', 'inline-flex'].includes(nav.display));
        assert.equal(nav.fontSize, '14px');
        assert.equal(nav.border, 'solid');
        assert.ok(nav.height >= 36);
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
        const type = await page.evaluate(() => ({
          body: getComputedStyle(document.body).fontSize,
          h1: getComputedStyle(document.querySelector('h1')).fontSize,
          h2: getComputedStyle(document.querySelector('h2')).fontSize,
          button: getComputedStyle(document.querySelector('button')).fontSize,
        }));
        assert.deepEqual(type, { body: '14px', h1: '22px', h2: '16px', button: '14px' });
        if (path === '/') {
          assert.equal(await page.locator('.more-actions').count(), 0);
          assert.equal(await page.locator('.secondary-actions button').count(), 4);
          assert.ok(await page.locator('#shutdown').isVisible());
        }
        if (process.env.UI_SCREENSHOT === '1') {
          const output = pathLib.resolve('logs/ui_qa');
          fs.mkdirSync(output, { recursive: true });
          await page.screenshot({ path: pathLib.join(output, `${path === '/' ? 'observe' : 'learn'}-${width}.png`) });
        }
      }
      await page.close();
    }
    console.log('dashboard UI navigation and actions: PASS');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
