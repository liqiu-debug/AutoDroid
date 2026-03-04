const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  page.on('console', msg => {
    if (msg.text().includes('Executing step payload:')) {
      console.log('BROWSER LOG:', msg.text());
    }
  });

  page.on('request', request => {
    if (request.url().includes('/device/execute_step') && request.method() === 'POST') {
      console.log('>> POST PAYLOAD:', request.postData());
    }
  });

  await page.goto('http://127.0.0.1:5173/ui/cases');
  // Wait for case list to load
  await page.waitForTimeout(2000);

  // Click the first case to open it
  const cases = await page.$$('.el-table__row');
  if (cases.length > 0) {
    await cases[0].click();
    await page.waitForTimeout(2000);

    // Select the first environment (assuming an environment exists)
    const envSelect = await page.$('.toolbar-right .el-select');
    if (envSelect) {
      await envSelect.click();
      await page.waitForTimeout(500);
      const envOptions = await page.$$('.el-select-dropdown__item');
      if (envOptions.length > 0) {
        await envOptions[0].click();
        await page.waitForTimeout(500);
      }
    }

    // Click the run step button for the first step
    const runButtons = await page.$$('button[title="执行步骤"]');
    if (runButtons.length > 0) {
      await runButtons[0].click();
      await page.waitForTimeout(1000); // Wait for request to fire
    } else {
      console.log('No run buttons found.');
    }
  } else {
    console.log('No cases found.');
  }

  await browser.close();
})();
